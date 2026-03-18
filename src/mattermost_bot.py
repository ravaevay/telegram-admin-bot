"""Mattermost admin bot — mirrors Telegram bot functionality.

Uses mattermostdriver (sync) for REST API + aiohttp for WebSocket and button callbacks.
All DO/mail backend modules are reused as-is.
"""

import asyncio
import io
import json
import logging
import re
import signal
import sys

import aiohttp
from aiohttp import web
from mattermostdriver import Driver

from config import (
    SSH_CONFIG,
    DIGITALOCEAN_TOKEN,
    MM_BOT_TOKEN,
    MM_SERVER_URL,
    MM_WEBHOOK_PORT,
    MM_WEBHOOK_HOST,
)
from modules.authorization import mm_is_authorized, mm_is_authorized_for_bot
from modules.create_test_instance import (
    create_droplet,
    create_snapshot,
    get_ssh_keys,
    get_images,
    get_domains,
    get_sizes,
    create_dns_record,
    delete_droplet,
    wait_for_action,
    DROPLET_TYPES,
)
from modules.create_k8s_cluster import (
    create_k8s_cluster,
    delete_k8s_cluster,
    get_k8s_cluster,
    get_k8s_versions,
    get_k8s_sizes,
    get_kubeconfig,
)
from modules.database import (
    init_db,
    get_expiring_instances,
    extend_instance_expiration,
    get_instance_by_id,
    get_instances_by_creator,
    update_instance_dns,
    record_ssh_key_usage,
    get_preferred_ssh_keys,
    get_expiring_k8s_clusters,
    get_provisioning_k8s_clusters,
    get_k8s_clusters_by_creator,
    get_k8s_cluster_by_id,
    update_k8s_cluster_status,
    extend_k8s_cluster_expiration,
)
from modules.mail import create_mailbox, generate_password, reset_password
from modules.mm_conversation import ConversationManager
from modules.mm_notifications import (
    send_notification as mm_send_notification,
    send_k8s_notification as mm_send_k8s_notification,
)
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
NOTIFY_INTERVAL_SECONDS = 43200  # 12 hours
K8S_POLL_INTERVAL_SECONDS = 30
CLEANUP_INTERVAL_SECONDS = 300  # 5 min — clean expired conversations

DROPLET_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,253}[a-zA-Z0-9]$")
SUBDOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

# Conversation flow names & states
FLOW_MAIL_CREATE = "mail_create"
FLOW_PASSWORD_RESET = "password_reset"
FLOW_DROPLET_CREATE = "droplet_create"
FLOW_DROPLET_MANAGE = "droplet_manage"
FLOW_K8S_CREATE = "k8s_create"
FLOW_K8S_MANAGE = "k8s_manage"

# Droplet creation states
ST_SELECT_SSH_KEY = "select_ssh_key"
ST_SELECT_IMAGE = "select_image"
ST_SELECT_DNS_ZONE = "select_dns_zone"
ST_INPUT_SUBDOMAIN = "input_subdomain"
ST_SELECT_TYPE = "select_type"
ST_SELECT_DURATION = "select_duration"
ST_INPUT_NAME = "input_name"

# Droplet management states
ST_MANAGE_ACTION = "manage_action"
ST_MANAGE_EXTEND = "manage_extend"
ST_MANAGE_CONFIRM_DELETE = "manage_confirm_delete"

# K8s creation states
ST_K8S_SELECT_VERSION = "k8s_select_version"
ST_K8S_SELECT_NODE_SIZE = "k8s_select_node_size"
ST_K8S_SELECT_NODE_COUNT = "k8s_select_node_count"
ST_K8S_SELECT_DURATION = "k8s_select_duration"
ST_K8S_INPUT_NAME = "k8s_input_name"

# K8s management states
ST_K8S_MANAGE_ACTION = "k8s_manage_action"
ST_K8S_MANAGE_EXTEND = "k8s_manage_extend"
ST_K8S_MANAGE_CONFIRM_DELETE = "k8s_manage_confirm_delete"

# Mail / password states
ST_MAIL_INPUT = "mail_input"
ST_RESET_INPUT = "reset_input"

# --- Globals ---
driver: Driver = None
bot_user_id: str = None
conversations = ConversationManager()


# ============================================================
# Helpers
# ============================================================


async def mm_api(func, *args, **kwargs):
    """Run sync mattermostdriver call in thread pool."""
    return await asyncio.to_thread(func, *args, **kwargs)


async def post_message(channel_id, text, props=None):
    """Post a message to a Mattermost channel."""
    options = {"channel_id": channel_id, "message": text}
    if props:
        options["props"] = props
    return await mm_api(driver.posts.create_post, options)


async def update_post(post_id, text, props=None):
    """Update an existing post."""
    options = {"id": post_id, "message": text}
    if props:
        options["props"] = props
    return await mm_api(driver.posts.update_post, post_id, options)


async def post_with_buttons(channel_id, text, buttons):
    """Post a message with interactive buttons.

    buttons: list of dicts with keys: id, name, context (dict)
    """
    actions = []
    for btn in buttons:
        actions.append(
            {
                "id": btn["id"],
                "type": "button",
                "name": btn["name"],
                "integration": {
                    "url": f"http://{MM_WEBHOOK_HOST}:{MM_WEBHOOK_PORT}/actions",
                    "context": btn.get("context", {}),
                },
            }
        )
    props = {"attachments": [{"text": text, "actions": actions}]}
    return await post_message(channel_id, "", props=props)


async def get_dm_channel(user_id):
    """Get or create a DM channel between the bot and a user."""
    result = await mm_api(driver.channels.create_direct_message_channel, [bot_user_id, user_id])
    return result["id"]


async def send_file(channel_id, filename, content_bytes):
    """Upload a file and post it to a channel."""
    file_resp = await mm_api(
        driver.files.upload_file,
        channel_id,
        {"files": (filename, io.BytesIO(content_bytes))},
    )
    file_id = file_resp["file_infos"][0]["id"]
    await mm_api(
        driver.posts.create_post,
        {"channel_id": channel_id, "message": "", "file_ids": [file_id]},
    )


# ============================================================
# WebSocket listener
# ============================================================


async def ws_listener():
    """Connect to Mattermost WebSocket and listen for events."""
    base = MM_SERVER_URL.rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/v4/websocket"

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    # Authenticate
                    await ws.send_json(
                        {"seq": 1, "action": "authentication_challenge", "data": {"token": MM_BOT_TOKEN}}
                    )
                    logger.info("WebSocket подключён к Mattermost")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event = json.loads(msg.data)
                                await handle_ws_event(event)
                            except Exception:
                                logger.exception("Ошибка обработки WS-события")
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except Exception:
            logger.exception("WebSocket disconnected, reconnecting in 5s...")
        await asyncio.sleep(5)


async def handle_ws_event(event):
    """Route WebSocket events."""
    if event.get("event") != "posted":
        return

    data = event.get("data", {})
    post_data = json.loads(data.get("post", "{}"))

    # Ignore bot's own messages
    if post_data.get("user_id") == bot_user_id:
        return

    # Only handle DM messages to the bot
    channel_type = data.get("channel_type", "")
    if channel_type != "D":
        return

    user_id = post_data.get("user_id", "")
    channel_id = post_data.get("channel_id", "")
    message = post_data.get("message", "").strip()

    if not message:
        return

    # Command routing
    if message.lower() == "!start":
        await cmd_start(user_id, channel_id)
    elif message.lower() == "!cancel":
        await cmd_cancel(user_id, channel_id)
    else:
        # Route to active conversation
        await route_text_input(user_id, channel_id, message)


# ============================================================
# Commands
# ============================================================


async def cmd_start(user_id, channel_id):
    """Show main menu with action buttons."""
    if not mm_is_authorized_for_bot(user_id):
        await post_message(channel_id, "У вас нет доступа к этому боту.")
        return

    conversations.end(user_id)

    buttons = [
        {"id": "create_mailbox", "name": "Создать почтовый ящик", "context": {"action": "create_mailbox"}},
        {"id": "reset_password", "name": "Сброс пароля", "context": {"action": "reset_password"}},
        {"id": "create_droplet", "name": "Создать инстанс", "context": {"action": "create_droplet"}},
        {"id": "manage_droplets", "name": "Управление инстансами", "context": {"action": "manage_droplets"}},
        {"id": "create_k8s", "name": "Создать K8s кластер", "context": {"action": "create_k8s"}},
        {"id": "manage_k8s", "name": "Мои K8s кластеры", "context": {"action": "manage_k8s"}},
    ]
    await post_with_buttons(channel_id, "Добро пожаловать! Выберите действие:", buttons)


async def cmd_cancel(user_id, channel_id):
    """Cancel active conversation."""
    conversations.end(user_id)
    await post_message(channel_id, "Операция отменена.")


# ============================================================
# Text input routing
# ============================================================


async def route_text_input(user_id, channel_id, text):
    """Route text input to the correct conversation handler."""
    conv = conversations.get(user_id)
    if conv is None:
        await post_message(channel_id, "Нет активной операции. Напишите `!start` для начала.")
        return

    flow = conv.flow_name
    state = conv.state

    if flow == FLOW_MAIL_CREATE and state == ST_MAIL_INPUT:
        await handle_mail_input(user_id, channel_id, text)
    elif flow == FLOW_PASSWORD_RESET and state == ST_RESET_INPUT:
        await handle_reset_input(user_id, channel_id, text)
    elif flow == FLOW_DROPLET_CREATE and state == ST_INPUT_SUBDOMAIN:
        await handle_subdomain_input(user_id, channel_id, text)
    elif flow == FLOW_DROPLET_CREATE and state == ST_INPUT_NAME:
        await handle_droplet_name_input(user_id, channel_id, text)
    elif flow == FLOW_K8S_CREATE and state == ST_K8S_INPUT_NAME:
        await handle_k8s_name_input(user_id, channel_id, text)
    else:
        await post_message(channel_id, "Ожидается выбор с помощью кнопок. Или напишите `!cancel` для отмены.")


# ============================================================
# Button action handler (aiohttp HTTP server)
# ============================================================


async def handle_action(request):
    """Handle Mattermost interactive message button callbacks."""
    logger.info(f"Action callback received: content_type={request.content_type}, method={request.method}")
    try:
        data = await request.json()
    except Exception:
        body = await request.text()
        logger.error(f"Failed to parse action body: {body[:500]}")
        return web.json_response({"error": "invalid json"}, status=400)

    user_id = data.get("user_id", "")
    channel_id = data.get("channel_id", "")
    context = data.get("context", {})
    action = context.get("action", "")

    logger.info(f"Button action: user={user_id}, action={action}")

    try:
        result = await dispatch_action(user_id, channel_id, action, context, data)
    except Exception:
        logger.exception(f"Ошибка обработки action={action}")
        result = None

    if result and isinstance(result, dict):
        return web.json_response(result)
    return web.json_response({"update": {"message": "", "props": {}}})


async def dispatch_action(user_id, channel_id, action, context, data):
    """Dispatch button action to the correct handler."""
    # --- Start menu ---
    if action == "create_mailbox":
        await start_mail_create(user_id, channel_id)
    elif action == "reset_password":
        await start_password_reset(user_id, channel_id)
    elif action == "create_droplet":
        await start_droplet_create(user_id, channel_id)
    elif action == "manage_droplets":
        await start_droplet_manage(user_id, channel_id)
    elif action == "create_k8s":
        await start_k8s_create(user_id, channel_id)
    elif action == "manage_k8s":
        await start_k8s_manage(user_id, channel_id)

    # --- SSH key selection ---
    elif action.startswith("ssh_toggle_"):
        await handle_ssh_toggle(user_id, channel_id, action, data)
    elif action == "ssh_more_keys":
        await handle_ssh_more_keys(user_id, channel_id, data)
    elif action == "ssh_confirm":
        await handle_ssh_confirm(user_id, channel_id)

    # --- Image selection ---
    elif action.startswith("image_"):
        await handle_image_select(user_id, channel_id, action)

    # --- DNS zone selection ---
    elif action.startswith("dns_zone_"):
        await handle_dns_zone_select(user_id, channel_id, action)

    # --- Droplet type ---
    elif action.startswith("droplet_type_"):
        await handle_droplet_type_select(user_id, channel_id, action)

    # --- Duration ---
    elif action.startswith("duration_"):
        await handle_duration_select(user_id, channel_id, action)

    # --- Droplet management ---
    elif action.startswith("my_extend_"):
        await handle_manage_extend_entry(user_id, channel_id, action)
    elif action.startswith("my_ext_days_"):
        await handle_manage_extend_confirm(user_id, channel_id, action)
    elif action.startswith("my_delete_"):
        await handle_manage_delete_entry(user_id, channel_id, action)
    elif action.startswith("my_confirm_delete_"):
        await handle_manage_delete_confirm(user_id, channel_id, action)
    elif action == "my_cancel_delete":
        conversations.end(user_id)
        await post_message(channel_id, "Удаление отменено.")

    # --- K8s creation ---
    elif action.startswith("k8s_version_"):
        await handle_k8s_version_select(user_id, channel_id, action)
    elif action.startswith("k8s_size_"):
        await handle_k8s_size_select(user_id, channel_id, action)
    elif action.startswith("k8s_count_"):
        await handle_k8s_count_select(user_id, channel_id, action)
    elif action.startswith("k8s_duration_"):
        await handle_k8s_duration_select(user_id, channel_id, action)

    # --- K8s management ---
    elif action.startswith("k8s_my_extend_"):
        await handle_k8s_manage_extend_entry(user_id, channel_id, action)
    elif action.startswith("k8s_ext_days_"):
        await handle_k8s_manage_extend_confirm(user_id, channel_id, action)
    elif action.startswith("k8s_my_delete_"):
        await handle_k8s_manage_delete_entry(user_id, channel_id, action)
    elif action.startswith("k8s_confirm_delete_"):
        await handle_k8s_manage_delete_confirm(user_id, channel_id, action)
    elif action == "k8s_cancel_delete":
        conversations.end(user_id)
        await post_message(channel_id, "Удаление отменено.")

    # --- Background job notification buttons ---
    elif action.startswith("bg_extend_"):
        await handle_bg_extend(user_id, channel_id, action)
    elif action.startswith("bg_delete_"):
        await handle_bg_delete(user_id, channel_id, action)
    elif action.startswith("bg_k8s_extend_"):
        await handle_bg_k8s_extend(user_id, channel_id, action)
    elif action.startswith("bg_k8s_delete_"):
        await handle_bg_k8s_delete(user_id, channel_id, action)

    return None


# ============================================================
# Mail creation flow
# ============================================================


async def start_mail_create(user_id, channel_id):
    if not mm_is_authorized(user_id, "mail"):
        await post_message(channel_id, "У вас нет прав для создания почтовых ящиков.")
        return
    conversations.start(user_id, FLOW_MAIL_CREATE, ST_MAIL_INPUT)
    await post_message(channel_id, "Введите имя почтового ящика:")


async def handle_mail_input(user_id, channel_id, text):
    mailbox_name = text.strip()
    password = generate_password()
    result = create_mailbox(mailbox_name, password, SSH_CONFIG)

    if result["success"]:
        # Use plain text instead of MarkdownV2
        msg = result["message"]
        # Strip Telegram MarkdownV2 escaping if present
        msg = re.sub(r"\\(.)", r"\1", msg)
        await post_message(channel_id, msg)
    else:
        await post_message(channel_id, f"Ошибка: {result['message']}")

    conversations.end(user_id)


# ============================================================
# Password reset flow
# ============================================================


async def start_password_reset(user_id, channel_id):
    if not mm_is_authorized(user_id, "mail"):
        await post_message(channel_id, "У вас нет прав для сброса паролей почтовых ящиков.")
        return
    conversations.start(user_id, FLOW_PASSWORD_RESET, ST_RESET_INPUT)
    await post_message(channel_id, "Введите имя почтового ящика для сброса пароля:")


async def handle_reset_input(user_id, channel_id, text):
    mailbox_name = text.strip()
    new_password = generate_password()
    result = reset_password(mailbox_name, new_password, SSH_CONFIG)

    if result["success"]:
        msg = result["message"]
        msg = re.sub(r"\\(.)", r"\1", msg)
        await post_message(channel_id, msg)
    else:
        await post_message(channel_id, f"Ошибка: {result['message']}")

    conversations.end(user_id)


# ============================================================
# Droplet creation flow
# ============================================================


async def start_droplet_create(user_id, channel_id):
    if not mm_is_authorized(user_id, "droplet"):
        await post_message(channel_id, "У вас нет прав для создания инстансов в DigitalOcean.")
        return

    result = await get_ssh_keys(DIGITALOCEAN_TOKEN)
    if not result["success"]:
        await post_message(channel_id, f"Ошибка: {result['message']}")
        return

    ssh_keys = result["keys"]
    if not ssh_keys:
        await post_message(channel_id, "Нет доступных SSH-ключей в DigitalOcean.")
        return

    # Reorder keys by user preference
    preferred_ids = get_preferred_ssh_keys(user_id)
    if preferred_ids:
        available_ids = {k["id"] for k in ssh_keys}
        valid_preferred = [pid for pid in preferred_ids if pid in available_ids]
        preferred_set = set(valid_preferred)
        preferred_keys = [k for pid in valid_preferred for k in ssh_keys if k["id"] == pid]
        remaining_keys = [k for k in ssh_keys if k["id"] not in preferred_set]
        ssh_keys = preferred_keys + remaining_keys
        preselect = {str(pid) for pid in valid_preferred[:3]}
    else:
        preselect = {str(k["id"]) for k in ssh_keys[:3]}

    conv = conversations.start(user_id, FLOW_DROPLET_CREATE, ST_SELECT_SSH_KEY)
    conv.data["ssh_keys_list"] = ssh_keys
    conv.data["selected_ssh_keys"] = preselect
    conv.data["ssh_keys_expanded"] = False

    await _post_ssh_key_buttons(channel_id, ssh_keys, preselect, expanded=False)


async def _post_ssh_key_buttons(channel_id, keys, selected_ids, expanded):
    visible_keys = keys if expanded or len(keys) <= 3 else keys[:3]
    buttons = []
    for key in visible_keys:
        key_id = str(key["id"])
        prefix = "✅" if key_id in selected_ids else "⬜"
        buttons.append(
            {"id": f"ssh_{key_id}", "name": f"{prefix} {key['name']}", "context": {"action": f"ssh_toggle_{key_id}"}}
        )

    if not expanded and len(keys) > 3:
        remaining = len(keys) - 3
        buttons.append(
            {"id": "ssh_more", "name": f"Другие ключи ({remaining})", "context": {"action": "ssh_more_keys"}}
        )

    count = len(selected_ids)
    buttons.append({"id": "ssh_confirm", "name": f"Продолжить ✓ ({count})", "context": {"action": "ssh_confirm"}})
    await post_with_buttons(channel_id, "Выберите SSH ключи:", buttons)


async def handle_ssh_toggle(user_id, channel_id, action, data):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    key_id = action.removeprefix("ssh_toggle_")
    selected = conv.data.get("selected_ssh_keys", set())

    if key_id in selected:
        selected.discard(key_id)
    else:
        selected.add(key_id)

    conv.data["selected_ssh_keys"] = selected
    conv.touch()

    await _post_ssh_key_buttons(
        channel_id, conv.data["ssh_keys_list"], selected, conv.data.get("ssh_keys_expanded", False)
    )


async def handle_ssh_more_keys(user_id, channel_id, data):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    conv.data["ssh_keys_expanded"] = True
    conv.touch()
    await _post_ssh_key_buttons(channel_id, conv.data["ssh_keys_list"], conv.data.get("selected_ssh_keys", set()), True)


async def handle_ssh_confirm(user_id, channel_id):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    selected = conv.data.get("selected_ssh_keys", set())
    if not selected:
        await post_message(channel_id, "Выберите хотя бы один SSH-ключ")
        return

    conv.data["ssh_key_ids"] = [int(k) for k in selected]
    conv.data.pop("ssh_keys_list", None)
    conv.data.pop("selected_ssh_keys", None)
    conv.data.pop("ssh_keys_expanded", None)
    conv.state = ST_SELECT_IMAGE
    conv.touch()

    # Show images
    result = await get_images(DIGITALOCEAN_TOKEN)
    if not result["success"]:
        await post_message(channel_id, f"Ошибка: {result['message']}")
        conversations.end(user_id)
        return

    images = sorted(result["images"], key=lambda x: x["distribution"])
    buttons = [
        {
            "id": f"img_{image['id']}",
            "name": f"{image['distribution']} {image['name']}",
            "context": {"action": f"image_{image['id']}"},
        }
        for image in images
    ]
    await post_with_buttons(channel_id, "Выберите образ:", buttons)


async def handle_image_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    image_id = action.removeprefix("image_")
    conv.data["image"] = image_id
    conv.touch()

    # Try DNS zones
    result = await get_domains(DIGITALOCEAN_TOKEN)
    if result["success"] and result["domains"]:
        buttons = [{"id": f"dns_{d}", "name": d, "context": {"action": f"dns_zone_{d}"}} for d in result["domains"]]
        buttons.append({"id": "dns_skip", "name": "Пропустить (без DNS)", "context": {"action": "dns_zone_skip"}})
        conv.state = ST_SELECT_DNS_ZONE
        await post_with_buttons(channel_id, "Выберите DNS-зону для создания записи:", buttons)
    else:
        conv.data["dns_zone"] = None
        conv.data["subdomain"] = None
        await _show_droplet_type_buttons(user_id, channel_id)


async def handle_dns_zone_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    zone = action.removeprefix("dns_zone_")
    if zone == "skip":
        conv.data["dns_zone"] = None
        conv.data["subdomain"] = None
        await _show_droplet_type_buttons(user_id, channel_id)
    else:
        conv.data["dns_zone"] = zone
        conv.state = ST_INPUT_SUBDOMAIN
        conv.touch()
        await post_message(channel_id, f"Введите имя субдомена для зоны {zone}:")


async def handle_subdomain_input(user_id, channel_id, text):
    conv = conversations.get(user_id)
    if not conv:
        return

    subdomain = text.strip().lower()
    if not SUBDOMAIN_RE.match(subdomain):
        await post_message(
            channel_id,
            "Недопустимое имя субдомена. Используйте латинские буквы, цифры и дефис "
            "(1-63 символа, начинается и заканчивается буквой или цифрой).\nПопробуйте ещё раз:",
        )
        return

    conv.data["subdomain"] = subdomain
    conv.touch()
    await _show_droplet_type_buttons(user_id, channel_id)


async def _show_droplet_type_buttons(user_id, channel_id):
    conv = conversations.get(user_id)
    if not conv:
        return

    sizes = await get_sizes(DIGITALOCEAN_TOKEN)
    buttons = []
    for slug, label in DROPLET_TYPES.items():
        price_info = sizes.get(slug)
        if price_info:
            btn_text = f"{label} — ${price_info['price_monthly']}/мес"
        else:
            btn_text = label
        buttons.append({"id": f"dt_{slug}", "name": btn_text, "context": {"action": f"droplet_type_{slug}"}})

    conv.state = ST_SELECT_TYPE
    conv.touch()
    await post_with_buttons(channel_id, "Выберите тип Droplet:", buttons)


async def handle_droplet_type_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    droplet_type = action.removeprefix("droplet_type_")
    conv.data["droplet_type"] = droplet_type

    sizes = await get_sizes(DIGITALOCEAN_TOKEN)
    price_info = sizes.get(droplet_type)
    if price_info:
        conv.data["price_monthly"] = price_info["price_monthly"]
        conv.data["price_hourly"] = price_info["price_hourly"]
        hourly = price_info["price_hourly"]
        durations = [("1 день", 1), ("3 дня", 3), ("Неделя", 7), ("2 недели", 14), ("Месяц", 30)]
        buttons = [
            {
                "id": f"dur_{days}",
                "name": f"{label} — ~${hourly * 24 * days:.2f}",
                "context": {"action": f"duration_{days}"},
            }
            for label, days in durations
        ]
    else:
        conv.data["price_monthly"] = None
        conv.data["price_hourly"] = None
        buttons = [
            {"id": f"dur_{d}", "name": label, "context": {"action": f"duration_{d}"}}
            for label, d in [("1 день", 1), ("3 дня", 3), ("Неделя", 7), ("2 недели", 14), ("Месяц", 30)]
        ]

    conv.state = ST_SELECT_DURATION
    conv.touch()
    await post_with_buttons(channel_id, "Выберите длительность аренды инстанса:", buttons)


async def handle_duration_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_DROPLET_CREATE:
        return

    duration = int(action.removeprefix("duration_"))
    conv.data["duration"] = duration
    conv.touch()

    dns_zone = conv.data.get("dns_zone")
    subdomain = conv.data.get("subdomain")
    if dns_zone and subdomain:
        droplet_name = f"{subdomain}.{dns_zone}"
        await _create_droplet_and_respond(user_id, channel_id, droplet_name)
    else:
        conv.state = ST_INPUT_NAME
        await post_message(channel_id, "Введите имя инстанса:")


async def handle_droplet_name_input(user_id, channel_id, text):
    conv = conversations.get(user_id)
    if not conv:
        return

    droplet_name = text.strip()
    if not DROPLET_NAME_RE.match(droplet_name):
        await post_message(
            channel_id,
            "Недопустимое имя инстанса. Используйте латинские буквы, цифры, точку, дефис или подчёркивание "
            "(2-255 символов, начинается и заканчивается буквой или цифрой).\nПопробуйте ещё раз:",
        )
        return

    await _create_droplet_and_respond(user_id, channel_id, droplet_name)


async def _create_droplet_and_respond(user_id, channel_id, droplet_name):
    conv = conversations.get(user_id)
    if not conv:
        return

    data = conv.data
    # Get MM user info for username
    mm_user = await mm_api(driver.users.get_user, user_id)
    creator_username = f"@{mm_user.get('username', user_id)}"
    creator_tag = mm_user.get("username", user_id)

    result = await create_droplet(
        DIGITALOCEAN_TOKEN,
        droplet_name,
        data["ssh_key_ids"],
        data["droplet_type"],
        data["image"],
        data["duration"],
        creator_id=user_id,
        creator_username=creator_username,
        price_monthly=data.get("price_monthly"),
        creator_tag=creator_tag,
        price_hourly=data.get("price_hourly"),
    )

    domain_name = None
    if result["success"]:
        dns_zone = data.get("dns_zone")
        subdomain = data.get("subdomain")
        if dns_zone and subdomain:
            dns_result = await create_dns_record(DIGITALOCEAN_TOKEN, dns_zone, subdomain, result["ip_address"])
            if dns_result["success"]:
                domain_name = dns_result["fqdn"]
                update_instance_dns(result["droplet_id"], domain_name, dns_result["record_id"], dns_zone)

        record_ssh_key_usage(user_id, data["ssh_key_ids"])

        droplet_type_label = DROPLET_TYPES.get(data["droplet_type"], data["droplet_type"])
        dns_line = f"\nDNS: {domain_name}" if domain_name else ""
        cost_line = f"\nCost: ~${data.get('price_monthly')}/month" if data.get("price_monthly") else ""
        msg = (
            f"**Instance successfully created!**\n\n"
            f"Name: `{result['droplet_name']}`\n"
            f"Connect: `root@{result['ip_address']}`\n"
            f"Type: `{droplet_type_label}`\n"
            f"Expires: `{result['expiration_date']}`"
            f"{dns_line}{cost_line}"
        )
        await post_message(channel_id, msg)

        await mm_send_notification(
            driver,
            action="created",
            droplet_name=result["droplet_name"],
            ip_address=result["ip_address"],
            droplet_type=data["droplet_type"],
            expiration_date=result["expiration_date"],
            creator_id=user_id,
            duration=data["duration"],
            creator_username=creator_username,
            domain_name=domain_name,
            price_monthly=data.get("price_monthly"),
        )
    else:
        await post_message(channel_id, f"Ошибка: {result['message']}")

    conversations.end(user_id)


# ============================================================
# Droplet management flow
# ============================================================


async def start_droplet_manage(user_id, channel_id):
    if not mm_is_authorized(user_id, "droplet"):
        await post_message(channel_id, "У вас нет прав для управления инстансами.")
        return

    instances = get_instances_by_creator(user_id)
    if not instances:
        await post_message(channel_id, "У вас нет активных инстансов.")
        return

    conversations.start(user_id, FLOW_DROPLET_MANAGE, ST_MANAGE_ACTION)

    for inst in instances:
        type_label = DROPLET_TYPES.get(inst["droplet_type"], inst["droplet_type"])
        dns_line = f"DNS: {inst['domain_name']}\n" if inst.get("domain_name") else ""

        cost_line = ""
        if inst.get("created_at") and inst.get("price_hourly"):
            try:
                created = datetime.strptime(inst["created_at"], "%Y-%m-%d %H:%M:%S")
                hours = (datetime.now() - created).total_seconds() / 3600
                cost = hours * inst["price_hourly"]
                cost_line = f"Потрачено: ~${cost:.2f}\n"
            except (ValueError, TypeError):
                pass

        text = (
            f"Имя: {inst['name']}\n"
            f"IP: {inst['ip_address']}\n"
            f"{dns_line}"
            f"Тип: {type_label}\n"
            f"{cost_line}"
            f"Срок действия: {inst['expiration_date']}"
        )
        did = inst["droplet_id"]
        buttons = [
            {"id": f"ext_{did}", "name": "Продлить", "context": {"action": f"my_extend_{did}"}},
            {"id": f"del_{did}", "name": "Удалить", "context": {"action": f"my_delete_{did}"}},
        ]
        await post_with_buttons(channel_id, text, buttons)


async def handle_manage_extend_entry(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv:
        conv = conversations.start(user_id, FLOW_DROPLET_MANAGE, ST_MANAGE_EXTEND)

    droplet_id = int(action.removeprefix("my_extend_"))
    conv.data["manage_droplet_id"] = droplet_id
    conv.state = ST_MANAGE_EXTEND
    conv.touch()

    buttons = [
        {"id": "ext3", "name": "3 дня", "context": {"action": "my_ext_days_3"}},
        {"id": "ext7", "name": "7 дней", "context": {"action": "my_ext_days_7"}},
    ]
    await post_with_buttons(channel_id, "На сколько продлить?", buttons)


async def handle_manage_extend_confirm(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv:
        return

    days = int(action.removeprefix("my_ext_days_"))
    droplet_id = conv.data.get("manage_droplet_id")
    instance = get_instance_by_id(droplet_id)
    if not instance:
        await post_message(channel_id, "Инстанс не найден.")
        conversations.end(user_id)
        return

    result = extend_instance_expiration(droplet_id, days)
    if result:
        await post_message(channel_id, f"Срок действия инстанса продлён на {days} дней.")
        await mm_send_notification(
            driver,
            action="extended",
            droplet_name=instance["name"],
            ip_address=instance["ip_address"],
            droplet_type=instance["droplet_type"],
            expiration_date=result,
            creator_id=user_id,
            duration=days,
            creator_username=instance.get("creator_username"),
        )
    else:
        await post_message(channel_id, "Ошибка при продлении инстанса.")

    conversations.end(user_id)


async def handle_manage_delete_entry(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv:
        conv = conversations.start(user_id, FLOW_DROPLET_MANAGE, ST_MANAGE_CONFIRM_DELETE)

    droplet_id = int(action.removeprefix("my_delete_"))
    conv.data["manage_droplet_id"] = droplet_id
    conv.state = ST_MANAGE_CONFIRM_DELETE
    conv.touch()

    buttons = [
        {"id": f"cfd_{droplet_id}", "name": "Да, удалить", "context": {"action": f"my_confirm_delete_{droplet_id}"}},
        {"id": "cancel_del", "name": "Отмена", "context": {"action": "my_cancel_delete"}},
    ]
    await post_with_buttons(channel_id, "Вы уверены, что хотите удалить этот инстанс?", buttons)


async def handle_manage_delete_confirm(user_id, channel_id, action):
    droplet_id = int(action.removeprefix("my_confirm_delete_"))

    instance = get_instance_by_id(droplet_id)
    if not instance:
        await post_message(channel_id, "Инстанс не найден.")
        conversations.end(user_id)
        return

    delete_result = await delete_droplet(
        DIGITALOCEAN_TOKEN,
        droplet_id,
        dns_zone=instance.get("dns_zone"),
        dns_record_id=instance.get("dns_record_id"),
    )
    if delete_result["success"]:
        await post_message(channel_id, "Инстанс был успешно удалён!")
        await mm_send_notification(
            driver,
            action="deleted",
            droplet_name=instance["name"],
            ip_address=instance["ip_address"],
            droplet_type=instance["droplet_type"],
            expiration_date=instance["expiration_date"],
            creator_id=user_id,
            creator_username=instance.get("creator_username"),
        )
    else:
        await post_message(channel_id, f"Ошибка при удалении: {delete_result['message']}")

    conversations.end(user_id)


# ============================================================
# K8s creation flow
# ============================================================


async def start_k8s_create(user_id, channel_id):
    if not mm_is_authorized(user_id, "k8s"):
        await post_message(channel_id, "У вас нет прав для создания K8s кластеров.")
        return

    result = await get_k8s_versions(DIGITALOCEAN_TOKEN)
    if not result["success"]:
        await post_message(channel_id, "Не удалось получить список версий Kubernetes. Попробуйте позже.")
        return

    conversations.start(user_id, FLOW_K8S_CREATE, ST_K8S_SELECT_VERSION)

    versions = result["versions"]
    default_slug = result["default_slug"]
    buttons = []
    for v in versions:
        slug = v["slug"]
        label = f"{'✅ ' if slug == default_slug else ''}{slug}"
        buttons.append({"id": f"kv_{slug}", "name": label, "context": {"action": f"k8s_version_{slug}"}})

    await post_with_buttons(channel_id, "Выберите версию Kubernetes:", buttons)


async def handle_k8s_version_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_K8S_CREATE:
        return

    version = action.removeprefix("k8s_version_")
    conv.data["k8s_version"] = version
    conv.state = ST_K8S_SELECT_NODE_SIZE
    conv.touch()

    result = await get_k8s_sizes(DIGITALOCEAN_TOKEN)
    if not result["success"] or not result["sizes"]:
        await post_message(channel_id, "Не удалось получить список типов узлов. Попробуйте позже.")
        conversations.end(user_id)
        return

    sizes = result["sizes"]
    conv.data["k8s_sizes"] = sizes

    buttons = [
        {
            "id": f"ks_{slug}",
            "name": f"{slug} — ${info.get('price_monthly', 0)}/мес",
            "context": {"action": f"k8s_size_{slug}"},
        }
        for slug, info in sizes.items()
    ]
    await post_with_buttons(channel_id, "Выберите тип узла:", buttons)


async def handle_k8s_size_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_K8S_CREATE:
        return

    node_size = action.removeprefix("k8s_size_")
    conv.data["k8s_node_size"] = node_size

    sizes = conv.data.get("k8s_sizes", {})
    size_info = sizes.get(node_size, {})
    conv.data["k8s_price_hourly_per_node"] = size_info.get("price_hourly", 0)
    conv.state = ST_K8S_SELECT_NODE_COUNT
    conv.touch()

    buttons = [
        {
            "id": f"kc_{n}",
            "name": f"{n} {'узел' if n == 1 else 'узла' if n < 5 else 'узлов'}",
            "context": {"action": f"k8s_count_{n}"},
        }
        for n in [1, 2, 3]
    ]
    await post_with_buttons(channel_id, "Выберите количество узлов:", buttons)


async def handle_k8s_count_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_K8S_CREATE:
        return

    node_count = int(action.removeprefix("k8s_count_"))
    conv.data["k8s_node_count"] = node_count

    price_per_node = conv.data.get("k8s_price_hourly_per_node", 0)
    total_hourly = price_per_node * node_count
    conv.state = ST_K8S_SELECT_DURATION
    conv.touch()

    durations = [("1 день", 1), ("3 дня", 3), ("Неделя", 7), ("2 недели", 14)]
    buttons = [
        {
            "id": f"kd_{days}",
            "name": f"{label} — ~${total_hourly * 24 * days:.2f}",
            "context": {"action": f"k8s_duration_{days}"},
        }
        for label, days in durations
    ]
    await post_with_buttons(channel_id, "Выберите длительность аренды кластера:", buttons)


async def handle_k8s_duration_select(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv or conv.flow_name != FLOW_K8S_CREATE:
        return

    duration = int(action.removeprefix("k8s_duration_"))
    conv.data["k8s_duration"] = duration
    conv.state = ST_K8S_INPUT_NAME
    conv.touch()

    await post_message(channel_id, "Введите имя кластера (латинские буквы, цифры и дефис, 2-255 символов):")


async def handle_k8s_name_input(user_id, channel_id, text):
    conv = conversations.get(user_id)
    if not conv:
        return

    cluster_name = text.strip()
    if not DROPLET_NAME_RE.match(cluster_name):
        await post_message(
            channel_id,
            "Недопустимое имя кластера. Используйте латинские буквы, цифры, точку, дефис или подчёркивание "
            "(2-255 символов, начинается и заканчивается буквой или цифрой).\nПопробуйте ещё раз:",
        )
        return

    await _create_k8s_and_respond(user_id, channel_id, cluster_name)


async def _create_k8s_and_respond(user_id, channel_id, cluster_name):
    conv = conversations.get(user_id)
    if not conv:
        return

    data = conv.data
    mm_user = await mm_api(driver.users.get_user, user_id)
    creator_username = f"@{mm_user.get('username', user_id)}"

    node_count = data.get("k8s_node_count", 2)
    price_per_node = data.get("k8s_price_hourly_per_node", 0)
    total_price_hourly = price_per_node * node_count if price_per_node else None

    result = await create_k8s_cluster(
        token=DIGITALOCEAN_TOKEN,
        name=cluster_name,
        region="fra1",
        version=data["k8s_version"],
        node_size=data["k8s_node_size"],
        node_count=node_count,
        duration=data["k8s_duration"],
        creator_id=user_id,
        creator_username=creator_username,
        price_hourly=total_price_hourly,
    )

    if result["success"]:
        cost_line = f"\nСтоимость: ~${total_price_hourly:.4f}/ч" if total_price_hourly else ""
        text = (
            f"**K8s кластер создаётся (~5-10 мин)**\n\n"
            f"Имя: {result['cluster_name']}\n"
            f"Регион: {result['region']}\n"
            f"Версия: {result['version']}\n"
            f"Узлы: {node_count}x {data['k8s_node_size']}"
            f"{cost_line}\n"
            f"Срок действия: {result['expiration_date']}"
        )
        await post_message(channel_id, text)
        await mm_send_k8s_notification(
            driver,
            action="created",
            cluster_name=result["cluster_name"],
            region=result["region"],
            node_size=data["k8s_node_size"],
            node_count=node_count,
            expiration_date=result["expiration_date"],
            creator_id=user_id,
            duration=data["k8s_duration"],
            creator_username=creator_username,
            price_hourly=total_price_hourly,
            version=result["version"],
        )
    else:
        await post_message(channel_id, f"Ошибка при создании кластера: {result['message']}")

    conversations.end(user_id)


# ============================================================
# K8s management flow
# ============================================================


async def start_k8s_manage(user_id, channel_id):
    if not mm_is_authorized(user_id, "k8s"):
        await post_message(channel_id, "У вас нет прав для управления K8s кластерами.")
        return

    clusters = get_k8s_clusters_by_creator(user_id)
    if not clusters:
        await post_message(channel_id, "У вас нет активных K8s кластеров.")
        return

    conversations.start(user_id, FLOW_K8S_MANAGE, ST_K8S_MANAGE_ACTION)

    for cluster in clusters:
        cost_line = ""
        if cluster.get("created_at") and cluster.get("price_hourly"):
            try:
                created = datetime.strptime(cluster["created_at"], "%Y-%m-%d %H:%M:%S")
                hours = (datetime.now() - created).total_seconds() / 3600
                cost = hours * cluster["price_hourly"]
                cost_line = f"Потрачено: ~${cost:.2f}\n"
            except (ValueError, TypeError):
                pass

        status_map = {"provisioning": "⏳", "running": "✅"}
        status_emoji = status_map.get(cluster["status"], "❌")
        text = (
            f"Имя: {cluster['cluster_name']}\n"
            f"Статус: {status_emoji} {cluster['status']}\n"
            f"Регион: {cluster['region']}\n"
            f"Узлы: {cluster['node_count']}x {cluster['node_size']}\n"
            f"{cost_line}"
            f"Срок действия: {cluster['expiration_date']}"
        )
        cid = cluster["cluster_id"]
        buttons = [
            {"id": f"kext_{cid}", "name": "Продлить", "context": {"action": f"k8s_my_extend_{cid}"}},
            {"id": f"kdel_{cid}", "name": "Удалить", "context": {"action": f"k8s_my_delete_{cid}"}},
        ]
        await post_with_buttons(channel_id, text, buttons)


async def handle_k8s_manage_extend_entry(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv:
        conv = conversations.start(user_id, FLOW_K8S_MANAGE, ST_K8S_MANAGE_EXTEND)

    cluster_id = action.removeprefix("k8s_my_extend_")
    conv.data["k8s_manage_cluster_id"] = cluster_id
    conv.state = ST_K8S_MANAGE_EXTEND
    conv.touch()

    buttons = [
        {"id": "kext3", "name": "3 дня", "context": {"action": "k8s_ext_days_3"}},
        {"id": "kext7", "name": "7 дней", "context": {"action": "k8s_ext_days_7"}},
    ]
    await post_with_buttons(channel_id, "На сколько продлить?", buttons)


async def handle_k8s_manage_extend_confirm(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv:
        return

    days = int(action.removeprefix("k8s_ext_days_"))
    cluster_id = conv.data.get("k8s_manage_cluster_id")

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await post_message(channel_id, "Кластер не найден.")
        conversations.end(user_id)
        return

    new_exp = extend_k8s_cluster_expiration(cluster_id, days)
    if new_exp:
        await post_message(channel_id, f"Срок действия кластера продлён на {days} дней.")
        await mm_send_k8s_notification(
            driver,
            action="extended",
            cluster_name=cluster["cluster_name"],
            region=cluster["region"],
            node_size=cluster["node_size"],
            node_count=cluster["node_count"],
            expiration_date=new_exp,
            creator_id=user_id,
            duration=days,
            creator_username=cluster.get("creator_username"),
        )
    else:
        await post_message(channel_id, "Ошибка при продлении кластера.")

    conversations.end(user_id)


async def handle_k8s_manage_delete_entry(user_id, channel_id, action):
    conv = conversations.get(user_id)
    if not conv:
        conv = conversations.start(user_id, FLOW_K8S_MANAGE, ST_K8S_MANAGE_CONFIRM_DELETE)

    cluster_id = action.removeprefix("k8s_my_delete_")
    conv.data["k8s_manage_cluster_id"] = cluster_id
    conv.state = ST_K8S_MANAGE_CONFIRM_DELETE
    conv.touch()

    buttons = [
        {"id": f"kcfd_{cluster_id}", "name": "Да, удалить", "context": {"action": f"k8s_confirm_delete_{cluster_id}"}},
        {"id": "k_cancel_del", "name": "Отмена", "context": {"action": "k8s_cancel_delete"}},
    ]
    await post_with_buttons(channel_id, "Вы уверены, что хотите удалить этот K8s кластер?", buttons)


async def handle_k8s_manage_delete_confirm(user_id, channel_id, action):
    cluster_id = action.removeprefix("k8s_confirm_delete_")

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await post_message(channel_id, "Кластер не найден.")
        conversations.end(user_id)
        return

    delete_result = await delete_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
    if delete_result["success"]:
        await post_message(channel_id, "K8s кластер успешно удалён!")
        await mm_send_k8s_notification(
            driver,
            action="deleted",
            cluster_name=cluster["cluster_name"],
            region=cluster["region"],
            node_size=cluster["node_size"],
            node_count=cluster["node_count"],
            expiration_date=cluster["expiration_date"],
            creator_id=user_id,
            creator_username=cluster.get("creator_username"),
        )
    else:
        await post_message(channel_id, f"Ошибка при удалении кластера: {delete_result['message']}")

    conversations.end(user_id)


# ============================================================
# Background job notification button handlers
# ============================================================


async def handle_bg_extend(user_id, channel_id, action):
    """Handle extend button from background notification."""
    # format: bg_extend_{days}_{droplet_id}
    parts = action.split("_")
    try:
        days = int(parts[2])
        droplet_id = int(parts[3])
    except (IndexError, ValueError):
        await post_message(channel_id, "Ошибка: некорректные данные запроса.")
        return

    instance = get_instance_by_id(droplet_id)
    if not instance:
        await post_message(channel_id, "Инстанс не найден.")
        return

    if str(instance["creator_id"]) != user_id:
        await post_message(channel_id, "У вас нет прав для продления этого инстанса.")
        return

    result = extend_instance_expiration(droplet_id, days)
    if result:
        await post_message(channel_id, f"Срок действия инстанса продлён на {days} дней.")
        await mm_send_notification(
            driver,
            action="extended",
            droplet_name=instance["name"],
            ip_address=instance["ip_address"],
            droplet_type=instance["droplet_type"],
            expiration_date=result,
            creator_id=user_id,
            duration=days,
            creator_username=instance.get("creator_username"),
        )
    else:
        await post_message(channel_id, "Ошибка при продлении инстанса.")


async def handle_bg_delete(user_id, channel_id, action):
    """Handle delete button from background notification."""
    droplet_id = int(action.removeprefix("bg_delete_"))
    instance = get_instance_by_id(droplet_id)
    if not instance:
        await post_message(channel_id, "Инстанс не найден.")
        return

    if str(instance["creator_id"]) != user_id:
        await post_message(channel_id, "У вас нет прав для удаления этого инстанса.")
        return

    delete_result = await delete_droplet(
        DIGITALOCEAN_TOKEN,
        droplet_id,
        dns_zone=instance.get("dns_zone"),
        dns_record_id=instance.get("dns_record_id"),
    )
    if delete_result["success"]:
        await post_message(channel_id, "Инстанс был успешно удалён!")
        await mm_send_notification(
            driver,
            action="deleted",
            droplet_name=instance["name"],
            ip_address=instance["ip_address"],
            droplet_type=instance["droplet_type"],
            expiration_date=instance["expiration_date"],
            creator_id=user_id,
            creator_username=instance.get("creator_username"),
        )
    else:
        await post_message(channel_id, f"Ошибка при удалении: {delete_result['message']}")


async def handle_bg_k8s_extend(user_id, channel_id, action):
    """Handle K8s extend button from background notification."""
    # format: bg_k8s_extend_{days}_{cluster_id}
    parts = action.split("_")
    try:
        days = int(parts[3])
        cluster_id = "_".join(parts[4:])
    except (IndexError, ValueError):
        await post_message(channel_id, "Ошибка: некорректные данные запроса.")
        return

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await post_message(channel_id, "K8s кластер не найден.")
        return

    if str(cluster["creator_id"]) != user_id:
        await post_message(channel_id, "У вас нет прав для продления этого кластера.")
        return

    new_exp = extend_k8s_cluster_expiration(cluster_id, days)
    if new_exp:
        await post_message(channel_id, f"Срок действия кластера продлён на {days} дней.")
        await mm_send_k8s_notification(
            driver,
            action="extended",
            cluster_name=cluster["cluster_name"],
            region=cluster["region"],
            node_size=cluster["node_size"],
            node_count=cluster["node_count"],
            expiration_date=new_exp,
            creator_id=user_id,
            duration=days,
            creator_username=cluster.get("creator_username"),
        )
    else:
        await post_message(channel_id, "Ошибка при продлении кластера.")


async def handle_bg_k8s_delete(user_id, channel_id, action):
    """Handle K8s delete button from background notification."""
    cluster_id = action.removeprefix("bg_k8s_delete_")
    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await post_message(channel_id, "K8s кластер не найден.")
        return

    if str(cluster["creator_id"]) != user_id:
        await post_message(channel_id, "У вас нет прав для удаления этого кластера.")
        return

    delete_result = await delete_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
    if delete_result["success"]:
        await post_message(channel_id, "K8s кластер успешно удалён!")
        await mm_send_k8s_notification(
            driver,
            action="deleted",
            cluster_name=cluster["cluster_name"],
            region=cluster["region"],
            node_size=cluster["node_size"],
            node_count=cluster["node_count"],
            expiration_date=cluster["expiration_date"],
            creator_id=user_id,
            creator_username=cluster.get("creator_username"),
        )
    else:
        await post_message(channel_id, f"Ошибка при удалении кластера: {delete_result['message']}")


# ============================================================
# Background jobs
# ============================================================


async def mm_notify_and_check_instances():
    """Background loop: check expiring instances and K8s clusters (mattermost platform only)."""
    while True:
        try:
            await _check_expiring_instances()
            await _check_expiring_k8s_clusters()
        except Exception:
            logger.exception("Ошибка в фоновой задаче mm_notify_and_check_instances")
        await asyncio.sleep(NOTIFY_INTERVAL_SECONDS)


async def _check_expiring_instances():
    expiring = get_expiring_instances(platform="mattermost")
    for instance in expiring:
        try:
            droplet_id = instance["droplet_id"]
            name = instance["name"]
            ip_address = instance["ip_address"]
            droplet_type = instance["droplet_type"]
            expiration_date = instance["expiration_date"]
            creator_id = instance["creator_id"]
            creator_username = instance.get("creator_username")

            if isinstance(expiration_date, str):
                try:
                    expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    logger.error(f"Ошибка при разборе даты: {expiration_date}")
                    continue

            time_left = (expiration_date - datetime.now()).total_seconds()

            if 0 < time_left <= 86400:
                try:
                    dm_channel = await get_dm_channel(str(creator_id))
                    buttons = [
                        {
                            "id": f"bgext3_{droplet_id}",
                            "name": "Продлить на 3 дня",
                            "context": {"action": f"bg_extend_3_{droplet_id}"},
                        },
                        {
                            "id": f"bgext7_{droplet_id}",
                            "name": "Продлить на 7 дней",
                            "context": {"action": f"bg_extend_7_{droplet_id}"},
                        },
                        {
                            "id": f"bgdel_{droplet_id}",
                            "name": "Удалить сейчас",
                            "context": {"action": f"bg_delete_{droplet_id}"},
                        },
                    ]
                    await post_with_buttons(
                        dm_channel,
                        f"Инстанс **'{name}'** с IP **{ip_address}** будет удалён через 24 часа.\n"
                        f"Хотите продлить срок действия или удалить его сейчас?",
                        buttons,
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки MM уведомления пользователю {creator_id}: {e}")

            elif time_left <= 0:
                logger.info(f"Инстанс '{name}' с ID {droplet_id} должен быть удалён. Создаём снэпшот...")
                snapshot_date = datetime.now().strftime("%Y%m%d")
                snapshot_name = f"{name}-expired-{snapshot_date}"
                try:
                    snap_result = await create_snapshot(DIGITALOCEAN_TOKEN, droplet_id, snapshot_name)
                    if snap_result["success"]:
                        action_id = snap_result["action_id"]
                        wait_result = await wait_for_action(DIGITALOCEAN_TOKEN, action_id)
                        if wait_result["success"]:
                            logger.info(f"Снэпшот '{snapshot_name}' создан для дроплета {droplet_id}.")
                            await mm_send_notification(
                                driver,
                                action="snapshot_created",
                                droplet_name=name,
                                ip_address=ip_address,
                                droplet_type=droplet_type,
                                expiration_date=str(expiration_date),
                                creator_id=creator_id,
                                creator_username=creator_username,
                            )
                except Exception as e:
                    logger.warning(f"Ошибка снэпшота для дроплета {droplet_id}: {e}. Продолжаем удаление.")

                delete_result = await delete_droplet(
                    DIGITALOCEAN_TOKEN,
                    droplet_id,
                    dns_zone=instance.get("dns_zone"),
                    dns_record_id=instance.get("dns_record_id"),
                )
                if delete_result["success"]:
                    logger.info(f"Инстанс '{name}' удалён (срок действия истёк).")
                    await mm_send_notification(
                        driver,
                        action="auto_deleted",
                        droplet_name=name,
                        ip_address=ip_address,
                        droplet_type=droplet_type,
                        expiration_date=str(expiration_date),
                        creator_id=creator_id,
                        creator_username=creator_username,
                    )
                else:
                    logger.error(f"Ошибка при удалении инстанса '{name}': {delete_result['message']}")

        except Exception as e:
            logger.error(f"Ошибка при обработке инстанса {instance}: {e}")


async def _check_expiring_k8s_clusters():
    expiring = get_expiring_k8s_clusters(platform="mattermost")
    for cluster in expiring:
        try:
            cluster_id = cluster["cluster_id"]
            cluster_name = cluster["cluster_name"]
            expiration_date = cluster["expiration_date"]
            creator_id = cluster["creator_id"]
            creator_username = cluster.get("creator_username")

            if isinstance(expiration_date, str):
                try:
                    expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue

            time_left = (expiration_date - datetime.now()).total_seconds()

            if 0 < time_left <= 86400:
                try:
                    dm_channel = await get_dm_channel(str(creator_id))
                    buttons = [
                        {
                            "id": f"bgkext3_{cluster_id}",
                            "name": "Продлить на 3 дня",
                            "context": {"action": f"bg_k8s_extend_3_{cluster_id}"},
                        },
                        {
                            "id": f"bgkext7_{cluster_id}",
                            "name": "Продлить на 7 дней",
                            "context": {"action": f"bg_k8s_extend_7_{cluster_id}"},
                        },
                        {
                            "id": f"bgkdel_{cluster_id}",
                            "name": "Удалить сейчас",
                            "context": {"action": f"bg_k8s_delete_{cluster_id}"},
                        },
                    ]
                    await post_with_buttons(
                        dm_channel,
                        f"K8s кластер **'{cluster_name}'** будет удалён через 24 часа.\n"
                        f"Хотите продлить срок действия или удалить его сейчас?",
                        buttons,
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки MM K8s уведомления пользователю {creator_id}: {e}")

            elif time_left <= 0:
                logger.info(f"K8s кластер '{cluster_name}' истёк. Удаляем...")
                delete_result = await delete_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
                if delete_result["success"]:
                    logger.info(f"K8s кластер '{cluster_name}' удалён (истёк срок).")
                    await mm_send_k8s_notification(
                        driver,
                        action="auto_deleted",
                        cluster_name=cluster_name,
                        region=cluster["region"],
                        node_size=cluster["node_size"],
                        node_count=cluster["node_count"],
                        expiration_date=str(expiration_date),
                        creator_id=creator_id,
                        creator_username=creator_username,
                    )
                else:
                    logger.error(f"Ошибка при автоудалении K8s кластера '{cluster_name}': {delete_result['message']}")

        except Exception as e:
            logger.error(f"Ошибка при обработке K8s кластера {cluster}: {e}")


async def mm_poll_provisioning_clusters():
    """Background loop: poll provisioning K8s clusters (mattermost platform only)."""
    while True:
        try:
            provisioning = get_provisioning_k8s_clusters(platform="mattermost")
            for cluster in provisioning:
                try:
                    cluster_id = cluster["cluster_id"]
                    cluster_name = cluster["cluster_name"]
                    creator_id = cluster["creator_id"]

                    status_result = await get_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
                    if not status_result["success"]:
                        continue

                    new_state = status_result.get("status")
                    endpoint = status_result.get("endpoint", "")

                    logger.info(f"MM K8s кластер '{cluster_name}' ({cluster_id}): статус DO = {new_state!r}")

                    if new_state in ("running", "degraded"):
                        ok = update_k8s_cluster_status(cluster_id, "running", endpoint=endpoint)
                        if not ok:
                            continue

                        degraded_note = (
                            "\n⚠️ Кластер запущен в деградированном состоянии." if new_state == "degraded" else ""
                        )
                        dm_channel = await get_dm_channel(str(creator_id))

                        endpoint_line = f"\nEndpoint: `{endpoint}`" if endpoint else ""
                        await post_message(
                            dm_channel, f"**K8s кластер {cluster_name} готов!**{endpoint_line}{degraded_note}"
                        )

                        # Send kubeconfig
                        kube_result = await get_kubeconfig(DIGITALOCEAN_TOKEN, cluster_id)
                        if kube_result["success"]:
                            try:
                                kubeconfig_bytes = kube_result["kubeconfig"].encode("utf-8")
                                await send_file(dm_channel, f"kubeconfig-{cluster_name}.yaml", kubeconfig_bytes)
                            except Exception as e:
                                logger.error(f"Ошибка отправки kubeconfig кластера {cluster_id}: {e}")

                        await mm_send_k8s_notification(
                            driver,
                            action="ready",
                            cluster_name=cluster_name,
                            region=cluster["region"],
                            node_size=cluster["node_size"],
                            node_count=cluster["node_count"],
                            expiration_date=cluster["expiration_date"],
                            creator_id=creator_id,
                            creator_username=cluster.get("creator_username"),
                            endpoint=endpoint,
                        )

                    elif new_state == "errored":
                        update_k8s_cluster_status(cluster_id, "errored")
                        dm_channel = await get_dm_channel(str(creator_id))
                        await post_message(
                            dm_channel,
                            f"**K8s кластер {cluster_name}** завершился с ошибкой при создании.",
                        )
                        await mm_send_k8s_notification(
                            driver,
                            action="errored",
                            cluster_name=cluster_name,
                            region=cluster["region"],
                            node_size=cluster["node_size"],
                            node_count=cluster["node_count"],
                            expiration_date=cluster["expiration_date"],
                            creator_id=creator_id,
                            creator_username=cluster.get("creator_username"),
                        )

                except Exception as e:
                    logger.error(f"Ошибка при проверке статуса K8s кластера {cluster}: {e}")

        except Exception:
            logger.exception("Ошибка в фоновой задаче mm_poll_provisioning_clusters")
        await asyncio.sleep(K8S_POLL_INTERVAL_SECONDS)


async def cleanup_conversations_loop():
    """Periodically clean up expired conversations."""
    while True:
        conversations.cleanup_expired()
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


# ============================================================
# Main
# ============================================================


async def main():
    global driver, bot_user_id

    if not MM_BOT_TOKEN:
        logger.error("MM_BOT_TOKEN не задан. Mattermost бот не запущен.")
        sys.exit(1)

    if not MM_SERVER_URL:
        logger.error("MM_SERVER_URL не задан. Mattermost бот не запущен.")
        sys.exit(1)

    # Parse server URL
    from urllib.parse import urlparse

    parsed = urlparse(MM_SERVER_URL)
    mm_host = parsed.hostname
    mm_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    mm_scheme = parsed.scheme

    logger.info(f"Подключение к Mattermost: {MM_SERVER_URL}")

    driver = Driver(
        {
            "url": mm_host,
            "token": MM_BOT_TOKEN,
            "scheme": mm_scheme,
            "port": mm_port,
            "basepath": "/api/v4",
            "verify": True,
            "timeout": 30,
        }
    )
    await mm_api(driver.login)
    # Patch auth_header to include custom User-Agent (passes CloudFront WAF
    # which blocks the default python-requests UA)
    _orig_auth_header = driver.client.auth_header

    def _patched_auth_header():
        headers = _orig_auth_header()
        if headers is None:
            headers = {}
        headers["User-Agent"] = "MattermostAdminBot/1.0 (mattermostdriver)"
        return headers

    driver.client.auth_header = _patched_auth_header
    bot_info = await mm_api(driver.users.get_user, "me")
    bot_user_id = bot_info["id"]
    logger.info(f"Mattermost бот авторизован: {bot_info['username']} (ID: {bot_user_id})")

    init_db()

    # Start aiohttp server for button callbacks
    app = web.Application()
    app.router.add_post("/actions", handle_action)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", MM_WEBHOOK_PORT)
    await site.start()
    logger.info(f"Action callback server запущен на 0.0.0.0:{MM_WEBHOOK_PORT}")

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Получен сигнал завершения")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Start background tasks
    tasks = [
        asyncio.create_task(ws_listener()),
        asyncio.create_task(mm_notify_and_check_instances()),
        asyncio.create_task(mm_poll_provisioning_clusters()),
        asyncio.create_task(cleanup_conversations_loop()),
    ]

    logger.info("Mattermost бот запущен")
    await stop_event.wait()

    # Cleanup
    logger.info("Завершение...")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()
    await mm_api(driver.logout)
    logger.info("Mattermost бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())

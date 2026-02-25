import io
import logging
import re
from warnings import filterwarnings

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.warnings import PTBUserWarning

from config import BOT_TOKEN, SSH_CONFIG, DIGITALOCEAN_TOKEN
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
from modules.authorization import is_authorized, is_authorized_for_bot
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
from modules.notifications import send_notification, send_k8s_notification
from datetime import datetime

# Suppress PTBUserWarning for CallbackQueryHandler in ConversationHandler
filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
NOTIFY_INTERVAL_SECONDS = 43200  # 12 hours
K8S_POLL_INTERVAL_SECONDS = 30   # poll provisioning clusters every 30s
CONVERSATION_TIMEOUT = 600  # 10 minutes

DROPLET_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,253}[a-zA-Z0-9]$")
SUBDOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

# ConversationHandler states
MAIL_INPUT = 0
RESET_INPUT = 0
SELECT_SSH_KEY, SELECT_IMAGE, SELECT_DNS_ZONE, INPUT_SUBDOMAIN, SELECT_TYPE, SELECT_DURATION, INPUT_NAME = range(7)
MANAGE_LIST, MANAGE_ACTION, MANAGE_EXTEND, MANAGE_CONFIRM_DELETE = range(100, 104)
K8S_SELECT_VERSION, K8S_SELECT_NODE_SIZE, K8S_SELECT_NODE_COUNT, K8S_SELECT_DURATION, K8S_INPUT_NAME = range(200, 205)
K8S_MANAGE_LIST, K8S_MANAGE_ACTION, K8S_MANAGE_EXTEND, K8S_MANAGE_CONFIRM_DELETE = range(205, 209)

# Track users who initiated /start in group chats
allowed_users = set()


# --- /start ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение с выбором действий."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_authorized_for_bot(user_id):
        logger.warning(f"Доступ запрещён для {user_id}")
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    if update.effective_chat.type in ["group", "supergroup"]:
        allowed_users.add(user_id)

    logger.info(f"Команда /start от пользователя {user_id} в чате {chat_id}")

    keyboard = [
        [InlineKeyboardButton("Создать почтовый ящик", callback_data="create_mailbox")],
        [InlineKeyboardButton("Сброс пароля", callback_data="reset_password")],
        [InlineKeyboardButton("Создать инстанс", callback_data="create_droplet")],
        [InlineKeyboardButton("Управление инстансами", callback_data="manage_droplets")],
        [InlineKeyboardButton("☸️ Создать K8s кластер", callback_data="create_k8s")],
        [InlineKeyboardButton("☸️ Мои K8s кластеры", callback_data="manage_k8s")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Добро пожаловать! Выберите действие:", reply_markup=reply_markup)


# --- Mail creation conversation ---


async def mail_create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало создания почтового ящика."""
    query = update.callback_query
    user_id = query.from_user.id

    if not _check_group_access(update, user_id):
        await query.answer("У вас нет доступа к этой кнопке.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if not is_authorized(user_id, "mail"):
        await query.message.reply_text("У вас нет прав для создания почтовых ящиков.")
        return ConversationHandler.END

    await query.message.reply_text("Введите имя почтового ящика:")
    return MAIL_INPUT


async def mail_create_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получение имени ящика и создание."""
    mailbox_name = update.message.text.strip()
    password = generate_password()
    result = create_mailbox(mailbox_name, password, SSH_CONFIG)

    if result["success"]:
        await update.message.reply_text(result["message"], parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(f"Ошибка: {result['message']}")

    return ConversationHandler.END


# --- Password reset conversation ---


async def reset_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало сброса пароля."""
    query = update.callback_query
    user_id = query.from_user.id

    if not _check_group_access(update, user_id):
        await query.answer("У вас нет доступа к этой кнопке.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if not is_authorized(user_id, "mail"):
        await query.message.reply_text("У вас нет прав для сброса паролей почтовых ящиков.")
        return ConversationHandler.END

    await query.message.reply_text("Введите имя почтового ящика для сброса пароля:")
    return RESET_INPUT


async def reset_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получение имени ящика и сброс пароля."""
    mailbox_name = update.message.text.strip()
    new_password = generate_password()
    result = reset_password(mailbox_name, new_password, SSH_CONFIG)

    if result["success"]:
        await update.message.reply_text(result["message"], parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(f"Ошибка: {result['message']}")

    return ConversationHandler.END


# --- Droplet creation conversation ---


def _build_ssh_key_keyboard(keys, selected_ids, expanded):
    """Построить inline-клавиатуру для мультивыбора SSH-ключей."""
    visible_keys = keys if expanded or len(keys) <= 3 else keys[:3]
    keyboard = []
    for key in visible_keys:
        key_id = str(key["id"])
        prefix = "✅" if key_id in selected_ids else "⬜"
        keyboard.append([InlineKeyboardButton(f"{prefix} {key['name']}", callback_data=f"ssh_toggle_{key_id}")])

    if not expanded and len(keys) > 3:
        remaining = len(keys) - 3
        keyboard.append([InlineKeyboardButton(f"Другие ключи ({remaining})", callback_data="ssh_more_keys")])

    count = len(selected_ids)
    keyboard.append([InlineKeyboardButton(f"Продолжить ✓ ({count})", callback_data="ssh_confirm")])
    return InlineKeyboardMarkup(keyboard)


async def droplet_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало создания инстанса — выбор SSH-ключа."""
    query = update.callback_query
    user_id = query.from_user.id

    if not _check_group_access(update, user_id):
        await query.answer("У вас нет доступа к этой кнопке.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if not is_authorized(user_id, "droplet"):
        await query.message.reply_text("У вас нет прав для создания инстансов в DigitalOcean.")
        return ConversationHandler.END

    result = await get_ssh_keys(DIGITALOCEAN_TOKEN)
    if not result["success"]:
        await query.message.reply_text(f"Ошибка: {result['message']}")
        return ConversationHandler.END

    ssh_keys = result["keys"]
    if not ssh_keys:
        await query.message.reply_text("Нет доступных SSH-ключей в DigitalOcean.")
        return ConversationHandler.END

    # Reorder keys by user preference (most frequently used first)
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

    context.user_data["ssh_keys_list"] = ssh_keys
    context.user_data["selected_ssh_keys"] = preselect
    context.user_data["ssh_keys_expanded"] = False

    reply_markup = _build_ssh_key_keyboard(ssh_keys, context.user_data["selected_ssh_keys"], False)
    await query.message.reply_text("Выберите SSH ключи:", reply_markup=reply_markup)
    return SELECT_SSH_KEY


async def droplet_toggle_ssh_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключение выбора SSH-ключа."""
    query = update.callback_query
    await query.answer()

    key_id = query.data.removeprefix("ssh_toggle_")
    selected = context.user_data.get("selected_ssh_keys", set())

    if key_id in selected:
        selected.discard(key_id)
    else:
        selected.add(key_id)

    context.user_data["selected_ssh_keys"] = selected
    reply_markup = _build_ssh_key_keyboard(
        context.user_data["ssh_keys_list"], selected, context.user_data.get("ssh_keys_expanded", False)
    )
    await query.edit_message_reply_markup(reply_markup=reply_markup)
    return SELECT_SSH_KEY


async def droplet_expand_ssh_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Раскрыть полный список SSH-ключей."""
    query = update.callback_query
    await query.answer()

    context.user_data["ssh_keys_expanded"] = True
    reply_markup = _build_ssh_key_keyboard(
        context.user_data["ssh_keys_list"], context.user_data.get("selected_ssh_keys", set()), True
    )
    await query.edit_message_reply_markup(reply_markup=reply_markup)
    return SELECT_SSH_KEY


async def droplet_confirm_ssh_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение выбора SSH-ключей — переход к выбору образа."""
    query = update.callback_query

    selected = context.user_data.get("selected_ssh_keys", set())
    if not selected:
        await query.answer("Выберите хотя бы один SSH-ключ", show_alert=True)
        return SELECT_SSH_KEY

    await query.answer()

    context.user_data["ssh_key_ids"] = [int(k) for k in selected]
    # Clean up temp data
    context.user_data.pop("ssh_keys_list", None)
    context.user_data.pop("selected_ssh_keys", None)
    context.user_data.pop("ssh_keys_expanded", None)

    result = await get_images(DIGITALOCEAN_TOKEN)
    if not result["success"]:
        await query.message.reply_text(f"Ошибка: {result['message']}")
        return ConversationHandler.END

    images = result["images"]
    sorted_images = sorted(images, key=lambda x: x["distribution"])
    keyboard = [
        [
            InlineKeyboardButton(
                f"{image['distribution']} {image['name']}",
                callback_data=f"image_{image['id']}",
            )
        ]
        for image in sorted_images
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите образ:", reply_markup=reply_markup)
    return SELECT_IMAGE


async def droplet_select_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор образа — переход к выбору DNS-зоны."""
    query = update.callback_query
    await query.answer()

    image_id = query.data.removeprefix("image_")
    context.user_data["image"] = image_id

    # Попытка получить список доменов для DNS
    result = await get_domains(DIGITALOCEAN_TOKEN)
    if result["success"] and result["domains"]:
        keyboard = [[InlineKeyboardButton(domain, callback_data=f"dns_zone_{domain}")] for domain in result["domains"]]
        keyboard.append([InlineKeyboardButton("Пропустить (без DNS)", callback_data="dns_zone_skip")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Выберите DNS-зону для создания записи:", reply_markup=reply_markup)
        return SELECT_DNS_ZONE

    # Нет доменов или ошибка — пропускаем DNS, переходим к выбору типа
    context.user_data["dns_zone"] = None
    context.user_data["subdomain"] = None
    return await _show_droplet_type_keyboard(query.message)


async def droplet_select_dns_zone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор DNS-зоны — переход к вводу субдомена или выбору типа."""
    query = update.callback_query
    await query.answer()

    zone = query.data.removeprefix("dns_zone_")
    if zone == "skip":
        context.user_data["dns_zone"] = None
        context.user_data["subdomain"] = None
        return await _show_droplet_type_keyboard(query.message)

    context.user_data["dns_zone"] = zone
    await query.message.reply_text(f"Введите имя субдомена для зоны {zone}:")
    return INPUT_SUBDOMAIN


async def droplet_input_subdomain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получение субдомена — переход к выбору типа дроплета."""
    subdomain = update.message.text.strip().lower()

    if not SUBDOMAIN_RE.match(subdomain):
        await update.message.reply_text(
            "Недопустимое имя субдомена. Используйте латинские буквы, цифры и дефис "
            "(1-63 символа, начинается и заканчивается буквой или цифрой).\nПопробуйте ещё раз:"
        )
        return INPUT_SUBDOMAIN

    context.user_data["subdomain"] = subdomain
    return await _show_droplet_type_keyboard(update.message)


async def _show_droplet_type_keyboard(message) -> int:
    """Показать клавиатуру выбора типа дроплета с ценами."""
    sizes = await get_sizes(DIGITALOCEAN_TOKEN)

    keyboard = []
    for slug, label in DROPLET_TYPES.items():
        price_info = sizes.get(slug)
        if price_info:
            btn_text = f"{label} — ${price_info['price_monthly']}/мес"
        else:
            btn_text = label
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"droplet_type_{slug}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text("Выберите тип Droplet:", reply_markup=reply_markup)
    return SELECT_TYPE


async def droplet_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор типа дроплета — переход к выбору длительности."""
    query = update.callback_query
    await query.answer()

    droplet_type = query.data.removeprefix("droplet_type_")
    context.user_data["droplet_type"] = droplet_type

    # Получаем цены для расчёта стоимости по длительности
    sizes = await get_sizes(DIGITALOCEAN_TOKEN)
    price_info = sizes.get(droplet_type)
    if price_info:
        context.user_data["price_monthly"] = price_info["price_monthly"]
        context.user_data["price_hourly"] = price_info["price_hourly"]
        hourly = price_info["price_hourly"]
        durations = [
            ("1 день", 1),
            ("3 дня", 3),
            ("Неделя", 7),
            ("2 недели", 14),
            ("Месяц", 30),
        ]
        keyboard = [
            [InlineKeyboardButton(f"{label} — ~${hourly * 24 * days:.2f}", callback_data=f"duration_{days}")]
            for label, days in durations
        ]
    else:
        context.user_data["price_monthly"] = None
        context.user_data["price_hourly"] = None
        keyboard = [
            [InlineKeyboardButton("1 день", callback_data="duration_1")],
            [InlineKeyboardButton("3 дня", callback_data="duration_3")],
            [InlineKeyboardButton("Неделя", callback_data="duration_7")],
            [InlineKeyboardButton("2 недели", callback_data="duration_14")],
            [InlineKeyboardButton("Месяц", callback_data="duration_30")],
        ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите длительность аренды инстанса:", reply_markup=reply_markup)
    return SELECT_DURATION


async def droplet_select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор длительности — запрос имени инстанса или создание с FQDN."""
    query = update.callback_query
    await query.answer()

    duration = int(query.data.removeprefix("duration_"))
    context.user_data["duration"] = duration

    # Если выбран DNS, используем FQDN как имя дроплета
    dns_zone = context.user_data.get("dns_zone")
    subdomain = context.user_data.get("subdomain")
    if dns_zone and subdomain:
        droplet_name = f"{subdomain}.{dns_zone}"
        return await _create_droplet_and_respond(query.message, query.from_user, context, droplet_name)

    await query.message.reply_text("Введите имя инстанса:")
    return INPUT_NAME


async def droplet_input_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получение имени и создание дроплета."""
    droplet_name = update.message.text.strip()

    if not DROPLET_NAME_RE.match(droplet_name):
        await update.message.reply_text(
            "Недопустимое имя инстанса. Используйте латинские буквы, цифры, точку, дефис или подчёркивание "
            "(2-255 символов, начинается и заканчивается буквой или цифрой).\nПопробуйте ещё раз:"
        )
        return INPUT_NAME

    return await _create_droplet_and_respond(update.message, update.effective_user, context, droplet_name)


async def _create_droplet_and_respond(message, user, context, droplet_name) -> int:
    """Общая логика создания дроплета, DNS-записи и отправки уведомления."""
    user_id = user.id
    creator_username = f"@{user.username}" if user.username else user.first_name
    creator_tag = user.username or user.first_name
    data = context.user_data

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
        # Создание DNS-записи (если указаны зона и субдомен)
        dns_zone = data.get("dns_zone")
        subdomain = data.get("subdomain")
        if dns_zone and subdomain:
            dns_result = await create_dns_record(DIGITALOCEAN_TOKEN, dns_zone, subdomain, result["ip_address"])
            if dns_result["success"]:
                domain_name = dns_result["fqdn"]
                update_instance_dns(
                    result["droplet_id"],
                    domain_name,
                    dns_result["record_id"],
                    dns_zone,
                )
                # Дополняем сообщение информацией о DNS
                result["message"] += f"\nDNS: `{domain_name}`"
            else:
                await message.reply_text(f"Инстанс создан, но DNS-запись не удалось создать: {dns_result['message']}")

        record_ssh_key_usage(user_id, data["ssh_key_ids"])

        await message.reply_text(result["message"], parse_mode="MarkdownV2")
        await send_notification(
            context.bot,
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
        await message.reply_text(f"Ошибка: {result['message']}")

    context.user_data.clear()
    return ConversationHandler.END


# --- Standalone callback handlers (extend / delete with ownership check) ---


async def handle_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Продление срока инстанса с проверкой владельца."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except BadRequest:
        pass

    try:
        parts = query.data.split("_")
        days = int(parts[1])
        droplet_id = int(parts[2])
    except (IndexError, ValueError):
        await query.message.reply_text("Ошибка: некорректные данные запроса.")
        return

    instance = get_instance_by_id(droplet_id)
    if not instance:
        await query.message.reply_text("Инстанс не найден.")
        return

    if instance["creator_id"] != user_id:
        logger.warning(f"Пользователь {user_id} попытался продлить чужой инстанс {droplet_id}")
        await query.message.reply_text("У вас нет прав для продления этого инстанса.")
        return

    result = extend_instance_expiration(droplet_id, days)
    logger.info(f"extend_instance_expiration result - {result}")
    if result:
        await query.message.reply_text(f"Срок действия инстанса продлён на {days} дней.")
        await send_notification(
            context.bot,
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
        await query.message.reply_text("Ошибка при продлении инстанса. Пожалуйста, попробуйте позже.")


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление инстанса с проверкой владельца."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except BadRequest:
        pass

    try:
        droplet_id = int(query.data.removeprefix("delete_"))
    except ValueError:
        await query.message.reply_text("Ошибка: некорректные данные запроса.")
        return

    instance = get_instance_by_id(droplet_id)
    if not instance:
        await query.message.reply_text("Инстанс не найден.")
        return

    if instance["creator_id"] != user_id:
        logger.warning(f"Пользователь {user_id} попытался удалить чужой инстанс {droplet_id}")
        await query.message.reply_text("У вас нет прав для удаления этого инстанса.")
        return

    delete_result = await delete_droplet(
        DIGITALOCEAN_TOKEN,
        droplet_id,
        dns_zone=instance.get("dns_zone"),
        dns_record_id=instance.get("dns_record_id"),
    )
    if delete_result["success"]:
        await query.message.edit_text("Инстанс был успешно удалён!")
        logger.info(f"Инстанс {droplet_id} был удалён по запросу пользователя {user_id}.")
        await send_notification(
            context.bot,
            action="deleted",
            droplet_name=instance["name"],
            ip_address=instance["ip_address"],
            droplet_type=instance["droplet_type"],
            expiration_date=instance["expiration_date"],
            creator_id=user_id,
            creator_username=instance.get("creator_username"),
        )
    else:
        await query.message.reply_text(f"Ошибка при удалении инстанса: {delete_result['message']}")


# --- Conversation cancel / timeout ---


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена текущей операции."""
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Операция отменена.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Операция отменена.")
    return ConversationHandler.END


async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка таймаута разговора."""
    context.user_data.clear()
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "Время ожидания истекло. Операция отменена. Используйте /start для начала."
        )
    return ConversationHandler.END


# --- Manage droplets conversation ---


async def manage_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа: показать список инстансов пользователя."""
    query = update.callback_query
    user_id = query.from_user.id

    if not _check_group_access(update, user_id):
        await query.answer("У вас нет доступа к этой кнопке.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if not is_authorized(user_id, "droplet"):
        await query.message.reply_text("У вас нет прав для управления инстансами.")
        return ConversationHandler.END

    return await _show_droplet_list(query.message, user_id)


async def _show_droplet_list(message, user_id) -> int:
    """Показать список инстансов пользователя с кнопками управления."""
    instances = get_instances_by_creator(user_id)

    if not instances:
        await message.reply_text("У вас нет активных инстансов.")
        return ConversationHandler.END

    for inst in instances:
        type_label = DROPLET_TYPES.get(inst["droplet_type"], inst["droplet_type"])
        dns_line = f"DNS: {inst['domain_name']}\n" if inst.get("domain_name") else ""

        # Расчёт потраченных средств
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
        keyboard = [
            [
                InlineKeyboardButton("Продлить", callback_data=f"my_extend_{inst['droplet_id']}"),
                InlineKeyboardButton("Удалить", callback_data=f"my_delete_{inst['droplet_id']}"),
            ]
        ]
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    return MANAGE_ACTION


async def manage_extend_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор срока продления инстанса."""
    query = update.callback_query
    await query.answer()

    droplet_id = int(query.data.removeprefix("my_extend_"))
    context.user_data["manage_droplet_id"] = droplet_id

    keyboard = [
        [InlineKeyboardButton("3 дня", callback_data="my_ext_days_3")],
        [InlineKeyboardButton("7 дней", callback_data="my_ext_days_7")],
    ]
    await query.message.reply_text("На сколько продлить?", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_EXTEND


async def manage_extend_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение продления инстанса."""
    query = update.callback_query
    await query.answer()

    days = int(query.data.removeprefix("my_ext_days_"))
    droplet_id = context.user_data.get("manage_droplet_id")
    user_id = query.from_user.id

    instance = get_instance_by_id(droplet_id)
    if not instance:
        await query.message.reply_text("Инстанс не найден.")
        context.user_data.clear()
        return ConversationHandler.END

    result = extend_instance_expiration(droplet_id, days)
    if result:
        await query.message.reply_text(f"Срок действия инстанса продлён на {days} дней.")
        await send_notification(
            context.bot,
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
        await query.message.reply_text("Ошибка при продлении инстанса.")

    context.user_data.clear()
    return ConversationHandler.END


async def manage_delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос подтверждения удаления инстанса."""
    query = update.callback_query
    await query.answer()

    droplet_id = int(query.data.removeprefix("my_delete_"))
    context.user_data["manage_droplet_id"] = droplet_id

    keyboard = [
        [
            InlineKeyboardButton("Да, удалить", callback_data=f"my_confirm_delete_{droplet_id}"),
            InlineKeyboardButton("Отмена", callback_data="my_cancel_delete"),
        ]
    ]
    await query.message.reply_text(
        "Вы уверены, что хотите удалить этот инстанс?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MANAGE_CONFIRM_DELETE


async def manage_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение удаления инстанса."""
    query = update.callback_query
    await query.answer()

    droplet_id = int(query.data.removeprefix("my_confirm_delete_"))
    user_id = query.from_user.id

    instance = get_instance_by_id(droplet_id)
    if not instance:
        await query.message.reply_text("Инстанс не найден.")
        context.user_data.clear()
        return ConversationHandler.END

    delete_result = await delete_droplet(
        DIGITALOCEAN_TOKEN,
        droplet_id,
        dns_zone=instance.get("dns_zone"),
        dns_record_id=instance.get("dns_record_id"),
    )
    if delete_result["success"]:
        await query.message.edit_text("Инстанс был успешно удалён!")
        await send_notification(
            context.bot,
            action="deleted",
            droplet_name=instance["name"],
            ip_address=instance["ip_address"],
            droplet_type=instance["droplet_type"],
            expiration_date=instance["expiration_date"],
            creator_id=user_id,
            creator_username=instance.get("creator_username"),
        )
    else:
        await query.message.reply_text(f"Ошибка при удалении: {delete_result['message']}")

    context.user_data.clear()
    return ConversationHandler.END


async def manage_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена удаления."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Удаление отменено.")
    context.user_data.clear()
    return ConversationHandler.END


async def manage_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возврат к списку инстансов."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    return await _show_droplet_list(query.message, user_id)


# --- K8s cluster creation conversation ---


async def k8s_create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало создания K8s кластера — выбор версии Kubernetes."""
    query = update.callback_query
    user_id = query.from_user.id

    if not _check_group_access(update, user_id):
        await query.answer("У вас нет доступа к этой кнопке.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if not is_authorized(user_id, "k8s"):
        await query.message.reply_text("У вас нет прав для создания K8s кластеров.")
        return ConversationHandler.END

    result = await get_k8s_versions(DIGITALOCEAN_TOKEN)
    if not result["success"]:
        await query.message.reply_text("Не удалось получить список версий Kubernetes. Попробуйте позже.")
        return ConversationHandler.END

    versions = result["versions"]
    default_slug = result["default_slug"]
    keyboard = []
    for v in versions:
        slug = v["slug"]
        label = f"{'✅ ' if slug == default_slug else ''}{slug}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"k8s_version_{slug}")])

    await query.message.reply_text("Выберите версию Kubernetes:", reply_markup=InlineKeyboardMarkup(keyboard))
    return K8S_SELECT_VERSION


async def k8s_select_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор версии K8s — переход к выбору размера узла."""
    query = update.callback_query
    await query.answer()

    version = query.data.removeprefix("k8s_version_")
    context.user_data["k8s_version"] = version

    result = await get_k8s_sizes(DIGITALOCEAN_TOKEN)
    if not result["success"] or not result["sizes"]:
        await query.message.reply_text("Не удалось получить список типов узлов. Попробуйте позже.")
        return ConversationHandler.END

    sizes = result["sizes"]
    context.user_data["k8s_sizes"] = sizes

    keyboard = []
    for slug, info in sizes.items():
        price_monthly = info.get("price_monthly", 0)
        keyboard.append([InlineKeyboardButton(f"{slug} — ${price_monthly}/мес", callback_data=f"k8s_size_{slug}")])

    await query.message.reply_text("Выберите тип узла:", reply_markup=InlineKeyboardMarkup(keyboard))
    return K8S_SELECT_NODE_SIZE


async def k8s_select_node_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор размера узла — переход к выбору количества узлов."""
    query = update.callback_query
    await query.answer()

    node_size = query.data.removeprefix("k8s_size_")
    context.user_data["k8s_node_size"] = node_size

    sizes = context.user_data.get("k8s_sizes", {})
    size_info = sizes.get(node_size, {})
    context.user_data["k8s_price_hourly_per_node"] = size_info.get("price_hourly", 0)

    keyboard = [
        [InlineKeyboardButton("1 узел", callback_data="k8s_count_1")],
        [InlineKeyboardButton("2 узла", callback_data="k8s_count_2")],
        [InlineKeyboardButton("3 узла", callback_data="k8s_count_3")],
    ]
    await query.message.reply_text("Выберите количество узлов:", reply_markup=InlineKeyboardMarkup(keyboard))
    return K8S_SELECT_NODE_COUNT


async def k8s_select_node_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор количества узлов — переход к выбору длительности."""
    query = update.callback_query
    await query.answer()

    node_count = int(query.data.removeprefix("k8s_count_"))
    context.user_data["k8s_node_count"] = node_count

    price_per_node = context.user_data.get("k8s_price_hourly_per_node", 0)
    total_hourly = price_per_node * node_count

    durations = [
        ("1 день", 1),
        ("3 дня", 3),
        ("Неделя", 7),
        ("2 недели", 14),
    ]
    keyboard = [
        [InlineKeyboardButton(f"{label} — ~${total_hourly * 24 * days:.2f}", callback_data=f"k8s_duration_{days}")]
        for label, days in durations
    ]
    await query.message.reply_text(
        "Выберите длительность аренды кластера:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return K8S_SELECT_DURATION


async def k8s_select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор длительности — запрос имени кластера."""
    query = update.callback_query
    await query.answer()

    duration = int(query.data.removeprefix("k8s_duration_"))
    context.user_data["k8s_duration"] = duration

    await query.message.reply_text("Введите имя кластера (латинские буквы, цифры и дефис, 2-255 символов):")
    return K8S_INPUT_NAME


async def k8s_input_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получение имени кластера и создание."""
    cluster_name = update.message.text.strip()

    if not DROPLET_NAME_RE.match(cluster_name):
        await update.message.reply_text(
            "Недопустимое имя кластера. Используйте латинские буквы, цифры, точку, дефис или подчёркивание "
            "(2-255 символов, начинается и заканчивается буквой или цифрой).\nПопробуйте ещё раз:"
        )
        return K8S_INPUT_NAME

    return await _create_k8s_cluster_and_respond(update.message, update.effective_user, context, cluster_name)


async def _create_k8s_cluster_and_respond(message, user, context, cluster_name) -> int:
    """Общая логика создания K8s кластера и отправки уведомлений."""
    user_id = user.id
    creator_username = f"@{user.username}" if user.username else user.first_name
    data = context.user_data

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
            f"K8s кластер создаётся (~5-10 мин)\n\n"
            f"Имя: {result['cluster_name']}\n"
            f"Регион: {result['region']}\n"
            f"Версия: {result['version']}\n"
            f"Узлы: {node_count}x {data['k8s_node_size']}"
            f"{cost_line}\n"
            f"Срок действия: {result['expiration_date']}"
        )
        await message.reply_text(text)
        await send_k8s_notification(
            context.bot,
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
        await message.reply_text(f"Ошибка при создании кластера: {result['message']}")

    context.user_data.clear()
    return ConversationHandler.END


# --- K8s cluster management conversation ---


async def k8s_manage_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа: показать список K8s кластеров пользователя."""
    query = update.callback_query
    user_id = query.from_user.id

    if not _check_group_access(update, user_id):
        await query.answer("У вас нет доступа к этой кнопке.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if not is_authorized(user_id, "k8s"):
        await query.message.reply_text("У вас нет прав для управления K8s кластерами.")
        return ConversationHandler.END

    return await _show_k8s_cluster_list(query.message, user_id)


async def _show_k8s_cluster_list(message, user_id) -> int:
    """Показать список K8s кластеров пользователя с кнопками управления."""
    clusters = get_k8s_clusters_by_creator(user_id)

    if not clusters:
        await message.reply_text("У вас нет активных K8s кластеров.")
        return ConversationHandler.END

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

        status_emoji = "⏳" if cluster["status"] == "provisioning" else "✅" if cluster["status"] == "running" else "❌"
        text = (
            f"Имя: {cluster['cluster_name']}\n"
            f"Статус: {status_emoji} {cluster['status']}\n"
            f"Регион: {cluster['region']}\n"
            f"Узлы: {cluster['node_count']}x {cluster['node_size']}\n"
            f"{cost_line}"
            f"Срок действия: {cluster['expiration_date']}"
        )
        keyboard = [
            [
                InlineKeyboardButton("Продлить", callback_data=f"k8s_my_extend_{cluster['cluster_id']}"),
                InlineKeyboardButton("Удалить", callback_data=f"k8s_my_delete_{cluster['cluster_id']}"),
            ]
        ]
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    return K8S_MANAGE_ACTION


async def k8s_manage_extend_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор срока продления K8s кластера."""
    query = update.callback_query
    await query.answer()

    cluster_id = query.data.removeprefix("k8s_my_extend_")
    context.user_data["k8s_manage_cluster_id"] = cluster_id

    keyboard = [
        [InlineKeyboardButton("3 дня", callback_data="k8s_ext_days_3")],
        [InlineKeyboardButton("7 дней", callback_data="k8s_ext_days_7")],
    ]
    await query.message.reply_text("На сколько продлить?", reply_markup=InlineKeyboardMarkup(keyboard))
    return K8S_MANAGE_EXTEND


async def k8s_manage_extend_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение продления K8s кластера."""
    query = update.callback_query
    await query.answer()

    days = int(query.data.removeprefix("k8s_ext_days_"))
    cluster_id = context.user_data.get("k8s_manage_cluster_id")
    user_id = query.from_user.id

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await query.message.reply_text("Кластер не найден.")
        context.user_data.clear()
        return ConversationHandler.END

    new_exp = extend_k8s_cluster_expiration(cluster_id, days)
    if new_exp:
        await query.message.reply_text(f"Срок действия кластера продлён на {days} дней.")
        await send_k8s_notification(
            context.bot,
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
        await query.message.reply_text("Ошибка при продлении кластера.")

    context.user_data.clear()
    return ConversationHandler.END


async def k8s_manage_delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос подтверждения удаления K8s кластера."""
    query = update.callback_query
    await query.answer()

    cluster_id = query.data.removeprefix("k8s_my_delete_")
    context.user_data["k8s_manage_cluster_id"] = cluster_id

    keyboard = [
        [
            InlineKeyboardButton("Да, удалить", callback_data=f"k8s_confirm_delete_{cluster_id}"),
            InlineKeyboardButton("Отмена", callback_data="k8s_cancel_delete"),
        ]
    ]
    await query.message.reply_text(
        "Вы уверены, что хотите удалить этот K8s кластер?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return K8S_MANAGE_CONFIRM_DELETE


async def k8s_manage_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение удаления K8s кластера."""
    query = update.callback_query
    await query.answer()

    cluster_id = query.data.removeprefix("k8s_confirm_delete_")
    user_id = query.from_user.id

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await query.message.reply_text("Кластер не найден.")
        context.user_data.clear()
        return ConversationHandler.END

    delete_result = await delete_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
    if delete_result["success"]:
        await query.message.edit_text("K8s кластер успешно удалён!")
        await send_k8s_notification(
            context.bot,
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
        await query.message.reply_text(f"Ошибка при удалении кластера: {delete_result['message']}")

    context.user_data.clear()
    return ConversationHandler.END


async def k8s_manage_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена удаления K8s кластера."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Удаление отменено.")
    context.user_data.clear()
    return ConversationHandler.END


# --- Background job ---


async def notify_and_check_instances(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки инстансов и K8s кластеров и отправки уведомлений."""
    expiring_instances = get_expiring_instances()

    for instance in expiring_instances:
        try:
            droplet_id = instance["droplet_id"]
            name = instance["name"]
            ip_address = instance["ip_address"]
            droplet_type = instance["droplet_type"]
            expiration_date = instance["expiration_date"]
            creator_id = instance["creator_id"]
            creator_username = instance.get("creator_username")

            logger.debug(f"expiration_date из БД: {expiration_date} (тип: {type(expiration_date)})")

            if isinstance(expiration_date, int):
                expiration_date = datetime.fromtimestamp(expiration_date)
            elif isinstance(expiration_date, str):
                try:
                    expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    logger.error(f"Ошибка при разборе даты: {expiration_date}")
                    continue

            time_left = (expiration_date - datetime.now()).total_seconds()
            logger.debug(f"Времени до удаления: {time_left} секунд")

            if 0 < time_left <= 86400:
                try:
                    user_chat = await context.bot.get_chat(creator_id)
                    await user_chat.send_message(
                        f"Инстанс **'{name}'** с IP **{ip_address}** будет удалён через 24 часа.\n"
                        f"Хотите продлить срок действия или удалить его сейчас?",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [InlineKeyboardButton("Продлить на 3 дня", callback_data=f"extend_3_{droplet_id}")],
                                [InlineKeyboardButton("Продлить на 7 дней", callback_data=f"extend_7_{droplet_id}")],
                                [InlineKeyboardButton("Удалить сейчас", callback_data=f"delete_{droplet_id}")],
                            ]
                        ),
                    )
                    logger.info(
                        f"Уведомление отправлено пользователю {creator_id} о предстоящем удалении инстанса '{name}'."
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения пользователю {creator_id}: {e}")

            elif time_left <= 0:
                logger.info(f"Инстанс '{name}' с ID {droplet_id} должен быть удалён. Создаём снэпшот...")

                # Создание снэпшота перед удалением
                snapshot_date = datetime.now().strftime("%Y%m%d")
                snapshot_name = f"{name}-expired-{snapshot_date}"
                try:
                    snap_result = await create_snapshot(DIGITALOCEAN_TOKEN, droplet_id, snapshot_name)
                    if snap_result["success"]:
                        action_id = snap_result["action_id"]
                        wait_result = await wait_for_action(DIGITALOCEAN_TOKEN, action_id)
                        if wait_result["success"]:
                            logger.info(f"Снэпшот '{snapshot_name}' создан для дроплета {droplet_id}.")
                            await send_notification(
                                context.bot,
                                action="snapshot_created",
                                droplet_name=name,
                                ip_address=ip_address,
                                droplet_type=droplet_type,
                                expiration_date=str(expiration_date),
                                creator_id=creator_id,
                                creator_username=creator_username,
                            )
                        else:
                            logger.warning(
                                f"Снэпшот для дроплета {droplet_id} не завершён: {wait_result.get('message')}. "
                                f"Продолжаем удаление."
                            )
                    else:
                        logger.warning(
                            f"Не удалось создать снэпшот для дроплета {droplet_id}: {snap_result.get('message')}. "
                            f"Продолжаем удаление."
                        )
                except Exception as e:
                    logger.warning(f"Ошибка снэпшота для дроплета {droplet_id}: {e}. Продолжаем удаление.")

                # Удаление дроплета
                delete_result = await delete_droplet(
                    DIGITALOCEAN_TOKEN,
                    droplet_id,
                    dns_zone=instance.get("dns_zone"),
                    dns_record_id=instance.get("dns_record_id"),
                )

                if delete_result["success"]:
                    logger.info(f"Инстанс '{name}' удалён, так как срок действия истёк.")
                    await send_notification(
                        context.bot,
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

    # --- K8s: expiry loop ---
    expiring_clusters = get_expiring_k8s_clusters()

    for cluster in expiring_clusters:
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
                    logger.error(f"Ошибка разбора даты K8s кластера: {expiration_date}")
                    continue

            time_left = (expiration_date - datetime.now()).total_seconds()

            if 0 < time_left <= 86400:
                try:
                    user_chat = await context.bot.get_chat(creator_id)
                    await user_chat.send_message(
                        f"K8s кластер **'{cluster_name}'** будет удалён через 24 часа.\n"
                        f"Хотите продлить срок действия или удалить его сейчас?",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [InlineKeyboardButton("Продлить на 3 дня", callback_data=f"k8s_extend_3_{cluster_id}")],
                                [
                                    InlineKeyboardButton(
                                        "Продлить на 7 дней", callback_data=f"k8s_extend_7_{cluster_id}"
                                    )
                                ],
                                [InlineKeyboardButton("Удалить сейчас", callback_data=f"k8s_delete_{cluster_id}")],
                            ]
                        ),
                    )
                    logger.info(f"Уведомление об истечении K8s кластера '{cluster_name}' отправлено {creator_id}.")
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления об истечении K8s кластера {creator_id}: {e}")

            elif time_left <= 0:
                logger.info(f"K8s кластер '{cluster_name}' истёк. Удаляем...")
                # NOTE: DOKS clusters don't support snapshots — delete directly
                delete_result = await delete_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
                if delete_result["success"]:
                    logger.info(f"K8s кластер '{cluster_name}' удалён (истёк срок).")
                    await send_k8s_notification(
                        context.bot,
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


async def poll_provisioning_clusters(context: ContextTypes.DEFAULT_TYPE):
    """Быстрый поллинг provisioning-кластеров каждые 30 сек. При переходе в running — отправляет kubeconfig."""
    provisioning_clusters = get_provisioning_k8s_clusters()
    if not provisioning_clusters:
        return

    for cluster in provisioning_clusters:
        try:
            cluster_id = cluster["cluster_id"]
            cluster_name = cluster["cluster_name"]
            creator_id = cluster["creator_id"]

            status_result = await get_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
            if not status_result["success"]:
                logger.warning(f"Не удалось получить статус K8s кластера {cluster_id}: {status_result.get('message')}")
                continue

            new_state = status_result.get("status")
            endpoint = status_result.get("endpoint", "")

            logger.info(f"K8s кластер '{cluster_name}' ({cluster_id}): статус DO = {new_state!r}")

            if new_state in ("running", "degraded"):
                ok = update_k8s_cluster_status(cluster_id, "running", endpoint=endpoint)
                if not ok:
                    logger.warning(f"Не удалось обновить статус кластера {cluster_id} в БД")
                    continue
                degraded_note = "\n⚠️ Кластер запущен в деградированном состоянии." if new_state == "degraded" else ""
                logger.info(f"K8s кластер '{cluster_name}' готов (state={new_state!r}). Получаем kubeconfig и уведомляем {creator_id}.")

                # Fetch kubeconfig
                kube_result = await get_kubeconfig(DIGITALOCEAN_TOKEN, cluster_id)

                endpoint_line = f"\nEndpoint: <code>{endpoint}</code>" if endpoint else ""
                try:
                    await context.bot.send_message(
                        chat_id=creator_id,
                        text=f"K8s кластер <b>{cluster_name}</b> готов!{endpoint_line}{degraded_note}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления о готовности кластера {creator_id}: {e}")

                if kube_result["success"]:
                    try:
                        kubeconfig_bytes = kube_result["kubeconfig"].encode("utf-8")
                        await context.bot.send_document(
                            chat_id=creator_id,
                            document=io.BytesIO(kubeconfig_bytes),
                            filename=f"kubeconfig-{cluster_name}.yaml",
                            caption="Kubeconfig для подключения к кластеру",
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки kubeconfig кластера {cluster_id} пользователю {creator_id}: {e}")
                else:
                    logger.warning(f"Не удалось получить kubeconfig для кластера {cluster_id}: {kube_result.get('message')}")

                await send_k8s_notification(
                    context.bot,
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
                logger.error(f"K8s кластер '{cluster_name}' завершился с ошибкой.")
                try:
                    await context.bot.send_message(
                        chat_id=creator_id,
                        text=f"K8s кластер <b>{cluster_name}</b> завершился с ошибкой при создании.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления об ошибке кластера {creator_id}: {e}")
                await send_k8s_notification(
                    context.bot,
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


# --- Standalone K8s callback handlers (extend / delete from background job notifications) ---


async def handle_k8s_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Продление K8s кластера из уведомления фоновой задачи."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except BadRequest:
        pass

    try:
        parts = query.data.split("_")
        # format: k8s_extend_{days}_{cluster_id}  (cluster_id may contain hyphens)
        days = int(parts[2])
        cluster_id = "_".join(parts[3:])
    except (IndexError, ValueError):
        await query.message.reply_text("Ошибка: некорректные данные запроса.")
        return

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await query.message.reply_text("K8s кластер не найден.")
        return

    if cluster["creator_id"] != user_id:
        logger.warning(f"Пользователь {user_id} попытался продлить чужой K8s кластер {cluster_id}")
        await query.message.reply_text("У вас нет прав для продления этого кластера.")
        return

    new_exp = extend_k8s_cluster_expiration(cluster_id, days)
    if new_exp:
        await query.message.reply_text(f"Срок действия кластера продлён на {days} дней.")
        await send_k8s_notification(
            context.bot,
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
        await query.message.reply_text("Ошибка при продлении кластера. Попробуйте позже.")


async def handle_k8s_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление K8s кластера из уведомления фоновой задачи."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except BadRequest:
        pass

    # format: k8s_delete_{cluster_id}  (cluster_id may contain hyphens)
    cluster_id = query.data.removeprefix("k8s_delete_")

    cluster = get_k8s_cluster_by_id(cluster_id)
    if not cluster:
        await query.message.reply_text("K8s кластер не найден.")
        return

    if cluster["creator_id"] != user_id:
        logger.warning(f"Пользователь {user_id} попытался удалить чужой K8s кластер {cluster_id}")
        await query.message.reply_text("У вас нет прав для удаления этого кластера.")
        return

    delete_result = await delete_k8s_cluster(DIGITALOCEAN_TOKEN, cluster_id)
    if delete_result["success"]:
        await query.message.edit_text("K8s кластер успешно удалён!")
        await send_k8s_notification(
            context.bot,
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
        await query.message.reply_text(f"Ошибка при удалении кластера: {delete_result['message']}")


# --- Error handler ---


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик исключений."""
    logger.error(msg="Ошибка во время обработки обновления:", exc_info=context.error)
    if isinstance(update, Update):
        message = update.effective_message
        if message:
            try:
                await message.reply_text("Произошла ошибка. Пожалуйста, попробуйте ещё раз.")
            except Exception:
                pass


# --- Helpers ---


def _check_group_access(update: Update, user_id: int) -> bool:
    """Проверка доступа пользователя в групповом чате."""
    if update.effective_chat.type in ["group", "supergroup"]:
        return user_id in allowed_users
    return True


# --- Main ---


def main():
    """Запуск бота."""
    logger.info("Запуск бота...")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation: mail creation
    mail_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(mail_create_entry, pattern=r"^create_mailbox$")],
        states={
            MAIL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, mail_create_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT,
        per_user=True,
        per_chat=True,
    )

    # Conversation: password reset
    reset_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(reset_entry, pattern=r"^reset_password$")],
        states={
            RESET_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reset_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT,
        per_user=True,
        per_chat=True,
    )

    # Conversation: droplet creation
    droplet_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(droplet_entry, pattern=r"^create_droplet$")],
        states={
            SELECT_SSH_KEY: [
                CallbackQueryHandler(droplet_toggle_ssh_key, pattern=r"^ssh_toggle_"),
                CallbackQueryHandler(droplet_expand_ssh_keys, pattern=r"^ssh_more_keys$"),
                CallbackQueryHandler(droplet_confirm_ssh_keys, pattern=r"^ssh_confirm$"),
            ],
            SELECT_IMAGE: [CallbackQueryHandler(droplet_select_image, pattern=r"^image_")],
            SELECT_DNS_ZONE: [CallbackQueryHandler(droplet_select_dns_zone, pattern=r"^dns_zone_")],
            INPUT_SUBDOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, droplet_input_subdomain)],
            SELECT_TYPE: [CallbackQueryHandler(droplet_select_type, pattern=r"^droplet_type_")],
            SELECT_DURATION: [CallbackQueryHandler(droplet_select_duration, pattern=r"^duration_")],
            INPUT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, droplet_input_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        conversation_timeout=CONVERSATION_TIMEOUT,
        per_user=True,
        per_chat=True,
    )

    # Conversation: manage droplets
    manage_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(manage_entry, pattern=r"^manage_droplets$")],
        states={
            MANAGE_ACTION: [
                CallbackQueryHandler(manage_extend_entry, pattern=r"^my_extend_"),
                CallbackQueryHandler(manage_delete_entry, pattern=r"^my_delete_"),
            ],
            MANAGE_EXTEND: [
                CallbackQueryHandler(manage_extend_confirm, pattern=r"^my_ext_days_"),
            ],
            MANAGE_CONFIRM_DELETE: [
                CallbackQueryHandler(manage_delete_confirm, pattern=r"^my_confirm_delete_"),
                CallbackQueryHandler(manage_cancel, pattern=r"^my_cancel_delete$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        conversation_timeout=CONVERSATION_TIMEOUT,
        per_user=True,
        per_chat=True,
    )

    # Conversation: K8s cluster creation
    k8s_create_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(k8s_create_entry, pattern=r"^create_k8s$")],
        states={
            K8S_SELECT_VERSION: [CallbackQueryHandler(k8s_select_version, pattern=r"^k8s_version_")],
            K8S_SELECT_NODE_SIZE: [CallbackQueryHandler(k8s_select_node_size, pattern=r"^k8s_size_")],
            K8S_SELECT_NODE_COUNT: [CallbackQueryHandler(k8s_select_node_count, pattern=r"^k8s_count_")],
            K8S_SELECT_DURATION: [CallbackQueryHandler(k8s_select_duration, pattern=r"^k8s_duration_")],
            K8S_INPUT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, k8s_input_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        conversation_timeout=CONVERSATION_TIMEOUT,
        per_user=True,
        per_chat=True,
    )

    # Conversation: K8s cluster management
    k8s_manage_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(k8s_manage_entry, pattern=r"^manage_k8s$")],
        states={
            K8S_MANAGE_ACTION: [
                CallbackQueryHandler(k8s_manage_extend_entry, pattern=r"^k8s_my_extend_"),
                CallbackQueryHandler(k8s_manage_delete_entry, pattern=r"^k8s_my_delete_"),
            ],
            K8S_MANAGE_EXTEND: [
                CallbackQueryHandler(k8s_manage_extend_confirm, pattern=r"^k8s_ext_days_"),
            ],
            K8S_MANAGE_CONFIRM_DELETE: [
                CallbackQueryHandler(k8s_manage_delete_confirm, pattern=r"^k8s_confirm_delete_"),
                CallbackQueryHandler(k8s_manage_cancel, pattern=r"^k8s_cancel_delete$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        conversation_timeout=CONVERSATION_TIMEOUT,
        per_user=True,
        per_chat=True,
    )

    # Register handlers (order matters — conversations first, then standalone callbacks)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(mail_conv)
    app.add_handler(reset_conv)
    app.add_handler(droplet_conv)
    app.add_handler(manage_conv)
    app.add_handler(k8s_create_conv)
    app.add_handler(k8s_manage_conv)
    app.add_handler(CallbackQueryHandler(handle_extend, pattern=r"^extend_"))
    app.add_handler(CallbackQueryHandler(handle_delete, pattern=r"^delete_"))
    app.add_handler(CallbackQueryHandler(handle_k8s_extend, pattern=r"^k8s_extend_"))
    app.add_handler(CallbackQueryHandler(handle_k8s_delete, pattern=r"^k8s_delete_"))

    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(notify_and_check_instances, interval=NOTIFY_INTERVAL_SECONDS)
    app.job_queue.run_repeating(poll_provisioning_clusters, interval=K8S_POLL_INTERVAL_SECONDS)

    app.run_polling()


if __name__ == "__main__":
    main()

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
)
from modules.mail import create_mailbox, generate_password, reset_password
from modules.notifications import send_notification
from datetime import datetime

# Suppress PTBUserWarning for CallbackQueryHandler in ConversationHandler
filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
NOTIFY_INTERVAL_SECONDS = 43200  # 12 hours
CONVERSATION_TIMEOUT = 600  # 10 minutes

DROPLET_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,253}[a-zA-Z0-9]$")
SUBDOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

# ConversationHandler states
MAIL_INPUT = 0
RESET_INPUT = 0
SELECT_SSH_KEY, SELECT_IMAGE, SELECT_DNS_ZONE, INPUT_SUBDOMAIN, SELECT_TYPE, SELECT_DURATION, INPUT_NAME = range(7)
MANAGE_LIST, MANAGE_ACTION, MANAGE_EXTEND, MANAGE_CONFIRM_DELETE = range(100, 104)

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


# --- Background job ---


async def notify_and_check_instances(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки инстансов и отправки уведомлений."""
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
    app.add_handler(CallbackQueryHandler(handle_extend, pattern=r"^extend_"))
    app.add_handler(CallbackQueryHandler(handle_delete, pattern=r"^delete_"))

    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(notify_and_check_instances, interval=NOTIFY_INTERVAL_SECONDS)

    app.run_polling()


if __name__ == "__main__":
    main()

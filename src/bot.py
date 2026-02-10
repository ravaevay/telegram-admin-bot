import logging
import re
from warnings import filterwarnings

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from modules.create_test_instance import create_droplet, get_ssh_keys, get_images, delete_droplet
from modules.authorization import is_authorized, is_authorized_for_bot
from modules.database import (
    init_db,
    get_expiring_instances,
    extend_instance_expiration,
    get_instance_by_id,
)
from modules.mail import create_mailbox, generate_password, reset_password
from datetime import datetime

# Suppress PTBUserWarning for CallbackQueryHandler in ConversationHandler
filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
NOTIFY_INTERVAL_SECONDS = 43200  # 12 hours
CONVERSATION_TIMEOUT = 600  # 10 minutes

DROPLET_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,253}[a-zA-Z0-9]$")

# ConversationHandler states
MAIL_INPUT = 0
RESET_INPUT = 0
SELECT_SSH_KEY, SELECT_IMAGE, SELECT_TYPE, SELECT_DURATION, INPUT_NAME = range(5)

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
    keyboard = [[InlineKeyboardButton(key["name"], callback_data=f"ssh_key_{key['id']}")] for key in ssh_keys]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите SSH ключ:", reply_markup=reply_markup)
    return SELECT_SSH_KEY


async def droplet_select_ssh_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор SSH-ключа — переход к выбору образа."""
    query = update.callback_query
    await query.answer()

    ssh_key_id = query.data.removeprefix("ssh_key_")
    context.user_data["ssh_key_id"] = ssh_key_id

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
    """Выбор образа — переход к выбору типа дроплета."""
    query = update.callback_query
    await query.answer()

    image_id = query.data.removeprefix("image_")
    context.user_data["image"] = image_id

    keyboard = [
        [InlineKeyboardButton("2GB-2vCPU-60GB", callback_data="droplet_type_s-2vcpu-2gb")],
        [InlineKeyboardButton("4GB-2vCPU-80GB", callback_data="droplet_type_s-2vcpu-4gb")],
        [InlineKeyboardButton("8GB-4vCPU-160GB", callback_data="droplet_type_s-4vcpu-8gb")],
        [InlineKeyboardButton("16GB-8vCPU-320GB", callback_data="droplet_type_s-8vcpu-16gb")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите тип Droplet:", reply_markup=reply_markup)
    return SELECT_TYPE


async def droplet_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор типа дроплета — переход к выбору длительности."""
    query = update.callback_query
    await query.answer()

    droplet_type = query.data.removeprefix("droplet_type_")
    context.user_data["droplet_type"] = droplet_type

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
    """Выбор длительности — запрос имени инстанса."""
    query = update.callback_query
    await query.answer()

    duration = int(query.data.removeprefix("duration_"))
    context.user_data["duration"] = duration

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

    user_id = update.effective_user.id
    data = context.user_data

    result = await create_droplet(
        DIGITALOCEAN_TOKEN,
        droplet_name,
        data["ssh_key_id"],
        data["droplet_type"],
        data["image"],
        data["duration"],
        creator_id=user_id,
    )

    if result["success"]:
        await update.message.reply_text(result["message"], parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(f"Ошибка: {result['message']}")

    context.user_data.clear()
    return ConversationHandler.END


# --- Standalone callback handlers (extend / delete with ownership check) ---


async def handle_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Продление срока инстанса с проверкой владельца."""
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

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
    else:
        await query.message.reply_text("Ошибка при продлении инстанса. Пожалуйста, попробуйте позже.")


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление инстанса с проверкой владельца."""
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

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

    delete_result = await delete_droplet(DIGITALOCEAN_TOKEN, droplet_id)
    if delete_result["success"]:
        await query.message.edit_text("Инстанс был успешно удалён!")
        logger.info(f"Инстанс {droplet_id} был удалён по запросу пользователя {user_id}.")
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


# --- Background job ---


async def notify_and_check_instances(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки инстансов и отправки уведомлений."""
    expiring_instances = get_expiring_instances()

    for instance in expiring_instances:
        try:
            droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id = instance

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
                logger.info(f"Инстанс '{name}' с ID {droplet_id} должен быть удалён. Запускаем удаление...")
                delete_result = await delete_droplet(DIGITALOCEAN_TOKEN, droplet_id)

                if delete_result["success"]:
                    logger.info(f"Инстанс '{name}' удалён, так как срок действия истёк.")
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
            SELECT_SSH_KEY: [CallbackQueryHandler(droplet_select_ssh_key, pattern=r"^ssh_key_")],
            SELECT_IMAGE: [CallbackQueryHandler(droplet_select_image, pattern=r"^image_")],
            SELECT_TYPE: [CallbackQueryHandler(droplet_select_type, pattern=r"^droplet_type_")],
            SELECT_DURATION: [CallbackQueryHandler(droplet_select_duration, pattern=r"^duration_")],
            INPUT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, droplet_input_name)],
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
    app.add_handler(CallbackQueryHandler(handle_extend, pattern=r"^extend_"))
    app.add_handler(CallbackQueryHandler(handle_delete, pattern=r"^delete_"))

    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(notify_and_check_instances, interval=NOTIFY_INTERVAL_SECONDS)

    app.run_polling()


if __name__ == "__main__":
    main()

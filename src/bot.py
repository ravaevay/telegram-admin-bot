import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, SSH_CONFIG, DIGITALOCEAN_TOKEN
from modules.create_test_instance import create_droplet, get_ssh_keys, get_images
from modules.authorization import is_authorized
from modules.database import init_db, save_instance, get_expiring_instances, extend_instance_expiration, delete_instance
from modules.mail import create_mailbox, generate_password, reset_password
from datetime import datetime

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

current_action = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение с выбором действий."""
    user_id = update.effective_user.id
    logger.info(f"Команда /start от пользователя {user_id}")

    keyboard = [
        [InlineKeyboardButton("Создать почтовый ящик", callback_data="create_mailbox")],
        [InlineKeyboardButton("Сброс пароля", callback_data="reset_password")],
        [InlineKeyboardButton("Создать инстанс", callback_data="create_droplet")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Добро пожаловать! Выберите действие:", reply_markup=reply_markup)

async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки."""
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "create_mailbox":
        await query.message.reply_text("Введите имя почтового ящика:")
        current_action[user_id] = {"action": "create_mailbox"}

    elif query.data == "reset_password":
        await query.message.reply_text("Введите имя почтового ящика для сброса пароля:")
        current_action[user_id] = {"action": "reset_password"}

    if query.data == "create_droplet" and is_authorized(user_id, "droplet"):
        result = get_ssh_keys(DIGITALOCEAN_TOKEN)
        if result["success"]:
            ssh_keys = result["keys"]
            keyboard = [[InlineKeyboardButton(key["name"], callback_data=f"ssh_key_{key['id']}")] for key in ssh_keys]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text("Выберите SSH ключ:", reply_markup=reply_markup)
            current_action[user_id] = {"action": "select_ssh_key"}
        else:
            await query.message.reply_text(f"Ошибка: {result['message']}")

    elif query.data.startswith("ssh_key_"):
        ssh_key_id = query.data.split("_")[2]
        result = get_images(DIGITALOCEAN_TOKEN)
        if result["success"]:
            images = result["images"]
            keyboard = [[InlineKeyboardButton(image["name"], callback_data=f"image_{image['id']}")] for image in images]
            reply_markup = InlineKeyboardMarkup(keyboard)
            current_action[user_id] = {"action": "select_image", "ssh_key_id": ssh_key_id}
            await query.message.reply_text("Выберите образ:", reply_markup=reply_markup)
        else:
            await query.message.reply_text(f"Ошибка: {result['message']}")

    elif query.data.startswith("image_"):
        image_id = query.data.split("_")[1]
        user_action = current_action.get(user_id, {})
        ssh_key_id = user_action.get("ssh_key_id")
        if ssh_key_id:
            keyboard = [
                [InlineKeyboardButton("2GB-2vCPU-60GB", callback_data="droplet_type_s-2vcpu-2gb")],
                [InlineKeyboardButton("4GB-2vCPU-80GB", callback_data="droplet_type_s-2vcpu-4gb")],
                [InlineKeyboardButton("8GB-4vCPU-160GB", callback_data="droplet_type_s-4vcpu-8gb")],
                [InlineKeyboardButton("16GB-8vCPU-320GB", callback_data="droplet_type_s-8vcpu-16gb")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            current_action[user_id] = {"action": "select_droplet_type", "ssh_key_id": ssh_key_id, "image": image_id}
            await query.message.reply_text("Выберите тип Droplet:", reply_markup=reply_markup)

    elif query.data.startswith("droplet_type_"):
        droplet_type = query.data.split("_")[2]
        user_action = current_action.get(user_id, {})
        if "ssh_key_id" in user_action and "image" in user_action:
            current_action[user_id] = {
                "action": "select_duration",
                "ssh_key_id": user_action["ssh_key_id"],
                "image": user_action["image"],
                "droplet_type": droplet_type
            }
            keyboard = [
                [InlineKeyboardButton("1 день", callback_data="duration_1")],
                [InlineKeyboardButton("3 дня", callback_data="duration_3")],
                [InlineKeyboardButton("Неделя", callback_data="duration_7")],
                [InlineKeyboardButton("2 недели", callback_data="duration_14")],
                [InlineKeyboardButton("Месяц", callback_data="duration_30")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text("Выберите длительность аренды инстанса:", reply_markup=reply_markup)

    elif query.data.startswith("duration_"):
        duration = int(query.data.split("_")[1])
        user_action = current_action.get(user_id, {})
        if "ssh_key_id" in user_action and "image" in user_action and "droplet_type" in user_action:
            current_action[user_id] = {
                "action": "create_droplet",
                "ssh_key_id": user_action["ssh_key_id"],
                "image": user_action["image"],
                "droplet_type": user_action["droplet_type"],
                "duration": duration
            }
            await query.message.reply_text("Введите имя инстанса:")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений от пользователя."""
    user_id = update.effective_user.id
    user_action = current_action.get(user_id)

    if user_action and user_action.get("action") == "create_mailbox":
        mailbox_name = update.message.text
        password = generate_password()
        result = create_mailbox(mailbox_name, password, SSH_CONFIG)
        if result["success"]:
            await update.message.reply_text(f"Почтовый ящик: {result['address']}\nПароль: {result['password']}")
        else:
            await update.message.reply_text(f"Ошибка: {result['message']}")
        del current_action[user_id]
    elif user_action and user_action.get("action") == "reset_password":
        mailbox_name = update.message.text
        new_password = generate_password()
        result = reset_password(mailbox_name, new_password, SSH_CONFIG)
        if result["success"]:
            await update.message.reply_text(
                f"Пароль успешно сброшен для {result['address']}.\nНовый пароль: {result['new_password']}"
            )
        else:
            await update.message.reply_text(f"Ошибка: {result['message']}")
        del current_action[user_id]

    if user_action and user_action.get("action") == "create_droplet":
        droplet_name = update.message.text
        ssh_key_id = user_action["ssh_key_id"]
        image_id = user_action["image"]
        droplet_type = user_action["droplet_type"]
        duration = user_action["duration"]

        result = create_droplet(
            DIGITALOCEAN_TOKEN, droplet_name, ssh_key_id, droplet_type, image_id, duration, creator_id=user_id
        )

        if result["success"]:
            await update.message.reply_text(
                f"Инстанс '{result['droplet_name']}' успешно создан.\n"
                f"Адрес подключения: root@{result['ip_address']}\n"
                f"Срок действия: до {result['expiration_date']}.")
        else:
            await update.message.reply_text(f"Ошибка: {result['message']}")
        del current_action[user_id]

async def notify_and_check_instances(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки инстансов и отправки уведомлений."""
    expiring_instances = get_expiring_instances()
    for instance in expiring_instances:
        droplet_id, creator_id, name, ip_address, expiration_date, status = instance
        expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d %H:%M:%S")
        time_left = expiration_date - datetime.now()

        if time_left.total_seconds() <= 86400:
            user_chat = await context.bot.get_chat(creator_id)
            await user_chat.send_message(
                f"Инстанс '{name}' с IP {ip_address} будет удалён через 24 часа. Хотите продлить срок действия?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Продлить на 3 дня", callback_data=f"extend_3_{droplet_id}")],
                    [InlineKeyboardButton("Продлить на 7 дней", callback_data=f"extend_7_{droplet_id}")]
                ])
            )
        elif time_left.total_seconds() <= 0:
            delete_result = delete_droplet(DIGITALOCEAN_TOKEN, droplet_id)
            if delete_result["success"]:
                delete_instance(droplet_id)  # Удаляем запись из базы данных
                logger.info(f"Инстанс '{name}' удалён, так как срок действия истёк.")
            else:
                logger.error(f"Ошибка при удалении инстанса '{name}': {delete_result['message']}")

def main():
    """Запуск бота."""
    logger.info("Запуск бота...")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_action))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(notify_and_check_instances, interval=3600)

    app.run_polling()

if __name__ == "__main__":
    main()


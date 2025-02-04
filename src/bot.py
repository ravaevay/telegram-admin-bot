import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, SSH_CONFIG, DIGITALOCEAN_TOKEN
from modules.create_test_instance import create_droplet, get_ssh_keys, get_images, delete_droplet
from modules.authorization import is_authorized, is_authorized_for_bot
from modules.database import init_db, save_instance, get_expiring_instances, extend_instance_expiration, delete_instance
from modules.mail import create_mailbox, generate_password, reset_password
from datetime import datetime

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

current_action = {}

allowed_users = set()  # Список пользователей, которые начали диалог

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение с выбором действий."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_authorized_for_bot(user_id):
        logger.warning(f"⚠️ Доступ запрещён для {user_id}")
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    
    # Если это группа, запоминаем пользователя, отправившего /start
    if update.effective_chat.type in ["group", "supergroup"]:
        allowed_users.add(user_id)

    logger.info(f"Команда /start от пользователя {user_id} в чате {chat_id}")

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

    # Проверяем, разрешено ли этому пользователю нажимать кнопки
    if update.effective_chat.type in ["group", "supergroup"]:
        if user_id not in allowed_users:
            await query.answer("❌ У вас нет доступа к этой кнопке.", show_alert=True)
            return
    
    await query.answer()

    if query.data == "create_mailbox":
        if not is_authorized(user_id, "mail"):
            await query.message.reply_text("❌ У вас нет прав для создания почтовых ящиков.")
            return
        await query.message.reply_text("Введите имя почтового ящика:")
        current_action[user_id] = {"action": "create_mailbox"}

    elif query.data == "reset_password":
        if not is_authorized(user_id, "mail"):
            await query.message.reply_text("❌ У вас нет прав для сброса паролей почтовых ящиков.")
            return
        await query.message.reply_text("Введите имя почтового ящика для сброса пароля:")
        current_action[user_id] = {"action": "reset_password"}

    if query.data == "create_droplet":
        if not is_authorized(user_id, "droplet"):
            await query.message.reply_text("❌ У вас нет прав для создания инстансов в DigitalOcean.")
            return
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
            # Сортируем образы по 'distribution'
            sorted_images = sorted(images, key=lambda x: x["distribution"])

            keyboard = [[InlineKeyboardButton(f"{image['distribution']} {image['name']}", callback_data=f"image_{image['id']}")] for image in sorted_images]
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
    
    elif query.data.startswith("extend_"):
        parts = query.data.split("_")
        days = int(parts[1])
        droplet_id = int(parts[2])

        logger.info(f"Продление инстанса ID {droplet_id} на {days} дней.")

        result = extend_instance_expiration(droplet_id, days)
        logger.info(f"extend_instance_expiration result - {result}")
        if result:
            await query.message.reply_text(f"Срок действия инстанса продлён на {days} дней.")
        else:
            await query.message.reply_text(f"Ошибка при продлении инстанса. Пожалуйста, попробуйте позже.")

    elif query.data.startswith("delete_"):
        droplet_id = int(query.data.split("_")[1])

        delete_result = delete_droplet(DIGITALOCEAN_TOKEN, droplet_id)
        if delete_result["success"]:
            delete_instance(droplet_id)  # Удаляем запись из базы данных
            await query.message.edit_text(f"✅ Инстанс был успешно удалён!")
            logger.info(f"Инстанс {droplet_id} был удалён по запросу пользователя.")
        else:
            await query.message.reply_text(f"❌ Ошибка при удалении инстанса: {delete_result['message']}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений от пользователя."""
    user_id = update.effective_user.id
    user_action = current_action.get(user_id)

    if user_action and user_action.get("action") == "create_mailbox":
        mailbox_name = update.message.text
        password = generate_password()
        result = create_mailbox(mailbox_name, password, SSH_CONFIG)
        if result["success"]:
            await update.message.reply_text(f"{result['message']}")
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
        try:
            droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id = instance
            
            logger.info(f"DEBUG: expiration_date из БД: {expiration_date} (тип: {type(expiration_date)})")

            # Приводим expiration_date к datetime
            if isinstance(expiration_date, int):  # Если это timestamp, конвертируем
                expiration_date = datetime.fromtimestamp(expiration_date)
            elif isinstance(expiration_date, str):  # Если строка, парсим
                try:
                    expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    logger.error(f"Ошибка при разборе даты: {expiration_date}")
                    continue  # Пропускаем этот инстанс

            time_left = (expiration_date - datetime.now()).total_seconds()
            logger.info(f"DEBUG: Времени до удаления: {time_left} секунд")

            if 0 < time_left <= 86400:  # Уведомление за 24 часа до удаления
                try:
                    user_chat = await context.bot.get_chat(creator_id)
                    await user_chat.send_message(
                        f"⚠️ Инстанс **'{name}'** с IP **{ip_address}** будет удалён через 24 часа.\n"
                        f"Хотите продлить срок действия или удалить его сейчас?",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Продлить на 3 дня", callback_data=f"extend_3_{droplet_id}")],
                            [InlineKeyboardButton("Продлить на 7 дней", callback_data=f"extend_7_{droplet_id}")],
                            [InlineKeyboardButton("🗑 Удалить сейчас", callback_data=f"delete_{droplet_id}")]
                        ])
                    )
                    logger.info(f"Уведомление отправлено пользователю {creator_id} о предстоящем удалении инстанса '{name}'.")
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения пользователю {creator_id}: {e}")

            elif time_left <= 0:  # Удаление, если время истекло
                logger.info(f"Инстанс '{name}' с ID {droplet_id} должен быть удалён. Запускаем удаление...")
                delete_result = delete_droplet(DIGITALOCEAN_TOKEN, droplet_id)

                if delete_result["success"]:
                    delete_instance(droplet_id)  # Удаляем запись из базы данных
                    logger.info(f"✅ Инстанс '{name}' удалён, так как срок действия истёк.")
                else:
                    logger.error(f"❌ Ошибка при удалении инстанса '{name}': {delete_result['message']}")

        except Exception as e:
            logger.error(f"Ошибка при обработке инстанса {instance}: {e}")
  

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик исключений."""
    logger.error(msg="Ошибка во время обработки обновления:", exc_info=context.error)
    if isinstance(update, Update):
        await update.message.reply_text("Произошла ошибка. Пожалуйста, попробуйте ещё раз.")


def main():
    """Запуск бота."""
    logger.info("Запуск бота...")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_action))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(notify_and_check_instances, interval=43200)

    app.run_polling()

if __name__ == "__main__":
    main()


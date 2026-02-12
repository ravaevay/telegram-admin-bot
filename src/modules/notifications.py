import logging

from config import NOTIFICATION_CHANNEL_ID

logger = logging.getLogger(__name__)

try:
    from modules.create_test_instance import DROPLET_TYPES
except Exception:
    DROPLET_TYPES = {}


async def send_notification(
    bot, action, droplet_name, ip_address, droplet_type, expiration_date, creator_id, duration=None
):
    """Send notification to the channel about droplet events."""
    if not NOTIFICATION_CHANNEL_ID:
        return

    try:
        type_label = DROPLET_TYPES.get(droplet_type, droplet_type)

        if action == "created":
            text = (
                f"Новый инстанс создан\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Срок действия: {expiration_date}\n"
                f"Создатель: {creator_id}"
            )
        elif action == "extended":
            duration_text = f"\nПродлён на: {duration} дн." if duration else ""
            text = (
                f"Инстанс продлён\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Новый срок: {expiration_date}{duration_text}\n"
                f"Пользователь: {creator_id}"
            )
        elif action == "deleted":
            text = (
                f"Инстанс удалён\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Пользователь: {creator_id}"
            )
        elif action == "auto_deleted":
            text = (
                f"Инстанс автоматически удалён\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Создатель: {creator_id}"
            )
        else:
            text = f"Неизвестное действие: {action} для инстанса {droplet_name}"

        await bot.send_message(chat_id=NOTIFICATION_CHANNEL_ID, text=text)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

import logging

from config import NOTIFICATION_CHANNEL_ID

logger = logging.getLogger(__name__)

logger.info("NOTIFICATION_CHANNEL_ID = '%s'", NOTIFICATION_CHANNEL_ID)

try:
    from modules.create_test_instance import DROPLET_TYPES
except Exception:
    DROPLET_TYPES = {}


async def send_notification(
    bot,
    action,
    droplet_name,
    ip_address,
    droplet_type,
    expiration_date,
    creator_id,
    duration=None,
    creator_username=None,
    domain_name=None,
    price_monthly=None,
):
    """Send notification to the channel about droplet events."""
    if not NOTIFICATION_CHANNEL_ID:
        logger.debug("Уведомление пропущено: NOTIFICATION_CHANNEL_ID не задан")
        return

    try:
        display_name = creator_username or str(creator_id)
        type_label = DROPLET_TYPES.get(droplet_type, droplet_type)

        if action == "created":
            dns_line = f"DNS: {domain_name}\n" if domain_name else ""
            cost_line = f"Стоимость: ~${price_monthly}/мес\n" if price_monthly else ""
            text = (
                f"Новый инстанс создан\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"{dns_line}"
                f"Тип: {type_label}\n"
                f"{cost_line}"
                f"Срок действия: {expiration_date}\n"
                f"Создатель: {display_name}"
            )
        elif action == "extended":
            duration_text = f"\nПродлён на: {duration} дн." if duration else ""
            text = (
                f"Инстанс продлён\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Новый срок: {expiration_date}{duration_text}\n"
                f"Пользователь: {display_name}"
            )
        elif action == "deleted":
            text = (
                f"Инстанс удалён\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Пользователь: {display_name}"
            )
        elif action == "auto_deleted":
            text = (
                f"Инстанс автоматически удалён\n\n"
                f"Имя: {droplet_name}\n"
                f"IP: {ip_address}\n"
                f"Тип: {type_label}\n"
                f"Создатель: {display_name}"
            )
        else:
            text = f"Неизвестное действие: {action} для инстанса {droplet_name}"

        logger.info("Отправка уведомления: %s — %s", action, droplet_name)
        await bot.send_message(chat_id=NOTIFICATION_CHANNEL_ID, text=text)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

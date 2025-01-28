import logging
from config import AUTHORIZED_GROUPS

logger = logging.getLogger(__name__)

def is_authorized(user_id, module):
    """Проверка авторизации пользователя для модуля."""
    if user_id in AUTHORIZED_GROUPS.get(module, []):
        return True
    logger.warning(f"Пользователь {user_id} не авторизован для модуля {module}.")
    return False

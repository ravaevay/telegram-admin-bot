import logging
from config import AUTHORIZED_GROUPS, MM_AUTHORIZED_GROUPS, GLOBAL_ADMIN_USERS
from modules.database import get_user_permissions

logger = logging.getLogger(__name__)


def is_global_admin(user_id):
    """Проверка, является ли пользователь глобальным админом (задаётся через env)."""
    return user_id in GLOBAL_ADMIN_USERS


def is_authorized(user_id, module):
    """Проверка авторизации пользователя для модуля (env-список ∪ права из БД ∪ глобальный админ)."""
    if is_global_admin(user_id):
        return True
    if user_id in AUTHORIZED_GROUPS.get(module, []):
        return True
    if module in get_user_permissions(user_id, platform="telegram"):
        return True
    logger.warning(f"Пользователь {user_id} не авторизован для модуля {module}.")
    return False


def is_authorized_for_bot(user_id):
    """Проверка авторизации пользователя для работы с ботом."""
    if is_global_admin(user_id):
        return True
    if any(user_id in users for users in AUTHORIZED_GROUPS.values()):
        return True
    if get_user_permissions(user_id, platform="telegram"):
        return True
    logger.warning(f"Пользователь {user_id} не авторизован для работы с ботом.")
    return False


def mm_is_authorized(user_id, module):
    """Проверка авторизации Mattermost-пользователя для модуля."""
    if user_id in MM_AUTHORIZED_GROUPS.get(module, []):
        return True
    logger.warning(f"MM пользователь {user_id} не авторизован для модуля {module}.")
    return False


def mm_is_authorized_for_bot(user_id):
    """Проверка авторизации Mattermost-пользователя для работы с ботом."""
    if any(user_id in users for users in MM_AUTHORIZED_GROUPS.values()):
        return True
    logger.warning(f"MM пользователь {user_id} не авторизован для работы с ботом.")
    return False

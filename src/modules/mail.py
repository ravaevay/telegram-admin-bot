import paramiko
import logging
import random
import string
import os
from config import MAIL_DB_USER, MAIL_DB_PASSWORD, MAIL_DEFAULT_DOMAIN  # Импорт переменных из .env

logger = logging.getLogger(__name__)

def generate_password(length=10):
    """Генерация случайного пароля."""
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

def ensure_mailbox_format(mailbox_name):
    """Проверяет и приводит mailbox_name к формату mailbox_name@domain.com."""
    if "@" not in mailbox_name:
        mailbox_name = f"{mailbox_name}@{MAIL_DEFAULT_DOMAIN}"
    return mailbox_name

def execute_ssh_command(command, ssh_config):
    """Выполнение команды через SSH."""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            ssh_config["host"],
            port=ssh_config["port"],
            username=ssh_config["username"],
            key_filename=ssh_config["key_path"]
        )
        stdin, stdout, stderr = ssh.exec_command(command)
        result = stdout.read().decode('utf-8').strip()
        error = stderr.read().decode('utf-8').strip()
        ssh.close()
        return result, error
    except Exception as e:
        logger.error(f"Ошибка подключения через SSH: {e}")
        return None, str(e)

def create_mailbox(mailbox_name, password, ssh_config):
    """Создание почтового ящика."""
    mailbox_name = ensure_mailbox_format(mailbox_name)
    
    command = (
        f'sudo docker exec onlyoffice-mail-server python /usr/src/iRedMail/tools/scripts/create_mailboxes.py '
        f'-d "onlyoffice-mysql-server" -u "{MAIL_DB_USER}" -p "{MAIL_DB_PASSWORD}" '
        f'-dn "onlyoffice_mailserver" -mba "{mailbox_name}" -mbp "{password}"'
    )
    result, error = execute_ssh_command(command, ssh_config)

    if error:
        return {"success": False, "message": error}
    if f"User '{mailbox_name}' exist" in result:
        return {"success": False, "message": f"Ящик {mailbox_name} уже существует."}
    
    return {
        "success": True,
        "address": mailbox_name,
        "password": password,
        "message": "Почтовый ящик успешно создан."
    }

def reset_password(mailbox_name, new_password, ssh_config):
    """Сброс пароля почтового ящика."""
    mailbox_name = ensure_mailbox_format(mailbox_name)

    command = (
        f'sudo docker exec onlyoffice-mail-server python /usr/src/iRedMail/tools/scripts/change_passwords.py '
        f'-d "onlyoffice-mysql-server" -u "{MAIL_DB_USER}" -p "{MAIL_DB_PASSWORD}" '
        f'-dn "onlyoffice_mailserver" -mba "{mailbox_name}" -mbp "{new_password}"'
    )
    result, error = execute_ssh_command(command, ssh_config)

    if error:
        return {"success": False, "message": error}
    if "password has been changed" in result:
        return {
            "success": True,
            "address": mailbox_name,
            "new_password": new_password,
            "message": "Пароль успешно сброшен."
        }

    return {"success": False, "message": "Не удалось сбросить пароль."}

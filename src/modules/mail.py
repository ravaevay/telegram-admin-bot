import paramiko
import logging
import random
import string
import shlex
import re
import os
from config import MAIL_DB_USER, MAIL_DB_PASSWORD, MAIL_DEFAULT_DOMAIN  # Импорт переменных из .env

logger = logging.getLogger(__name__)

_MD_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')

def _escape_md(text):
    """Экранирование спецсимволов для Telegram MarkdownV2."""
    return _MD_ESCAPE_RE.sub(r'\\\1', str(text))

def generate_password(length=10):
    """Генерация случайного пароля."""
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

MAILBOX_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')

def validate_mailbox_name(mailbox_name):
    """Валидация имени почтового ящика. Возвращает (is_valid, error_message)."""
    local_part = mailbox_name.split("@")[0] if "@" in mailbox_name else mailbox_name
    if not local_part:
        return False, "Имя ящика не может быть пустым."
    if len(local_part) > 64:
        return False, "Имя ящика слишком длинное (максимум 64 символа)."
    if not MAILBOX_NAME_RE.match(local_part):
        return False, "Имя ящика содержит недопустимые символы. Допустимы: латинские буквы, цифры, точка, дефис, подчёркивание."
    return True, ""

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
    valid, error = validate_mailbox_name(mailbox_name)
    if not valid:
        return {"success": False, "message": error}

    mailbox_name = ensure_mailbox_format(mailbox_name)

    command = (
        f'sudo docker exec onlyoffice-mail-server python /usr/src/iRedMail/tools/scripts/create_mailboxes.py '
        f'-d "onlyoffice-mysql-server" -u {shlex.quote(MAIL_DB_USER)} -p {shlex.quote(MAIL_DB_PASSWORD)} '
        f'-dn "onlyoffice_mailserver" -mba {shlex.quote(mailbox_name)} -mbp {shlex.quote(password)}'
    )
    result, error = execute_ssh_command(command, ssh_config)

    if error:
        return {"success": False, "message": error}
    if f"User '{mailbox_name}' exist" in result:
        return {"success": False, "message": f"Ящик {mailbox_name} уже существует."}
    
    msg = (
        f"*Mailbox successfully created\\!*\n\n"
        f"*Credentials:*\n"
        f"Email: `{_escape_md(mailbox_name)}`\n"
        f"Password: `{_escape_md(password)}`\n\n"
        f"*Connection Settings:*\n"
        f"IMAP/SMTP Server: `mx1\\.onlyoffice\\.com`\n"
        f"IMAP Port: `143` \\(STARTTLS\\)\n"
        f"SMTP Port: `587` \\(STARTTLS\\)\n"
        f"Auth Method: Simple password"
    )

    return {
        "success": True,
        "address": mailbox_name,
        "password": password,
        "message": msg,
    }

def reset_password(mailbox_name, new_password, ssh_config):
    """Сброс пароля почтового ящика."""
    valid, error = validate_mailbox_name(mailbox_name)
    if not valid:
        return {"success": False, "message": error}

    mailbox_name = ensure_mailbox_format(mailbox_name)

    command = (
        f'sudo docker exec onlyoffice-mail-server python /usr/src/iRedMail/tools/scripts/change_passwords.py '
        f'-d "onlyoffice-mysql-server" -u {shlex.quote(MAIL_DB_USER)} -p {shlex.quote(MAIL_DB_PASSWORD)} '
        f'-dn "onlyoffice_mailserver" -mba {shlex.quote(mailbox_name)} -mbp {shlex.quote(new_password)}'
    )
    result, error = execute_ssh_command(command, ssh_config)

    if error:
        return {"success": False, "message": error}
    if "password has been changed" in result:
        msg = (
            f"*Password successfully reset\\!*\n\n"
            f"Email: `{_escape_md(mailbox_name)}`\n"
            f"New password: `{_escape_md(new_password)}`"
        )
        return {
            "success": True,
            "address": mailbox_name,
            "new_password": new_password,
            "message": msg,
        }

    return {"success": False, "message": "Не удалось сбросить пароль."}

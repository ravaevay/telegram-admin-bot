from dotenv import load_dotenv
import os

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

SSH_CONFIG = {
    "host": os.getenv("SSH_HOST"),
    "port": int(os.getenv("SSH_PORT", 22)),
    "username": os.getenv("SSH_USERNAME"),
    "key_path": os.getenv("SSH_KEY_PATH"),
}

DIGITALOCEAN_TOKEN = os.getenv("DIGITALOCEAN_TOKEN")

AUTHORIZED_GROUPS = {
    "mail": list(map(int, os.getenv("AUTHORIZED_MAIL_USERS", "").split(","))),
    "droplet": list(map(int, os.getenv("AUTHORIZED_DROPLET_USERS", "").split(","))),
}

MAIL_DEFAULT_DOMAIN = os.getenv("MAIL_DEFAULT_DOMAIN")
MAIL_DB_USER = os.getenv("MAIL_DB_USER")
MAIL_DB_PASSWORD = os.getenv("MAIL_DB_PASSWORD")
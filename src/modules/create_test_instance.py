import asyncio
import logging
import re

import httpx

from modules.database import save_instance, delete_instance
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL = "https://api.digitalocean.com/v2/"

DROPLET_TYPES = {
    "s-2vcpu-2gb": "2GB-2vCPU-60GB",
    "s-2vcpu-4gb": "4GB-2vCPU-80GB",
    "s-4vcpu-8gb": "8GB-4vCPU-160GB",
    "s-8vcpu-16gb": "16GB-8vCPU-320GB",
}

IP_POLL_ATTEMPTS = 10
IP_POLL_INTERVAL = 5  # seconds

_MD_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_md(text):
    """Экранирование спецсимволов для Telegram MarkdownV2."""
    return _MD_ESCAPE_RE.sub(r"\\\1", str(text))


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


async def get_ssh_keys(token):
    """Получить список SSH-ключей из DigitalOcean."""
    try:
        keys = []
        url = BASE_URL + "account/keys?per_page=200"
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            while url:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                keys.extend(data.get("ssh_keys", []))
                url = data.get("links", {}).get("pages", {}).get("next")
        return {"success": True, "keys": keys}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении SSH-ключей: {e}")
        return {"success": False, "message": str(e)}


async def get_images(token):
    """Получить список доступных образов из DigitalOcean."""
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await client.get(BASE_URL + "images?type=distribution")
            response.raise_for_status()

        images = response.json().get("images", [])
        sorted_images = sorted(images, key=lambda x: x["distribution"])
        return {"success": True, "images": sorted_images}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении образов: {e}")
        return {"success": False, "message": str(e)}


async def create_droplet(token, name, ssh_key_id, droplet_type, image, duration, creator_id):
    """Создаёт Droplet в DigitalOcean."""
    try:
        expiration_date = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "name": name,
            "region": "fra1",
            "size": droplet_type,
            "image": image,
            "ssh_keys": [ssh_key_id],
            "backups": False,
            "ipv6": True,
            "monitoring": True,
        }

        headers = {**_auth_headers(token), "Content-Type": "application/json"}

        async with httpx.AsyncClient(headers=headers) as client:
            # Создание инстанса
            response = await client.post(BASE_URL + "droplets", json=payload)
            response.raise_for_status()

            droplet = response.json().get("droplet", {})
            droplet_id = droplet.get("id")
            droplet_name = droplet.get("name")

            # Ожидание настройки IP (async — не блокирует event loop)
            ip_address = None
            for _ in range(IP_POLL_ATTEMPTS):
                response = await client.get(BASE_URL + f"droplets/{droplet_id}")
                response.raise_for_status()
                networks = response.json().get("droplet", {}).get("networks", {}).get("v4", [])
                if networks:
                    ip_address = networks[0].get("ip_address")
                    break
                await asyncio.sleep(IP_POLL_INTERVAL)

        if not ip_address:
            ip_address = "Не удалось получить IP-адрес"

        # Сохраняем данные в БД
        save_instance(droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id)
        logger.info(f"Инстанс {name} создан. ID: {droplet_id}, IP: {ip_address}, срок действия до {expiration_date}")

        droplet_type_label = DROPLET_TYPES.get(droplet_type, droplet_type)
        msg = (
            f"*Instance successfully created\\!*\n\n"
            f"Name: `{_escape_md(droplet_name)}`\n"
            f"Connect: `root@{_escape_md(ip_address)}`\n"
            f"Type: `{_escape_md(droplet_type_label)}`\n"
            f"Expires: `{_escape_md(expiration_date)}`"
        )

        return {
            "success": True,
            "droplet_name": droplet_name,
            "ip_address": ip_address,
            "expiration_date": expiration_date,
            "message": msg,
        }

    except httpx.HTTPError as e:
        logger.error(f"Ошибка при создании Droplet: {e}")
        return {"success": False, "message": str(e)}


async def delete_droplet(token, droplet_id):
    """Удаляет Droplet из DigitalOcean и запись из базы данных."""
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await client.delete(f"{BASE_URL}droplets/{droplet_id}")
            response.raise_for_status()

        # Удаление из базы данных
        delete_instance(droplet_id)
        logger.info(f"Инстанс ID {droplet_id} успешно удалён из DigitalOcean и базы данных.")

        return {"success": True}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при удалении Droplet ID {droplet_id}: {e}")
        return {"success": False, "message": str(e)}

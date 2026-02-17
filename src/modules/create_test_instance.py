import asyncio
import logging
import re
import time

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

_size_cache = {"data": None, "timestamp": 0}
_SIZE_CACHE_TTL = 3600

_MD_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_md(text):
    """Экранирование спецсимволов для Telegram MarkdownV2."""
    return _MD_ESCAPE_RE.sub(r"\\\1", str(text))


_TAG_CLEAN_RE = re.compile(r"[^a-zA-Z0-9_:.\-]")


def _sanitize_tag(raw: str) -> str:
    """Очистка строки для использования как тег DigitalOcean."""
    tag = raw.lstrip("@")
    tag = _TAG_CLEAN_RE.sub("", tag)
    return tag[:255] if tag else "unknown"


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


async def get_domains(token):
    """Получить список доменов из DigitalOcean."""
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await client.get(BASE_URL + "domains")
            response.raise_for_status()

        domains = [d["name"] for d in response.json().get("domains", [])]
        return {"success": True, "domains": domains}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении доменов: {e}")
        return {"success": False, "message": str(e)}


async def create_dns_record(token, domain, subdomain, ip_address):
    """Создаёт A-запись DNS в DigitalOcean."""
    try:
        payload = {
            "type": "A",
            "name": subdomain,
            "data": ip_address,
            "ttl": 3600,
        }
        headers = {**_auth_headers(token), "Content-Type": "application/json"}

        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.post(BASE_URL + f"domains/{domain}/records", json=payload)
            response.raise_for_status()

        record = response.json().get("domain_record", {})
        record_id = record.get("id")
        fqdn = f"{subdomain}.{domain}"
        return {"success": True, "record_id": record_id, "fqdn": fqdn}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при создании DNS-записи: {e}")
        return {"success": False, "message": str(e)}


async def delete_dns_record(token, domain, record_id):
    """Удаляет DNS-запись из DigitalOcean."""
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await client.delete(BASE_URL + f"domains/{domain}/records/{record_id}")
            response.raise_for_status()

        return {"success": True}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при удалении DNS-записи {record_id}: {e}")
        return {"success": False, "message": str(e)}


async def get_sizes(token):
    """Получить список размеров Droplet из DigitalOcean (с кэшированием)."""
    global _size_cache

    if _size_cache["data"] is not None and (time.time() - _size_cache["timestamp"]) < _SIZE_CACHE_TTL:
        return _size_cache["data"]

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await client.get(BASE_URL + "sizes?per_page=200")
            response.raise_for_status()

        sizes = {}
        for s in response.json().get("sizes", []):
            sizes[s["slug"]] = {
                "price_monthly": s["price_monthly"],
                "price_hourly": s["price_hourly"],
            }

        _size_cache["data"] = sizes
        _size_cache["timestamp"] = time.time()
        return sizes
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении размеров: {e}")
        return {}


async def create_droplet(
    token,
    name,
    ssh_key_id,
    droplet_type,
    image,
    duration,
    creator_id,
    creator_username=None,
    price_monthly=None,
    creator_tag=None,
    price_hourly=None,
):
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
        if creator_tag:
            payload["tags"] = [f"creator:{_sanitize_tag(creator_tag)}"]

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
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_instance(
            droplet_id,
            name,
            ip_address,
            droplet_type,
            expiration_date,
            ssh_key_id,
            creator_id,
            creator_username,
            created_at=created_at,
            price_hourly=price_hourly,
        )
        logger.info(f"Инстанс {name} создан. ID: {droplet_id}, IP: {ip_address}, срок действия до {expiration_date}")

        droplet_type_label = DROPLET_TYPES.get(droplet_type, droplet_type)
        msg = (
            f"*Instance successfully created\\!*\n\n"
            f"Name: `{_escape_md(droplet_name)}`\n"
            f"Connect: `root@{_escape_md(ip_address)}`\n"
            f"Type: `{_escape_md(droplet_type_label)}`\n"
            f"Expires: `{_escape_md(expiration_date)}`"
        )
        if price_monthly is not None:
            msg += f"\nCost: \\~\\${_escape_md(str(price_monthly))}/month"

        return {
            "success": True,
            "droplet_id": droplet_id,
            "droplet_name": droplet_name,
            "ip_address": ip_address,
            "expiration_date": expiration_date,
            "message": msg,
        }

    except httpx.HTTPError as e:
        logger.error(f"Ошибка при создании Droplet: {e}")
        return {"success": False, "message": str(e)}


async def delete_droplet(token, droplet_id, dns_zone=None, dns_record_id=None):
    """Удаляет Droplet из DigitalOcean и запись из базы данных."""
    try:
        # Удаление DNS-записи (если указана)
        if dns_zone and dns_record_id:
            dns_result = await delete_dns_record(token, dns_zone, dns_record_id)
            if not dns_result["success"]:
                logger.warning(
                    f"Не удалось удалить DNS-запись {dns_record_id} для зоны {dns_zone}: {dns_result.get('message')}"
                )

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


async def create_snapshot(token, droplet_id, snapshot_name):
    """Создаёт снэпшот дроплета перед удалением."""
    try:
        payload = {"type": "snapshot", "name": snapshot_name}
        headers = {**_auth_headers(token), "Content-Type": "application/json"}

        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.post(f"{BASE_URL}droplets/{droplet_id}/actions", json=payload)
            response.raise_for_status()

        action = response.json().get("action", {})
        action_id = action.get("id")
        logger.info(f"Снэпшот '{snapshot_name}' запущен для дроплета {droplet_id}, action_id={action_id}")
        return {"success": True, "action_id": action_id}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка создания снэпшота для дроплета {droplet_id}: {e}")
        return {"success": False, "message": str(e)}


async def wait_for_action(token, action_id, timeout=600, interval=15):
    """Ожидание завершения действия DigitalOcean."""
    deadline = time.time() + timeout
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            while time.time() < deadline:
                response = await client.get(f"{BASE_URL}actions/{action_id}")
                response.raise_for_status()
                status = response.json().get("action", {}).get("status")
                if status == "completed":
                    logger.info(f"Действие {action_id} завершено успешно.")
                    return {"success": True}
                if status == "errored":
                    logger.error(f"Действие {action_id} завершилось с ошибкой.")
                    return {"success": False, "message": "Action errored"}
                await asyncio.sleep(interval)
        logger.warning(f"Действие {action_id} не завершилось за {timeout}с.")
        return {"success": False, "message": "Timeout"}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при ожидании действия {action_id}: {e}")
        return {"success": False, "message": str(e)}

import logging
from config import DIGITALOCEAN_TOKEN
from modules.database import save_instance, delete_instance
import requests
from datetime import datetime, timedelta
import time

logger = logging.getLogger(__name__)

BASE_URL = "https://api.digitalocean.com/v2/"
HEADERS = {
    "Authorization": f"Bearer {DIGITALOCEAN_TOKEN}",
    "Content-Type": "application/json"
}

DROPLET_TYPES = {
    "s-2vcpu-2gb": "2GB-2vCPU-60GB",
    "s-2vcpu-4gb": "4GB-2vCPU-80GB",
    "s-4vcpu-8gb": "8GB-4vCPU-160GB",
    "s-8vcpu-16gb": "16GB-8vCPU-320GB"
}

def get_ssh_keys(token):
    """Получить список SSH-ключей из DigitalOcean."""
    try:
        response = requests.get(BASE_URL + "account/keys", headers={
            "Authorization": f"Bearer {token}"
        })
        response.raise_for_status()
        keys = response.json().get("ssh_keys", [])
        return {"success": True, "keys": keys}
    except requests.RequestException as e:
        logger.error(f"Ошибка при получении SSH-ключей: {e}")
        return {"success": False, "message": str(e)}

def get_images(token):
    """Получить список доступных образов из DigitalOcean."""
    try:
        response = requests.get(BASE_URL + "images?type=distribution", headers={
            "Authorization": f"Bearer {token}"
        })
        response.raise_for_status()
        images = response.json().get("images", [])
        return {"success": True, "images": images}
    except requests.RequestException as e:
        logger.error(f"Ошибка при получении образов: {e}")
        return {"success": False, "message": str(e)}

def create_droplet(token, name, ssh_key_id, droplet_type, image, duration, creator_id):
    """Создаёт Droplet в DigitalOcean."""
    try:
        expiration_date = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "name": name,
            "region": "fra1",
            "size": droplet_type,
            "image": image,
            "ssh_keys": [ssh_key_id],
            "backups": False,
            "ipv6": True,
            "monitoring": True
        }

        # Создание инстанса
        response = requests.post(BASE_URL + "droplets", headers=HEADERS, json=data)
        response.raise_for_status()

        droplet = response.json().get("droplet", {})
        droplet_id = droplet.get("id")
        droplet_name = droplet.get("name")

        # Ожидание настройки IP
        ip_address = None
        for _ in range(10):  # Максимум 10 попыток ожидания
            response = requests.get(BASE_URL + f"droplets/{droplet_id}", headers=HEADERS)
            response.raise_for_status()
            networks = response.json().get("droplet", {}).get("networks", {}).get("v4", [])
            if networks:
                ip_address = networks[0].get("ip_address")
                break
            time.sleep(5)

        if not ip_address:
            ip_address = "Не удалось получить IP-адрес"

        # Сохраняем данные в БД
        save_instance(droplet_id, name, droplet_type, expiration_date, ssh_key_id, creator_id)
        logger.info(f"Инстанс {name} создан. ID: {droplet_id}, IP: {ip_address}, срок действия до {expiration_date}")

        return {"success": True, "droplet_name": droplet_name, "ip_address": ip_address, "expiration_date": expiration_date}

    except requests.RequestException as e:
        logger.error(f"Ошибка при создании Droplet: {e}")
        return {"success": False, "message": str(e)}

def delete_droplet(token, droplet_id):
    """Удаляет Droplet из DigitalOcean и запись из базы данных."""
    try:
        response = requests.delete(f"{BASE_URL}droplets/{droplet_id}", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        
        # Удаление из базы данных
        delete_instance(droplet_id)
        logger.info(f"Инстанс ID {droplet_id} успешно удалён из DigitalOcean и базы данных.")

        return {"success": True}
    except requests.RequestException as e:
        logger.error(f"Ошибка при удалении Droplet ID {droplet_id}: {e}")
        return {"success": False, "message": str(e)}

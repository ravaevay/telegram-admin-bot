import sqlite3
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DB_PATH = "instances.db"


def init_db():
    """Инициализация базы данных."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
        CREATE TABLE IF NOT EXISTS instances (
            droplet_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            droplet_type TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            ssh_key_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL
        )
        """)
        connection.commit()
    logger.info("База данных инициализирована.")


def save_instance(droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id):
    """Сохранение информации об инстансе в базу данных."""
    try:
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute(
                """
            INSERT INTO instances (droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id),
            )
            connection.commit()
        logger.info(f"Инстанс {name} (ID: {droplet_id}) сохранён в базе данных.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при сохранении инстанса {name} в базе данных: {e}")


def get_instance_by_id(droplet_id):
    """Получить инстанс по ID. Возвращает dict или None."""
    try:
        with sqlite3.connect(DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                "SELECT droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id "
                "FROM instances WHERE droplet_id = ?",
                (droplet_id,),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении инстанса ID {droplet_id}: {e}")
        return None


def get_expiring_instances():
    """Получить инстансы, срок действия которых истекает через 24 часа."""
    try:
        with sqlite3.connect(DB_PATH) as connection:
            cursor = connection.execute("""
            SELECT droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id
            FROM instances
            WHERE expiration_date <= datetime('now', 'localtime', '+1 day')
            """)
            return cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка инстансов с истекающим сроком действия: {e}")
        return []


def extend_instance_expiration(droplet_id, days):
    """Продлить срок действия инстанса в базе данных."""
    logger.info(f"Продление инстанса ID {droplet_id} на {days} дней")

    try:
        with sqlite3.connect(DB_PATH) as connection:
            cursor = connection.execute("SELECT expiration_date FROM instances WHERE droplet_id = ?", (droplet_id,))
            row = cursor.fetchone()
            if not row:
                logger.error(f"Инстанс ID {droplet_id} не найден в БД.")
                return None

            current_expiration = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            new_expiration = current_expiration + timedelta(days=days)
            new_expiration_str = new_expiration.strftime("%Y-%m-%d %H:%M:%S")

            connection.execute(
                "UPDATE instances SET expiration_date = ? WHERE droplet_id = ?", (new_expiration_str, droplet_id)
            )
            connection.commit()

            logger.info(f"Инстанс {droplet_id} продлен до {new_expiration_str}")
            return new_expiration_str

    except Exception as e:
        logger.error(f"Ошибка при продлении инстанса: {e}")
        return None


def delete_instance(droplet_id):
    """Удаляет запись об инстансе из базы данных."""
    try:
        with sqlite3.connect(DB_PATH) as connection:
            cursor = connection.execute("DELETE FROM instances WHERE droplet_id = ?", (droplet_id,))
            connection.commit()
            if cursor.rowcount > 0:
                logger.info(f"Запись о инстансе ID {droplet_id} успешно удалена из базы данных.")
                return True
            else:
                logger.warning(f"Запись о инстансе ID {droplet_id} не найдена в базе данных.")
                return False
    except sqlite3.Error as e:
        logger.error(f"Ошибка при удалении инстанса ID {droplet_id} из базы данных: {e}")
        return False

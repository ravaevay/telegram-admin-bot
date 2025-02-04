import sqlite3
import logging

logger = logging.getLogger(__name__)

DB_PATH = "instances.db"

def init_db():
    """Инициализация базы данных."""
    connection = sqlite3.connect(DB_PATH)
    cursor = connection.cursor()
    cursor.execute("""
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
    connection.close()
    logger.info("База данных инициализирована.")

def save_instance(droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id):
    """Сохранение информации об инстансе в базу данных."""
    try:
        connection = sqlite3.connect(DB_PATH)
        cursor = connection.cursor()
        cursor.execute("""
        INSERT INTO instances (droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id))
        connection.commit()
        logger.info(f"Инстанс {name} (ID: {droplet_id}) сохранён в базе данных.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при сохранении инстанса {name} в базе данных: {e}")
    finally:
        connection.close()

def get_expiring_instances():
    """Получить инстансы, срок действия которых истекает через 24 часа."""
    try:
        connection = sqlite3.connect(DB_PATH)
        cursor = connection.cursor()
        cursor.execute("""
        SELECT droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id
        FROM instances
        WHERE expiration_date <= datetime('now', '+1 day')
        """)
        instances = cursor.fetchall()
        return instances
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка инстансов с истекающим сроком действия: {e}")
        return []
    finally:
        connection.close()

def extend_instance_expiration(droplet_id, days):
    """Продлить срок действия инстанса."""
    try:
        connection = sqlite3.connect(DB_PATH)
        cursor = connection.cursor()
        cursor.execute("""
        UPDATE instances
        SET expiration_date = datetime(expiration_date, ? || ' days')
        WHERE droplet_id = ?
        """, (days, droplet_id))
        connection.commit()
        logger.info(f"Инстанс ID {droplet_id} продлён на {days} дней.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при продлении инстанса ID {droplet_id}: {e}")
    finally:
        connection.close()

def delete_instance(droplet_id):
    """Удаляет запись об инстансе из базы данных."""
    try:
        connection = sqlite3.connect(DB_PATH)
        cursor = connection.cursor()
        cursor.execute("DELETE FROM instances WHERE droplet_id = ?", (droplet_id,))
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
    finally:
        connection.close()

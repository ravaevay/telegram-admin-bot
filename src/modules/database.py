import sqlite3

def init_db():
    """Инициализация базы данных."""
    connection = sqlite3.connect("instances.db")
    cursor = connection.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS instances (
        droplet_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        droplet_type TEXT NOT NULL,
        expiration_date TEXT NOT NULL,
        ssh_key_id INTEGER NOT NULL,
        creator_id INTEGER NOT NULL
    )
    """)
    connection.commit()
    connection.close()

def save_instance(droplet_id, name, droplet_type, expiration_date, ssh_key_id, creator_id):
    """Сохранение информации об инстансе в базу данных."""
    connection = sqlite3.connect("instances.db")
    cursor = connection.cursor()
    cursor.execute("""
    INSERT INTO instances (droplet_id, name, droplet_type, expiration_date, ssh_key_id, creator_id)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (droplet_id, name, droplet_type, expiration_date, ssh_key_id, creator_id))
    connection.commit()
    connection.close()

def get_expiring_instances():
    """Получить инстансы, срок действия которых истекает через 24 часа."""
    connection = sqlite3.connect("instances.db")
    cursor = connection.cursor()
    cursor.execute("""
    SELECT droplet_id, name, droplet_type, expiration_date, ssh_key_id, creator_id
    FROM instances
    WHERE expiration_date <= datetime('now', '+1 day')
    """)
    instances = cursor.fetchall()
    connection.close()
    return instances

def extend_instance_expiration(droplet_id, days):
    """Продлить срок действия инстанса."""
    connection = sqlite3.connect("instances.db")
    cursor = connection.cursor()
    cursor.execute("""
    UPDATE instances
    SET expiration_date = datetime(expiration_date, ? || ' days')
    WHERE droplet_id = ?
    """, (days, droplet_id))
    connection.commit()
    connection.close()


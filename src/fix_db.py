import sqlite3

connection = sqlite3.connect("instances.db")
cursor = connection.cursor()

# Убедитесь, что таблица содержит колонку droplet_type
cursor.execute("""
ALTER TABLE instances ADD COLUMN droplet_type TEXT
""")
connection.commit()
connection.close()

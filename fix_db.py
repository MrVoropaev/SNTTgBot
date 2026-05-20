import sqlite3

conn = sqlite3.connect("database.db")
cursor = conn.cursor()

try:
    cursor.execute(
        "ALTER TABLE users ADD COLUMN plot TEXT"
    )

    print("COLUMN plot ADDED")

except Exception as e:
    print(e)

conn.commit()
conn.close()
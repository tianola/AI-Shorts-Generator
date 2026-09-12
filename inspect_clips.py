import sqlite3
import os

db_path = os.path.abspath(os.path.join("storage", "ai_shorts.db"))
print(f"Checking {db_path}")
try:
	conn = sqlite3.connect(db_path)
	cursor = conn.cursor()
	cursor.execute("SELECT start, end, id FROM clips")
	for row in cursor.fetchall():
		print(f"ID: {row[2]}, Start: {row[0]}, End: {row[1]}, Duration: {row[1] - row[0]}")
	conn.close()
except Exception as e:
	print(f"Error: {e}")

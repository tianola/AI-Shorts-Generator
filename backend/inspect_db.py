import sqlite3

con = sqlite3.connect(r"E:\01-AI-PROJECTS\ai-shorts-generator\storage\app.db")
print("--- jobs ---")
for row in con.execute("PRAGMA table_info(jobs)"):
	print(row)
print("--- clips ---")
try:
	for row in con.execute("PRAGMA table_info(clips)"):
		print(row)
except Exception as e:
	print("clips error:", e)
con.close()
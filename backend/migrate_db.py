import sqlite3

con = sqlite3.connect(r"E:\01-AI-PROJECTS\ai-shorts-generator\storage\app.db")
cur = con.cursor()
cols = [r[1] for r in cur.execute("PRAGMA table_info(jobs)")]
if "channel_id" not in cols:
	cur.execute("ALTER TABLE jobs ADD COLUMN channel_id VARCHAR")
	print("Added channel_id to jobs")
else:
	print("channel_id already exists")
con.commit()
con.close()
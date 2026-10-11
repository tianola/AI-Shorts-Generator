import sqlite3

con = sqlite3.connect(r"E:\01-AI-PROJECTS\ai-shorts-generator\storage\app.db")
cur = con.cursor()
cols = [r[1] for r in cur.execute("PRAGMA table_info(jobs)")]
if "channel_id" not in cols:
    cur.execute("ALTER TABLE jobs ADD COLUMN channel_id VARCHAR")
    print("Added channel_id to jobs")
else:
    print("channel_id already exists")
if "hook_style_list" not in cols:
    cur.execute("ALTER TABLE jobs ADD COLUMN hook_style_list VARCHAR")
    print("Added hook_style_list to jobs")
else:
    print("hook_style_list already exists")
if "is_zero_view_diagnostic" not in cols:
    cur.execute("ALTER TABLE jobs ADD COLUMN is_zero_view_diagnostic BOOLEAN DEFAULT 0")
    print("Added is_zero_view_diagnostic to jobs")
else:
    print("is_zero_view_diagnostic already exists")
con.commit()
con.close()
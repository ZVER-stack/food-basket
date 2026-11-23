import json
from pathlib import Path
import sqlite3
from contextlib import closing


class Storage:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            db.commit()

    def get(self, user_id):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute('SELECT payload FROM sessions WHERE user_id=?', (int(user_id),)).fetchone()
        return json.loads(row[0]) if row else {'mode': 'WAIT_ADDRESS', 'zone_id': None, 'history': []}

    def save(self, user_id, payload):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute('INSERT INTO sessions VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload',
                       (int(user_id), json.dumps(payload, ensure_ascii=False)))
            db.commit()

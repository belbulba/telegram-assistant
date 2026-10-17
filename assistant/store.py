import json
import sqlite3
import time
import uuid

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
        ''')
        self.db.commit()

    def history(self):
        rows = self.db.execute('SELECT body FROM history ORDER BY id DESC LIMIT 20').fetchall()
        return [item for row in reversed(rows) for item in json.loads(row[0])]

    def add(self, items):
        with self.db:
            self.db.execute('INSERT INTO history(body) VALUES (?)', (json.dumps(items, ensure_ascii=False),))

    def clear(self):
        with self.db:
            self.db.execute('DELETE FROM history')
            self.db.execute('DELETE FROM pending')

    def stage(self, body):
        key = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO pending VALUES(?,?,?)', (key, json.dumps(body), time.time()))
        return key

    def take(self, key):
        with self.db:
            row = self.db.execute('SELECT body,created FROM pending WHERE id=?', (key,)).fetchone()
            self.db.execute('DELETE FROM pending WHERE id=?', (key,))
        if not row or time.time() - row[1] > 600:
            return None
        return json.loads(row[0])

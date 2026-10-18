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
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS otp_deliveries(id TEXT PRIMARY KEY, status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS reminders(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, due REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
            event_id TEXT, event_start REAL, retry_at REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL, delivered REAL);
        CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due);
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

    def setting(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, str(value)))
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def reserve_otp(self, message_id):
        with self.db:
            self.db.execute('DELETE FROM otp_deliveries WHERE created < ?', (time.time() - 7 * 86400,))
            return self.db.execute('INSERT OR IGNORE INTO otp_deliveries VALUES(?,?,?)',
                                   (message_id, 'sending', time.time())).rowcount == 1

    def finish_otp(self, message_id, status):
        with self.db:
            self.db.execute('UPDATE otp_deliveries SET status=? WHERE id=?', (status, message_id))

    def take(self, key):
        with self.db:
            row = self.db.execute('SELECT body,created FROM pending WHERE id=?', (key,)).fetchone()
            self.db.execute('DELETE FROM pending WHERE id=?', (key,))
        if not row or time.time() - row[1] > 600:
            return None
        return json.loads(row[0])

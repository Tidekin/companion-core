"""Encrypted SQLite cache and immutable request queue, scoped to one HTTPS origin."""

import hashlib
import json
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion_chat_client.protection import protect, unprotect


class Cache:
    def __init__(self, directory, origin):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.origin = origin
        self.db = sqlite3.connect(directory / (hashlib.sha256(origin.encode()).hexdigest() + '.db'))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (
                event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS queue (
                event_id TEXT PRIMARY KEY, payload BLOB NOT NULL,
                admitted INTEGER NOT NULL DEFAULT 0, error TEXT);
        ''')

    def key(self):
        path = self.directory / 'device.dpapi'
        if not path.exists():
            key = Ed25519PrivateKey.generate()
            try:
                with path.open('xb') as stream:
                    stream.write(protect(key.private_bytes_raw()))
            except FileExistsError:
                pass
        return Ed25519PrivateKey.from_private_bytes(unprotect(path.read_bytes()))

    @staticmethod
    def pack(value):
        return protect(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())

    @staticmethod
    def unpack(value):
        return json.loads(unprotect(value))

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return self.unpack(row[0]) if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, self.pack(value)))

    def enqueue(self, packet):
        if self.db.execute('SELECT count(*) FROM queue').fetchone()[0] >= 100:
            raise ValueError('待处理消息已满，请先同步。')
        with self.db:
            self.db.execute('INSERT INTO queue(event_id,payload) VALUES (?,?)',
                            (packet['capture']['event_id'], self.pack(packet)))

    def pending(self):
        return [{'event_id': event_id, 'packet': self.unpack(payload),
                 'admitted': bool(admitted), 'error': error}
                for event_id, payload, admitted, error in self.db.execute(
                    'SELECT event_id,payload,admitted,error FROM queue ORDER BY rowid')]

    def acknowledge(self, event_id):
        with self.db:
            self.db.execute('UPDATE queue SET admitted=1,error=NULL WHERE event_id=?', (event_id,))

    def reject(self, event_id, code):
        with self.db:
            self.db.execute('UPDATE queue SET error=? WHERE event_id=?', (code, event_id))

    def merge(self, items):
        with self.db:
            for item in items:
                self.db.execute('INSERT OR REPLACE INTO turns VALUES (?,?,?)',
                                (item['event_id'], item['sequence'], self.pack(item)))
                # The original task is terminal; no request is recreated from history.
                if item['redacted'] or item['state'] in {'SUCCEEDED', 'FAILED', 'CANCELLED'}:
                    self.db.execute('DELETE FROM queue WHERE event_id=?', (item['event_id'],))

    def reconcile(self, event_ids):
        with self.db:
            for event_id, in self.db.execute('SELECT event_id FROM turns').fetchall():
                if event_id not in event_ids:
                    self.db.execute('DELETE FROM turns WHERE event_id=?', (event_id,))

    def turns(self):
        return [self.unpack(row[0]) for row in self.db.execute(
            'SELECT payload FROM turns ORDER BY sequence')]

    def discard_rejected(self):
        with self.db:
            self.db.execute('DELETE FROM queue WHERE error IS NOT NULL AND admitted=0')

    def close(self):
        self.db.close()

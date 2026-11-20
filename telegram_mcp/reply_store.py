"""Durable, independent Telegram reply subscriptions. No Telegram/client imports."""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


MAX_TTL = 7 * 86400


def default_db_path():
    override = os.getenv('TELEGRAM_REPLY_DB')
    if override:
        return Path(override).expanduser()
    return Path(os.getenv('XDG_STATE_HOME', Path.home() / '.local/state')) / 'telegram-mcp/replies.db'


class ReplyStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('Reply database must not be a symlink')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, sender_id INTEGER, topic_id INTEGER,
                    reply_to INTEGER, received_at REAL NOT NULL,
                    UNIQUE(account,chat_id,message_id));
                CREATE INDEX IF NOT EXISTS event_chat ON events(account,chat_id,message_id);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    anchor INTEGER NOT NULL, sender_id INTEGER, topic_id INTEGER,
                    client TEXT NOT NULL, session_id TEXT NOT NULL, endpoint TEXT NOT NULL,
                    mode TEXT NOT NULL, settle REAL NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active');
                CREATE INDEX IF NOT EXISTS sub_target ON subscriptions(client,session_id,status);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                    status TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS assignments (
                    subscription_id TEXT NOT NULL, event_id INTEGER NOT NULL,
                    delivery_id TEXT NOT NULL, PRIMARY KEY(subscription_id,event_id));
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def observe(self, *, account, chat_id, message_id, sender_id=None, topic_id=None,
                reply_to=None, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('''INSERT OR IGNORE INTO events
                (account,chat_id,message_id,sender_id,topic_id,reply_to,received_at)
                VALUES (?,?,?,?,?,?,?)''',
                       (account, chat_id, message_id, sender_id, topic_id, reply_to, now))

    def subscribe(self, *, account, chat_id, after_message_id, client, session_id,
                  endpoint='', sender_id=None, topic_id=None, mode='until_task_complete',
                  ttl_seconds=48 * 3600, settle_seconds=6, now=None):
        if client not in ('codex', 'claude'):
            raise ValueError('client must be codex or claude')
        if not session_id or len(session_id) > 200:
            raise ValueError('Explicit session_id is required')
        if not account or not isinstance(chat_id, int) or chat_id == 0:
            raise ValueError('Explicit account and numeric chat_id are required')
        if not isinstance(after_message_id, int) or after_message_id < 0:
            raise ValueError('after_message_id must be nonnegative')
        if not 0 < ttl_seconds <= MAX_TTL or not 0 <= settle_seconds <= 60:
            raise ValueError('TTL must be 1s..7d and settle_seconds 0..60')
        if mode not in ('first_reply', 'until_task_complete'):
            raise ValueError('Unsupported subscription mode')
        if client == 'codex' and not endpoint:
            raise ValueError('A verified Codex Unix socket endpoint is required')
        now = time.time() if now is None else now
        with self.connection() as db:
            existing = db.execute('''SELECT * FROM subscriptions WHERE account=? AND chat_id=?
                AND anchor=? AND client=? AND session_id=? AND sender_id IS ? AND topic_id IS ?
                AND mode=? AND status='active' AND expires_at>?''',
                                  (account, chat_id, after_message_id, client, session_id,
                                   sender_id, topic_id, mode, now)).fetchone()
            if existing:
                return dict(existing)
            sid = str(uuid.uuid4())
            db.execute('''INSERT INTO subscriptions
                (id,account,chat_id,anchor,sender_id,topic_id,client,session_id,endpoint,mode,
                 settle,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (sid, account, chat_id, after_message_id, sender_id, topic_id, client,
                        session_id, endpoint, mode, settle_seconds, now, now + ttl_seconds))
            return dict(db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone())

    def list_subscriptions(self, *, session_id=None, client=None):
        with self.connection() as db:
            rows = db.execute('''SELECT * FROM subscriptions WHERE (? IS NULL OR session_id=?)
                AND (? IS NULL OR client=?) ORDER BY created_at''',
                              (session_id, session_id, client, client)).fetchall()
            return [dict(r) for r in rows]

    def cancel(self, subscription_id, session_id):
        with self.connection() as db:
            self._owned_sub(db, subscription_id, session_id)
            db.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (subscription_id,))
            db.execute("UPDATE deliveries SET status='cancelled' WHERE subscription_id=? AND status!='acked'",
                       (subscription_id,))
        return {'id': subscription_id, 'status': 'cancelled'}

    @staticmethod
    def _owned_sub(db, sid, session_id):
        sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
        if sub is None or sub['session_id'] != session_id:
            raise ValueError('Subscription does not belong to this session')
        return sub

    @staticmethod
    def _batch(row):
        data = json.loads(row['payload'])
        data.update({k: row[k] for k in ('id', 'subscription_id', 'status', 'attempts', 'last_error')})
        return data

    def prepare(self, *, client=None, session_id=None, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at<=?", (now,))
            subs = db.execute('''SELECT * FROM subscriptions WHERE status='active'
                AND (? IS NULL OR client=?) AND (? IS NULL OR session_id=?)''',
                              (client, client, session_id, session_id)).fetchall()
            for sub in subs:
                if db.execute("SELECT 1 FROM deliveries WHERE subscription_id=? AND status IN ('ready','sending','sent')",
                              (sub['id'],)).fetchone():
                    continue
                rows = db.execute('''SELECT e.* FROM events e WHERE account=? AND chat_id=?
                    AND message_id>? AND (? IS NULL OR sender_id=?)
                    AND (? IS NULL OR topic_id=?) AND NOT EXISTS (
                        SELECT 1 FROM assignments a WHERE a.subscription_id=? AND a.event_id=e.id)
                    ORDER BY e.message_id''',
                                  (sub['account'], sub['chat_id'], sub['anchor'], sub['sender_id'],
                                   sub['sender_id'], sub['topic_id'], sub['topic_id'], sub['id'])).fetchall()
                if not rows or max(r['received_at'] for r in rows) + sub['settle'] > now:
                    continue
                rows = rows[:100]
                did = str(uuid.uuid4())
                payload = {k: sub[k] for k in ('account', 'chat_id', 'client', 'session_id', 'endpoint', 'topic_id')}
                payload.update(anchor_message_id=sub['anchor'], message_ids=[r['message_id'] for r in rows])
                db.execute('''INSERT INTO deliveries(id,subscription_id,status,payload,created_at,updated_at)
                    VALUES (?,?,'ready',?,?,?)''', (did, sub['id'], json.dumps(payload), now, now))
                db.executemany('INSERT INTO assignments VALUES (?,?,?)',
                               [(sub['id'], r['id'], did) for r in rows])
            return [self._batch(r) for r in db.execute('''SELECT d.* FROM deliveries d JOIN subscriptions s
                ON s.id=d.subscription_id WHERE d.status='ready' AND d.next_attempt<=?
                AND s.status='active' AND (? IS NULL OR s.client=?)
                AND (? IS NULL OR s.session_id=?) ORDER BY d.created_at''',
                                                      (now, client, client, session_id, session_id))]

    def deliveries(self, *, status=None, client=None, session_id=None):
        with self.connection() as db:
            return [self._batch(r) for r in db.execute('''SELECT d.* FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription_id
                WHERE (? IS NULL OR d.status=?) AND (? IS NULL OR s.client=?)
                AND (? IS NULL OR s.session_id=?) AND s.status='active'
                ORDER BY d.created_at''', (status, status, client, client, session_id, session_id))]

    def claim(self, delivery_id, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            return db.execute('''UPDATE deliveries SET status='sending', attempts=attempts+1,
                updated_at=? WHERE id=? AND status='ready' AND next_attempt<=?
                AND EXISTS (SELECT 1 FROM subscriptions WHERE id=subscription_id
                            AND status='active' AND expires_at>?)''',
                              (now, delivery_id, now, now)).rowcount == 1

    def mark_sent(self, delivery_id, *, now=None):
        with self.connection() as db:
            db.execute("UPDATE deliveries SET status='sent', updated_at=?, last_error='' WHERE id=? AND status='sending'",
                       (time.time() if now is None else now, delivery_id))

    def fail(self, delivery_id, error, *, retryable, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute('SELECT attempts FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            delay = min(300, 2 ** min(row['attempts'] + 2, 8))
            db.execute('''UPDATE deliveries SET status=?,updated_at=?,next_attempt=?,last_error=?
                WHERE id=? AND status IN ('ready','sending')''',
                       ('ready' if retryable else 'sending', now, now + delay, str(error)[:500], delivery_id))

    def acknowledge(self, delivery_id, session_id, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown delivery')
            sub = self._owned_sub(db, row['subscription_id'], session_id)
            if row['status'] == 'cancelled':
                raise ValueError('Delivery cancelled')
            db.execute("UPDATE deliveries SET status='acked',updated_at=? WHERE id=?", (now, delivery_id))
            if sub['mode'] == 'first_reply':
                db.execute("UPDATE subscriptions SET status='completed' WHERE id=?", (sub['id'],))
        return {'id': delivery_id, 'status': 'acked'}

    def prune(self, *, now=None):
        """Bound metadata retention; active subscriptions never span more than seven days."""
        cutoff = (time.time() if now is None else now) - MAX_TTL
        with self.connection() as db:
            db.execute('DELETE FROM events WHERE received_at<? AND NOT EXISTS '
                       '(SELECT 1 FROM assignments WHERE event_id=events.id)', (cutoff,))

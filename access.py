"""Invitation-only accounts, password hashing and persistent login throttling."""

import hashlib
import re
import secrets
import sqlite3
import time
import unicodedata

from werkzeug.security import check_password_hash, generate_password_hash

from store import DomainError

ROLES = {'coordinator': 'Координатор', 'warehouse': 'Сотрудник склада', 'observer': 'Наблюдатель'}
PERMISSIONS = {
    'receipt': {'coordinator', 'warehouse'},
    'request': {'coordinator'},
    'reserve': {'coordinator'},
    'issue': {'coordinator', 'warehouse'},
    'cancel': {'coordinator'},
    'catalog': {'coordinator'},
    'accounts': {'coordinator'},
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL, role TEXT NOT NULL
        CHECK(role IN ('coordinator','warehouse','observer')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')));
CREATE TABLE IF NOT EXISTS invitations (
    digest TEXT PRIMARY KEY, role TEXT NOT NULL, expires INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1)));
CREATE TABLE IF NOT EXISTS login_limits (
    bucket TEXT PRIMARY KEY, failures INTEGER NOT NULL, since INTEGER NOT NULL);
'''


class Access:
    def __init__(self, store):
        self.store = store
        self.dummy_hash = generate_password_hash(secrets.token_urlsafe(24))
        with store.connection() as db:
            db.executescript(SCHEMA)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def normalize_username(username):
        return unicodedata.normalize('NFKC', username.strip()).casefold()

    def user(self, user_id):
        rows = self.store.query('SELECT id,username,role FROM users WHERE id=?', (user_id,))
        return rows[0] if rows else None

    def invite(self, role, actor=None, bootstrap=False):
        if role not in ROLES:
            raise DomainError('Выберите существующую роль.')
        token = secrets.token_urlsafe(32)
        with self.store.connection(write=True) as db:
            if bootstrap and db.execute('SELECT 1 FROM users').fetchone():
                raise DomainError('Первый аккаунт уже создан. Приглашения создаёт координатор на сайте.')
            if bootstrap:
                # Reissuing an initial invitation invalidates previous unused ones.
                db.execute('UPDATE invitations SET used=1 WHERE used=0')
            db.execute('INSERT INTO invitations(digest,role,expires) VALUES(?,?,?)',
                       (self.digest(token), role, int(time.time()) + 86400))
            self.store._event(db, 'Приглашение сотрудника', {'роль': ROLES[role], 'оператор': actor or 'владелец сервера'})
        return token

    def invitation(self, token):
        if not token or len(token) > 100:
            return None
        rows = self.store.query('SELECT role FROM invitations WHERE digest=? AND used=0 AND expires>?',
                                (self.digest(token), int(time.time())))
        return rows[0] if rows else None

    def register(self, token, username, password):
        username = self.normalize_username(username)
        if not re.fullmatch(r'[a-zа-яё0-9_.-]{3,32}', username):
            raise DomainError('Логин: 3–32 русские или латинские буквы, цифры, точка, дефис или подчёркивание. Без пробелов.')
        if not 12 <= len(password) <= 128:
            raise DomainError('Пароль должен содержать от 12 до 128 символов.')
        password_hash = generate_password_hash(password)
        with self.store.connection(write=True) as db:
            invite = db.execute('SELECT * FROM invitations WHERE digest=? AND used=0 AND expires>?',
                                (self.digest(token), int(time.time()))).fetchone()
            if not invite:
                raise DomainError('Приглашение использовано или срок действия истёк.')
            try:
                user_id = db.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',
                                     (username, password_hash, invite['role'])).lastrowid
            except sqlite3.IntegrityError:
                raise DomainError('Этот логин занят. Выберите другой.') from None
            db.execute('UPDATE invitations SET used=1 WHERE digest=?', (invite['digest'],))
            self.store._event(db, 'Создание аккаунта', {'логин': username, 'роль': ROLES[invite['role']]})
        return self.user(user_id)

    def login(self, username, password, peer):
        # Bucket by network peer without retaining its address in the database.
        bucket = self.digest(self.store.session_secret() + str(peer))
        now = int(time.time())
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM login_limits WHERE since<?', (now-900,))
            limit = db.execute('SELECT failures FROM login_limits WHERE bucket=?', (bucket,)).fetchone()
            if limit and limit['failures'] >= 8:
                raise DomainError('Слишком много попыток. Повторите вход через 15 минут.')
            row = db.execute('SELECT * FROM users WHERE username=?', (self.normalize_username(username),)).fetchone()
            valid_hash = check_password_hash(row['password_hash'] if row else self.dummy_hash,
                                             password if len(password) <= 128 else '')
            valid = bool(row and valid_hash and len(password) <= 128)
            if valid:
                db.execute('DELETE FROM login_limits WHERE bucket=?', (bucket,))
                return dict(id=row['id'], username=row['username'], role=row['role'])
            db.execute('INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(bucket) '
                       'DO UPDATE SET failures=failures+1', (bucket, now))
        return None


def can(user, action):
    return bool(user and user['role'] in PERMISSIONS.get(action, set()))

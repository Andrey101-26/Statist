"""SQLite storage and atomic inventory operations. Quantities are whole units."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class DomainError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    destination TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','reserved','issued','cancelled')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')));
CREATE TABLE IF NOT EXISTS request_lines (
    request_id INTEGER NOT NULL REFERENCES requests(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    PRIMARY KEY(request_id, product_id));
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    source TEXT NOT NULL, created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')));
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity != 0),
    receipt_id INTEGER REFERENCES receipts(id),
    request_id INTEGER REFERENCES requests(id),
    CHECK((receipt_id IS NOT NULL AND request_id IS NULL AND quantity > 0)
       OR (receipt_id IS NULL AND request_id IS NOT NULL AND quantity < 0)),
    UNIQUE(receipt_id, product_id), UNIQUE(request_id, product_id));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, request_id INTEGER REFERENCES requests(id),
    action TEXT NOT NULL, details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')));
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS movements_stock ON movements(warehouse_id, product_id);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            # Stable read snapshots; serialize writers before stock checks.
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def query(self, sql, args=()):
        with self.connection() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def catalog(self, table):
        if table not in ('products', 'warehouses'):
            raise ValueError('Unknown catalog')
        return self.query(f'SELECT * FROM {table} ORDER BY name')

    @staticmethod
    def _text(value, label, limit=160):
        value = str(value).strip()
        if not value or len(value) > limit:
            raise DomainError(f'{label}: введите от 1 до {limit} символов.')
        return value

    @staticmethod
    def _lines(db, lines):
        if not lines:
            raise DomainError('Укажите количество хотя бы одного товара.')
        checked = {}
        for product, quantity in lines.items():
            if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
                raise DomainError('Количество должно быть целым числом от 1 до 1 000 000.')
            if not db.execute('SELECT 1 FROM products WHERE id=?', (product,)).fetchone():
                raise DomainError('Товар отсутствует в справочнике.')
            checked[product] = quantity
        return checked

    @staticmethod
    def _warehouse(db, warehouse):
        if not db.execute('SELECT 1 FROM warehouses WHERE id=?', (warehouse,)).fetchone():
            raise DomainError('Выберите существующий склад.')

    @staticmethod
    def _event(db, action, details, request_id=None):
        db.execute('INSERT INTO events(request_id,action,details) VALUES(?,?,?)',
                   (request_id, action, json.dumps(details, ensure_ascii=False)))

    @staticmethod
    def _balance(db, warehouse, product):
        on_hand = db.execute('SELECT COALESCE(SUM(quantity),0) FROM movements '
                             'WHERE warehouse_id=? AND product_id=?', (warehouse, product)).fetchone()[0]
        reserved = db.execute('SELECT COALESCE(SUM(l.quantity),0) FROM request_lines l '
                              'JOIN requests r ON r.id=l.request_id WHERE '
                              'r.warehouse_id=? AND l.product_id=? AND r.status="reserved"',
                              (warehouse, product)).fetchone()[0]
        return on_hand, reserved

    def inventory(self):
        with self.connection() as db:
            rows = []
            for warehouse in db.execute('SELECT * FROM warehouses ORDER BY name').fetchall():
                for product in db.execute('SELECT * FROM products ORDER BY name').fetchall():
                    on_hand, reserved = self._balance(db, warehouse['id'], product['id'])
                    rows.append(dict(warehouse=warehouse['name'], warehouse_id=warehouse['id'],
                                     product=product['name'], product_id=product['id'], unit=product['unit'],
                                     on_hand=on_hand, reserved=reserved, available=on_hand-reserved))
            return rows

    def receive(self, warehouse, source, lines, token):
        source = self._text(source, 'Источник поступления')
        token = self._text(token, 'Идентификатор операции', 64)
        with self.connection(write=True) as db:
            existing = db.execute('SELECT id FROM receipts WHERE token=?', (token,)).fetchone()
            if existing:
                return existing['id']
            self._warehouse(db, warehouse)
            lines = self._lines(db, lines)
            receipt_id = db.execute('INSERT INTO receipts(token,warehouse_id,source) VALUES(?,?,?)',
                                     (token, warehouse, source)).lastrowid
            for product, quantity in lines.items():
                db.execute('INSERT INTO movements(warehouse_id,product_id,quantity,receipt_id) '
                           'VALUES(?,?,?,?)', (warehouse, product, quantity, receipt_id))
            self._event(db, 'Поступление', {'номер': receipt_id, 'источник': source, 'позиции': lines})
            return receipt_id

    def create_request(self, warehouse, destination, lines, code, note=''):
        destination = self._text(destination, 'Получатель или пункт передачи')
        code = self._text(code, 'Идентификатор заявки', 64)
        if len(note) > 500:
            raise DomainError('Примечание должно содержать не более 500 символов.')
        with self.connection(write=True) as db:
            existing = db.execute('SELECT id FROM requests WHERE code=?', (code,)).fetchone()
            if existing:
                return existing['id']
            self._warehouse(db, warehouse)
            lines = self._lines(db, lines)
            request_id = db.execute('INSERT INTO requests(code,warehouse_id,destination,note) '
                                    'VALUES(?,?,?,?)', (code, warehouse, destination, note.strip())).lastrowid
            db.executemany('INSERT INTO request_lines VALUES(?,?,?)',
                           [(request_id, product, quantity) for product, quantity in lines.items()])
            self._event(db, 'Создание заявки', {'получатель': destination}, request_id)
            return request_id

    def request(self, request_id):
        rows = self.query('SELECT r.*,w.name AS warehouse FROM requests r '
                          'JOIN warehouses w ON w.id=r.warehouse_id WHERE r.id=?', (request_id,))
        if not rows:
            return None
        result = rows[0]
        with self.connection() as db:
            result['lines'] = []
            for line in db.execute('SELECT l.*,p.name,p.unit FROM request_lines l '
                                    'JOIN products p ON p.id=l.product_id WHERE request_id=?', (request_id,)):
                on_hand, reserved = self._balance(db, result['warehouse_id'], line['product_id'])
                available = on_hand-reserved
                result['lines'].append(dict(line, available=available,
                    shortage=max(0, line['quantity']-available) if result['status']=='draft' else 0))
        result['events'] = self.query('SELECT * FROM events WHERE request_id=? ORDER BY id', (request_id,))
        return result

    def transition(self, request_id, action):
        with self.connection(write=True) as db:
            request = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if not request:
                raise DomainError('Заявка не найдена.')
            status = request['status']
            lines = db.execute('SELECT l.*,p.name FROM request_lines l JOIN products p '
                               'ON p.id=l.product_id WHERE request_id=?', (request_id,)).fetchall()
            if action == 'reserve' and status == 'draft':
                deficits = []
                for line in lines:
                    on_hand, reserved = self._balance(db, request['warehouse_id'], line['product_id'])
                    if on_hand-reserved < line['quantity']:
                        deficits.append(f'{line["name"]}: не хватает {line["quantity"]-(on_hand-reserved)}')
                if deficits:
                    raise DomainError('Резерв не создан. ' + '; '.join(deficits) + '.')
                new_status, title = 'reserved', 'Резервирование'
            elif action == 'issue' and status == 'reserved':
                for line in lines:
                    on_hand, reserved = self._balance(db, request['warehouse_id'], line['product_id'])
                    if on_hand < reserved:
                        raise DomainError('Остатки не согласованы с резервами. Выдача остановлена.')
                    db.execute('INSERT INTO movements(warehouse_id,product_id,quantity,request_id) '
                               'VALUES(?,?,?,?)', (request['warehouse_id'], line['product_id'],
                                                   -line['quantity'], request_id))
                new_status, title = 'issued', 'Передача помощи'
            elif action == 'cancel' and status in ('draft', 'reserved'):
                new_status, title = 'cancelled', 'Отмена заявки'
            else:
                raise DomainError('Действие недоступно для текущего статуса заявки. Обновите страницу.')
            db.execute('UPDATE requests SET status=? WHERE id=?', (new_status, request_id))
            self._event(db, title, {'из': status, 'в': new_status}, request_id)

    def seed_demo(self):
        """Explicit opt-in seed. No real recipient or donor data."""
        with self.connection(write=True) as db:
            if db.execute('SELECT 1 FROM warehouses').fetchone():
                raise DomainError('Демо заполняется только в новой пустой базе.')
            db.executemany('INSERT INTO warehouses(name) VALUES(?)', [('Учебный склад «Центральный»',),
                                                                       ('Учебный склад «Северный»',)])
            db.executemany('INSERT INTO products(name,unit) VALUES(?,?)', [
                ('Вода питьевая, 1,5 л', 'бут.'), ('Крупа гречневая, 1 кг', 'упак.'),
                ('Консервы рыбные, 250 г', 'бан.'), ('Одеяло', 'шт.'), ('Набор гигиены', 'набор')])
            db.execute('INSERT INTO metadata VALUES("demo","1")')
        self.receive(1, 'Вымышленный сбор №1', {1: 120, 2: 80, 3: 60, 4: 15, 5: 30}, 'demo-receipt-1')
        self.receive(2, 'Вымышленный сбор №2', {1: 40, 2: 25, 3: 20, 4: 5, 5: 10}, 'demo-receipt-2')
        request_id = self.create_request(1, 'Учебный пункт передачи «А»', {1: 20, 2: 10, 5: 5}, 'demo-request-1')
        self.transition(request_id, 'reserve')
        self.create_request(1, 'Учебный пункт передачи «Б»', {4: 25, 5: 10}, 'demo-request-2',
                            'Пример дефицита: одеял на выбранном складе недостаточно.')

"""Strict CSV catalog import. Quantities and stock are never inferred."""

import csv
import io
import sqlite3

from store import DomainError


def parse_csv(data, kind):
    if kind not in ('products', 'warehouses'):
        raise DomainError('Неизвестный справочник.')
    if len(data) > 50_000:
        raise DomainError('CSV должен быть не больше 50 КБ.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise DomainError('Сохраните CSV в кодировке UTF-8.') from None
    expected = ['Наименование', 'Единица'] if kind == 'products' else ['Наименование']
    reader = csv.DictReader(io.StringIO(text, newline=''), delimiter=';', strict=True)
    if reader.fieldnames != expected:
        raise DomainError('Нужны столбцы: ' + ';'.join(expected) + '. Скачайте шаблон.')
    rows, seen = [], set()
    try:
        for index, row in enumerate(reader, 2):
            if index > 201:
                raise DomainError('За один раз можно загрузить не более 200 строк.')
            if None in row or any(v is None for v in row.values()):
                raise DomainError(f'Строка {index}: неверное число столбцов.')
            name = ' '.join(row['Наименование'].split())
            unit = row.get('Единица', '').strip()
            if not 1 <= len(name) <= 160 or (kind == 'products' and not 1 <= len(unit) <= 20):
                raise DomainError(f'Строка {index}: заполните название (до 160 символов) и единицу (до 20).')
            if name.startswith(('=', '+', '-', '@')) or unit.startswith(('=', '+', '-', '@')):
                raise DomainError(f'Строка {index}: формулы в справочниках не допускаются.')
            if name.casefold() in seen:
                raise DomainError(f'Строка {index}: название повторяется в файле.')
            seen.add(name.casefold())
            rows.append((name, unit))
    except csv.Error:
        raise DomainError('Не удалось разобрать CSV. Проверьте кавычки и разделитель «;».') from None
    if not rows:
        raise DomainError('CSV не содержит данных.')
    return rows


def import_catalog(store, kind, rows, actor):
    if kind not in ('products', 'warehouses'):
        raise DomainError('Неизвестный справочник.')
    added, skipped = 0, 0
    with store.connection(write=True) as db:
        existing = {row['name'].casefold(): row for row in db.execute(f'SELECT * FROM {kind}')}
        for name, unit in rows:
            previous = existing.get(name.casefold())
            if previous:
                if kind == 'products' and previous['unit'] != unit:
                    raise DomainError(f'«{name}» уже учитывается в единице «{previous["unit"]}». Импорт отменён.')
                skipped += 1
                continue
            try:
                if kind == 'products':
                    db.execute('INSERT INTO products(name,unit) VALUES(?,?)', (name, unit))
                else:
                    db.execute('INSERT INTO warehouses(name) VALUES(?)', (name,))
            except sqlite3.IntegrityError:
                raise DomainError('Конфликт справочника. Обновите страницу и повторите импорт.') from None
            added += 1
        store._event(db, 'Импорт справочника', {'тип': kind, 'добавлено': added,
                                              'пропущено': skipped, 'оператор': actor})
    return added, skipped

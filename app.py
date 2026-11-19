"""Statist: local educational prototype. Run python app.py --demo."""

import argparse
import csv
import io
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for
from jinja2 import DictLoader

from store import DomainError, Store
from ui import TEMPLATES

STATUSES = {'draft': 'Ожидает резерва', 'reserved': 'В резерве',
            'issued': 'Передана', 'cancelled': 'Отменена'}


def create_app(database=None, testing=False):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get('STATIST_SECRET_KEY') or secrets.token_hex(32),
                      TESTING=testing, MAX_CONTENT_LENGTH=64 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    app.jinja_loader = DictLoader(TEMPLATES)
    store = Store(database or os.environ.get('STATIST_DATABASE') or
                  Path(__file__).parent / 'instance' / 'statist.sqlite3')
    app.extensions['store'] = store

    @app.before_request
    def csrf_protection():
        session.setdefault('csrf', secrets.token_hex(32))
        if request.method == 'POST' and not secrets.compare_digest(
                session['csrf'], request.form.get('csrf', '')):
            abort(400, 'Форма устарела или открыта в другой сессии. Обновите страницу и повторите действие.')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.context_processor
    def common():
        return dict(csrf=session['csrf'], statuses=STATUSES,
                    demo=bool(store.query('SELECT 1 FROM metadata WHERE key="demo" AND value="1"')))

    def request_rows():
        return store.query('SELECT r.*,w.name AS warehouse FROM requests r '
                           'JOIN warehouses w ON w.id=r.warehouse_id ORDER BY r.id DESC')

    @app.get('/')
    def dashboard():
        rows = request_rows()
        counts = {status: sum(r['status'] == status for r in rows) for status in STATUSES}
        return render_template('dashboard.html', title='Обзор', section='dashboard', rows=rows[:6],
                               counts=counts, warehouses=store.catalog('warehouses'),
                               products=store.catalog('products'))

    @app.get('/stock')
    def stock():
        return render_template('stock.html', title='Остатки на складах', section='stock',
                               inventory=store.inventory())

    def document_form(kind):
        products, warehouses = store.catalog('products'), store.catalog('warehouses')
        values = request.form if request.method == 'POST' else {}
        error = None
        token = values.get('token') or uuid4().hex
        if request.method == 'POST':
            try:
                # Iterate submitted quantity fields too: unknown products must not be silently ignored.
                lines = {}
                for key in request.form:
                    if key.startswith('q_') and request.form[key].strip():
                        quantity = int(request.form[key])
                        product_id = int(key[2:])
                        if quantity != 0:
                            lines[product_id] = quantity
                warehouse = int(request.form.get('warehouse', ''))
                if kind == 'request':
                    request_id = store.create_request(warehouse, request.form.get('party', ''), lines,
                                                      token, request.form.get('note', ''))
                    flash('Заявка создана. Проверьте состав и зарезервируйте товары.', 'success')
                    return redirect(url_for('request_detail', request_id=request_id))
                receipt_id = store.receive(warehouse, request.form.get('party', ''), lines, token)
                flash(f'Поступление № {receipt_id} сохранено. Остатки обновлены.', 'success')
                return redirect(url_for('stock'))
            except DomainError as exc:
                error = str(exc)
            except (ValueError, TypeError):
                error = 'Выберите склад и введите целое количество для каждой нужной позиции.'
        return render_template('form.html', title='Новая заявка' if kind == 'request' else 'Новое поступление',
                               section='requests' if kind == 'request' else 'stock', kind=kind,
                               products=products, warehouses=warehouses, values=values, token=token,
                               error=error), 422 if error else 200

    @app.route('/receipts/new', methods=['GET', 'POST'])
    def new_receipt():
        return document_form('receipt')

    @app.route('/requests/new', methods=['GET', 'POST'])
    def new_request():
        return document_form('request')

    @app.get('/requests')
    def requests():
        return render_template('requests.html', title='Заявки', section='requests', rows=request_rows())

    @app.get('/requests/<int:request_id>')
    def request_detail(request_id):
        r = store.request(request_id)
        if not r:
            abort(404)
        return render_template('detail.html', title=f'Заявка № {request_id}', section='requests', r=r)

    @app.post('/requests/<int:request_id>/<action>')
    def change_request(request_id, action):
        if not store.request(request_id):
            abort(404)
        try:
            store.transition(request_id, action)
            flash({'reserve': 'Товары зарезервированы.', 'issue': 'Передача подтверждена.',
                   'cancel': 'Заявка отменена. Резерв освобождён.'}[action], 'success')
        except DomainError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('request_detail', request_id=request_id))

    @app.get('/journal')
    def journal():
        events = store.query('SELECT * FROM events ORDER BY id DESC LIMIT 200')
        for event in events:
            event['details'] = json.loads(event['details'])
        return render_template('journal.html', title='Журнал операций', section='journal', events=events)

    @app.get('/reports/stock.csv')
    def stock_csv():
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['Склад', 'Товар', 'Единица', 'На складе', 'В резерве', 'Доступно'])
        for row in store.inventory():
            # Prefix spreadsheet formulas even if catalogs become editable later.
            cells = [row[key] for key in ('warehouse','product','unit','on_hand','reserved','available')]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@'))
                             else v for v in cells])
        return Response('\ufeff' + output.getvalue(), content_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="statist-stock.csv"'})

    @app.get('/requests/<int:request_id>/document')
    def transfer_document(request_id):
        r = store.request(request_id)
        if not r:
            abort(404)
        if r['status'] != 'issued':
            abort(409, 'Лист передачи доступен после подтверждения фактической передачи.')
        issued_at = next(e['created_at'] for e in r['events'] if e['action'] == 'Передача помощи')
        return render_template('document.html', title='Лист передачи', section='requests', r=r,
                               issued_at=issued_at)

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def error_page(error):
        messages = {404: 'Страница не найдена.', 413: 'Слишком большой запрос.'}
        return render_template('error.html', title=f'Ошибка {error.code}', section='',
                               message=messages.get(error.code, error.description)), error.code

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Statist — учебный складской учёт')
    parser.add_argument('--demo', action='store_true', help='Заполнить новую базу вымышленными данными')
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    application = create_app()
    if args.demo:
        try:
            application.extensions['store'].seed_demo()
        except DomainError as exc:
            print(exc)
    application.run(host='127.0.0.1', port=args.port, debug=False)

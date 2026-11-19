"""Statist application factory. Hosted entry point: wsgi.application."""

import argparse
import csv
import io
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from jinja2 import DictLoader

from store import DomainError, Store
from ui import TEMPLATES
from access import Access, ROLES, can
from access_ui import TEMPLATES as ACCESS_TEMPLATES
from catalogs import import_catalog, parse_csv

STATUSES = {'draft': 'Ожидает резерва', 'reserved': 'В резерве',
            'issued': 'Передана', 'cancelled': 'Отменена'}


def create_app(database=None, testing=False):
    app = Flask(__name__)
    app.config.update(TESTING=testing, MAX_CONTENT_LENGTH=64 * 1024,
                      PUBLIC_DEMO=os.environ.get('STATIST_PUBLIC_DEMO', '1') == '1',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    app.jinja_loader = DictLoader(TEMPLATES | ACCESS_TEMPLATES)
    store = Store(database or os.environ.get('STATIST_DATABASE') or
                  Path(__file__).parent / 'instance' / 'statist.sqlite3')
    app.extensions['store'] = store
    access = Access(store)
    app.extensions['access'] = access
    app.config['SECRET_KEY'] = os.environ.get('STATIST_SECRET_KEY') or store.session_secret()
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

    @app.before_request
    def csrf_protection():
        session.setdefault('csrf', secrets.token_hex(32))
        g.user = access.user(session.get('user_id')) if session.get('user_id') else None
        g.demo = bool(store.query('SELECT 1 FROM metadata WHERE key="demo" AND value="1"'))
        required = {'new_receipt': 'receipt', 'new_request': 'request',
                    'catalogs': 'catalog', 'catalog_template': 'catalog', 'accounts': 'accounts'}
        permission = required.get(request.endpoint)
        if request.endpoint == 'change_request':
            permission = (request.view_args or {}).get('action', 'unknown')
        if permission and not can(g.user, permission):
            if not g.user and request.method == 'GET':
                return redirect(url_for('login'))
            abort(403, 'Недостаточно прав для этого действия.')
        public = {'login', 'join', 'accept_invitation', 'health'}
        if not g.user and not (g.demo and app.config['PUBLIC_DEMO']) and request.endpoint and request.endpoint not in public:
            return redirect(url_for('login'))
        if request.method == 'POST' and not secrets.compare_digest(
                session['csrf'].encode(), request.form.get('csrf', '').encode()):
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
        return dict(csrf=session['csrf'], statuses=STATUSES, demo=g.demo,
                    user=g.user, roles=ROLES, can=lambda action: can(g.user, action))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            try:
                user = access.login(request.form.get('username', ''), request.form.get('password', ''),
                                    request.remote_addr)
                if user:
                    session.clear()
                    session.update(user_id=user['id'], csrf=secrets.token_hex(32))
                    session.permanent = True
                    return redirect(url_for('dashboard'))
                error = 'Неверный логин или пароль.'
            except DomainError as exc:
                error = str(exc)
        return render_template('login.html', title='Вход', section='', error=error), 401 if error else 200

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.get('/join/<token>')
    def accept_invitation(token):
        if not access.invitation(token):
            abort(410, 'Приглашение использовано или срок действия истёк.')
        session['invitation'] = token
        return redirect(url_for('join'))

    @app.route('/join', methods=['GET', 'POST'])
    def join():
        token = session.get('invitation', '')
        invitation = access.invitation(token)
        if not invitation:
            abort(410, 'Для создания аккаунта нужна действующая ссылка от координатора.')
        error = None
        if request.method == 'POST':
            try:
                if request.form.get('password') != request.form.get('password_repeat'):
                    raise DomainError('Пароли не совпадают.')
                user = access.register(token, request.form.get('username', ''), request.form.get('password', ''))
                session.clear()
                session.update(user_id=user['id'], csrf=secrets.token_hex(32))
                session.permanent = True
                return redirect(url_for('dashboard'))
            except DomainError as exc:
                error = str(exc)
        return render_template('join.html', title='Создание аккаунта', section='',
                               invitation=invitation, error=error), 422 if error else 200

    @app.route('/accounts', methods=['GET', 'POST'])
    def accounts():
        invitation_url, error = None, None
        if request.method == 'POST':
            try:
                token = access.invite(request.form.get('role'), actor=g.user['username'])
                invitation_url = url_for('accept_invitation', token=token, _external=True)
            except DomainError as exc:
                error = str(exc)
        users = store.query('SELECT username,role,created_at FROM users ORDER BY id')
        return render_template('accounts.html', title='Сотрудники', section='accounts',
                               users=users, invitation_url=invitation_url, error=error), 422 if error else 200

    @app.route('/catalogs', methods=['GET', 'POST'])
    def catalogs():
        error = None
        if request.method == 'POST':
            try:
                kind = request.form.get('kind')
                upload = request.files.get('file')
                if not upload or not upload.filename:
                    raise DomainError('Выберите CSV-файл.')
                data = upload.read(50_001)
                rows = parse_csv(data, kind)
                added, skipped = import_catalog(store, kind, rows, g.user['username'])
                flash(f'Импорт завершён. Добавлено: {added}. Уже существовали: {skipped}. Остатки не менялись.', 'success')
                return redirect(url_for('catalogs'))
            except DomainError as exc:
                error = str(exc)
        return render_template('catalogs.html', title='Справочники', section='catalogs', error=error,
                               products=store.catalog('products'), warehouses=store.catalog('warehouses')), 422 if error else 200

    @app.get('/catalogs/<kind>/template.csv')
    def catalog_template(kind):
        examples = {'products': 'Наименование;Единица\nБумага туалетная;рулон\n',
                    'warehouses': 'Наименование\nУчебный склад «Восточный»\n'}
        if kind not in examples:
            abort(404)
        return Response('\ufeff' + examples[kind], content_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{kind}-template.csv"'})

    def request_rows():
        return store.query('SELECT r.*,w.name AS warehouse FROM requests r '
                           'JOIN warehouses w ON w.id=r.warehouse_id ORDER BY r.id DESC')

    @app.get('/healthz')
    def health():
        store.query('SELECT 1')
        return {'status': 'ok'}

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
                                                      token, request.form.get('note', ''), actor=g.user['username'])
                    flash('Заявка создана. Проверьте состав и зарезервируйте товары.', 'success')
                    return redirect(url_for('request_detail', request_id=request_id))
                receipt_id = store.receive(warehouse, request.form.get('party', ''), lines, token,
                                           actor=g.user['username'])
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
            store.transition(request_id, action, actor=g.user['username'])
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
    @app.errorhandler(403)
    @app.errorhandler(409)
    @app.errorhandler(410)
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

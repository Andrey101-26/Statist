import csv
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import create_app
from manage import initialize_demo
from store import DomainError, Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.store.seed_demo()

    def tearDown(self):
        self.tmp.cleanup()

    def balance(self, product=1, warehouse=1):
        return next(r for r in self.store.inventory()
                    if r['warehouse_id'] == warehouse and r['product_id'] == product)

    def test_reserve_issue_and_persistence(self):
        before = self.balance()
        self.store.transition(1, 'issue')
        after = self.balance()
        self.assertEqual(after['on_hand'], before['on_hand'] - 20)
        self.assertEqual(after['reserved'], 0)
        self.assertEqual(after['available'], before['available'])
        self.assertEqual(Store(self.path).request(1)['status'], 'issued')

    def test_shortage_rolls_back_all_lines(self):
        before = self.store.inventory()
        with self.assertRaisesRegex(DomainError, 'не хватает 10'):
            self.store.transition(2, 'reserve')
        self.assertEqual(self.store.inventory(), before)
        self.assertEqual(self.store.request(2)['status'], 'draft')
        self.assertEqual(len(self.store.request(2)['events']), 1)

    def test_cancel_releases_without_changing_physical_stock(self):
        before = self.balance()
        self.store.transition(1, 'cancel')
        after = self.balance()
        self.assertEqual(after['on_hand'], before['on_hand'])
        self.assertEqual(after['reserved'], 0)
        self.assertEqual(after['available'], before['available'] + 20)

    def test_second_issue_and_issue_without_reserve_are_rejected(self):
        with self.assertRaises(DomainError):
            self.store.transition(2, 'issue')
        self.store.transition(1, 'issue')
        after = self.store.inventory()
        with self.assertRaises(DomainError):
            self.store.transition(1, 'issue')
        self.assertEqual(self.store.inventory(), after)

    def test_duplicate_post_tokens_do_not_duplicate_stock_or_request(self):
        first = self.store.receive(1, 'Учебный источник', {1: 5}, 'same-token')
        second = self.store.receive(1, 'Учебный источник', {1: 5}, 'same-token')
        self.assertEqual(first, second)
        self.assertEqual(self.balance()['on_hand'], 125)
        one = self.store.create_request(1, 'Пункт В', {1: 5}, 'same-request')
        two = self.store.create_request(1, 'Пункт В', {1: 5}, 'same-request')
        self.assertEqual(one, two)

    def test_invalid_quantities_and_references_do_not_create_movements(self):
        before = self.store.inventory()
        for quantity in (0, -1, 1.5, True, 1_000_001):
            with self.assertRaises(DomainError):
                self.store.receive(1, 'Источник', {1: quantity}, f'invalid-{quantity}')
        with self.assertRaises(DomainError):
            self.store.receive(1, 'Источник', {999: 5}, 'invalid-product')
        with self.assertRaises(DomainError):
            self.store.receive(999, 'Источник', {1: 5}, 'invalid-warehouse')
        self.assertEqual(self.store.inventory(), before)

    def test_concurrent_reservations_cannot_overbook(self):
        ids = [self.store.create_request(2, f'Пункт {i}', {1: 30}, f'parallel-{i}') for i in range(2)]
        barrier = threading.Barrier(2)

        def reserve(request_id):
            barrier.wait()
            try:
                Store(self.path).transition(request_id, 'reserve')
                return True
            except DomainError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, ids))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.balance(warehouse=2)['available'], 10)

    def test_replenishment_allows_previously_deficient_request(self):
        self.store.receive(1, 'Дополнительный сбор', {4: 10}, 'replenish')
        self.store.transition(2, 'reserve')
        self.store.transition(2, 'issue')
        self.assertEqual(self.balance(product=4)['on_hand'], 0)

    def test_deployment_initialization_preserves_existing_operations(self):
        self.store.transition(1, 'issue')
        before = self.store.inventory()
        self.assertFalse(initialize_demo(self.store))
        self.assertEqual(self.store.inventory(), before)
        self.assertEqual(self.store.request(1)['status'], 'issued')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.tmp.name) / 'web.sqlite3', testing=True)
        self.store = self.app.extensions['store']
        self.store.seed_demo()
        self.client = self.app.test_client()
        token = self.app.extensions['access'].invite('coordinator', bootstrap=True)
        user = self.app.extensions['access'].register(token, 'test-coordinator', 'a-long-test-password')
        self.client.get('/')
        with self.client.session_transaction() as session:
            session['user_id'] = user['id']
            self.csrf = session['csrf']

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_pages_render_and_unissued_document_is_blocked(self):
        for path in ('/', '/stock', '/requests', '/requests/1', '/requests/2',
                     '/receipts/new', '/requests/new', '/journal'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('lang="ru"', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/requests/1/document').status_code, 409)
        self.assertEqual(self.client.get('/requests/999').status_code, 404)

    def test_csrf_blocks_mutation_and_get_never_mutates(self):
        self.assertEqual(self.client.post('/requests/1/issue').status_code, 400)
        self.assertEqual(self.client.get('/requests/1/issue').status_code, 405)
        self.assertEqual(self.store.request(1)['status'], 'reserved')

    def test_non_ascii_csrf_is_rejected_without_server_error(self):
        response = self.client.post('/requests/1/issue', data={'csrf': 'неверный ключ'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.request(1)['status'], 'reserved')

    def test_session_survives_application_restart(self):
        restarted = create_app(self.store.path, testing=True)
        client = restarted.test_client()
        client.set_cookie('session', self.client.get_cookie('session').value)
        response = client.post('/requests/1/issue', data={'csrf': self.csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(restarted.extensions['store'].request(1)['status'], 'issued')
        self.assertEqual(client.get('/healthz').json, {'status': 'ok'})

    def test_complete_flow_through_http_and_escaped_input(self):
        response = self.client.post('/requests/new', data={
            'csrf': self.csrf, 'token': 'web-request', 'warehouse': '2', 'q_1': '5',
            'party': '<script>alert(1)</script>', 'note': 'Учебная заявка'})
        self.assertEqual(response.status_code, 302)
        location = response.headers['Location']
        page = self.client.get(location).get_data(as_text=True)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.client.post(location + '/reserve', data={'csrf': self.csrf})
        self.client.post(location + '/issue', data={'csrf': self.csrf})
        document = self.client.get(location + '/document')
        self.assertEqual(document.status_code, 200)
        self.assertIn('Лист передачи', document.get_data(as_text=True))
        self.assertEqual(self.store.request(int(location.split('/')[-1]))['status'], 'issued')

    def test_validation_keeps_entered_values(self):
        response = self.client.post('/receipts/new', data={
            'csrf': self.csrf, 'token': 'bad-input', 'warehouse': '1',
            'party': 'Пробный сбор', 'q_1': '-3'})
        self.assertEqual(response.status_code, 422)
        self.assertIn('Пробный сбор', response.get_data(as_text=True))

    def test_csv_reconciles_with_stock(self):
        response = self.client.get('/reports/stock.csv')
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0][-1], 'Доступно')
        self.assertTrue(all(int(row[3])-int(row[4]) == int(row[5]) for row in rows[1:]))


if __name__ == '__main__':
    unittest.main()

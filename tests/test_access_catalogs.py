import io
import tempfile
import unittest
from pathlib import Path

from app import create_app
from catalogs import import_catalog, parse_csv
from store import DomainError


class AccessAndImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.tmp.name) / 'access.sqlite3', testing=True)
        self.store = self.app.extensions['store']
        self.store.seed_demo()
        self.access = self.app.extensions['access']
        token = self.access.invite('coordinator', bootstrap=True)
        self.coordinator = self.access.register(token, 'coordinator', 'test-password-long-enough')

    def tearDown(self):
        self.tmp.cleanup()

    def client_for(self, user):
        client = self.app.test_client()
        client.get('/')
        with client.session_transaction() as session:
            if user:
                session['user_id'] = user['id']
            csrf = session['csrf']
        return client, csrf

    def make_user(self, role):
        token = self.access.invite(role, actor='coordinator')
        return self.access.register(token, role, 'test-password-long-enough')

    def test_guest_can_view_demo_but_cannot_mutate_with_valid_csrf(self):
        client, csrf = self.client_for(None)
        self.assertEqual(client.get('/').status_code, 200)
        self.assertNotIn('Подтвердить передачу', client.get('/requests/1').get_data(as_text=True))
        for path in ('/requests/1/issue', '/requests/1/cancel', '/requests/new', '/receipts/new', '/accounts', '/catalogs'):
            self.assertEqual(client.post(path, data={'csrf': csrf}).status_code, 403)
        self.assertEqual(self.store.request(1)['status'], 'reserved')
        self.assertEqual(client.get('/receipts/new').status_code, 302)

    def test_private_mode_protects_reads_and_exports(self):
        self.app.config['PUBLIC_DEMO'] = False
        client, _ = self.client_for(None)
        for path in ('/', '/stock', '/requests/1', '/reports/stock.csv', '/journal'):
            self.assertEqual(client.get(path).status_code, 302)
        self.assertEqual(client.get('/healthz').status_code, 200)

    def test_role_checks_are_server_side(self):
        observer, token = self.client_for(self.make_user('observer'))
        self.assertEqual(observer.get('/stock').status_code, 200)
        self.assertEqual(observer.post('/requests/1/issue', data={'csrf': token}).status_code, 403)
        worker, token = self.client_for(self.make_user('warehouse'))
        self.assertEqual(worker.get('/receipts/new').status_code, 200)
        for path in ('/requests/new', '/requests/2/reserve', '/requests/1/cancel', '/catalogs', '/accounts'):
            self.assertEqual(worker.post(path, data={'csrf': token}).status_code, 403)
        self.assertEqual(worker.post('/requests/1/issue', data={'csrf': token}).status_code, 302)
        self.assertEqual(self.store.request(1)['status'], 'issued')

    def test_invitation_is_single_use_role_cannot_be_overridden(self):
        token = self.access.invite('observer')
        client, csrf = self.client_for(None)
        self.assertEqual(client.get('/join/' + token).status_code, 302)
        self.assertEqual(client.post('/join', data={'csrf': csrf, 'username': 'new-person',
            'password': 'test-password-long-enough', 'password_repeat': 'test-password-long-enough',
            'role': 'coordinator'}).status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(self.access.user(session['user_id'])['role'], 'observer')
        with self.assertRaises(DomainError):
            self.access.register(token, 'another-person', 'test-password-long-enough')
        self.assertEqual(client.get('/join/' + token).status_code, 410)

    def test_login_logout_rotation_and_password_hashing(self):
        client, csrf = self.client_for(None)
        response = client.post('/login', data={'csrf': csrf, 'username': 'COORDINATOR',
                                              'password': 'test-password-long-enough'})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertNotEqual(csrf, session['csrf'])
            self.assertEqual(session['user_id'], self.coordinator['id'])
            csrf = session['csrf']
        self.assertNotEqual(self.store.query('SELECT password_hash FROM users')[0]['password_hash'],
                            'test-password-long-enough')
        client.post('/logout', data={'csrf': csrf})
        self.assertEqual(client.get('/accounts').status_code, 302)

    def test_login_throttle_persists(self):
        for _ in range(8):
            self.assertIsNone(self.access.login('coordinator', 'wrong', 'test-peer'))
        with self.assertRaisesRegex(DomainError, 'Слишком много'):
            self.access.login('coordinator', 'test-password-long-enough', 'test-peer')

    def test_bootstrap_cannot_create_another_owner(self):
        with self.assertRaises(DomainError):
            self.access.invite('coordinator', bootstrap=True)

    def test_catalog_validation_and_atomic_rollback(self):
        before = self.store.catalog('products')
        with self.assertRaises(DomainError):
            parse_csv('Наименование;Единица\nНовый товар;шт.\nБез единицы;\n'.encode(), 'products')
        with self.assertRaises(DomainError):
            parse_csv('Наименование;Единица\nОдин;шт.\nодин;шт.\n'.encode(), 'products')
        with self.assertRaises(DomainError):
            import_catalog(self.store, 'products', [('Новый товар', 'шт.'), ('Одеяло', 'кг')], 'coordinator')
        self.assertEqual(self.store.catalog('products'), before)

    def test_csv_upload_does_not_change_stock_and_repeat_skips(self):
        client, csrf = self.client_for(self.coordinator)
        before = self.store.inventory()
        content = 'Наименование;Единица\nБумага туалетная;рулон\n'.encode('utf-8-sig')
        for _ in range(2):
            response = client.post('/catalogs', data={'csrf': csrf, 'kind': 'products',
                'file': (io.BytesIO(content), 'products.csv')}, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.store.catalog('products')), 6)
        after = self.store.inventory()
        self.assertEqual([r for r in after if r['product_id'] <= 5], before)
        self.assertTrue(all(r['on_hand'] == 0 for r in after if r['product_id'] > 5))
        self.assertEqual(client.get('/catalogs/products/template.csv').status_code, 200)
        for path in ('/accounts', '/catalogs'):
            self.assertEqual(client.get(path).status_code, 200)


if __name__ == '__main__':
    unittest.main()

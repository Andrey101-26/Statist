"""One-off database setup. Does not start a development web server."""

import argparse

from app import create_app
from store import DomainError


def initialize_demo(store):
    if store.query('SELECT 1 FROM metadata WHERE key="demo" AND value="1"'):
        return False
    store.seed_demo()
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Statist database setup')
    parser.add_argument('command', choices=['init-demo', 'invite-owner'])
    parser.add_argument('--base-url', default='https://rey33.pythonanywhere.com')
    args = parser.parse_args()
    try:
        app = create_app()
        if args.command == 'init-demo':
            created = initialize_demo(app.extensions['store'])
            print('Demo data initialized.' if created else 'Existing demo data preserved.')
        else:
            token = app.extensions['access'].invite('coordinator', bootstrap=True)
            print(args.base_url.rstrip('/') + '/join/' + token)
    except DomainError as exc:
        parser.exit(1, str(exc) + '\n')

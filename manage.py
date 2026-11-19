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
    parser.add_argument('command', choices=['init-demo'])
    parser.parse_args()
    try:
        created = initialize_demo(create_app().extensions['store'])
        print('Demo data initialized.' if created else 'Existing demo data preserved.')
    except DomainError as exc:
        parser.exit(1, str(exc) + '\n')

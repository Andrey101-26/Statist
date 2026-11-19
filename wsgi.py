"""WSGI entry point for PythonAnywhere or another Python host."""

from app import create_app

application = create_app()
# Hosted installations must use HTTPS. No proxy headers are trusted blindly.
application.config['SESSION_COOKIE_SECURE'] = True

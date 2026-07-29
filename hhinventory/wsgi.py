"""WSGI config for hhinventory project.

Elastic Beanstalk's Python platform looks for a Procfile entry
(see Procfile) pointing at `hhinventory.wsgi:application`.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hhinventory.settings")

application = get_wsgi_application()

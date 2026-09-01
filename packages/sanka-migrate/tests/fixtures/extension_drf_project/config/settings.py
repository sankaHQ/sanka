# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "sanka-wheel-acceptance-only"
DEBUG = False
ALLOWED_HOSTS = ["testserver"]
ROOT_URLCONF = "config.urls"
MIDDLEWARE = []
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "api",
    "rest_framework",
]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
}
USE_TZ = True

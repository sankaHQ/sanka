# SPDX-License-Identifier: AGPL-3.0-only
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "sanka-auth-fixture-only"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
ROOT_URLCONF = "board_config.urls"
MIDDLEWARE: list[str] = []
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "rest_framework.authtoken",
    "bulletins",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SANKA_TEST_DB", str(BASE_DIR / "db.sqlite3")),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
}

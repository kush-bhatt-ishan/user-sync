"""
Django settings for userSync project.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file from either root or userSync folder
if (BASE_DIR / ".env").exists():
    load_dotenv(BASE_DIR / ".env")
elif (BASE_DIR / "userSync" / ".env").exists():
    load_dotenv(BASE_DIR / "userSync" / ".env")
else:
    load_dotenv()

DEFAULT_COMPANY_NAME = os.getenv("DEFAULT_COMPANY_NAME", "Ishan Technologies")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "sync.log")

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'file': {
            'level': LOG_LEVEL,
            'class': 'logging.FileHandler',
            'filename': str(BASE_DIR / LOG_FILE),
            'formatter': 'verbose',
        },
        'console': {
            'level': LOG_LEVEL,
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'formatters': {
        'verbose': {
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
    },
    'loggers': {
        'myapp': {
            'handlers': ['file', 'console'],
            'level': LOG_LEVEL,
            'propagate': True,
        },
    },
}

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'django-insecure-6yfq@#=7i50!0!jl7ifz*91kj6-txmpem*jpi%qg^15kd*2am*')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv('DEBUG', 'True') == 'True'

allowed_hosts_raw = os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost,portal-app-new')
ALLOWED_HOSTS = [h.strip() for h in allowed_hosts_raw.split(',') if h.strip()]
if DEBUG and '*' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('*')

# ── Authentication & Sessions ────────────────────────────────────────────────
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# Session security
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 1209600  # 14 days default
SESSION_SAVE_EVERY_REQUEST = True

# Microsoft SSO (MSAL)
AZ_TENANT_ID = os.getenv('AZ_TENANT_ID', '')
AZ_CLIENT_ID = os.getenv('AZ_CLIENT_ID', '')
AZ_CLIENT_SECRET = os.getenv('AZ_CLIENT_SECRET', '')
AZ_SSO_CLIENT_ID = os.getenv('AZ_SSO_CLIENT_ID', os.getenv('AZ_CLIENT_ID', ''))
AZ_SSO_CLIENT_SECRET = os.getenv('AZ_SSO_CLIENT_SECRET', os.getenv('AZ_CLIENT_SECRET', ''))
AZ_SSO_REDIRECT_URI = os.getenv('AZ_SSO_REDIRECT_URI', 'http://127.0.0.1:8000/auth/callback/')

# Application definition
INSTALLED_APPS = [
    'myapp',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'userSync.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'userSync.wsgi.application'

# Database
# Prefer existing db.sqlite3 in BASE_DIR or userSync
db_path = BASE_DIR / 'db.sqlite3'
if not db_path.exists() and (BASE_DIR / 'userSync' / 'db.sqlite3').exists():
    db_path = BASE_DIR / 'userSync' / 'db.sqlite3'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': db_path,
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

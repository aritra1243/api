"""
Django settings for fuel_route_api project.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'django-insecure-fuel-route-api-secret-key-change-in-production')

DEBUG = os.getenv('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'corsheaders',
    'optimizer',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'fuel_route_api.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'fuel_route_api.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
    ],
}

# CORS
CORS_ALLOW_ALL_ORIGINS = True

# Cache — using in-memory cache for speed
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'fuel-route-cache',
    }
}

# App settings
GEOAPIFY_API_KEY = os.getenv('GEOAPIFY_API_KEY', '').strip()
ORS_API_KEY = os.getenv('ORS_API_KEY', '').strip()
ROUTING_ENGINE = os.getenv('ROUTING_ENGINE', 'geoapify' if GEOAPIFY_API_KEY else 'osrm').lower()

# Check data/ directory first, then root directory
_candidate_paths = [
    BASE_DIR / 'data' / 'fuel-prices-for-be-assessment.csv',
    BASE_DIR / 'fuel-prices-for-be-assessment.csv',
    BASE_DIR / 'data' / 'fuel_prices.csv',
]
FUEL_CSV_PATH = next((p for p in _candidate_paths if p.exists()), _candidate_paths[0])

MAPS_DIR = BASE_DIR / 'maps'
VEHICLE_RANGE_MILES = 500
VEHICLE_MPG = 10

# Media
MEDIA_URL = '/maps/'
MEDIA_ROOT = MAPS_DIR

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_TZ = True

import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-eoi)ng2+&$69=ctha#a!gs(@f6v7dv5sy@dqrjh+k0!vs-chay')

# تعطيل الـ DEBUG في بيئة الإنتاج تلقائياً إذا توفر متغير بيئة على Render
DEBUG = os.environ.get('RENDER') is None

ALLOWED_HOSTS = ['fantazy-league.onrender.com', '127.0.0.1', 'localhost', '*']

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    
    # Cloudinary storage app MUST be before staticfiles
    'cloudinary_storage',
    'django.contrib.staticfiles',
    'cloudinary',
    
    'fantasy',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # لإدارة الملفات الثابتة في البيئة الإنتاجية
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'fantasy_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'fantasy.context_processors.global_timer_context',
                'fantasy.context_processors.current_league_sponsors',
            ],
        },
    },
]

WSGI_APPLICATION = 'fantasy_project.wsgi.application'

# Database Configuration (PostgreSQL dynamically, fallbacks to SQLite)
DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Cache Configuration (مهم جداً لعمل django-ratelimit بدون أخطاء 500)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Cairo'
USE_I18N = True
USE_TZ = True

# Cloudinary Configuration (التحقق الأمني لعدم ضرب السيرفر في حال غياب المفاتيح)
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME')

if CLOUDINARY_CLOUD_NAME:
    CLOUDINARY_STORAGE = {
        'CLOUD_NAME': CLOUDINARY_CLOUD_NAME,
        'API_KEY': os.environ.get('CLOUDINARY_API_KEY'),
        'API_SECRET': os.environ.get('CLOUDINARY_API_SECRET'),
    }
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
else:
    DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'

# Static Files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# إنشاء مجلد staticfiles برمجياً لمنع انهيار WhiteNoise إذا لم يُنفّذ أمر collectstatic
STATIC_ROOT.mkdir(parents=True, exist_ok=True)

# فحص وجود المجلد الرئيسي للملفات الثابتة الخاصة بالدومين لمنع تحذير (staticfiles.W004)
STATICFILES_DIRS = [
    BASE_DIR / 'static',
] if (BASE_DIR / 'static').exists() else []

# محرك تخزين مرن للملفات الثابتة يمنع مشاكل اختفاء الصور واللوجو
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

# Media Files Config
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'squad_builder'
LOGOUT_REDIRECT_URL = 'login'

CSRF_TRUSTED_ORIGINS = [
    'http://127.0.0.1:8000',
    'http://localhost:8000',
    'https://*.onrender.com',
    'https://*.up.railway.app',
]

CSRF_COOKIE_HTTPONLY = False

# Reverse Proxy & Ratelimit Configuration for Render / Cloudflare
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# قراءة الـ IP عبر Render بدواعي Ratelimit (تُغير إلى HTTP_CF_CONNECTING_IP عند التواجد خلف Cloudflare)
RATELIMIT_IP_META_KEY = 'HTTP_X_FORWARDED_FOR'
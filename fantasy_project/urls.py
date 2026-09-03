from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('fantasy.urls')),
]

# ربط مسار الميديا لعرض الصور المرفوعة أونلاين على الهوست دائماً
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
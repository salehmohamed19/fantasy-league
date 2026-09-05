from django.utils import timezone
from datetime import timedelta
from .models import Gameweek

def global_timer_context(request):
    now = timezone.now()
    
    # جلب أول جولة غير منتهية
    next_gameweek = Gameweek.objects.filter(is_finished=False).order_by('number').first()
    
    timer_target = None
    timer_mode = 'WAITING_FOR_OPEN'
    
    if next_gameweek:
        # 1. حالة انتظار فتح التشكيلة (مغلقة حالياً)
        if hasattr(next_gameweek, 'is_open_for_editing') and not next_gameweek.is_open_for_editing:
            timer_mode = 'WAITING_FOR_OPEN'
            timer_target = getattr(next_gameweek, 'opening_time', None)
        else:
            # 2. حالة التشكيلة مفتوحة وننتظر الإغلاق
            timer_mode = 'CLOSING_SOON'
            timer_target = getattr(next_gameweek, 'deadline', None)

    # حماية افتراضية: إذا كانت الحقول فارغة في قاعدة البيانات يتم حساب الخميس القادم
    if not timer_target:
        days_until_thursday = (3 - now.weekday()) % 7
        if days_until_thursday == 0:
            days_until_thursday = 7
        timer_target = (now + timedelta(days=days_until_thursday)).replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        'next_gameweek': next_gameweek,
        'timer_target': timer_target,
        'timer_mode': timer_mode,
    }
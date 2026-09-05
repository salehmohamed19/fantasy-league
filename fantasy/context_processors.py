from django.utils import timezone
from datetime import timedelta
from .models import Gameweek

def global_timer_context(request):
    now = timezone.now()
    
    # جلب أول جولة غير منتهية
    next_gameweek = Gameweek.objects.filter(is_finished=False).order_by('number').first()
    
    timer_target = None
    timer_mode = None
    
    if next_gameweek:
        # 1. حالة انتظار فتح التشكيلة (مغلقة حالياً)
        if hasattr(next_gameweek, 'is_open_for_editing') and not next_gameweek.is_open_for_editing:
            timer_mode = 'WAITING_FOR_OPEN'
            
            # محاولة جلب موعد الفتح المباشر
            timer_target = getattr(next_gameweek, 'opening_time', None)
            
            # إذا لم يُحدد opening_time، يتم حسابه تلقائياً (الخميس القادم)
            if not timer_target:
                # حساب عدد الأيام المتبقية حتى يوم الخميس (Thursday = 3 في Python)
                days_until_thursday = (3 - now.weekday()) % 7
                if days_until_thursday == 0 and now.hour >= 0:
                    days_until_thursday = 7
                
                next_thursday = now + timedelta(days=days_until_thursday)
                timer_target = next_thursday.replace(hour=0, minute=0, second=0, microsecond=0)

        # 2. حالة التشكيلة مفتوحة وننتظر الإغلاق
        else:
            timer_mode = 'CLOSING_SOON'
            timer_target = getattr(next_gameweek, 'deadline', None)
            
    return {
        'next_gameweek': next_gameweek,
        'timer_target': timer_target,
        'timer_mode': timer_mode,
    }
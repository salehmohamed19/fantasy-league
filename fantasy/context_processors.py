from django.utils import timezone
from .models import Gameweek  # استدعاء نموذج الجولات الخاص بك

def global_timer_context(request):
    now = timezone.now()
    
    # جلب الجولة القادمة أو الحالية
    next_gameweek = Gameweek.objects.filter(is_finished=False).order_by('number').first()
    
    timer_target = None
    timer_mode = None
    
    if next_gameweek:
        # إذا كانت التشكيلة مغلقة وننتظر فتحها (مثلاً بعد السبت وحتى الخميس)
        if not next_gameweek.is_open_for_editing:
            timer_mode = 'WAITING_FOR_OPEN'
            timer_target = next_gameweek.opening_time  # حقل موعد الفتح (الخميس)
        else:
            # إذا كانت التشكيلة مفتوحة وننتظر الإغلاق (حتى الموعد النهائي)
            timer_mode = 'CLOSING_SOON'
            timer_target = next_gameweek.deadline      # حقل الموعد النهائي
            
    return {
        'next_gameweek': next_gameweek,
        'timer_target': timer_target,
        'timer_mode': timer_mode,
    }
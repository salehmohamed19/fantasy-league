from django.utils import timezone
from datetime import timedelta
from .models import Gameweek, League, UserFantasyTeam

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


def current_league_sponsors(request):
    """
    جلب رعاة البطولة الحالية التي يتصفحها المستخدم ديناميكياً
    """
    league = None
    
    if request.user.is_authenticated:
        # 1. البحث عن league_id من الـ GET parameter إذا اختار المستخدم بطولة معينة من الـ Dropdown
        league_id = request.GET.get('league_id')
        if league_id:
            league = League.objects.filter(id=league_id, is_active=True).first()
            
        # 2. إذا لم تُحدد في الـ GET، يتم اختيار أول بطولة ينتمي إليها فريق المستخدم
        if not league:
            user_team = UserFantasyTeam.objects.filter(user=request.user).first()
            if user_team:
                league = user_team.league

    # 3. إذا لم توجد بطولة محددة، اجلب أول بطولة نشطة افتراضياً
    if not league:
        league = League.objects.filter(is_active=True).first()

    sponsors = league.sponsors.all() if league else []
    
    return {
        'current_sponsors': sponsors,
        'sponsors_league_name': league.name if league else ''
    }
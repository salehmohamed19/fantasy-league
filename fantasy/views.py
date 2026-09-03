from decimal import Decimal
from collections import Counter

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.forms import UserCreationForm, PasswordChangeForm
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.db.models import Sum, Prefetch, Q, Max
from django.views.decorators.csrf import csrf_protect

from .models import (
    League, RealTeam, Player, Gameweek, 
    PlayerGameweekStat, UserFantasyTeam, UserSquad, UserProfile
)
from .forms import UserUpdateForm, ProfileUpdateForm, TeamNameUpdateForm


# ==========================================
# 0. الدوال المساعدة وحساب النقاط والأسعار والإيقافات
# ==========================================

def check_gameweek_lock(active_league):
    """دالة مساعدة لحماية التعديل وإغلاق الجولة"""
    current_gw = Gameweek.objects.filter(league=active_league, is_finished=False).order_by('number').first()
    if not current_gw:
        current_gw = Gameweek.objects.filter(league=active_league).order_by('-number').first()

    if not current_gw:
        return None, "لا توجد جولة حالية للتعديل."

    return current_gw, None


def process_gameweek_suspensions(active_league):
    """تحديث الإيقافات لكل اللاعبين الموقوفين في الدوري عند نهاية الجولة"""
    suspended_players = Player.objects.filter(team__league=active_league, is_suspended=True)
    for player in suspended_players:
        player.process_gameweek_suspension()


def calculate_and_save_squad_points(gameweek):
    """دالة لحساب نقاط كافة التشكيلات لجولة معينة بدعم الكابتن ونائب الكابتن وتحديث نقاط الفريق"""
    stats = PlayerGameweekStat.objects.filter(gameweek=gameweek)
    player_points = {stat.player_id: stat.points for stat in stats}
    
    # 🟢 الاعتماد المباشر على حقل played=True لمعرفة من شارك فعلياً
    played_players = set(stats.filter(played=True).values_list('player_id', flat=True))

    squads = UserSquad.objects.filter(gameweek=gameweek).prefetch_related('starting_players')

    for squad in squads:
        gw_points = 0
        captain_id = squad.captain_id
        vice_captain_id = squad.vice_captain_id

        # التحقق مما إذا كان الكابتن شارك بالفعل أم لا
        captain_played = captain_id in played_players if captain_id else False
        
        # تحديد من سيأخذ مضاعفة النقاط (x2)
        effective_captain_id = captain_id if captain_played else (vice_captain_id if vice_captain_id in played_players else None)

        for player in squad.starting_players.all():
            pts = player_points.get(player.id, 0)

            if effective_captain_id and player.id == effective_captain_id:
                pts *= 2

            gw_points += pts

        transfers_cost = getattr(squad, 'transfers_cost', 0) or 0
        final_gw_points = max(0, gw_points - transfers_cost)

        squad.points_earned = final_gw_points
        squad.save()

        # اعادة تجميع إجمالي النقاط للجولات المنشورة فقط
        total_pts = UserSquad.objects.filter(
            user_team=squad.user_team,
            gameweek__is_published=True
        ).aggregate(total=Sum('points_earned'))['total'] or 0

        squad.user_team.total_points = total_pts
        squad.user_team.save()


def update_player_prices_for_gameweek(gameweek):
    """تحديث أسعار اللاعبين تلقائياً بناءً على النقاط المسجلة"""
    stats = PlayerGameweekStat.objects.filter(gameweek=gameweek)

    for stat in stats:
        player = stat.player
        pts = stat.points
        old_price = player.price

        if pts >= 10:
            player.price += Decimal('0.2')
        elif pts >= 6:
            player.price += Decimal('0.1')
        elif pts < 2:
            player.price -= Decimal('0.1')

        if player.price < Decimal('3.5'):
            player.price = Decimal('3.5')

        if old_price != player.price:
            player.save(update_fields=['price'])


def get_squad_builder_context(request, user_team, active_league, current_gameweek):
    """
    دالة مساعدة مجمعة لبناء السياق (Context) لصفحة التشكيلة 
    وحساب النقاط الإجمالية TOT لكل اللاعبين المتاحين في سوق الانتقالات
    """
    squad = UserSquad.objects.prefetch_related(
        'starting_players__team',
        'substitutes__team'
    ).filter(user_team=user_team, gameweek=current_gameweek).first()

    if not squad:
        squad = UserSquad.objects.create(user_team=user_team, gameweek=current_gameweek)

        prev_squad = UserSquad.objects.filter(
            user_team=user_team,
            gameweek__number__lt=current_gameweek.number
        ).order_by('-gameweek__number').first()

        if prev_squad:
            squad.starting_players.set(prev_squad.starting_players.all())
            squad.substitutes.set(prev_squad.substitutes.all())
            squad.captain = prev_squad.captain
            squad.vice_captain = prev_squad.vice_captain
            squad.save()

    all_squad_players = list(squad.starting_players.all()) + list(squad.substitutes.all())

    stats = PlayerGameweekStat.objects.filter(gameweek=current_gameweek)
    player_stats_map = {stat.player_id: stat for stat in stats}

    total_stats = PlayerGameweekStat.objects.filter(
        gameweek__league=active_league,
        gameweek__is_published=True
    ).values('player_id').annotate(total=Sum('points'))

    player_total_points = {stat['player_id']: stat['total'] for stat in total_stats}

    def attach_status_info(player):
        stat = player_stats_map.get(player.id)
        # ✅ تم التعديل للاعتماد على الاسم المفرد بحسب الموديل
        player.yellow_card = stat.yellow_card if stat else False
        player.red_card = stat.red_card if stat else False
        player.is_suspended_now = player.is_suspended and player.suspended_matches_left > 0
        player.total_pts = player_total_points.get(player.id, 0)

    starters_list = []
    gk_list, def_list, mid_list, fwd_list = [], [], [], []

    for player in squad.starting_players.all():
        stat = player_stats_map.get(player.id)
        pts = stat.points if stat else 0
        player.current_pts = pts * 2 if squad.captain_id == player.id else pts
        attach_status_info(player)
        starters_list.append(player)

        pos = getattr(player, 'main_category', getattr(player, 'position', '')).upper()
        if pos == 'GK':
            gk_list.append(player)
        elif pos == 'DEF':
            def_list.append(player)
        elif pos == 'MID':
            mid_list.append(player)
        elif pos == 'FWD':
            fwd_list.append(player)
        else:
            mid_list.append(player)

    subs_list = []
    for player in squad.substitutes.all():
        stat = player_stats_map.get(player.id)
        player.current_pts = stat.points if stat else 0
        attach_status_info(player)
        subs_list.append(player)

    real_teams = RealTeam.objects.filter(league=active_league)
    squad_player_ids = set([p.id for p in all_squad_players])

    team_counts = Counter([p.team_id for p in all_squad_players])
    disabled_team_ids = set([team_id for team_id, count in team_counts.items() if count >= 2])

    selected_team_id = request.GET.get('team', '').strip()
    selected_position = request.GET.get('position', '').strip()
    search_query = request.GET.get('search', '').strip()

    available_players = Player.objects.filter(team__league=active_league).select_related('team')

    if search_query:
        available_players = available_players.filter(name__icontains=search_query)

    if selected_position:
        pos_map = {
            'GK': ['GK'],
            'DEF': ['CB', 'RB', 'LB', 'RWB', 'LWB'],
            'MID': ['CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW'],
            'FWD': ['ST', 'CF']
        }

        if selected_position in pos_map:
            available_players = available_players.filter(position__in=pos_map[selected_position])
        else:
            available_players = available_players.filter(position=selected_position)

    if selected_team_id and selected_team_id.isdigit():
        available_players = available_players.filter(team_id=int(selected_team_id))

    for player in available_players:
        attach_status_info(player)

    return {
        'user_team': user_team,
        'current_league': active_league,
        'gameweek': current_gameweek,
        'squad': squad,
        'starters': starters_list,
        'subs': subs_list,
        'gk_list': gk_list,
        'def_list': def_list,
        'mid_list': mid_list,
        'fwd_list': fwd_list,
        'real_teams': real_teams,
        'available_players': available_players,
        'disabled_team_ids': disabled_team_ids,
        'squad_player_ids': squad_player_ids,
        'selected_team_id': selected_team_id,
        'selected_position': selected_position,
        'search_query': search_query,
    }


# ==========================================
# 1. مركز البطولات والانضمام (Leagues Hub)
# ==========================================

@login_required
def leagues_hub(request):
    user_teams = UserFantasyTeam.objects.filter(user=request.user)
    joined_league_ids = user_teams.values_list('league_id', flat=True)

    available_leagues = League.objects.filter(is_active=True, is_public=True).exclude(id__in=joined_league_ids)
    my_teams = user_teams.select_related('league')

    context = {
        'available_leagues': available_leagues,
        'my_teams': my_teams,
    }
    return render(request, 'fantasy/leagues_hub.html', context)


@login_required
def join_league(request, league_id=None):
    if request.method == "POST":
        code = request.POST.get('league_code', '').strip()
        league = League.objects.filter(code=code, is_active=True).first()
        if not league:
            messages.error(request, "كود البطولة غير صحيح أو غير مفعل!")
            return redirect('leagues_hub')
    else:
        league = get_object_or_404(League, id=league_id, is_active=True)

    user_team, created = UserFantasyTeam.objects.get_or_create(
        user=request.user,
        league=league,
        defaults={'name': f"فريق {request.user.username}"}
    )

    if created:
        messages.success(request, f"تم الانضمام لبطولة {league.name} بنجاح!")
    else:
        messages.info(request, f"أنت مشترك بالفعل في بطولة {league.name}.")

    return redirect(f'/squad-builder/?league_id={league.id}')


# ==========================================
# 2. بناء التشكيلة (Squad Builder)
# ==========================================

@login_required
def squad_builder(request):
    user_teams = UserFantasyTeam.objects.filter(user=request.user)

    if not user_teams.exists():
        return redirect('leagues_hub')

    league_id = request.GET.get('league_id') or request.GET.get('league_id_input') or request.session.get('active_league_id')

    if league_id:
        user_team = user_teams.filter(league_id=league_id).first()
        if not user_team:
            user_team = user_teams.first()
    else:
        user_team = user_teams.first()

    request.session['active_league_id'] = user_team.league_id
    active_league = user_team.league
    current_gameweek, lock_error = check_gameweek_lock(active_league)

    if lock_error or not current_gameweek:
        context = {
            'user_team': user_team,
            'current_league': active_league,
            'lock_message': lock_error or "لا توجد جولة حالية.",
        }
        return render(request, 'fantasy/no_gameweek.html', context)

    context = get_squad_builder_context(request, user_team, active_league, current_gameweek)

    if request.headers.get('HX-Request'):
        return render(request, 'fantasy/partials/player_list.html', context)

    return render(request, 'fantasy/squad.html', context)


# ==========================================
# 3. عمليات التشكيلة (إضافة، حذف، حفظ، تبديل، كابتن، نائب كابتن)
# ==========================================

@login_required
def add_player_to_squad(request, player_id):
    """إضافة لاعب جديد للفريق وإدارة الميزانية بدون ضرب معادلات الأسعار"""
    player = get_object_or_404(Player, id=player_id)
    active_league = player.team.league

    current_gameweek, lock_error = check_gameweek_lock(active_league)
    if lock_error or not current_gameweek or not getattr(current_gameweek, 'is_open', False):
        if request.headers.get('HX-Request'):
            return HttpResponse("التعديل مغلق لهذه الجولة حالياً!", status=400)
        messages.error(request, "التعديل مغلق لهذه الجولة حالياً!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    user_team = get_object_or_404(UserFantasyTeam, user=request.user, league=active_league)
    squad, _ = UserSquad.objects.get_or_create(user_team=user_team, gameweek=current_gameweek)

    current_starters = list(squad.starting_players.all())
    current_subs = list(squad.substitutes.all())
    all_current_players = current_starters + current_subs

    if player in all_current_players:
        if request.headers.get('HX-Request'):
            return HttpResponse("اللاعب موجود بالفعل في تشكيلتك!", status=400)
        messages.error(request, "اللاعب موجود بالفعل في تشكيلتك!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    if len(all_current_players) >= 8:
        if request.headers.get('HX-Request'):
            return HttpResponse("التشكيلة مكتملة (8 لاعبين)! لا يمكنك إضافة المزيد.", status=400)
        messages.error(request, "التشكيلة مكتملة (8 لاعبين)! لا يمكنك إضافة المزيد.")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    team_player_count = sum(1 for p in all_current_players if p.team_id == player.team_id)
    if team_player_count >= 2:
        if request.headers.get('HX-Request'):
            return HttpResponse("لا يمكنك اختيار أكثر من 2 لاعبين من نفس الفريق!", status=400)
        messages.error(request, "لا يمكنك اختيار أكثر من 2 لاعبين من نفس الفريق!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    if user_team.budget < player.price:
        if request.headers.get('HX-Request'):
            return HttpResponse("الميزانية المتبقية لا تكفي لشراء هذا اللاعب!", status=400)
        messages.error(request, "الميزانية المتبقية لا تكفي لشراء هذا اللاعب!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    if len(current_starters) < 6:
        squad.starting_players.add(player)
        if not squad.captain:
            squad.captain = player
        elif not squad.vice_captain and squad.captain != player:
            squad.vice_captain = player
    else:
        squad.substitutes.add(player)

    squad.is_saved = False
    squad.save()

    user_team.budget -= player.price
    user_team.save()

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/squad.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
def save_squad(request):
    """حفظ التشكيلة"""
    if request.method == "POST":
        league_id = request.POST.get('league_id') or request.GET.get('league_id')
        user_team = get_object_or_404(UserFantasyTeam, user=request.user, league_id=league_id)
        current_gw, lock_error = check_gameweek_lock(user_team.league)

        if lock_error or not current_gw or not getattr(current_gw, 'is_open', False):
            messages.error(request, lock_error or "التعديل مغلق لهذه الجولة حالياً!")
            return redirect(f'/squad-builder/?league_id={user_team.league.id}')

        squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gw).first()
        if squad:
            total_players = squad.starting_players.count() + squad.substitutes.count()

            if total_players < 8:
                messages.error(request, "يجب إكمال التشكيلة (8 لاعبين: 6 أساسيين و 2 احتياطي) قبل الحفظ!")
            else:
                squad.is_saved = True
                squad.save()
                messages.success(request, "تم حفظ تشكيلتك وتثبيتها بنجاح لهذه الجولة!")

        return redirect(f'/squad-builder/?league_id={user_team.league.id}')

    return redirect('squad_builder')


@login_required
def remove_player_from_squad(request, player_id):
    """إزالة لاعب وإعادة ثمنه المباشر للميزانية بدون إفساد ميزانية بقية اللاعبين"""
    player = get_object_or_404(Player, id=player_id)
    active_league = player.team.league

    current_gameweek, lock_error = check_gameweek_lock(active_league)
    if lock_error or not current_gameweek or not getattr(current_gameweek, 'is_open', False):
        if request.headers.get('HX-Request'):
            return HttpResponse("التعديل مغلق لهذه الجولة حالياً!", status=400)
        messages.error(request, "التعديل مغلق لهذه الجولة حالياً!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    user_team = get_object_or_404(UserFantasyTeam, user=request.user, league=active_league)
    squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gameweek).first()

    if squad:
        removed = False
        if squad.starting_players.filter(id=player.id).exists():
            squad.starting_players.remove(player)
            removed = True
        elif squad.substitutes.filter(id=player.id).exists():
            squad.substitutes.remove(player)
            removed = True

        if removed:
            if squad.captain == player:
                squad.captain = squad.starting_players.first()
            if squad.vice_captain == player:
                squad.vice_captain = None

            squad.save()

            user_team.budget += player.price
            if user_team.budget > Decimal('100.0'):
                user_team.budget = Decimal('100.0')
            user_team.save()

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/squad.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
def set_captain(request, player_id):
    """تحديد الكابتن"""
    player = get_object_or_404(Player, id=player_id)
    active_league = player.team.league

    current_gameweek, lock_error = check_gameweek_lock(active_league)
    if lock_error or not current_gameweek or not getattr(current_gameweek, 'is_open', False):
        if request.headers.get('HX-Request'):
            return HttpResponse("التعديل مغلق لهذه الجولة حالياً!", status=400)
        messages.error(request, "التعديل مغلق لهذه الجولة حالياً!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    user_team = get_object_or_404(UserFantasyTeam, user=request.user, league=active_league)
    squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gameweek).first()

    if squad and squad.starting_players.filter(id=player.id).exists():
        if squad.vice_captain == player:
            squad.vice_captain = squad.captain
        squad.captain = player
        squad.save()

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/squad.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
def set_vice_captain(request, player_id):
    """تحديد نائب الكابتن"""
    player = get_object_or_404(Player, id=player_id)
    active_league = player.team.league

    current_gameweek, lock_error = check_gameweek_lock(active_league)
    if lock_error or not current_gameweek or not getattr(current_gameweek, 'is_open', False):
        if request.headers.get('HX-Request'):
            return HttpResponse("التعديل مغلق لهذه الجولة حالياً!", status=400)
        messages.error(request, "التعديل مغلق لهذه الجولة حالياً!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    user_team = get_object_or_404(UserFantasyTeam, user=request.user, league=active_league)
    squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gameweek).first()

    if squad and squad.starting_players.filter(id=player.id).exists():
        if squad.captain != player:
            squad.vice_captain = player
            squad.save()

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/squad.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
def swap_players(request, starter_id, sub_id):
    """تبديل لاعب أساسي بآخر احتياطي (مجاني بالكامل داخل التشكيلة)"""
    starter = get_object_or_404(Player, id=starter_id)
    active_league = starter.team.league

    current_gameweek, lock_error = check_gameweek_lock(active_league)
    if lock_error or not current_gameweek or not getattr(current_gameweek, 'is_open', False):
        if request.headers.get('HX-Request'):
            return HttpResponse("التعديل مغلق لهذه الجولة حالياً!", status=400)
        messages.error(request, "التعديل مغلق لهذه الجولة حالياً!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    user_team = UserFantasyTeam.objects.filter(user=request.user, league=active_league).first()
    squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gameweek).first()

    if squad:
        sub = get_object_or_404(Player, id=sub_id)

        if squad.starting_players.filter(id=starter.id).exists() and squad.substitutes.filter(id=sub.id).exists():
            squad.starting_players.remove(starter)
            squad.substitutes.remove(sub)

            squad.starting_players.add(sub)
            squad.substitutes.add(starter)

            if squad.captain == starter:
                squad.captain = sub
            elif squad.vice_captain == starter:
                squad.vice_captain = sub

            squad.save()
            messages.success(request, "تم تبديل المراكز داخل التشكيلة بنجاح!")

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/squad.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
def move_to_starter(request, player_id):
    player = get_object_or_404(Player, id=player_id)
    active_league = player.team.league

    current_gameweek, lock_error = check_gameweek_lock(active_league)
    if lock_error or not current_gameweek or not getattr(current_gameweek, 'is_open', False):
        if request.headers.get('HX-Request'):
            return HttpResponse("التعديل مغلق لهذه الجولة حالياً!", status=400)
        messages.error(request, "التعديل مغلق لهذه الجولة حالياً!")
        return redirect(f'/squad-builder/?league_id={active_league.id}')

    user_team = UserFantasyTeam.objects.filter(user=request.user, league=active_league).first()
    squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gameweek).first()

    if squad and squad.starting_players.count() < 6 and squad.substitutes.filter(id=player.id).exists():
        squad.substitutes.remove(player)
        squad.starting_players.add(player)
        squad.save()

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/squad.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


# ==========================================
# 4. جدول الترتيب (Leaderboard)
# ==========================================

def leaderboard(request):
    league_id = request.GET.get('league_id')
    all_leagues = League.objects.filter(is_active=True)

    if league_id:
        active_league = all_leagues.filter(id=league_id).first()
    else:
        active_league = all_leagues.first()

    if not active_league:
        context = {
            'teams': [],
            'active_league': None,
            'all_leagues': all_leagues,
            'current_gameweek': None,
        }
        return render(request, 'fantasy/leaderboard.html', context)

    all_gameweeks = list(Gameweek.objects.filter(league=active_league).order_by('number'))
    current_gameweek = next((gw for gw in reversed(all_gameweeks) if not getattr(gw, 'is_finished', False)), None)
    if not current_gameweek and all_gameweeks:
        current_gameweek = all_gameweeks[-1]

    if request.user.is_staff:
        stats = PlayerGameweekStat.objects.filter(gameweek__league=active_league)
    else:
        stats = PlayerGameweekStat.objects.filter(gameweek__league=active_league, gameweek__is_published=True)

    stats_dict = {(stat.gameweek_id, stat.player_id): stat.points for stat in stats}
    
    # 🟢 الاعتماد على حقل played=True عند فحص مشاركة الكابتن في جدول الترتيب
    played_players_set = set(
        stats.filter(played=True).values_list('gameweek_id', 'player_id')
    )

    teams = UserFantasyTeam.objects.filter(league=active_league).select_related('user').prefetch_related(
        Prefetch(
            'squads',
            queryset=UserSquad.objects.prefetch_related('starting_players').select_related('gameweek'),
            to_attr='fetched_squads'
        )
    )

    teams_list = []

    for team in teams:
        total_pts = 0
        current_pts = 0

        squads_by_gw = {s.gameweek_id: s for s in getattr(team, 'fetched_squads', [])}
        sorted_squads = sorted(getattr(team, 'fetched_squads', []), key=lambda s: s.gameweek.number)

        for gw in all_gameweeks:
            if not gw.is_published and not request.user.is_staff:
                continue

            squad = squads_by_gw.get(gw.id)

            if not squad:
                squad = next((s for s in reversed(sorted_squads) if s.gameweek.number < gw.number), None)

            if not squad and sorted_squads:
                squad = sorted_squads[0]

            if squad:
                gw_pts = 0
                captain_id = squad.captain_id
                vice_captain_id = squad.vice_captain_id

                captain_played = (gw.id, captain_id) in played_players_set if captain_id else False
                effective_captain_id = captain_id if captain_played else (vice_captain_id if (gw.id, vice_captain_id) in played_players_set else None)

                for player in squad.starting_players.all():
                    pts = stats_dict.get((gw.id, player.id), 0)
                    if effective_captain_id and player.id == effective_captain_id:
                        pts *= 2
                    gw_pts += pts

                transfers_cost = getattr(squad, 'transfers_cost', 0) or 0
                gw_pts = max(0, gw_pts - transfers_cost)

                total_pts += gw_pts

                if current_gameweek and gw.id == current_gameweek.id:
                    current_pts = gw_pts

        team.total_points = total_pts
        team.current_gw_points = current_pts
        team.is_current_user = (request.user.is_authenticated and team.user_id == request.user.id)

        teams_list.append(team)

    teams_list.sort(key=lambda x: x.total_points, reverse=True)

    context = {
        'teams': teams_list,
        'active_league': active_league,
        'all_leagues': all_leagues,
        'current_gameweek': current_gameweek,
    }

    return render(request, 'fantasy/leaderboard.html', context)


# ==========================================
# 5. إدارة الجولات (لوحة الأدمن)
# ==========================================

@staff_member_required
def get_player_previous_yellow_cards(request):
    """API لجلب عدد الكروت الصفراء السابقة للاعب قبل الجولة المحددة لتنبيه الأدمن"""
    player_id = request.GET.get('player_id')
    gameweek_id = request.GET.get('gameweek_id')
    
    if not player_id or not gameweek_id:
        return JsonResponse({'previous_yellows': 0, 'has_yellow_before': False})

    current_stat = PlayerGameweekStat.objects.filter(id=gameweek_id).first() if str(gameweek_id).isdigit() else None
    
    query = PlayerGameweekStat.objects.filter(player_id=player_id)
    if current_stat:
        query = query.filter(gameweek__number__lt=current_stat.gameweek.number)

    # ✅ تم التعديل للفحص المباشر للقيمة البوليانية yellow_card=True
    previous_yellows = query.filter(yellow_card=True).count()

    return JsonResponse({
        'previous_yellows': previous_yellows,
        'has_yellow_before': previous_yellows > 0
    })


@staff_member_required
def close_current_gameweek(request):
    if request.method == "POST":
        league_id = request.POST.get('league_id')
        active_league = League.objects.filter(id=league_id, is_active=True).first() if league_id else League.objects.filter(is_active=True).first()

        if not active_league:
            messages.error(request, "لا يوجد دوري نشط حالياً!")
            return redirect('leaderboard')

        current_gw = Gameweek.objects.filter(league=active_league, is_finished=False).order_by('number').first()

        if current_gw:
            current_gw.is_open = False
            current_gw.is_finished = True
            current_gw.save(update_fields=['is_open', 'is_finished'])

            messages.success(request, f"تم إغلاق الجولة {current_gw.number} وتحديدها كانتهاء لتظهر في الأرشيف!")
        else:
            messages.warning(request, "لا توجد جولة مفتوحة حالياً لإغلاقها!")

        return redirect(f"/leaderboard/?league_id={active_league.id}")

    return redirect('leaderboard')


@staff_member_required
def open_next_gameweek(request):
    if request.method == "POST":
        league_id = request.POST.get('league_id')
        active_league = League.objects.filter(id=league_id, is_active=True).first() if league_id else League.objects.filter(is_active=True).first()

        if not active_league:
            messages.error(request, "لا يوجد دوري نشط حالياً!")
            return redirect('leaderboard')

        current_gw = Gameweek.objects.filter(league=active_league, is_finished=False).order_by('number').first()

        if current_gw and current_gw.is_open:
            messages.warning(request, f"الجولة {current_gw.number} مفتوحة بالفعل!")
            return redirect(f"/leaderboard/?league_id={active_league.id}")

        if current_gw and not current_gw.is_open:
            current_gw.is_open = True
            current_gw.save(update_fields=['is_open'])
            messages.success(request, f"تم إعادة فتح الجولة {current_gw.number} لتعديل التشكيلات!")
        else:
            last_gw = Gameweek.objects.filter(league=active_league).order_by('-number').first()
            next_number = (last_gw.number + 1) if last_gw else 1

            new_gw = Gameweek.objects.create(
                league=active_league,
                number=next_number,
                is_open=True,
                is_finished=False
            )
            messages.success(request, f"تم فتح الجولة {new_gw.number} بنجاح لبدء التعديلات!")

        return redirect(f"/leaderboard/?league_id={active_league.id}")

    return redirect('leaderboard')


@staff_member_required
def close_and_advance_gameweek(request):
    if request.method == "POST":
        league_id = request.POST.get('league_id')
        active_league = League.objects.filter(id=league_id, is_active=True).first() if league_id else League.objects.filter(is_active=True).first()

        if not active_league:
            messages.error(request, "لا يوجد دوري نشط حالياً!")
            return redirect('leaderboard')

        current_gw = Gameweek.objects.filter(league=active_league, is_finished=False).order_by('number').first()
        if current_gw:
            current_gw.is_open = False
            current_gw.is_finished = True
            current_gw.is_published = False
            current_gw.save(update_fields=['is_open', 'is_finished', 'is_published'])

            process_gameweek_suspensions(active_league)

        last_gw = Gameweek.objects.filter(league=active_league).order_by('-number').first()
        next_number = (last_gw.number + 1) if last_gw else 1

        new_gw = Gameweek.objects.create(
            league=active_league,
            number=next_number,
            is_open=True,
            is_finished=False,
            is_published=False
        )

        messages.success(request, f"تم إنهاء الجولة وخصم مباريات الإيقاف وفتح الجولة {new_gw.number}!")
        return redirect(f"/leaderboard/?league_id={active_league.id}")

    return redirect('leaderboard')


@staff_member_required
def publish_gameweek_stats(request, gw_id):
    if request.method == "POST":
        gameweek = get_object_or_404(Gameweek, id=gw_id)

        calculate_and_save_squad_points(gameweek)
        update_player_prices_for_gameweek(gameweek)

        gameweek.is_published = True
        gameweek.save(update_fields=['is_published'])

        messages.success(request, f"تم اعتماد ونشر إحصائيات ونقاط الجولة {gameweek.number} بنجاح!")
        return redirect(request.META.get('HTTP_REFERER', 'leagues_history'))

    return redirect('leagues_history')


# ==========================================
# 6. الحساب الشخصي والسجل والمصادقة
# ==========================================

class CustomLoginView(LoginView):
    template_name = 'fantasy/login.html'
    redirect_authenticated_user = True


@csrf_protect
def register(request):
    if request.user.is_authenticated:
        return redirect('squad_builder')

    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('squad_builder')
    else:
        form = UserCreationForm()

    return render(request, 'fantasy/register.html', {'form': form})


@login_required
def profile_view(request):
    user = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)

    user_teams = UserFantasyTeam.objects.filter(user=user).select_related('league')

    teams_stats = []
    total_points_all_leagues = 0

    for team in user_teams:
        total_pts = UserSquad.objects.filter(
            user_team=team, 
            gameweek__is_published=True
        ).aggregate(total=Sum('points_earned'))['total'] or 0

        total_points_all_leagues += total_pts

        latest_published_gw = Gameweek.objects.filter(league=team.league, is_published=True).order_by('-number').first()
        current_gw_pts = 0
        if latest_published_gw:
            squad = UserSquad.objects.filter(user_team=team, gameweek=latest_published_gw).first()
            if squad:
                current_gw_pts = squad.points_earned

        teams_stats.append({
            'team': team,
            'total_points': total_pts,
            'current_gw_points': current_gw_pts,
            'latest_gw_number': latest_published_gw.number if latest_published_gw else None,
        })

    u_form = UserUpdateForm(instance=user)
    p_form = ProfileUpdateForm(instance=profile)
    pass_form = PasswordChangeForm(user)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_profile':
            u_form = UserUpdateForm(request.POST, instance=user)
            p_form = ProfileUpdateForm(request.POST, request.FILES, instance=profile)

            # معالجة رفع الصورة مباشرة وضمان حفظها في Cloudinary
            if 'avatar' in request.FILES:
                profile.avatar = request.FILES['avatar']
                profile.save()

            if u_form.is_valid() and p_form.is_valid():
                u_form.save()
                p_form.save()
                messages.success(request, 'تم تحديث البيانات الشخصية والصورة بنجاح!')
                return redirect('profile')
            else:
                # في حال وجود خطأ في الفاليديشن للبيانات النصية، تظل الصورة محفوظة
                if 'avatar' in request.FILES:
                    messages.success(request, 'تم تحديث الصورة بنجاح!')
                    return redirect('profile')

        elif action == 'change_password':
            pass_form = PasswordChangeForm(user, request.POST)
            if pass_form.is_valid():
                user = pass_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'تم تغيير كلمة المرور بنجاح!')
                return redirect('profile')
            else:
                error_messages = []
                for field, errors in pass_form.errors.items():
                    for error in errors:
                        error_messages.append(error)

                custom_error_msg = (
                    "عفواً، كلمة المرور غير قوية بما يكفي!\n"
                    f"الأخطاء بالتفصيل: {' | '.join(error_messages)}"
                )
                messages.error(request, custom_error_msg)

        elif action == 'update_team_name':
            team_id = request.POST.get('team_id')
            team = get_object_or_404(UserFantasyTeam, id=team_id, user=user)
            t_form = TeamNameUpdateForm(request.POST, instance=team)
            if t_form.is_valid():
                t_form.save()
                messages.success(request, f'تم تعديل اسم الفريق في بطولة {team.league.name} بنجاح!')
                return redirect('profile')

    context = {
        'u_form': u_form,
        'p_form': p_form,
        'pass_form': pass_form,
        'teams_stats': teams_stats,
        'total_points_all_leagues': total_points_all_leagues,
        'leagues_count': user_teams.count(),
    }
    return render(request, 'profile.html', context)



@login_required
def gameweeks_history_view(request):
    leagues_data = []
    user_teams = UserFantasyTeam.objects.filter(user=request.user)

    for team in user_teams:
        closed_gameweeks = Gameweek.objects.filter(
            league=team.league,
            is_finished=True
        ).order_by('number')

        gw_list = []
        highest_pts = 0

        published_squads = UserSquad.objects.filter(
            user_team=team,
            gameweek__is_finished=True,
            gameweek__is_published=True
        )
        if published_squads.exists():
            highest_pts = max([s.points_earned for s in published_squads], default=0)

        for gw in closed_gameweeks:
            squad = UserSquad.objects.filter(user_team=team, gameweek=gw).first()
            has_stats = PlayerGameweekStat.objects.filter(gameweek=gw).exists()

            pts = squad.points_earned if squad else 0

            gw_list.append({
                'id': gw.id,  
                'number': gw.number,
                'is_published': gw.is_published,
                'points': pts if gw.is_published else None,
                'is_highest': (pts == highest_pts and highest_pts > 0 and gw.is_published),
                'date': getattr(gw, 'created_at', None),
                'has_stats': has_stats,
            })

        leagues_data.append({
            'league': team.league,
            'team': team,
            'total_points': team.total_points,
            'gameweeks': gw_list,
        })

    return render(request, 'leagues_history.html', {'leagues_data': leagues_data})


@login_required
def view_closed_squad(request, gw_id):
    gameweek = get_object_or_404(Gameweek, id=gw_id)
    user_team = UserFantasyTeam.objects.filter(user=request.user, league=gameweek.league).first()

    squad = None
    starters_list = []
    bench_list = []

    if user_team:
        squad = UserSquad.objects.filter(user_team=user_team, gameweek=gameweek).first()

    show_points = gameweek.is_published or request.user.is_staff

    if squad:
        stats = PlayerGameweekStat.objects.filter(gameweek=gameweek)
        stats_dict = {st.player_id: st.points for st in stats}
        
        # 🟢 الاعتماد على played=True لعرض حالة الكابتن المباشر/البديل في الشاشة المغلظة
        played_players_set = set(stats.filter(played=True).values_list('player_id', flat=True))

        captain_id = squad.captain_id
        vice_captain_id = squad.vice_captain_id
        captain_played = captain_id in played_players_set if captain_id else False
        effective_captain_id = captain_id if captain_played else (vice_captain_id if vice_captain_id in played_players_set else None)

        for player in squad.starting_players.all():
            base_pts = stats_dict.get(player.id, 0)
            is_captain = (squad.captain_id == player.id)
            is_vice = (squad.vice_captain_id == player.id)
            is_effective = (effective_captain_id == player.id)

            final_pts = base_pts * 2 if is_effective else base_pts

            starters_list.append({
                'id': player.id,
                'name': player.name,
                'position_display': player.get_position_display(),
                'club_name': player.team.name if hasattr(player, 'team') else '',
                'points': final_pts if show_points else "-",
                'is_captain': is_captain,
                'is_vice': is_vice,
                'is_effective_captain': is_effective,
            })

        for player in squad.substitutes.all():
            bench_list.append({
                'id': player.id,
                'name': player.name,
                'position_display': player.get_position_display(),
                'points': stats_dict.get(player.id, 0) if show_points else "-",
            })

    context = {
        'gameweek': gameweek,
        'squad': squad,
        'starters_list': starters_list,
        'bench_players': bench_list,
        'show_points': show_points,
    }
    return render(request, 'closed_squad_view.html', context)
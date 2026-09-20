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
from django.db.models import Sum, Prefetch, Q, Count, Max
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.template.loader import render_to_string
from django.core.paginator import Paginator
from django_ratelimit.decorators import ratelimit
from django.utils.decorators import method_decorator
from django.db.models.functions import Coalesce

from .models import (
    League, RealTeam, Player, Gameweek, Match,
    PlayerGameweekStat, UserFantasyTeam, UserSquad, UserProfile
)

# جلب الموديلات بأمان في حال وجود PlayerStatusUpdate أو LeaguePrize أو Award
try:
    from .models import PlayerStatusUpdate, LeaguePrize, Award
except ImportError:
    PlayerStatusUpdate = None
    LeaguePrize = None
    Award = None

from .forms import UserUpdateForm, ProfileUpdateForm, TeamNameUpdateForm


# ==========================================
# 0. الدوال المساعدة وحساب النقاط والأسعار والإيقافات
# ==========================================

def check_gameweek_lock(active_league):
    """دالة مساعدة لحماية التعديل وإغلاق الجولة"""
    if not active_league:
        return None, "لم يتم تحديد بطولة صالحة."

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
        if hasattr(player, 'process_gameweek_suspension'):
            player.process_gameweek_suspension()


def calculate_and_save_squad_points(gameweek):
    """دالة لحساب نقاط كافة التشكيلات لجولة معينة بدعم الكابتن، نائب الكابتن، والخواص (Chips)"""
    stats = PlayerGameweekStat.objects.filter(gameweek=gameweek)
    player_points = {stat.player_id: stat.points for stat in stats}
    
    played_players = set(stats.filter(played=True).values_list('player_id', flat=True))
    squads = UserSquad.objects.filter(gameweek=gameweek).prefetch_related('starting_players', 'substitutes')

    for squad in squads:
        gw_points = 0
        captain_id = squad.captain_id
        vice_captain_id = squad.vice_captain_id
        active_chip = getattr(squad, 'active_chip', 'NONE') or 'NONE'

        captain_played = captain_id in played_players if captain_id else False
        effective_captain_id = captain_id if captain_played else (vice_captain_id if vice_captain_id in played_players else None)

        # 1. نقاط الأساسيين
        for player in squad.starting_players.all():
            pts = player_points.get(player.id, 0)

            if effective_captain_id and player.id == effective_captain_id:
                multiplier = 3 if active_chip == 'TC' else 2
                pts *= multiplier

            gw_points += pts

        # 2. نقاط البدلاء في حال تفعيل Bench Boost
        if active_chip == 'BB':
            for sub_player in squad.substitutes.all():
                gw_points += player_points.get(sub_player.id, 0)

        # 3. خصم التبديلات (إلغاؤها في حال Wildcard أو Free Hit)
        transfers_cost = getattr(squad, 'transfers_cost', 0) or 0
        if active_chip in ['WC', 'FH']:
            transfers_cost = 0

        final_gw_points = max(0, gw_points - transfers_cost)

        squad.points_earned = final_gw_points
        squad.save()

        # 4. تثبيت استهلاك الخواص في حساب المستخدم
        user_team = squad.user_team
        if active_chip == 'TC':
            user_team.triple_captain_used = True
        elif active_chip == 'BB':
            user_team.bench_boost_used = True
        elif active_chip == 'WC':
            user_team.wildcard_used = True
        elif active_chip == 'FH':
            user_team.free_hit_used = True

        total_pts = UserSquad.objects.filter(
            user_team=user_team,
            gameweek__is_published=True
        ).aggregate(total=Sum('points_earned'))['total'] or 0

        user_team.total_points = total_pts
        user_team.save()


def update_player_prices_for_gameweek(gameweek):
    """تحديث أسعار اللاعبين تلقائياً بناءً على النقاط المسجلة"""
    stats = PlayerGameweekStat.objects.filter(gameweek=gameweek).select_related('player')

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
    دالة عالية الأداء ومحسّنة لبناء Context التشكيلة واستجابة سريعة جداً.
    """
    player_queryset = Player.objects.select_related('team')
    
    squad = UserSquad.objects.select_related('captain', 'vice_captain').prefetch_related(
        Prefetch('starting_players', queryset=player_queryset),
        Prefetch('substitutes', queryset=player_queryset)
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

    all_starters = list(squad.starting_players.all())
    all_subs = list(squad.substitutes.all())
    all_squad_players = all_starters + all_subs
    squad_player_ids = {p.id for p in all_squad_players}

    squad_ids_list = [p.id for p in all_squad_players]
    current_stats = PlayerGameweekStat.objects.filter(
        gameweek=current_gameweek,
        player_id__in=squad_ids_list
    )
    player_stats_map = {stat.player_id: stat for stat in current_stats}

    def attach_status_info(player):
        stat = player_stats_map.get(player.id)
        player.yellow_card = stat.yellow_card if stat else False
        player.red_card = stat.red_card if stat else False
        player.is_suspended_now = player.is_suspended and getattr(player, 'suspended_matches_left', 0) > 0
        player.stat_points = stat.points if stat else 0

    starters_list = []
    gk_list, def_list, mid_list, fwd_list = [], [], [], []

    for player in all_starters:
        attach_status_info(player)
        multiplier = 3 if (getattr(squad, 'active_chip', 'NONE') == 'TC' and squad.captain_id == player.id) else (2 if squad.captain_id == player.id else 1)
        player.current_pts = player.stat_points * multiplier
        starters_list.append(player)

        pos = (getattr(player, 'main_category', None) or getattr(player, 'position', '') or '').upper()
        if pos == 'GK':
            gk_list.append(player)
        elif pos in ['DEF', 'CB', 'RB', 'LB', 'RWB', 'LWB']:
            def_list.append(player)
        elif pos in ['MID', 'CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW']:
            mid_list.append(player)
        else:
            fwd_list.append(player)

    subs_list = []
    for player in all_subs:
        attach_status_info(player)
        player.current_pts = player.stat_points
        subs_list.append(player)

    real_teams = RealTeam.objects.filter(league=active_league).only('id', 'name', 'logo')

    team_counts = Counter([p.team_id for p in all_squad_players])
    disabled_team_ids = {team_id for team_id, count in team_counts.items() if count >= 2}

    selected_team_id = request.GET.get('team', '').strip()
    selected_position = request.GET.get('position', '').strip()
    search_query = request.GET.get('search', '').strip()

    available_players = Player.objects.filter(team__league=active_league).select_related('team')

    if search_query:
        available_players = available_players.filter(name__icontains=search_query)

    if selected_position:
        pos_map = {
            'GK': ['GK'],
            'DEF': ['CB', 'RB', 'LB', 'RWB', 'LWB', 'DEF'],
            'MID': ['CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW', 'MID'],
            'FWD': ['ST', 'CF', 'FWD']
        }
        if selected_position in pos_map:
            available_players = available_players.filter(position__in=pos_map[selected_position])

    if selected_team_id and selected_team_id.isdigit():
        available_players = available_players.filter(team_id=int(selected_team_id))

    deadline = getattr(current_gameweek, 'deadline', None)
    time_remaining = (deadline - timezone.now()) if deadline and deadline > timezone.now() else None

    season_stats = UserSquad.objects.filter(user_team=user_team, gameweek__is_published=True).aggregate(
        total_points=Sum('points_earned')
    )

    return {
        'user_team': user_team,
        'current_league': active_league,
        'gameweek': current_gameweek,
        'deadline': deadline,
        'time_remaining': time_remaining,
        'squad': squad,
        'starters': starters_list,
        'subs': subs_list,
        'gk_list': gk_list,
        'def_list': def_list,
        'mid_list': mid_list,
        'fwd_list': fwd_list,
        'real_teams': real_teams,
        'available_players': available_players[:40],
        'disabled_team_ids': disabled_team_ids,
        'squad_player_ids': squad_player_ids,
        'selected_team_id': selected_team_id,
        'selected_position': selected_position,
        'search_query': search_query,
        'season_stats': season_stats,
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
@ratelimit(key='ip', rate='30/m', block=True)
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

    if request.headers.get('HX-Request') and request.GET.get('target') == 'market':
        return render(request, 'fantasy/partials/player_list.html', context)

    return render(request, 'fantasy/squad.html', context)

# ==========================================
# 3. عمليات التشكيلة (إضافة، حذف، حفظ، تبديل، كابتن، نائب كابتن)
# ==========================================

def render_htmx_squad_response(request, user_team, active_league, current_gameweek):
    """
    دالة موحدة لإرجاع استجابة HTMX تحدّث التشكيلة وسوق اللاعبين والميزانية في نفس الوقت
    """
    context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
    
    squad_html = render_to_string('fantasy/squad.html', context, request=request)
    player_list_html = render_to_string('fantasy/partials/player_list.html', context, request=request)
    
    combined_response = f"""
    {squad_html}
    <span id="user-budget" hx-swap-oob="true">{user_team.budget}M</span>
    <div id="player-market-list" hx-swap-oob="true">
        {player_list_html}
    </div>
    """
    return HttpResponse(combined_response)


@login_required
@ratelimit(key='ip', rate='20/m', block=True)
def remove_player_from_squad(request, player_id):
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

            squad.is_saved = False
            squad.save()

            user_team.budget += player.price
            user_team.save()

    if request.headers.get('HX-Request'):
        return render_htmx_squad_response(request, user_team, active_league, current_gameweek)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
@ratelimit(key='ip', rate='20/m', block=True)
def add_player_to_squad(request, player_id):
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
        return render_htmx_squad_response(request, user_team, active_league, current_gameweek)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
@ratelimit(key='ip', rate='10/m', block=True)
def save_squad(request):
    if request.method == "POST":
        league_id = request.POST.get('league_id') or request.GET.get('league_id')
        user_team = get_object_or_404(UserFantasyTeam, user=request.user, league_id=league_id)
        current_gw, lock_error = check_gameweek_lock(user_team.league)

        if lock_error or not current_gw or not getattr(current_gw, 'is_open', False):
            messages.error(request, lock_error or "التعديل مغلق لهذه الجولة حالياً!")
            return redirect(f'/squad-builder/?league_id={user_team.league.id}')

        squad = UserSquad.objects.filter(user_team=user_team, gameweek=current_gw).first()
        if squad:
            starting_players = squad.starting_players.all()
            total_players = starting_players.count() + squad.substitutes.count()

            if total_players < 8:
                messages.error(request, "يجب إكمال التشكيلة (8 لاعبين: 6 أساسيين و 2 احتياطي) قبل الحفظ!")
            else:
                gk_count, def_count, mid_count = 0, 0, 0

                pos_map = {
                    'GK': ['GK'],
                    'DEF': ['CB', 'RB', 'LB', 'RWB', 'LWB', 'DEF'],
                    'MID': ['CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW', 'MID'],
                }

                for player in starting_players:
                    pos = getattr(player, 'main_category', getattr(player, 'position', '')).upper()
                    
                    if pos in pos_map['GK']:
                        gk_count += 1
                    elif pos in pos_map['DEF']:
                        def_count += 1
                    elif pos in pos_map['MID']:
                        mid_count += 1

                if gk_count != 1:
                    messages.error(request, "عفواً، يجب وجود حارس مرمى واحد فقط في التشكيلة الأساسية!")
                elif def_count < 1:
                    messages.error(request, "عفواً، يجب وجود مدافع واحد على الأقل في التشكيلة الأساسية!")
                elif mid_count < 1:
                    messages.error(request, "عفواً، يجب وجود لاعب خط وسط واحد على الأقل في التشكيلة الأساسية!")
                else:
                    squad.is_saved = True
                    squad.save()
                    messages.success(request, "تم حفظ تشكيلتك وتثبيتها بنجاح لهذه الجولة!")

        return redirect(f'/squad-builder/?league_id={user_team.league.id}')

    return redirect('squad_builder')


@login_required
def set_captain(request, player_id):
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
        return render(request, 'fantasy/partials/pitch.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
def set_vice_captain(request, player_id):
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
        return render(request, 'fantasy/partials/pitch.html', context)

    return redirect(f'/squad-builder/?league_id={active_league.id}')


@login_required
@ratelimit(key='ip', rate='15/m', block=True)
def swap_players(request, starter_id, sub_id):
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
            squad.refresh_from_db(fields=['substitutions_count', 'transfers_cost'])
            current_subs = squad.substitutions_count or 0

            if current_subs >= 2:
                err_msg = "عفواً! استنفذت الحد الأقصى للتبديلات لهذه الجولة (تبديلان فقط)."
                if request.headers.get('HX-Request'):
                    return HttpResponse(err_msg, status=400)
                messages.error(request, err_msg)
                return redirect(f'/squad-builder/?league_id={active_league.id}')

            squad.starting_players.remove(starter)
            squad.substitutes.remove(sub)
            squad.starting_players.add(sub)
            squad.substitutes.add(starter)

            if squad.captain == starter:
                squad.captain = sub
            elif squad.vice_captain == starter:
                squad.vice_captain = sub

            squad.substitutions_count = current_subs + 1
            squad.save()

    if request.headers.get('HX-Request'):
        context = get_squad_builder_context(request, user_team, active_league, current_gameweek)
        return render(request, 'fantasy/partials/pitch.html', context)

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

    active_league = all_leagues.filter(id=league_id).first() if league_id else all_leagues.first()

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
    played_players_set = set(stats.filter(played=True).values_list('gameweek_id', 'player_id'))

    teams = UserFantasyTeam.objects.filter(league=active_league).select_related('user').prefetch_related(
        Prefetch(
            'squads',
            queryset=UserSquad.objects.prefetch_related('starting_players', 'substitutes').select_related('gameweek'),
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
                active_chip = getattr(squad, 'active_chip', 'NONE') or 'NONE'

                captain_played = (gw.id, captain_id) in played_players_set if captain_id else False
                effective_captain_id = captain_id if captain_played else (vice_captain_id if (gw.id, vice_captain_id) in played_players_set else None)

                for player in squad.starting_players.all():
                    pts = stats_dict.get((gw.id, player.id), 0)
                    if effective_captain_id and player.id == effective_captain_id:
                        multiplier = 3 if active_chip == 'TC' else 2
                        pts *= multiplier
                    gw_pts += pts

                if active_chip == 'BB':
                    for sub_player in squad.substitutes.all():
                        gw_pts += stats_dict.get((gw.id, sub_player.id), 0)

                transfers_cost = getattr(squad, 'transfers_cost', 0) or 0
                if active_chip in ['WC', 'FH']:
                    transfers_cost = 0

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
# 5. إدارة الجولات وإحصائيات المباريات (لوحة الأدمن / Staff)
# ==========================================

@staff_member_required(login_url='login')
def enter_match_stats(request):
    leagues = League.objects.filter(is_active=True)
    selected_league_id = request.GET.get('league_id')
    selected_gameweek_id = request.GET.get('gameweek_id')
    selected_team_id = request.GET.get('team_id')

    selected_league = None
    selected_gameweek = None
    selected_team = None
    gameweeks = []
    teams = []
    players_data = []

    if selected_league_id and selected_league_id.isdigit():
        selected_league = get_object_or_404(League, id=int(selected_league_id))
        gameweeks = Gameweek.objects.filter(league=selected_league).order_by('-number')
        teams = RealTeam.objects.filter(league=selected_league)

    if selected_gameweek_id and selected_gameweek_id.isdigit():
        selected_gameweek = get_object_or_404(Gameweek, id=int(selected_gameweek_id))

    if selected_team_id and selected_team_id.isdigit() and selected_gameweek:
        selected_team = get_object_or_404(RealTeam, id=int(selected_team_id))
        players = Player.objects.filter(team=selected_team).order_by('position')

        for player in players:
            stat, _ = PlayerGameweekStat.objects.get_or_create(
                player=player,
                gameweek=selected_gameweek,
                defaults={'team': selected_team}
            )
            players_data.append({
                'player': player,
                'stat': stat
            })

    if request.method == 'POST' and selected_gameweek and selected_team:
        for item in players_data:
            player_id = str(item['player'].id)
            stat = item['stat']

            stat.played = f'played_{player_id}' in request.POST
            stat.goals = int(request.POST.get(f'goals_{player_id}', 0))
            stat.assists = int(request.POST.get(f'assists_{player_id}', 0))
            stat.clean_sheet = f'clean_sheet_{player_id}' in request.POST
            stat.yellow_card = f'yellow_card_{player_id}' in request.POST
            stat.red_card = f'red_card_{player_id}' in request.POST
            stat.penalties_taken = int(request.POST.get(f'penalties_taken_{player_id}', 0))
            stat.penalties_missed = int(request.POST.get(f'penalties_missed_{player_id}', 0))
            
            stat.suspension_reason = request.POST.get(f'suspension_reason_{player_id}', 'NONE')
            stat.suspension_matches = int(request.POST.get(f'suspension_matches_{player_id}', 0))
            stat.suspension_notes = request.POST.get(f'suspension_notes_{player_id}', '')

            stat.save()

        messages.success(request, f'تم حفظ إحصائيات فريق {selected_team.name} للجولة {selected_gameweek.number} بنجاح!')
        return redirect(f"{request.path}?league_id={selected_league_id}&gameweek_id={selected_gameweek_id}&team_id={selected_team_id}")

    context = {
        'leagues': leagues,
        'gameweeks': gameweeks,
        'teams': teams,
        'selected_league_id': int(selected_league_id) if selected_league_id and selected_league_id.isdigit() else None,
        'selected_gameweek_id': int(selected_gameweek_id) if selected_gameweek_id and selected_gameweek_id.isdigit() else None,
        'selected_team_id': int(selected_team_id) if selected_team_id and selected_team_id.isdigit() else None,
        'selected_gameweek': selected_gameweek,
        'selected_team': selected_team,
        'players_data': players_data,
        'suspension_reasons': getattr(PlayerGameweekStat, 'SUSPENSION_REASONS', []),
    }
    return render(request, 'admin_enter_stats.html', context)


@staff_member_required
def get_player_previous_yellow_cards(request):
    """إرجاع عدد البطاقات الصفراء السابقة للاعب قبل الجولة الحالية"""
    player_id = request.GET.get('player_id')
    gameweek_id = request.GET.get('gameweek_id')
    
    if not player_id or not gameweek_id or not str(player_id).isdigit() or not str(gameweek_id).isdigit():
        return JsonResponse({'previous_yellows': 0, 'has_yellow_before': False})

    target_gw = Gameweek.objects.filter(id=int(gameweek_id)).first()
    if not target_gw:
        return JsonResponse({'previous_yellows': 0, 'has_yellow_before': False})

    previous_yellows = PlayerGameweekStat.objects.filter(
        player_id=int(player_id),
        gameweek__league=target_gw.league,
        gameweek__number__lt=target_gw.number,
        yellow_card=True
    ).count()

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

        messages.success(request, f"تم خصم مباريات الإيقاف وفتح الجولة الجديدة {new_gw.number}!")
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

    @method_decorator(ratelimit(key='ip', rate='10/m', block=True))
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.session.set_expiry(600)
        return response


@csrf_protect
@ratelimit(key='ip', rate='5/m', block=True)
def register(request):
    if request.user.is_authenticated:
        return redirect('squad_builder')

    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            request.session.set_expiry(600)
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

            if 'avatar' in request.FILES:
                profile.avatar = request.FILES['avatar']
                profile.save()

            if u_form.is_valid() and p_form.is_valid():
                u_form.save()
                p_form.save()
                messages.success(request, 'تم تحديث البيانات الشخصية والصورة بنجاح!')
                return redirect('profile')
            else:
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
        stats_dict = {st.player_id: st for st in stats}
        played_players_set = set(stats.filter(played=True).values_list('player_id', flat=True))

        captain_id = squad.captain_id
        vice_captain_id = squad.vice_captain_id
        active_chip = getattr(squad, 'active_chip', 'NONE') or 'NONE'
        captain_played = captain_id in played_players_set if captain_id else False
        effective_captain_id = captain_id if captain_played else (vice_captain_id if vice_captain_id in played_players_set else None)

        for player in squad.starting_players.all():
            st = stats_dict.get(player.id)
            base_pts = st.points if st else 0
            is_captain = (squad.captain_id == player.id)
            is_vice = (squad.vice_captain_id == player.id)
            is_effective = (effective_captain_id == player.id)

            multiplier = 3 if (active_chip == 'TC' and is_effective) else (2 if is_effective else 1)
            final_pts = base_pts * multiplier

            starters_list.append({
                'id': player.id,
                'name': player.name,
                'position_display': player.get_position_display(),
                'club_name': player.team.name if hasattr(player, 'team') else '',
                'points': final_pts if show_points else "-",
                'is_captain': is_captain,
                'is_vice': is_vice,
                'is_effective_captain': is_effective,
                'yellow_card': st.yellow_card if st else False,
                'red_card': st.red_card if st else False,
            })

        for player in squad.substitutes.all():
            st = stats_dict.get(player.id)
            bench_list.append({
                'id': player.id,
                'name': player.name,
                'position_display': player.get_position_display(),
                'points': st.points if (st and show_points) else "-",
                'yellow_card': st.yellow_card if st else False,
                'red_card': st.red_card if st else False,
            })

    context = {
        'gameweek': gameweek,
        'squad': squad,
        'starters_list': starters_list,
        'bench_players': bench_list,
        'show_points': show_points,
    }
    return render(request, 'closed_squad_view.html', context)


# ==========================================
# 7. المودال والمقارنة والأخبار والجوائز
# ==========================================

def player_detail_modal(request, player_id):
    player = get_object_or_404(Player, id=player_id)
    
    try:
        total_pts = player.total_points() if callable(getattr(player, 'total_points', None)) else 0
        total_g = player.total_goals() if callable(getattr(player, 'total_goals', None)) else 0
        total_a = player.total_assists() if callable(getattr(player, 'total_assists', None)) else 0
        matches_count = player.gameweek_stats.filter(played=True).count()
        clean_sheets_count = player.gameweek_stats.filter(clean_sheet=True).count()
        yellow_cards_count = player.gameweek_stats.filter(yellow_card=True).count()
        red_cards_count = player.gameweek_stats.filter(red_card=True).count()
    except Exception:
        total_pts, total_g, total_a, matches_count, clean_sheets_count, yellow_cards_count, red_cards_count = 0, 0, 0, 0, 0, 0, 0

    stats = {
        'total_points': total_pts,
        'total_goals': total_g,
        'total_assists': total_a,
        'matches_played': matches_count,
        'clean_sheets': clean_sheets_count,
        'yellow_cards': yellow_cards_count,
        'red_cards': red_cards_count,
    }

    try:
        next_matches = Match.objects.filter(
            Q(home_team=player.team) | Q(away_team=player.team),
            is_finished=False
        ).select_related('home_team', 'away_team', 'gameweek').order_by('match_date')[:3]
    except Exception:
        next_matches = []

    try:
        ownership = player.ownership_percentage() if callable(getattr(player, 'ownership_percentage', None)) else 0.0
    except Exception:
        ownership = 0.0

    context = {
        'player': player,
        'stats': stats,
        'next_matches': next_matches,
        'ownership': ownership,
    }
    
    return render(request, 'fantasy/partials/player_modal.html', context)


@login_required
def compare_players(request):
    p1_id = request.GET.get('player1')
    p2_id = request.GET.get('player2')
    q1 = request.GET.get('q1', '').strip()
    q2 = request.GET.get('q2', '').strip()

    player1 = Player.objects.filter(id=p1_id).first() if p1_id and p1_id.isdigit() else None
    player2 = Player.objects.filter(id=p2_id).first() if p2_id and p2_id.isdigit() else None

    def get_player_data(player):
        if not player:
            return None
        
        stats = player.gameweek_stats.filter(gameweek__is_published=True)
        
        next_matches = Match.objects.filter(
            Q(home_team=player.team) | Q(away_team=player.team),
            is_finished=False
        ).select_related('home_team', 'away_team', 'gameweek').order_by('match_date')[:3]

        return {
            'player': player,
            'total_points': stats.aggregate(total=Sum('points'))['total'] or 0,
            'goals': stats.aggregate(total=Sum('goals'))['total'] or 0,
            'assists': stats.aggregate(total=Sum('assists'))['total'] or 0,
            'matches_played': stats.filter(played=True).count(),
            'clean_sheets': stats.filter(clean_sheet=True).count(),
            'yellow_cards': stats.filter(yellow_card=True).count(),
            'red_cards': stats.filter(red_card=True).count(),
            'ownership': player.ownership_percentage() if callable(getattr(player, 'ownership_percentage', None)) else 0,
            'next_matches': next_matches,
        }

    active_league_id = request.session.get('active_league_id')
    base_players = Player.objects.filter(team__league_id=active_league_id).select_related('team') if active_league_id else Player.objects.select_related('team')

    p1_search_results = base_players.filter(name__icontains=q1) if q1 else base_players
    p2_search_results = base_players.filter(name__icontains=q2) if q2 else base_players

    context = {
        'p1': get_player_data(player1),
        'p2': get_player_data(player2),
        'p1_search_results': p1_search_results[:15],
        'p2_search_results': p2_search_results[:15],
        'q1': q1,
        'q2': q2,
        'selected_p1_id': player1.id if player1 else None,
        'selected_p2_id': player2.id if player2 else None,
    }

    target_header = request.headers.get('HX-Target')
    if target_header == 'p1-results':
        return render(request, 'fantasy/partials/p1_search_results.html', context)
    elif target_header == 'p2-results':
        return render(request, 'fantasy/partials/p2_search_results.html', context)

    if request.headers.get('HX-Request'):
        return render(request, 'fantasy/partials/comparison_results.html', context)

    return render(request, 'fantasy/compare.html', context)


def news_and_awards(request):
    now = timezone.now()
    
    next_gameweek = Gameweek.objects.filter(is_finished=False).order_by('number').first()
    deadline = getattr(next_gameweek, 'deadline', None) if next_gameweek else None
    time_remaining = (deadline - now) if deadline and deadline > now else None
    
    injured_players = Player.objects.filter(is_injured=True).select_related('team')
    suspended_players = Player.objects.filter(is_suspended=True).select_related('team')
    
    injuries_and_news = None
    if PlayerStatusUpdate:
        injuries_and_news = PlayerStatusUpdate.objects.filter(
            is_active=True
        ).exclude(chance_of_playing=100).select_related('player', 'player__team').order_by('chance_of_playing', '-updated_at')

    completed_gameweeks = Gameweek.objects.filter(is_finished=True, is_published=True).order_by('-number')
    weekly_heroes = []

    for gw in completed_gameweeks:
        top_squads = UserSquad.objects.filter(gameweek=gw).order_by('-points_earned')
        if top_squads.exists():
            max_pts = top_squads.first().points_earned
            best_performers = top_squads.filter(points_earned=max_pts).select_related('user_team__user')
            weekly_heroes.append({
                'gameweek': gw,
                'top_score': max_pts,
                'performers': best_performers,
            })

    last_finished_gw = completed_gameweeks.first()
    manager_of_the_week = None
    if last_finished_gw and weekly_heroes:
        first_hero = weekly_heroes[0]
        first_performer = first_hero['performers'].first()
        if first_performer:
            manager_of_the_week = {
                'user_team': first_performer.user_team,
                'points': first_hero['top_score'],
                'gameweek': last_finished_gw
            }

    top_performers = {}
    if last_finished_gw:
        positions = ['GK', 'DEF', 'MID', 'FWD']
        for pos in positions:
            top_player = Player.objects.filter(
                position=pos,
                gameweek_stats__gameweek=last_finished_gw
            ).annotate(
                gw_points=Sum('gameweek_stats__points')
            ).order_by('-gw_points').first()
            
            if top_player:
                top_performers[pos] = top_player

    manual_awards = Award.objects.all().select_related('winner').order_by('-date_awarded') if Award else []
    prizes = LeaguePrize.objects.all() if LeaguePrize else []

    context = {
        'next_gameweek': next_gameweek,
        'deadline': deadline,
        'time_remaining': time_remaining,
        'injured_players': injured_players,
        'suspended_players': suspended_players,
        'injuries_and_news': injuries_and_news,
        'weekly_heroes': weekly_heroes,
        'manager_of_the_week': manager_of_the_week,
        'top_performers': top_performers,
        'manual_awards': manual_awards,
        'prizes': prizes,
    }
    return render(request, 'fantasy/news_and_awards.html', context)


# ==========================================
# 8. إدارة الخواص التكتيكية (Chips)
# ==========================================

@login_required
@ratelimit(key='ip', rate='15/m', block=True)
def activate_chip(request, team_id, chip_code):
    user_team = get_object_or_404(UserFantasyTeam, id=team_id, user=request.user)
    
    current_gw = Gameweek.objects.filter(
        league=user_team.league, 
        is_finished=False
    ).order_by('number').first()

    if not current_gw or not current_gw.is_open:
        messages.error(request, "عذراً، التغييرات والخواص مغلقة حالياً لهذه الجولة.")
        return redirect('squad_builder')

    squad, _ = UserSquad.objects.get_or_create(
        user_team=user_team,
        gameweek=current_gw
    )

    already_used_in_squads = UserSquad.objects.filter(
        user_team=user_team,
        active_chip=chip_code
    ).exclude(gameweek=current_gw).exists()

    chip_verify_map = {
        'TC': (user_team.triple_captain_used or already_used_in_squads, 'Triple Captain (x3)'),
        'BB': (user_team.bench_boost_used or already_used_in_squads, 'Bench Boost'),
        'WC': (user_team.wildcard_used or already_used_in_squads, 'Wildcard'),
        'FH': (user_team.free_hit_used or already_used_in_squads, 'Free Hit'),
    }

    if chip_code not in chip_verify_map:
        messages.error(request, "خاصية غير صالحة.")
        return redirect('squad_builder')

    is_used, chip_name = chip_verify_map[chip_code]

    if is_used:
        messages.error(request, f"لقد قمت باستخدام خاصية {chip_name} بالفعل هذا الموسم!")
        return redirect('squad_builder')

    if squad.active_chip == chip_code:
        squad.active_chip = 'NONE'
        squad.save()
        messages.info(request, f"تم إلغاء تفعيل خاصية {chip_name}.")
    else:
        squad.active_chip = chip_code
        squad.save()
        messages.success(request, f"تم تفعيل خاصية {chip_name} بنجاح للجولة {current_gw.number}! 🚀")

    return redirect('squad_builder')


# ==========================================
# 9. قائمة ترتيب اللاعبين والنظام العام
# ==========================================

def player_leaderboard(request):
    # 1. تجميع الإحصائيات مع الحسابات الصحيحة من الجولات المنشورة
    players_list = Player.objects.select_related('team').annotate(
        total_pts=Coalesce(Sum('gameweek_stats__points', filter=Q(gameweek_stats__gameweek__is_published=True)), 0),
        goals=Coalesce(Sum('gameweek_stats__goals', filter=Q(gameweek_stats__gameweek__is_published=True)), 0),
        assists=Coalesce(Sum('gameweek_stats__assists', filter=Q(gameweek_stats__gameweek__is_published=True)), 0),
        clean_sheets=Coalesce(Count('gameweek_stats', filter=Q(gameweek_stats__clean_sheet=True, gameweek_stats__gameweek__is_published=True)), 0),
        yellow_cards=Coalesce(Count('gameweek_stats', filter=Q(gameweek_stats__yellow_card=True, gameweek_stats__gameweek__is_published=True)), 0),
        red_cards=Coalesce(Count('gameweek_stats', filter=Q(gameweek_stats__red_card=True, gameweek_stats__gameweek__is_published=True)), 0)
    )

    # 2. البحث بالاسم
    search_query = request.GET.get('search', '').strip()
    if search_query:
        players_list = players_list.filter(name__icontains=search_query)

    # 3. الفلترة بالفريق
    team_id = request.GET.get('team', '').strip()
    if team_id and team_id.isdigit():
        players_list = players_list.filter(team_id=int(team_id))

    # 4. إصلاح الفلترة حسب المركز (دعم المراكز الفرعية والرئيسية)
    position = request.GET.get('position', '').strip()
    if position:
        pos_map = {
            'GK': ['GK'],
            'DEF': ['CB', 'RB', 'LB', 'RWB', 'LWB', 'DEF'],
            'MID': ['CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW', 'MID'],
            'FWD': ['ST', 'CF', 'FWD']
        }
        if position in pos_map:
            # البحث بالمراكز المتطابقة أو عبر main_category إن وجدت
            players_list = players_list.filter(
                Q(position__in=pos_map[position]) | Q(main_category=position)
            )
        else:
            players_list = players_list.filter(position=position)

    # 5. إصلاح خيارات الترتيب لتشمل كافة الخيارات الموجودة في الواجهة
    sort_by = request.GET.get('sort_by', '-total_points')
    
    allowed_sorts = {
        '-total_points': '-total_pts',
        'total_points': 'total_pts',
        '-goals': '-goals',
        '-assists': '-assists',
        '-clean_sheets': '-clean_sheets',
        '-price': '-price',
        'price': 'price',
        'name': 'name',
    }
    
    actual_sort = allowed_sorts.get(sort_by, '-total_pts')
    players_list = players_list.order_by(actual_sort, '-total_pts')

    total_players_count = players_list.count()

    # Pagination
    paginator = Paginator(players_list, 20)
    page_number = request.GET.get('page')
    players = paginator.get_page(page_number)

    real_teams = RealTeam.objects.all()

    context = {
        'players': players,
        'real_teams': real_teams,
        'total_players_count': total_players_count,
        'search_query': search_query,
        'selected_team': team_id,
        'selected_position': position,
        'selected_sort': sort_by,
    }

    return render(request, 'fantasy/player_leaderboard.html', context)


def ping(request):
    return HttpResponse("OK", content_type="text/plain")


def custom_csrf_failure_view(request, reason=""):
    return render(request, '403_csrf.html', status=403)
from django.contrib import admin
from django.contrib import messages
from .models import (
    League, 
    RealTeam, 
    Match,
    Player, 
    Gameweek, 
    PlayerGameweekStat, 
    UserFantasyTeam, 
    UserSquad,
    NewsAndUpdate,
    PlayerStatusUpdate,
    LeaguePrize,
    Award,
    UserProfile
)


# 🟢 Action لإنشاء سجلات إحصائيات جميع اللاعبين تلقائياً للجولة المحددة
@admin.action(description='⚡ إنشاء سجلات إحصائيات جميع اللاعبين لهذه الجولة تلقائياً')
def generate_gameweek_stats(modeladmin, request, queryset):
    created_count = 0
    for gameweek in queryset:
        players = Player.objects.filter(team__league=gameweek.league)
        for player in players:
            stat, created = PlayerGameweekStat.objects.get_or_create(
                player=player,
                gameweek=gameweek,
                defaults={'team': player.team}
            )
            if created:
                created_count += 1
    messages.success(request, f"تم إنشاء {created_count} سجل إحصائيات جديد للاعبين بنجاح!")


@admin.action(description='حساب نقاط الجولة وتحديث الترتيب وخصم مباريات الإيقاف')
def calculate_gameweek_points(modeladmin, request, queryset):
    for gameweek in queryset:
        # 1. جلب كافة إحصائيات ونقاط اللاعبين لهذه الجولة
        stats = PlayerGameweekStat.objects.filter(gameweek=gameweek)
        player_points = {stat.player_id: stat.points for stat in stats}
        
        # الاعتماد على played=True لمعرفة من شارك فعلياً في الجولة
        played_players = set(stats.filter(played=True).values_list('player_id', flat=True))

        # 2. حساب نقاط التشكيلات وتحديث الترتيب مع دعم الكابتن المباشر ونائب الكابتن
        squads = UserSquad.objects.filter(gameweek=gameweek).prefetch_related('starting_players')
        
        for squad in squads:
            total_squad_points = 0
            captain_id = squad.captain_id
            vice_captain_id = squad.vice_captain_id

            captain_played = captain_id in played_players if captain_id else False
            effective_captain_id = captain_id if captain_played else (vice_captain_id if vice_captain_id in played_players else None)

            for player in squad.starting_players.all():
                p_points = player_points.get(player.id, 0)
                if effective_captain_id and player.id == effective_captain_id:
                    p_points *= 2
                total_squad_points += p_points
            
            transfers_cost = getattr(squad, 'transfers_cost', 0) or 0
            squad.points_earned = max(0, total_squad_points - transfers_cost)
            squad.save()
            
            user_team = squad.user_team
            all_squads = UserSquad.objects.filter(user_team=user_team, gameweek__is_published=True)
            user_team.total_points = sum(s.points_earned for s in all_squads)
            user_team.save()

        # 3. خصم مباريات الإيقاف عبر دالة الموديل
        suspended_players = Player.objects.filter(
            team__league=gameweek.league, 
            is_suspended=True, 
            suspended_matches_left__gt=0
        )
        
        for player in suspended_players:
            player.process_gameweek_suspension()

    messages.success(request, "تم حساب نقاط الجولة وتحديث الترتيب والإيقافات بنجاح!")


# 1️⃣ Inline لعرض إحصائيات الجولات داخل صفحة اللاعب نفسه
class PlayerGameweekStatInlineForPlayer(admin.TabularInline):
    model = PlayerGameweekStat
    extra = 0
    fields = ('gameweek', 'played', 'goals', 'assists', 'yellow_card', 'red_card', 'clean_sheet', 'points')
    readonly_fields = ('points',)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            instance.save()  # استدعاء دالة save المخصصة بالموديل لحساب الكروت والنقاط
        formset.save_m2m()


# 2️⃣ Inline لإدخال إحصائيات لاعبي الفريق دفعة واحدة من داخل صفحة الفريق
class PlayerGameweekStatInlineForTeam(admin.TabularInline):
    model = PlayerGameweekStat
    extra = 0
    fields = ('gameweek', 'player', 'played', 'goals', 'assists', 'yellow_card', 'red_card', 'clean_sheet', 'points')
    readonly_fields = ('points',)
    show_change_link = True

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "player":
            parent_team_id = request.resolver_match.kwargs.get('object_id')
            if parent_team_id:
                kwargs["queryset"] = Player.objects.filter(team_id=parent_team_id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            instance.save()
        formset.save_m2m()


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_public', 'is_active')
    search_fields = ('name', 'code')


@admin.register(RealTeam)
class RealTeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'league')
    list_filter = ('league',)
    search_fields = ('name',)
    inlines = [PlayerGameweekStatInlineForTeam]


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ('home_team', 'away_team', 'gameweek', 'match_date', 'home_score', 'away_score', 'is_finished', 'league')
    list_filter = ('league', 'gameweek', 'is_finished', 'match_date')
    search_fields = ('home_team__name', 'away_team__name')
    list_editable = ('home_score', 'away_score', 'is_finished')
    ordering = ('-match_date',)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = (
        'name', 
        'team', 
        'position', 
        'price', 
        'has_yellow_card', 
        'has_red_card', 
        'is_suspended', 
        'suspended_matches_left'
    )
    list_filter = ('is_suspended', 'has_yellow_card', 'has_red_card', 'position', 'team__league', 'team')
    search_fields = ('name', 'team__name')
    list_editable = ('price', 'has_yellow_card', 'has_red_card')
    raw_id_fields = ('team',)
    inlines = [PlayerGameweekStatInlineForPlayer]

    fieldsets = (
        ('بيانات اللاعب الأساسية', {
            'fields': ('name', 'team', 'position', 'price')
        }),
        ('حالة الكروت والتأديب (تراكمية)', {
            'fields': (
                ('has_yellow_card', 'has_red_card'),
                ('is_suspended', 'suspended_matches_left')
            ),
        }),
    )

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            instance.save()
        formset.save_m2m()


@admin.register(Gameweek)
class GameweekAdmin(admin.ModelAdmin):
    list_display = ('number', 'league', 'start_date', 'deadline', 'is_open', 'is_finished', 'is_published')
    list_filter = ('league', 'is_open', 'is_finished', 'is_published')
    actions = [generate_gameweek_stats, calculate_gameweek_points]


@admin.register(PlayerGameweekStat)
class PlayerGameweekStatAdmin(admin.ModelAdmin):
    list_display = (
        'player', 
        'team',
        'gameweek', 
        'played',
        'player_has_previous_yellow',
        'yellow_card', 
        'red_card', 
        'goals', 
        'assists', 
        'clean_sheet', 
        'points'
    )
    list_filter = ('gameweek', 'team', 'played', 'yellow_card', 'red_card', 'clean_sheet')
    search_fields = ('player__name', 'team__name')
    list_editable = ('played', 'yellow_card', 'red_card', 'goals', 'assists', 'clean_sheet')
    raw_id_fields = ('player', 'gameweek')

    @admin.display(boolean=True, description='إنذار سابق؟')
    def player_has_previous_yellow(self, obj):
        return obj.player.has_yellow_card

    fieldsets = (
        ('المعلومات الأساسية والنقاط', {
            'fields': ('player', 'team', 'gameweek', 'played', 'points')
        }),
        ('الأداء والكروت للجولة', {
            'fields': (
                ('yellow_card', 'red_card'),
                ('goals', 'assists'),
                ('penalties_saved', 'penalties_missed', 'own_goals'),
                'clean_sheet'
            ),
        }),
        ('إدارة العقوبات والإيقافات', {
            'classes': ('collapse',),
            'fields': (
                'suspension_reason',
                'suspension_matches',
                'suspension_notes'
            ),
        }),
    )


@admin.register(UserFantasyTeam)
class UserFantasyTeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'league', 'budget', 'total_points')
    list_filter = ('league',)
    search_fields = ('name', 'user__username')
    raw_id_fields = ('user',)


@admin.register(UserSquad)
class UserSquadAdmin(admin.ModelAdmin):
    list_display = ('user_team', 'gameweek', 'captain', 'vice_captain', 'points_earned', 'transfers_cost')
    list_filter = ('gameweek', 'user_team__league')
    search_fields = ('user_team__name',)
    raw_id_fields = ('user_team', 'captain', 'vice_captain')
    filter_horizontal = ('starting_players', 'substitutes')


@admin.register(NewsAndUpdate)
class NewsAndUpdateAdmin(admin.ModelAdmin):
    list_display = ('title', 'league', 'is_pinned', 'created_at')
    list_filter = ('league', 'is_pinned', 'created_at')
    search_fields = ('title', 'content')
    list_editable = ('is_pinned',)
    ordering = ('-created_at',)


@admin.register(PlayerStatusUpdate)
class PlayerStatusUpdateAdmin(admin.ModelAdmin):
    list_display = ('player', 'chance_of_playing', 'status_reason', 'is_active', 'updated_at')
    list_filter = ('chance_of_playing', 'is_active')
    search_fields = ('player__name', 'status_reason', 'news_text')
    list_editable = ('chance_of_playing', 'is_active')


@admin.register(LeaguePrize)
class LeaguePrizeAdmin(admin.ModelAdmin):
    list_display = ('icon_emoji', 'title', 'prize_type', 'order')
    list_filter = ('prize_type',)
    search_fields = ('title', 'description')
    list_editable = ('order',)
    ordering = ('order',)


@admin.register(Award)
class AwardAdmin(admin.ModelAdmin):
    list_display = ('icon', 'title', 'winner', 'date_awarded')
    search_fields = ('title', 'winner__username')
    ordering = ('-date_awarded',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'avatar')
    search_fields = ('user__username',)
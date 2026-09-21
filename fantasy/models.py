from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db.models import Sum
from cloudinary.models import CloudinaryField


# 1. الدوري والبطولات
class League(models.Model):
    name = models.CharField(max_length=100, verbose_name="اسم البطولة")
    code = models.CharField(max_length=10, unique=True, blank=True, null=True, verbose_name="كود الانضمام الخاص")
    is_public = models.BooleanField(default=True, verbose_name="بطولة عامة مفتوحة")
    is_active = models.BooleanField(default=True, verbose_name="نشطة حالياً")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "بطولة / دوري"
        verbose_name_plural = "البطولات والدوريات"

# 1.1 رعاة البطولة
class LeagueSponsor(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='sponsors', verbose_name="البطولة")
    name = models.CharField(max_length=100, verbose_name="اسم الراعي")
    logo = CloudinaryField('شعار الراعي', folder='sponsor_logos', null=True, blank=True)
    website_url = models.URLField(blank=True, null=True, verbose_name="رابط موقع الراعي / صفحة الفيسبوك")
    order = models.PositiveIntegerField(default=0, verbose_name="ترتيب العرض")

    class Meta:
        ordering = ['order', 'id']
        verbose_name = "راعي بطولة"
        verbose_name_plural = "رعاة البطولات"

    def __str__(self):
        return f"{self.name} - ({self.league.name})"
    
# 2. الفرق الحقيقية
class RealTeam(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='teams', verbose_name="الدوري")
    name = models.CharField(max_length=100, verbose_name="اسم الفريق")
    logo = CloudinaryField('شعار الفريق', folder='team_logos', null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.league.name})"

    class Meta:
        verbose_name = "فريق حقيقي"
        verbose_name_plural = "الفرق الحقيقية"


# 3. جدول المباريات
class Match(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='matches', verbose_name="الدوري")
    gameweek = models.ForeignKey('Gameweek', on_delete=models.CASCADE, related_name='matches', verbose_name="الجولة")
    home_team = models.ForeignKey(RealTeam, on_delete=models.CASCADE, related_name='home_matches', verbose_name="الفريق المستضيف")
    away_team = models.ForeignKey(RealTeam, on_delete=models.CASCADE, related_name='away_matches', verbose_name="الفريق الضيف")
    match_date = models.DateTimeField(verbose_name="تاريخ ووقت المباراة")
    is_finished = models.BooleanField(default=False, verbose_name="انتهت المباراة")
    home_score = models.PositiveIntegerField(default=0, verbose_name="أهداف المستضيف")
    away_score = models.PositiveIntegerField(default=0, verbose_name="أهداف الضيف")

    class Meta:
        verbose_name = "مباراة"
        verbose_name_plural = "المباريات"

    def __str__(self):
        return f"{self.home_team.name} vs {self.away_team.name} (الجولة {self.gameweek.number})"


# 4. اللاعبون الحقيقيون
class Player(models.Model):
    DETAILED_POSITION_CHOICES = [
        ('GK', 'حارس مرمى'),
        ('CB', 'قلب دفاع'),
        ('RB', 'ظهير أيمن (باك يمين)'),
        ('LB', 'ظهير أيسر (باك شمال)'),
        ('RWB', 'جناح دفاعي أيمن'),
        ('LWB', 'جناح دفاعي أيسر'),
        ('CDM', 'وسط مدافع (ديفندر)'),
        ('CM', 'وسط ملعب (ارتكاز)'),
        ('CAM', 'صانع ألعاب'),
        ('RM', 'وسط أيمن'),
        ('LM', 'وسط أيسر'),
        ('RW', 'جناح أيمن'),
        ('LW', 'جناح أيسر'),
        ('ST', 'مهاجم صريح (راس حرب)'),
        ('CF', 'مهاجم ثاني / وهمي'),
    ]

    team = models.ForeignKey(RealTeam, on_delete=models.CASCADE, related_name='players', verbose_name="الفريق")
    name = models.CharField(max_length=100, verbose_name="اسم اللاعب")
    photo = CloudinaryField('صورة اللاعب', folder='player_photos', null=True, blank=True)
    position = models.CharField(max_length=5, choices=DETAILED_POSITION_CHOICES, verbose_name="المركز التفصيلي")
    price = models.DecimalField(max_digits=4, decimal_places=1, default=5.0, verbose_name="السعر")

    # حالة العقوبات والإيقاف
    is_suspended = models.BooleanField(default=False, verbose_name="معاقب/موقوف")
    suspended_matches_left = models.PositiveIntegerField(default=0, verbose_name="المباريات المتبقية للإيقاف")
    has_yellow_card = models.BooleanField(default=False, verbose_name="يوجد إنذار سابق (أصفر)")
    has_red_card = models.BooleanField(default=False, verbose_name="حاصل على كارت أحمر / طرد")

    # حالة الإصابة والأخبار السريعة
    is_injured = models.BooleanField(default=False, verbose_name="مصاب / مشكوك بمشاركته")
    injury_news = models.CharField(max_length=255, blank=True, null=True, verbose_name="تفاصيل الإصابة")
    chance_of_playing = models.PositiveIntegerField(default=100, verbose_name="نسبة احتمالية المشاركة %")

    @property
    def main_category(self):
        if self.position == 'GK':
            return 'GK'
        elif self.position in ['CB', 'RB', 'LB', 'RWB', 'LWB']:
            return 'DEF'
        elif self.position in ['CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW']:
            return 'MID'
        else:
            return 'FWD'

    @property
    def total_yellow_cards(self):
        return self.gameweek_stats.filter(yellow_card=True).count()

    @property
    def total_red_cards(self):
        return self.gameweek_stats.filter(red_card=True).count()

    def ownership_percentage(self):
        total_teams = UserFantasyTeam.objects.filter(league=self.team.league).count()
        if total_teams == 0:
            return 0.0
        teams_with_player = UserSquad.objects.filter(
            user_team__league=self.team.league,
            starting_players=self
        ).values('user_team').distinct().count()
        return round((teams_with_player / total_teams) * 100, 1)

    def total_points(self):
        return self.gameweek_stats.aggregate(Sum('points'))['points__sum'] or 0

    def total_goals(self):
        return self.gameweek_stats.aggregate(Sum('goals'))['goals__sum'] or 0

    def total_assists(self):
        return self.gameweek_stats.aggregate(Sum('assists'))['assists__sum'] or 0

    def process_gameweek_suspension(self):
        if self.suspended_matches_left > 0:
            self.suspended_matches_left -= 1
            if self.suspended_matches_left == 0:
                self.is_suspended = False
                self.has_red_card = False
            self.save(update_fields=['suspended_matches_left', 'is_suspended', 'has_red_card'])

    def save(self, *args, **kwargs):
        if self.has_red_card:
            self.has_yellow_card = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.get_position_display()}) - {self.team.name}"

    class Meta:
        verbose_name = "لاعب"
        verbose_name_plural = "اللاعبون"


# 5. الجولات
class Gameweek(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, verbose_name="الدوري")
    number = models.PositiveIntegerField(verbose_name="رقم الجولة")
    
    # حقول المواعيد
    start_date = models.DateTimeField(null=True, blank=True, verbose_name="موعد فتح التشكيلة (الخميس 16:00)")
    deadline = models.DateTimeField(null=True, blank=True, verbose_name="موعد إغلاق التشكيلة (السبت 16:00)")
    
    is_open = models.BooleanField(default=True, verbose_name="باب التغيير مفتوح")
    is_finished = models.BooleanField(default=False, verbose_name="مغلقة/منتهية")
    is_published = models.BooleanField(default=False, verbose_name="تم اعتماد ونشر النقاط")

    class Meta:
        unique_together = ('league', 'number')
        verbose_name = "جولة"
        verbose_name_plural = "الجولات"

    def set_fixed_schedule(self):
        """تحديد موعد الإغلاق يوم السبت الساعة 16:00 والفتح يوم الخميس الساعة 16:00"""
        from datetime import timedelta
        from django.utils import timezone

        base_date = timezone.now()
        days_until_saturday = (5 - base_date.weekday()) % 7
        saturday_deadline = (base_date + timedelta(days=days_until_saturday)).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        thursday_start = saturday_deadline - timedelta(days=2)

        if not self.deadline:
            self.deadline = saturday_deadline
        if not self.start_date:
            self.start_date = thursday_start

    def save(self, *args, **kwargs):
        if not self.deadline or not self.start_date:
            self.set_fixed_schedule()

        is_newly_finished = False
        if self.pk:
            old_instance = Gameweek.objects.filter(pk=self.pk).first()
            if old_instance and not old_instance.is_finished and self.is_finished:
                is_newly_finished = True
        elif self.is_finished:
            is_newly_finished = True

        super().save(*args, **kwargs)

        if is_newly_finished:
            suspended_players = Player.objects.filter(
                team__league=self.league,
                is_suspended=True, 
                suspended_matches_left__gt=0
            )
            for player in suspended_players:
                player.process_gameweek_suspension()

    def __str__(self):
        return f"الجولة {self.number} - {self.league.name}"


# 6. إحصائيات اللاعب في الجولة
class PlayerGameweekStat(models.Model):
    SUSPENSION_REASONS = [
        ('NONE', 'لا يوجد'),
        ('RED_CARD', 'طرد مباشر / كروت'),
        ('DISCIPLINARY', 'عقوبة أخلاقية / سلوك'),
        ('CLUB_DECISION', 'قرار إداري / إيقاف نادٍ'),
    ]

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='gameweek_stats', verbose_name="اللاعب")
    gameweek = models.ForeignKey(Gameweek, on_delete=models.CASCADE, verbose_name="الجولة")
    team = models.ForeignKey(RealTeam, on_delete=models.CASCADE, null=True, blank=True, verbose_name="الفريق")

    played = models.BooleanField(default=False, verbose_name="شارك في المباراة (+2)")
    goals = models.PositiveIntegerField(default=0, verbose_name="الأهداف")
    assists = models.PositiveIntegerField(default=0, verbose_name="الأسيست (+3)")
    clean_sheet = models.BooleanField(default=False, verbose_name="كلين شيت")
    
    penalties_saved = models.PositiveIntegerField(default=0, verbose_name="ضربات جزاء تصدى لها الحارس (+5)")
    penalties_missed = models.PositiveIntegerField(default=0, verbose_name="ضربات الجزاء الضائعة (-2)")
    own_goals = models.PositiveIntegerField(default=0, verbose_name="أهداف عكسية مرماها (-2)")

    yellow_card = models.BooleanField(default=False, verbose_name="كارت أصفر للجولة (-1)")
    red_card = models.BooleanField(default=False, verbose_name="كارت أحمر للجولة (-3)")

    suspension_reason = models.CharField(max_length=20, choices=SUSPENSION_REASONS, default='NONE', verbose_name="سبب العقوبة")
    suspension_matches = models.PositiveIntegerField(default=0, verbose_name="عدد مباريات الإيقاف")
    suspension_notes = models.TextField(blank=True, null=True, verbose_name="ملاحظات تفاصيل العقوبة")

    points = models.IntegerField(default=0, verbose_name="النقاط الإجمالية")

    class Meta:
        unique_together = ('player', 'gameweek')
        verbose_name = "إحصائية لاعب لجولة"
        verbose_name_plural = "إحصائيات اللاعبين للجولات"

    def save(self, *args, **kwargs):
        if not self.team_id or self.team != self.player.team:
            self.team = self.player.team

        # 1. فحص تراكم الإنذارات: حساب الإنذارات السابقة للاعب بدون الجولة الحالية
        previous_yellows = PlayerGameweekStat.objects.filter(
            player=self.player, 
            yellow_card=True
        ).exclude(pk=self.pk).count()

        # إذا كان هذا الإنذار هو الإنذار الثالث (أي 2 سابقين + 1 حالي)
        if self.yellow_card and (previous_yellows + 1) >= 3:
            # تحويل الإنذار الثالث تلقائياً إلى إيقاف تراكمي
            self.red_card = True
            self.suspension_matches = 1

        # 2. حساب النقاط المباشرة للجولة
        pts = 0
        category = self.player.main_category

        # نقاط المشاركة
        if self.played:
            pts += 2

        # نقاط الأهداف
        if category in ['GK', 'DEF']:
            pts += (self.goals * 6)
        elif category == 'MID':
            pts += (self.goals * 5)
        elif category == 'FWD':
            pts += (self.goals * 4)

        # نقاط الأسيست
        pts += (self.assists * 3)

        # نقاط الكلين شيت
        if self.clean_sheet:
            if category in ['GK', 'DEF']:
                pts += 4
            elif category == 'MID':
                pts += 1

        # تصديات ضربات الجزاء
        if category == 'GK':
            pts += (self.penalties_saved * 5)

        # الخصومات (ضربات الجزاء الضائعة / الأهداف العكسية / الكروت)
        pts -= (self.penalties_missed * 2)
        pts -= (self.own_goals * 2)
        
        if self.yellow_card:
            pts -= 1
        if self.red_card:
            pts -= 3

        self.points = pts

        # 3. تحديث حالة إيقاف اللاعب في موديل Player
        if self.red_card or self.suspension_matches > 0:
            self.player.is_suspended = True
            if self.suspension_matches > 0:
                self.player.suspended_matches_left = self.suspension_matches
            else:
                self.player.suspended_matches_left = 1
            self.player.save(update_fields=['is_suspended', 'suspended_matches_left'])

        super().save(*args, **kwargs)
        
    def __str__(self):
        return f"إحصائيات {self.player.name} - {self.gameweek} ({self.points} نقطة)"


# 7. فريق المشترك
class UserFantasyTeam(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fantasy_teams', verbose_name="المستخدم")
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='user_teams', verbose_name="الدوري")
    name = models.CharField(max_length=100, verbose_name="اسم فرقتك")
    budget = models.DecimalField(max_digits=5, decimal_places=1, default=100.0, verbose_name="الميزانية المتبقية")
    total_points = models.IntegerField(default=0, verbose_name="إجمالي النقاط")

    triple_captain_used = models.BooleanField(default=False, verbose_name="تم استخدام Triple Captain")
    bench_boost_used = models.BooleanField(default=False, verbose_name="تم استخدام Bench Boost")
    free_hit_used = models.BooleanField(default=False, verbose_name="تم استخدام Free Hit")
    wildcard_used = models.BooleanField(default=False, verbose_name="تم استخدام Wildcard")

    class Meta:
        unique_together = ('user', 'league')
        verbose_name = "فريق فانتسي للمستخدم"
        verbose_name_plural = "فرق المستخدمين"

    def __str__(self):
        return f"{self.name} ({self.user.username}) - {self.league.name}"


# 8. تشكيلة المستخدم للجولة
class UserSquad(models.Model):
    CHIP_CHOICES = [
        ('NONE', 'بدون خاصية'),
        ('TC', 'Triple Captain (x3)'),
        ('BB', 'Bench Boost'),
        ('FH', 'Free Hit'),
        ('WC', 'Wildcard'),
    ]

    user_team = models.ForeignKey(UserFantasyTeam, on_delete=models.CASCADE, related_name='squads', verbose_name="فريق المستخدم")
    gameweek = models.ForeignKey(Gameweek, on_delete=models.CASCADE, verbose_name="الجولة")
    starting_players = models.ManyToManyField(Player, related_name='starters', verbose_name="الأساسيين (6)")
    substitutes = models.ManyToManyField(Player, related_name='subs', blank=True, verbose_name="الاحتياط (2)")
    
    captain = models.ForeignKey(
        Player, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='captained_in', 
        verbose_name="الكابتن (x2)"
    )
    vice_captain = models.ForeignKey(
        Player, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='vice_captained_in', 
        verbose_name="نائب الكابتن"
    )

    active_chip = models.CharField(max_length=10, choices=CHIP_CHOICES, default='NONE', verbose_name="الخاصية المفعّلة")
    transfers_made = models.PositiveIntegerField(default=0, verbose_name="عدد التبديلات المنجزة")
    substitutions_count = models.PositiveIntegerField(default=0, verbose_name="عدد حركة تبديلات الدكة")
    transfers_cost = models.IntegerField(default=0, verbose_name="خصم التغييرات")
    is_saved = models.BooleanField(default=False, verbose_name="تم حفظ التشكيلة")
    points_earned = models.IntegerField(default=0, verbose_name="نقاط الجولة")

    class Meta:
        unique_together = ('user_team', 'gameweek')
        verbose_name = "تشكيلة جولة للمستخدم"
        verbose_name_plural = "تشكيلات الجولات للمستخدمين"

    def clean(self):
        super().clean()
        if self.captain and self.vice_captain and self.captain == self.vice_captain:
            raise ValidationError("لا يمكن اختيار نفس اللاعب ككابتن ونائب كابتن في نفس الوقت.")
        
        if self.pk:
            if self.starting_players.count() > 6:
                raise ValidationError("لا يمكن إضافة أكثر من 6 لاعبين في التشكيلة الأساسية.")
            if self.substitutes.count() > 2:
                raise ValidationError("دكة الاحتياطي لا تتسع لأكثر من لاعبين اثنين (2) فقط.")

    def __str__(self):
        return f"تشكيلة {self.user_team.name} - {self.gameweek}"
    

# 9. مركز الأخبار والتحديثات العامة
class NewsAndUpdate(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='news', verbose_name="الدوري")
    title = models.CharField(max_length=200, verbose_name="عنوان الخبر")
    content = models.TextField(verbose_name="محتوى الخبر")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ النشر")
    is_pinned = models.BooleanField(default=False, verbose_name="خبر مثبت")

    class Meta:
        ordering = ['-is_pinned', '-created_at']
        verbose_name = "خبر / تحديث"
        verbose_name_plural = "مركز الأخبار"

    def __str__(self):
        return self.title


# 10. تحديثات إصابات وغيابات اللاعبين
class PlayerStatusUpdate(models.Model):
    PLAY_CHANCE_CHOICES = [
        (0, 'مستبعد / مصاب (0%)'),
        (25, 'شكوك عالية (25%)'),
        (50, 'شكوك متوسطة (50%)'),
        (75, 'احتمال كبير للمشاركة (75%)'),
        (100, 'جاهز للعب (100%)'),
    ]

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='status_updates', verbose_name="اللاعب")
    status_reason = models.CharField(max_length=255, verbose_name="سبب الغياب/الشك (مثال: إصابة خلفية، إيقاف كروت)")
    chance_of_playing = models.IntegerField(choices=PLAY_CHANCE_CHOICES, default=0, verbose_name="احتمالية اللعب")
    news_text = models.TextField(blank=True, null=True, verbose_name="تفاصيل إضافية للخبر")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="تاريخ التحديث")
    is_active = models.BooleanField(default=True, verbose_name="خبر نشط")

    class Meta:
        verbose_name = "تحديث حالة لاعب"
        verbose_name_plural = "تحديثات حالات اللاعبين"

    def __str__(self):
        return f"{self.player.name} - {self.get_chance_of_playing_display()}"


# 11. لوحة جوائز الدوري العامة
class LeaguePrize(models.Model):
    PRIZE_TYPE_CHOICES = [
        ('season', 'بطل الموسم'),
        ('monthly', 'بطل الشهر'),
        ('gameweek', 'بطل الجولة'),
    ]
    title = models.CharField(max_length=150, verbose_name="عنوان الجائزة")
    prize_type = models.CharField(max_length=20, choices=PRIZE_TYPE_CHOICES, default='season', verbose_name="نوع الجائزة")
    description = models.TextField(verbose_name="وصف الجائزة أو المكافأة")
    icon_emoji = models.CharField(max_length=10, default="🏆", verbose_name="رمز تعبيري (Emoji)")
    order = models.PositiveIntegerField(default=0, verbose_name="ترتيب العرض")

    class Meta:
        ordering = ['order']
        verbose_name = "جائزة الدوري"
        verbose_name_plural = "جوائز الدوري"

    def __str__(self):
        return self.title


# 12. الجوائز والتكريمات الفردية للمستخدمين
class Award(models.Model):
    title = models.CharField(max_length=200, verbose_name="اسم الجائزة")
    winner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='awards', verbose_name="الفائز")
    description = models.TextField(blank=True, null=True, verbose_name="وصف الجائزة / المناسبة")
    icon = models.CharField(max_length=50, default="🏆", verbose_name="الإيموجي/الأيقونة")
    date_awarded = models.DateField(auto_now_add=True, verbose_name="تاريخ التتويج")

    class Meta:
        ordering = ['-date_awarded']
        verbose_name = "جائزة / وسام مستخدم"
        verbose_name_plural = "جوائز وأوسمة المستخدمين"

    def __str__(self):
        return f"{self.title} - {self.winner.username}"


# 13. بروفايل المستخدم
class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile', verbose_name="المستخدم")
    avatar = CloudinaryField('الصورة الشخصية', folder='avatars/', null=True, blank=True)

    def __str__(self):
        return f"Profile of {self.user.username}"

    class Meta:
        verbose_name = "ملف شخصي"
        verbose_name_plural = "الملفات الشخصية"


# ==========================================
# SIGNALS
# ==========================================

@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
    else:
        UserProfile.objects.get_or_create(user=instance)
    instance.profile.save()


@receiver(post_save, sender=RealTeam)
def sync_players_on_team_update(sender, instance, **kwargs):
    players = instance.players.all()
    for player in players:
        player.save()
        PlayerGameweekStat.objects.filter(player=player).update(team=instance)
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


# 2. الفرق الحقيقية
class RealTeam(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='teams', verbose_name="الدوري")
    name = models.CharField(max_length=100, verbose_name="اسم الفريق")
    logo = CloudinaryField('image', folder='team_logos', null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.league.name})"


# 3. اللاعبون الحقيقيون
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
    position = models.CharField(max_length=5, choices=DETAILED_POSITION_CHOICES, verbose_name="المركز التفصيلي")
    price = models.DecimalField(max_digits=4, decimal_places=1, default=5.0, verbose_name="السعر")

    is_suspended = models.BooleanField(default=False, verbose_name="معاقب/موقوف")
    suspended_matches_left = models.PositiveIntegerField(default=0, verbose_name="المباريات المتبقية للإيقاف")
    
    has_yellow_card = models.BooleanField(default=False, verbose_name="يوجد إنذار سابق (أصفر)")
    has_red_card = models.BooleanField(default=False, verbose_name="حاصل على كارت أحمر / طرد")

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

    def process_gameweek_suspension(self):
        """خصم جولة إيقاف وتطهير عقوبة اللاعب عند الانتهاء"""
        if self.suspended_matches_left > 0:
            self.suspended_matches_left -= 1
            if self.suspended_matches_left == 0:
                self.is_suspended = False
                self.has_red_card = False  # إزالة شارة الأحمر بعد انقضاء الإيقاف
            self.save(update_fields=['suspended_matches_left', 'is_suspended', 'has_red_card'])

    def save(self, *args, **kwargs):
        if self.has_red_card:
            self.has_yellow_card = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.get_position_display()}) - {self.team.name}"


# 4. الجولات
class Gameweek(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, verbose_name="الدوري")
    number = models.PositiveIntegerField(verbose_name="رقم الجولة")
    is_open = models.BooleanField(default=True, verbose_name="باب التغيير مفتوح")
    is_finished = models.BooleanField(default=False, verbose_name="مغلقة/منتهية")
    is_published = models.BooleanField(default=False, verbose_name="تم اعتماد ونشر النقاط")

    class Meta:
        unique_together = ('league', 'number')

    def save(self, *args, **kwargs):
        # التحقق مما إذا كانت الجولة تُغلق/تُنهى الآن لخصم الإيقافات
        is_newly_finished = False
        if self.pk:
            old_instance = Gameweek.objects.filter(pk=self.pk).first()
            if old_instance and not old_instance.is_finished and self.is_finished:
                is_newly_finished = True
        elif self.is_finished:
            is_newly_finished = True

        super().save(*args, **kwargs)

        # 🟢 عند إنهاء الجولة: خصم جولة إيقاف واحدة من كافة اللاعبين الموقوفين في البطولة
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


# 5. إحصائيات اللاعب في الجولة
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

    played = models.BooleanField(default=False, verbose_name="شارك في المباراة")
    goals = models.PositiveIntegerField(default=0, verbose_name="الأهداف")
    assists = models.PositiveIntegerField(default=0, verbose_name="الأسيست")
    clean_sheet = models.BooleanField(default=False, verbose_name="كلين شيت")
    
    yellow_card = models.BooleanField(default=False, verbose_name="كارت أصفر للجولة")
    red_card = models.BooleanField(default=False, verbose_name="كارت أحمر للجولة")

    penalties_taken = models.PositiveIntegerField(default=0, verbose_name="ضربات الجزاء المسددة")
    penalties_missed = models.PositiveIntegerField(default=0, verbose_name="ضربات الجزاء الضائعة")

    suspension_reason = models.CharField(max_length=20, choices=SUSPENSION_REASONS, default='NONE', verbose_name="سبب العقوبة")
    suspension_matches = models.PositiveIntegerField(default=0, verbose_name="عدد مباريات الإيقاف")
    suspension_notes = models.TextField(blank=True, null=True, verbose_name="ملاحظات تفاصيل العقوبة")

    points = models.IntegerField(default=0, verbose_name="النقاط")

    class Meta:
        unique_together = ('player', 'gameweek')

    def save(self, *args, **kwargs):
        # 1. المزامنة التلقائية للفريق من موديل اللاعب
        if not self.team_id or self.team != self.player.team:
            self.team = self.player.team

        # 2. إدارة حالة الإيقاف والكروت في موديل Player
        player_updated = False

        if self.suspension_matches > 0:
            self.player.is_suspended = True
            self.player.suspended_matches_left = self.suspension_matches
            player_updated = True

        if self.red_card:
            self.player.has_red_card = True
            self.player.has_yellow_card = False
            player_updated = True
        elif self.yellow_card:
            # لو اللاعب معاه إنذار سابق بالفعل من جولة سابقة
            if self.player.has_yellow_card:
                self.player.has_red_card = True
                self.player.has_yellow_card = False
                self.red_card = True
                self.yellow_card = False
                # إنذارين تراكميين = طرد وإيقاف تلقائي جولة واحدة
                if self.suspension_matches == 0:
                    self.suspension_matches = 1
                    self.player.is_suspended = True
                    self.player.suspended_matches_left = 1
            else:
                self.player.has_yellow_card = True
            player_updated = True

        if player_updated:
            self.player.save()

        # 3. حساب النقاط
        pts = (self.goals * 4) + (self.assists * 3)
        if self.clean_sheet:
            category = self.player.main_category
            if category in ['GK', 'DEF']:
                pts += 4
            elif category == 'MID':
                pts += 1

        if self.yellow_card:
            pts -= 1
        if self.red_card:
            pts -= 3

        pts -= (self.penalties_missed * 1)
        self.points = pts

        super().save(*args, **kwargs)

    def __str__(self):
        return f"إحصائيات {self.player.name} - {self.gameweek}"


# 6. فريق المشترك
class UserFantasyTeam(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fantasy_teams', verbose_name="المستخدم")
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='user_teams', verbose_name="الدوري")
    name = models.CharField(max_length=100, verbose_name="اسم فرقتك")
    budget = models.DecimalField(max_digits=5, decimal_places=1, default=100.0, verbose_name="الميزانية المتبقية")
    total_points = models.IntegerField(default=0, verbose_name="إجمالي النقاط")

    class Meta:
        unique_together = ('user', 'league')

    def __str__(self):
        return f"{self.name} ({self.user.username}) - {self.league.name}"


# 7. تشكيلة المستخدم
class UserSquad(models.Model):
    user_team = models.ForeignKey(UserFantasyTeam, on_delete=models.CASCADE, related_name='squads', verbose_name="فريق المستخدم")
    gameweek = models.ForeignKey(Gameweek, on_delete=models.CASCADE, verbose_name="الجولة")
    starting_players = models.ManyToManyField(Player, related_name='starters', verbose_name="الأساسيين (6)")
    substitutes = models.ManyToManyField(Player, related_name='subs', blank=True, verbose_name="الاحتياط (4)")
    
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

    transfers_made = models.PositiveIntegerField(default=0, verbose_name="عدد التبديلات المنجزة")
    transfers_cost = models.IntegerField(default=0, verbose_name="خصم التغييرات")
    is_saved = models.BooleanField(default=False, verbose_name="تم حفظ التشكيلة")
    points_earned = models.IntegerField(default=0, verbose_name="نقاط الجولة")

    class Meta:
        unique_together = ('user_team', 'gameweek')

    def clean(self):
        super().clean()
        if self.captain and self.vice_captain and self.captain == self.vice_captain:
            raise ValidationError("لا يمكن اختيار نفس اللاعب ككابتن ونائب كابتن في نفس الوقت.")

    def __str__(self):
        return f"تشكيلة {self.user_team.name} - {self.gameweek}"


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile', verbose_name="المستخدم")
    avatar = CloudinaryField('avatar', folder='avatars/', null=True, blank=True)

    def __str__(self):
        return f"Profile of {self.user.username}"


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
from django.core.management.base import BaseCommand
from fantasy.models import Player  # استبدل fantasy باسم تطبيقك إذا كان مختلفاً


class Command(BaseCommand):
    help = 'تحديث وتعبئة إحصائيات اللاعبين التراكمية المجمعة من الجولات'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING('بدء عملية تحديث إحصائيات اللاعبين...'))

        players = Player.objects.all()
        total_players = players.count()
        updated_count = 0

        for player in players:
            try:
                player.update_accumulated_stats()
                updated_count += 1
                self.stdout.write(f'تم تحديث: {player.name} ({updated_count}/{total_players})')
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'خطأ أثناء تحديث اللاعب {player.name}: {str(e)}'))

        self.stdout.write(
            self.style.SUCCESS(f'تمت العملية بنجاح! تم تحديث {updated_count} لاعب من إجمالي {total_players}.')
        )
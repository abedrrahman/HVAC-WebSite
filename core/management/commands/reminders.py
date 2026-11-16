from django.core.management.base import BaseCommand

from core.services import run_reminders


class Command(BaseCommand):
    help = "Run the reminder check once. Safe to run at any time; it never creates duplicates."

    def handle(self, *args, **options):
        self.stdout.write(f"New notifications: {run_reminders()}")

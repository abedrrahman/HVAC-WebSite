"""Background process. Runs independently of web traffic, so reminders fire with nobody signed in.
All state is in PostgreSQL: after a restart the next pass catches up on anything missed."""
import logging
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from core.doc_search import index_pending
from core.management.commands.backup import run_backup
from core.models import SystemSettings
from core.services import run_reminders

log = logging.getLogger(__name__)
INTERVAL_SECONDS = 15 * 60


def run_pass():
    """One cycle. Never raises: nothing restarts the worker, so an outage must only cost this cycle."""
    close_old_connections()
    try:
        created = run_reminders()
        log.info("Reminder pass finished, %s new", created)
    except Exception:
        log.exception("Reminder pass failed; retrying next cycle")
    try:
        index_pending()
    except Exception:
        log.exception("Document indexing failed; retrying next cycle")
    try:
        cfg = SystemSettings.get()
        if not cfg.last_backup_run or timezone.now() - cfg.last_backup_run > timedelta(days=1):
            log.info("Backup written: %s", run_backup())
    except Exception as e:
        log.exception("Backup failed")
        try:
            SystemSettings.objects.filter(pk=1).update(last_backup_status=f"FAILED: {e}"[:300])
        except Exception:
            log.exception("Could not record the backup failure; the database may be down")


class Command(BaseCommand):
    help = "Run reminders every 15 minutes and a backup once a day."

    def handle(self, *args, **options):
        log.info("Worker started")
        while True:
            run_pass()
            time.sleep(INTERVAL_SECONDS)

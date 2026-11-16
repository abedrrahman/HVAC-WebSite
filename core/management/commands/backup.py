import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import SystemSettings

KEEP = 14


def find_pg_dump():
    found = shutil.which("pg_dump")
    if found:
        return found
    versions = sorted(Path(r"C:\Program Files\PostgreSQL").glob("*/bin/pg_dump.exe"), key=lambda p: int(p.parent.parent.name))
    return str(versions[-1]) if versions else None


def prune(folder):
    """Keep the last KEEP days: every backup of the latest day (each one precedes a data change that may need undoing),
    and the newest backup of each earlier day. Many backups in one day never push out older days."""
    by_day = {}
    for path in sorted(folder.glob("backup-*.zip")):
        by_day.setdefault(path.name[7:15], []).append(path)  # the YYYYMMDD part
    days = sorted(by_day)
    keep = set(by_day[days[-1]]) if days else set()
    keep |= {by_day[day][-1] for day in days[-KEEP:]}
    for paths in by_day.values():
        for path in paths:
            if path not in keep:
                path.unlink()


def run_backup():
    """Database dump plus uploaded files, zipped into BACKUP_ROOT. The newest 14 are kept."""
    pg_dump = find_pg_dump()
    if not pg_dump:
        raise CommandError("pg_dump was not found")
    db = settings.DATABASES["default"]
    settings.BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    dump = settings.BACKUP_ROOT / f"db-{stamp}.dump"
    archive = settings.BACKUP_ROOT / f"backup-{stamp}.zip"
    partial = settings.BACKUP_ROOT / f"backup-{stamp}.tmp"  # only a finished zip gets the name that rotation counts
    try:
        subprocess.run([pg_dump, "-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], "-F", "c", "-f", str(dump), db["NAME"]],
                       check=True, env={**os.environ, "PGPASSWORD": db["PASSWORD"]}, capture_output=True, text=True)
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dump, dump.name)
            if settings.PRIVATE_ROOT.exists():
                for path in settings.PRIVATE_ROOT.rglob("*"):
                    if path.is_file():
                        z.write(path, Path("files") / path.relative_to(settings.PRIVATE_ROOT))
        partial.replace(archive)
    finally:
        dump.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    prune(settings.BACKUP_ROOT)
    SystemSettings.objects.filter(pk=SystemSettings.get().pk).update(last_backup_run=timezone.now(), last_backup_status=archive.name)
    return archive


class Command(BaseCommand):
    help = "Back up the database and uploaded files."

    def handle(self, *args, **options):
        self.stdout.write(f"Backup written: {run_backup().name}")

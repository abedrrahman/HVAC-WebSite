from django.core.management.base import BaseCommand

from core.doc_search import index_pending
from core.models import DocumentPage


class Command(BaseCommand):
    help = "Read the text of new or replaced manuals so the assistant can search them."

    def handle(self, *args, **options):
        done = index_pending()
        self.stdout.write(f"Indexed {done} documents; {DocumentPage.objects.count()} pages searchable.")

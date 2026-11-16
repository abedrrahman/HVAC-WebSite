"""Creates the clean starting state: roles, main categories, document types, countries, and the first administrator."""
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Category, Country, DocumentType, Role, SystemSettings, User
from core.perms import DEFAULT_ROLES

CATEGORIES = [
    ("Chillers", "المبردات (تشيلر)"), ("Rooftop Units (RTU)", "وحدات السطح (RTU)"), ("Air Handling Units (AHU)", "وحدات مناولة الهواء (AHU)"),
    ("Close Control Units (CCU)", "وحدات التحكم الدقيق (CCU)"), ("Fan Coil Units (FCU)", "وحدات ملف المروحة (FCU)"), ("Heat Pumps", "المضخات الحرارية"),
    ("Package Units (PKG)", "الوحدات المجمعة (PKG)"), ("Dehumidification Units", "وحدات إزالة الرطوبة"), ("Dry Coolers", "المبردات الجافة"),
    ("Precision Air Conditioning Units", "وحدات التكييف الدقيق"), ("Split and Ducted Air Conditioning Units", "وحدات التكييف المنفصلة والمجراة"),
    ("Other Equipment", "معدات أخرى"),
]
DOCUMENT_TYPES = [
    ("Operation Manuals", "أدلة التشغيل"), ("Installation Manuals", "أدلة التركيب"), ("Maintenance Manuals", "أدلة الصيانة"),
    ("Technical Data Sheets", "صحائف البيانات الفنية"), ("Selection Sheets", "صحائف الاختيار"), ("Wiring Diagrams", "مخططات التوصيل"),
    ("Electrical Diagrams", "المخططات الكهربائية"), ("Spare Parts Lists", "قوائم قطع الغيار"), ("Controller Manuals", "أدلة وحدات التحكم"),
    ("Troubleshooting Guides", "أدلة استكشاف الأعطال"), ("Commissioning Reports", "تقارير التشغيل الأولي"), ("Certificates", "الشهادات"),
    ("Other Documents", "مستندات أخرى"),
]
COUNTRIES = [("Jordan", "الأردن", "JO"), ("Palestine", "فلسطين", "PS"), ("Iraq", "العراق", "IQ"), ("Qatar", "قطر", "QA"), ("Saudi Arabia", "السعودية", "SA")]


def seed_reference_data():
    for i, (en, ar) in enumerate(CATEGORIES, 1):
        Category.objects.get_or_create(name_en=en, defaults={"name_ar": ar, "sort_order": i})
    for en, ar in DOCUMENT_TYPES:
        DocumentType.objects.get_or_create(name_en=en, defaults={"name_ar": ar})
    for i, (en, ar, code) in enumerate(COUNTRIES, 1):
        Country.objects.get_or_create(name_en=en, defaults={"name_ar": ar, "code": code, "sort_order": i})
    for (en, ar), codes in DEFAULT_ROLES.items():
        Role.objects.get_or_create(name_en=en, defaults={"name_ar": ar, "permissions": codes})
    SystemSettings.get()


class Command(BaseCommand):
    help = "Seed reference data and create the first administrator. Safe to run repeatedly."

    def handle(self, *args, **options):
        seed_reference_data()
        if User.objects.exists():
            self.stdout.write("Reference data checked. Users already exist, so no administrator was created.")
            return
        password = secrets.token_urlsafe(12)
        User.objects.create_superuser("admin", password=password, role=Role.objects.get(name_en="Administrator"), must_change_password=True)
        # The password lives in a local file rather than scrollback or logs; the user replaces it at first sign-in.
        path = settings.BASE_DIR / "FIRST_LOGIN.txt"
        path.write_text(f"Username: admin\nTemporary password: {password}\nYou will be asked to change it at first sign-in. Delete this file afterwards.\n", encoding="utf-8")
        self.stdout.write(f"Administrator created. Credentials written to {path.name}")

from django.shortcuts import redirect
from django.urls import resolve, reverse
from django.utils import translation
from django.utils.http import urlencode

from .models import Notification

LANGS = {"en", "ar"}
PUBLIC_VIEWS = {"login", "set_language", "api_login", "api_sync", "api_report", "api_alarms", "api_alarm_ask", "api_document_file"}  # the api views check the app's token themselves


class LanguageMiddleware:
    """Cookie decides the language; English is the default."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        lang = request.COOKIES.get("lang")
        lang = lang if lang in LANGS else "en"
        translation.activate(lang)
        request.LANGUAGE_CODE = lang
        response = self.get_response(request)
        translation.deactivate()
        return response


class LoginRequiredMiddleware:
    """Secure by default: every view needs a signed-in user unless listed as public."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, args, kwargs):
        name = resolve(request.path_info).url_name
        if name in PUBLIC_VIEWS:
            return None
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
        if request.user.must_change_password and name not in {"password_change", "logout"}:
            return redirect("password_change")
        return None


NAV = [
    ("dashboard", "Dashboard", "dashboard", None),
    ("equipment", "Equipment", "unit_list", "equipment.view"),
    ("clients", "Clients", "client_list", "clients.view"),
    ("factory", "Manufacturers", "manufacturer_list", "manufacturers.view"),
    ("file", "Technical Documents", "document_list", "documents.view"),
    ("wrench", "Alarm Lookup", "alarm_lookup", "documents.view"),
    ("contract", "Maintenance Contracts", "contract_list", "contracts.view"),
    ("calendar", "Maintenance Calendar", "calendar", "visits.view"),
    ("wrench", "Maintenance Visits", "visit_list", "visits.view"),
    ("file", "Maintenance Reports", "maintenance_reports", "visits.view"),
    ("bell", "Notifications", "notifications", None),
    ("chart", "Reports", "report_list", "reports.view"),
    ("users", "User Management", "user_list", "users.manage"),
    ("settings", "System Settings", "settings", "settings.manage"),
]


# Phones get these five in a bar at the bottom, with labels short enough to fit.
BOTTOM_NAV = {"dashboard": "Dashboard", "unit_list": "Equipment", "client_list": "Clients", "alarm_lookup": "Alarms", "calendar": "Calendar"}


def shell_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else ""
    prefixes = {"unit": "unit_list", "category": "unit_list", "subtype": "unit_list", "field": "unit_list", "catalog": "unit_list",
                "client": "client_list", "manufacturer": "manufacturer_list", "model": "manufacturer_list",
                "document": "document_list", "contract": "contract_list", "schedule": "contract_list",
                "visit": "visit_list", "maintenance": "maintenance_reports", "report": "report_list", "user": "user_list", "role": "user_list",
                "country": "settings", "doctype": "settings", "activity": "settings"}
    active = "settings" if (current or "").startswith("report_form") else prefixes.get((current or "").split("_")[0], current)
    return {
        "nav": [{"icon": i, "label": l, "url": reverse(u), "active": u == active, "short": BOTTOM_NAV.get(u)} for i, l, u, p in NAV if not p or request.user.has(p)],
        "unread_count": Notification.objects.filter(user=request.user, read_at__isnull=True).count(),
    }

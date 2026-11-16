"""The phone app's connection: sign in for a token, download clients, units and document links, upload finished reports.
Token requests carry no cookies, so these views skip the CSRF check; the token is the proof of identity."""
import hashlib
import json
import secrets
from datetime import date
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone, translation
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ..forms import IMAGE_EXTENSIONS, validate_upload
from ..models import AlarmCode, Attachment, DocumentAssignment, FieldReport, User, Visit
from ..services import clients_for, documents_for, new_attachment, notify_report, record, units_for, visit_from_report, visits_for
from .documents import _serve, alarm_ask
from .general import LOCK_SECONDS, MAX_LOGIN_FAILURES, client_ip

MAX_PHOTOS = 30


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


def api_user(view):
    """Finds the user from 'Authorization: Token ...'. A missing, wrong or deactivated token gets 401, never a login page."""
    @csrf_exempt
    @wraps(view)
    def wrapped(request, *a, **k):
        kind, _, token = request.headers.get("Authorization", "").partition(" ")
        user = User.objects.filter(api_token_hash=_hash(token), is_active=True).select_related("role").first() if kind == "Token" and token else None
        if not user:
            return _error("Sign in again.", 401)
        request.user = user
        return view(request, *a, **k)
    return wrapped


@csrf_exempt
@require_POST
def api_login(request):
    """Username and password in, token out. Shares the website's lockout, so the app cannot be used to guess passwords faster."""
    try:
        body = json.loads(request.body or b"{}")
        username, password = str(body["username"])[:150], str(body["password"])
    except (ValueError, KeyError, TypeError):
        return _error("Send username and password.", 400)
    key = f"login:{username.lower()}:{client_ip(request)}"
    if cache.get(key, 0) >= MAX_LOGIN_FAILURES:
        return _error("Too many failed attempts. Try again in 15 minutes.", 429)
    user = authenticate(request, username=username, password=password)
    if not user or not user.is_active:
        cache.set(key, cache.get(key, 0) + 1, LOCK_SECONDS)
        return _error("Wrong username or password.", 401)
    cache.delete(key)
    if user.must_change_password:
        return _error("Change your temporary password on the website first.", 403)
    token = secrets.token_hex(32)
    user.api_token_hash = _hash(token)  # one phone per user: a new sign-in replaces the old token
    user.save(update_fields=["api_token_hash"])
    record(user, "app_signed_in", user)
    return JsonResponse({"token": token, "name": str(user), "language": user.language})


def _base(request):
    return settings.SITE_URL or request.build_absolute_uri("/").rstrip("/")


@require_GET
@api_user
def api_sync(request):
    """Everything the app keeps on the phone, limited to what this user may see on the website."""
    user, base = request.user, _base(request)
    if not user.has("equipment.view"):
        return _error("Your role cannot view equipment.", 403)
    units = list(units_for(user).filter(is_active=True).select_related("client", "category", "subtype", "model__manufacturer"))
    docs = {d.pk: d for d in documents_for(user).filter(is_archived=False).select_related("doc_type")} if user.has("documents.view") else {}
    reach = {"unit": {}, "model": {}, "subtype": {}, "category": {}, "manufacturer": {}, "all": []}
    for level, doc, *targets in DocumentAssignment.objects.filter(document__in=docs).values_list("level", "document_id", "unit_id", "model_id", "subtype_id", "category_id", "manufacturer_id"):
        if level == "all":
            reach["all"].append(doc)
        else:
            target = targets[["unit", "model", "subtype", "category", "manufacturer"].index(level)]
            reach[level].setdefault(target, []).append(doc)

    def unit_docs(u):
        found = (reach["unit"].get(u.pk, []) + reach["model"].get(u.model_id, []) + reach["subtype"].get(u.subtype_id, [])
                 + reach["category"].get(u.category_id, []) + reach["manufacturer"].get(u.model.manufacturer_id, []) + reach["all"])
        return sorted(set(found))

    today = timezone.localdate()
    open_visits = visits_for(user).open().filter(unit__in=units).order_by("scheduled_date") if user.has("visits.view") else Visit.objects.none()
    client_ids = {u.client_id for u in units}
    return JsonResponse({
        "synced_at": timezone.now().isoformat(),
        "clients": [{"id": c.pk, "name_en": c.name_en, "name_ar": c.name_ar, "country": c.country.name_en, "city": c.city}
                    for c in clients_for(user).filter(pk__in=client_ids).select_related("country")],
        "units": [{"id": u.pk, "name_en": u.name_en, "name_ar": u.name_ar, "client_id": u.client_id, "category": u.category.name_en, "subtype": u.subtype.name_en if u.subtype else "",
                   "manufacturer": u.model.manufacturer.name_en, "model": u.model.model_number, "serial_number": u.serial_number, "project": u.project, "location": u.location,
                   "qr_token": u.qr_token, "url": base + u.get_absolute_url(), "document_ids": unit_docs(u)} for u in units],
        "documents": [{"id": d.pk, "name_en": d.name_en, "name_ar": d.name_ar, "type": d.doc_type.name_en, "language": d.language,
                       "url": base + reverse("document_file", args=[d.pk]), "file_name": d.original_name, "size": d.size} for d in docs.values()],
        "visits": [{"id": v.pk, "number": v.number, "unit_id": v.unit_id, "scheduled_date": v.scheduled_date.isoformat(), "type": v.visit_type, "overdue": v.scheduled_date < today}
                   for v in open_visits],
    })


@require_GET
@api_user
def api_document_file(request, pk):
    """A document's file for the app to keep on the phone, so manuals open with no signal. Same visibility rules as the website."""
    if not request.user.has("documents.view"):
        return _error("Your role cannot view documents.", 403)
    doc = documents_for(request.user).filter(is_archived=False, pk=pk).first()
    if not doc:
        return _error("This document is not available.", 404)
    return _serve(request, doc.file, doc.original_name, False)


@require_GET
@api_user
def api_alarm_ask(request):
    """The Alarm Lookup assistant for the app: the same answer the website gives, in the language the app asks for."""
    with translation.override("ar" if request.GET.get("lang") == "ar" else "en"):
        return alarm_ask(request)


@require_GET
@api_user
def api_alarms(request):
    """The whole alarm list, so the app can look codes up with no signal. The app sends the version it holds and gets nothing back when it is current."""
    if not request.user.has("documents.view"):
        return _error("Your role cannot view documents.", 403)
    fields = ("code", "key", "controller__name_en", "name_en", "name_ar", "causes_en", "causes_ar", "checks_en", "checks_ar", "effect_en", "effect_ar",
              "action_en", "action_ar", "reset_en", "reset_ar")
    alarms = [list(row) for row in AlarmCode.objects.order_by("pk").values_list(*fields)]
    # A fingerprint of the text itself: a corrected alarm reaches the phones, which a count or the newest id would miss.
    version = hashlib.md5(json.dumps(alarms).encode()).hexdigest()
    if request.GET.get("have") == version:
        return JsonResponse({"version": version, "alarms": None})
    return JsonResponse({"version": version, "alarms": alarms, "fields": [f.replace("controller__name_en", "controller") for f in fields]})


@require_POST
@api_user
def api_report(request):
    """A finished report from the app becomes a completed visit on the unit, with the PDF and photos attached.
    Sending the same report again returns the same visit, so the app can safely retry after a lost connection."""
    user = request.user
    if not user.has("visits.update"):
        return _error("Your role cannot complete visits.", 403)
    try:
        data = json.loads(request.POST.get("report", ""))
        report_id = str(data["id"])[:64]
        assert report_id and isinstance(data, dict)
    except (ValueError, KeyError, TypeError, AssertionError):
        return _error("The report data is missing or unreadable.", 400)
    done = Visit.objects.filter(app_report_id=report_id).first()
    if done:
        return JsonResponse({"visit_id": done.pk, "number": done.number, "url": _base(request) + done.get_absolute_url(), "already_uploaded": True, "pending": False})
    if FieldReport.objects.filter(app_report_id=report_id).exists():
        return JsonResponse({"visit_id": None, "number": "", "url": _base(request) + reverse("maintenance_reports") + "#daily", "already_uploaded": True, "pending": True})

    mine = units_for(user).filter(is_active=True)
    unit = mine.filter(pk=data["unit_id"]).first() if str(data.get("unit_id", "")).isdigit() else None
    serial = str(data.get("serial_no", "")).strip()
    model = str(data.get("machine_model", "")).strip()
    if not unit and serial and model:
        # Hand-typed serials can be short ("12345"): the model has to agree too, or the report waits under Daily reports.
        matches = list(mine.filter(serial_number__iexact=serial, model__model_number__iexact=model)[:2])
        unit = matches[0] if len(matches) == 1 else None

    pdf, photos = request.FILES.get("pdf"), request.FILES.getlist("photo")[:MAX_PHOTOS]
    try:
        if pdf:
            validate_upload(pdf, {"pdf"})
        for p in photos:
            validate_upload(p, IMAGE_EXTENSIONS)
    except ValidationError as e:
        return _error(e.messages[0], 400)
    try:  # the app writes dates without leading zeros, such as 2026-7-2
        when = min(date(*(int(x) for x in str(data.get("date", "")).split("-")[:3])), timezone.localdate())
    except (ValueError, TypeError):
        when = timezone.localdate()

    text = lambda key, limit: str(data.get(key, "") or "")[:limit]
    # The whole report is kept either way, so the report page can show every reading in its own box.
    kept = dict(app_report_id=report_id, data=data, client_name=text("client", 200), site=text("site", 200), machine_type=text("type", 30),
                machine_model=text("machine_model", 120), serial_no=text("serial_no", 120), maintenance_type=text("maintenance_type", 30),
                report_date=when, uploaded_by=user)
    with transaction.atomic():
        if unit:
            open_visit = None
            if str(data.get("visit_id", "")).isdigit():
                open_visit = visits_for(user).open().select_for_update().filter(pk=data["visit_id"], unit=unit).first()
            owner = visit_from_report(unit, data, user, when, report_id, open_visit)
            report = FieldReport.objects.create(visit=owner, **kept)
            answer = {"visit_id": owner.pk, "number": owner.number, "url": _base(request) + owner.get_absolute_url(), "pending": False}
            files = {"visit": owner}
        else:
            # The site or unit is not in the database: keep the whole report under Daily Reports until someone adds them.
            owner = report = FieldReport.objects.create(**kept)
            answer = {"visit_id": None, "number": "", "url": _base(request) + reverse("maintenance_reports") + "#daily", "pending": True}
            files = {"field_report": owner}
        if pdf:
            new_attachment(pdf, user, Attachment.Kind.REPORT, **files)
        for p in photos:
            new_attachment(p, user, Attachment.Kind.PHOTO, **files)
        record(user, "app_report_uploaded", owner, f"{text('technician', 100)} · {len(photos)} photos")
        what = " · ".join(x for x in (str(unit) if unit else text("client", 200) or text("site", 200), text("machine_model", 120)) if x) or report_id
        notify_report(report, "reports.review", "received", f"New maintenance report waiting for the foreman: {what}",
                      f"تقرير صيانة جديد بانتظار رئيس العمال: {what}", actor=user)
    return JsonResponse({**answer, "already_uploaded": False}, status=201)

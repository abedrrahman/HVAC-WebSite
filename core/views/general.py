"""Sign-in, language, dashboard, search, notifications, reports, users, settings."""
import calendar as pycal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout, update_session_auth_hash
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .. import forms as f
from ..i18n import t
from ..models import (ActivityLog, Category, Client, Contract, Country, Document, DocumentType, EquipmentModel, Manufacturer, Notification, Role,
                      Subtype, SystemSettings, Unit, User, Visit, add_months)
from ..perms import PERMISSION_LABELS, PERMISSIONS
from ..phone_report import LIMIT_DEFS, UNITS, limit_text, resolved_limits
from ..services import clients_for, contracts_for, documents_for, in_category, limits_for, record, run_reminders, units_for, visits_for
from .common import action, active_badge, fmt, form_page, link, list_page, post_only, requires, saved
from .maintenance import week_bounds

MAX_LOGIN_FAILURES = 5
LOCK_SECONDS = 900


def remember_language(response, lang):
    response.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")


def client_ip(request):
    """Behind the HTTPS proxy every request comes from the proxy, which adds the real visitor last; earlier entries can be faked.
    Only a proxy on this PC is believed: anyone else could send the header themselves and dodge the login lockout."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and request.META.get("REMOTE_ADDR") in ("127.0.0.1", "::1"):
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = f.LoginForm(request, data=request.POST or None)
    if request.method == "POST":
        # shortcut: per-process lockout keyed by username and IP; use a shared cache if running several processes.
        key = f"login:{request.POST.get('username', '')[:150].lower()}:{client_ip(request)}"
        if cache.get(key, 0) >= MAX_LOGIN_FAILURES:
            messages.error(request, t("Too many failed attempts. Try again in 15 minutes."))
        elif form.is_valid():
            cache.delete(key)
            auth_login(request, form.get_user())
            target = request.POST.get("next", "")
            safe = url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure())
            response = redirect(target if safe else "dashboard")
            if "lang" not in request.COOKIES:  # a new browser starts in the user's saved language
                remember_language(response, request.user.language)
            return response
        else:
            cache.set(key, cache.get(key, 0) + 1, LOCK_SECONDS)
    return render(request, "login.html", {"form": form, "next": request.GET.get("next", request.POST.get("next", ""))})


@post_only
def logout_view(request):
    auth_logout(request)
    return redirect("login")


def password_change(request):
    form = f.ChangePasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.api_token_hash = None  # the phone app must sign in again with the new password
        user.save(update_fields=["must_change_password", "api_token_hash"])
        update_session_auth_hash(request, user)
        record(user, "password_changed", user)
        messages.success(request, t("Saved successfully."))
        return redirect("dashboard")
    intro = t("Please choose a new password to continue.") if request.user.must_change_password else None
    return form_page(request, form, "Change Password", reverse("dashboard"), intro=intro)


@post_only
def set_language(request):
    lang = request.POST.get("lang")
    response = JsonResponse({"ok": True})
    if lang in ("en", "ar"):
        remember_language(response, lang)
        if request.user.is_authenticated and request.user.language != lang:
            request.user.language = lang
            request.user.save(update_fields=["language"])
    return response


def _bars(rows):
    rows = [(label or "—", n) for label, n in rows if n]
    top = max((n for _, n in rows), default=1)
    total = sum(n for _, n in rows) or 1
    return [{"label": label, "n": n, "pct": round(n / top * 100), "share": round(n / total * 100)} for label, n in rows]


def dashboard(request):
    u = request.user
    today = timezone.localdate()
    units = units_for(u).filter(is_active=True) if u.has("equipment.view") else Unit.objects.none()
    visits = visits_for(u) if u.has("visits.view") else Visit.objects.none()
    contracts = contracts_for(u) if u.has("contracts.view") else Contract.objects.none()
    docs = documents_for(u).filter(is_archived=False) if u.has("documents.view") else Document.objects.none()
    month_end = today.replace(day=pycal.monthrange(today.year, today.month)[1])
    week_end = week_bounds(today)[1]
    lang_field = "name_ar" if request.LANGUAGE_CODE == "ar" else "name_en"

    def group(field):
        rows = units.values(f"{field}__{lang_field}", f"{field}__name_en").annotate(n=Count("pk", distinct=True)).order_by("-n")[:10]
        return _bars([(r[f"{field}__{lang_field}"] or r[f"{field}__name_en"], r["n"]) for r in rows])

    maintenance_cards = [
        ("Active Maintenance Contracts", contracts.with_status("active").count(), reverse("contract_list") + "?status=active", "contract", ""),
        ("Contracts Expiring Soon", contracts.with_status("expiring").count(), reverse("contract_list") + "?status=expiring", "bell", "warn"),
        ("Total Scheduled Visits", visits.open().filter(scheduled_date__gte=today).count(), reverse("visit_list"), "calendar", ""),
        ("Visits Due This Week", visits.open().filter(scheduled_date__gte=today, scheduled_date__lte=week_end).count(), reverse("visit_list") + "?quick=week", "calendar", "warn"),
        ("Visits Due This Month", visits.open().filter(scheduled_date__gte=today, scheduled_date__lte=month_end).count(), reverse("visit_list") + "?quick=month", "calendar", ""),
        ("Overdue Visits", visits.with_status("overdue").count(), reverse("visit_list") + "?status=overdue", "alert", "bad"),
        ("Completed Visits", visits.filter(status="completed").count(), reverse("visit_list") + "?status=completed", "check", "good"),
    ]
    # Visits per month: five months back, this month, six ahead.
    start = add_months(today.replace(day=1), -5)
    per_month = {r["m"]: r for r in visits.exclude(status="cancelled").filter(scheduled_date__gte=start, scheduled_date__lt=add_months(start, 12))
                 .order_by().annotate(m=TruncMonth("scheduled_date")).values("m").annotate(n=Count("pk"), done=Count("pk", filter=Q(status="completed")))}
    months = [{"date": add_months(start, i), "n": per_month.get(add_months(start, i), {}).get("n", 0), "done": per_month.get(add_months(start, i), {}).get("done", 0)} for i in range(12)]
    top = max(m["n"] for m in months) or 1
    for m in months:
        m.update(pct=round(m["n"] / top * 100), now=m["date"].month == today.month and m["date"].year == today.year, peak=m["n"] == top and m["n"] > 0)
    status = [("overdue", "Overdue", maintenance_cards[5][1]), ("todo", "To Do", maintenance_cards[2][1]),
              ("in_progress", "In Progress", visits.filter(status="in_progress").count()), ("completed", "Completed", maintenance_cards[6][1])]
    total = sum(n for _, _, n in status)
    return render(request, "dashboard.html", {
        "today": today, "maintenance_cards": maintenance_cards, "months": months, "has_visits": any(m["n"] for m in months),
        "status_parts": [{"key": k, "label": label, "n": n, "pct": round(n / total * 100) if total else 0} for k, label, n in status], "status_total": total,
        "attention_cards": [maintenance_cards[i] for i in (5, 3, 1)],
        "summary_cards": [maintenance_cards[i] for i in (0, 2, 4, 6)],
        "equipment_cards": [
            ("Total Equipment Units", units.count(), reverse("unit_list"), "equipment"),
            ("Total Clients", clients_for(u).filter(is_active=True, units__is_active=True).distinct().count() if u.has("clients.view") else 0, reverse("client_list"), "clients"),
            ("Total Manufacturers", Manufacturer.objects.filter(is_active=True).count() if u.has("manufacturers.view") else 0, reverse("manufacturer_list"), "factory"),
            ("Total Equipment Categories", Category.objects.filter(is_active=True).count(), reverse("catalog") if u.has("catalog.manage") else reverse("unit_list"), "layers"),
            ("Total Technical Documents", docs.count(), reverse("document_list"), "file"),
        ],
        "by_category": group("category"), "by_country": group("client__country"), "by_manufacturer": group("model__manufacturer"),
        "recent_units": units.select_related("client", "category").order_by("-created_at")[:6],
        "recent_docs": docs.select_related("doc_type").order_by("-uploaded_at")[:6],
        "upcoming": visits.open().filter(scheduled_date__gte=today).select_related("unit", "client").order_by("scheduled_date")[:6],
        "overdue": visits.with_status("overdue").select_related("unit", "client").order_by("scheduled_date")[:6],
        "expiring": contracts.with_status("expiring").exclude(renewals__status="active").select_related("client").order_by("end_date")[:6],
        "notifications": u.notifications.all()[:6],
        "show_setup": u.has("catalog.manage") and not Unit.objects.exists(),
    })


class SearchFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    kind = forms.ChoiceField(label="Result type", required=False, choices=[("", "All"), ("equipment", "Equipment"), ("clients", "Clients"), ("documents", "Documents"), ("contracts", "Contracts"), ("visits", "Maintenance Visits")])
    country = forms.ModelChoiceField(Country.objects.all(), label="Country", required=False)
    category = forms.ModelChoiceField(Category.objects.all(), label="Main Equipment Type", required=False)
    client = forms.ModelChoiceField(Client.objects.all(), label="Client", required=False)
    manufacturer = forms.ModelChoiceField(Manufacturer.objects.all(), label="Manufacturer", required=False)


def search(request):
    u = request.user
    form = SearchFilter(request.GET or None)
    form.fields["client"].queryset = clients_for(u)
    groups = []
    if form.is_valid():
        d = form.cleaned_data
        q, kind = d["q"].strip(), d["kind"]
        filtered = q or d["country"] or d["category"] or d["client"] or d["manufacturer"]
        if filtered:
            limit = 50

            def want(k, perm):
                return (not kind or kind == k) and u.has(perm)

            if want("equipment", "equipment.view"):
                qs = units_for(u).select_related("client__country", "category", "subtype", "model__manufacturer")
                if q:
                    qs = qs.filter(Q(name_en__icontains=q) | Q(name_ar__icontains=q) | Q(serial_number__icontains=q) | Q(model__model_number__icontains=q)
                                   | Q(category__name_en__icontains=q) | Q(category__name_ar__icontains=q) | Q(subtype__name_en__icontains=q) | Q(subtype__name_ar__icontains=q)
                                   | Q(model__manufacturer__name_en__icontains=q) | Q(model__manufacturer__name_ar__icontains=q) | Q(client__name_en__icontains=q) | Q(client__name_ar__icontains=q)
                                   | Q(client__country__name_en__icontains=q) | Q(client__country__name_ar__icontains=q))
                qs = qs.filter(**{k: v for k, v in {"client__country": d["country"], "category": d["category"], "client": d["client"], "model__manufacturer": d["manufacturer"]}.items() if v})
                groups.append(("Equipment", [(x.get_absolute_url(), x.name, f"{x.client.name} · {x.model.manufacturer.name} {x.model.model_number} · {x.serial_number or '—'}") for x in qs[:limit]]))
            if want("clients", "clients.view") and not (d["category"] or d["manufacturer"]):
                qs = clients_for(u).select_related("country")
                if q:
                    qs = qs.filter(Q(name_en__icontains=q) | Q(name_ar__icontains=q) | Q(country__name_en__icontains=q) | Q(country__name_ar__icontains=q) | Q(city__icontains=q))
                qs = qs.filter(**{k: v for k, v in {"country": d["country"], "pk": d["client"].pk if d["client"] else None}.items() if v})
                groups.append(("Clients", [(x.get_absolute_url(), x.name, f"{x.country.name} · {x.city}") for x in qs[:limit]]))
            if want("documents", "documents.view") and not (d["country"] or d["client"]):
                qs = documents_for(u).filter(is_archived=False).select_related("doc_type")
                if q:
                    qs = qs.filter(Q(name_en__icontains=q) | Q(name_ar__icontains=q) | Q(tags__icontains=q) | Q(original_name__icontains=q))
                if d["manufacturer"]:
                    qs = qs.filter(manufacturer=d["manufacturer"])
                if d["category"]:
                    qs = in_category(qs, d["category"])
                groups.append(("Documents", [(x.get_absolute_url(), x.name, x.doc_type.name) for x in qs[:limit]]))
            if want("contracts", "contracts.view") and not (d["category"] or d["manufacturer"]):
                qs = contracts_for(u).select_related("client")
                if q:
                    qs = qs.filter(Q(number__icontains=q) | Q(title__icontains=q) | Q(client__name_en__icontains=q) | Q(client__name_ar__icontains=q))
                qs = qs.filter(**{k: v for k, v in {"client__country": d["country"], "client": d["client"]}.items() if v})
                groups.append(("Contracts", [(x.get_absolute_url(), x.number, f"{x.title} · {x.client.name}") for x in qs[:limit]]))
            if want("visits", "visits.view"):
                qs = visits_for(u).select_related("unit", "client")
                if q:
                    qs = qs.filter(Q(number__icontains=q) | Q(unit__serial_number__icontains=q) | Q(unit__name_en__icontains=q) | Q(unit__name_ar__icontains=q))
                qs = qs.filter(**{k: v for k, v in {"client__country": d["country"], "unit__category": d["category"], "client": d["client"], "unit__model__manufacturer": d["manufacturer"]}.items() if v})
                groups.append(("Maintenance Visits", [(x.get_absolute_url(), x.number, f"{x.client.name} · {x.unit.name} · {fmt(x.scheduled_date)}") for x in qs.order_by("-scheduled_date")[:limit]]))
    return render(request, "search.html", {"form": form, "groups": groups, "searched": bool(request.GET.get("q") or len(request.GET) > 1), "total": sum(len(g[1]) for g in groups)})


# ---- Notifications ----

def notifications(request):
    show = request.GET.get("show", "unread")
    qs = request.user.notifications.select_related("visit", "contract")
    qs = qs.filter(read_at__isnull=show != "read") if show in ("unread", "read") else qs
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "notifications.html", {"page": page, "show": show, "unread": request.user.notifications.filter(read_at__isnull=True).count()})


@post_only
def notification_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    if not n.read_at:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    return redirect(request.POST.get("next") if request.POST.get("next") == n.target_url else "notifications")


@post_only
def notifications_read_all(request):
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notifications")


# ---- Users and roles ----

@requires("users.manage")
def user_list(request):
    return list_page(request, "User Management", User.objects.select_related("role").order_by("username"),
                     ["Username", "Name", "Email", "Role", "Edits reports", "Status", "Last sign-in"],
                     lambda x: [link(reverse("user_edit", args=[x.pk]), x.username), x.get_full_name() or "—", x.email or "—", x.role.name if x.role else ("—"),
                                t("Yes") if x.can_edit_reports or x.has("users.manage") else "—", active_badge(x.is_active), fmt(x.last_login)],
                     actions=[action("Add User", reverse("user_new")), action("Roles & Permissions", reverse("role_list"), "settings", False)])


@requires("users.manage")
def user_edit(request, pk=None):
    obj = get_object_or_404(User, pk=pk) if pk else None
    form = f.UserForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        if obj and obj.pk == request.user.pk and not form.cleaned_data["is_active"]:
            form.add_error("is_active", t("You cannot deactivate your own account."))
        else:
            before = bool(obj and User.objects.filter(pk=obj.pk).values_list("can_edit_reports", flat=True).first())  # the form has already changed obj
            x = form.save()
            saved(request, x, "updated" if pk else "created",
                  "" if before == x.can_edit_reports else "May now edit maintenance reports" if x.can_edit_reports else "Can no longer edit maintenance reports")
            return redirect("user_list")
    return form_page(request, form, "Edit User" if pk else "Add User", reverse("user_list"))


@requires("users.manage")
def role_list(request):
    roles = Role.objects.annotate(n=Count("user")).order_by("pk")
    return list_page(request, "Roles & Permissions", roles, ["Role", "Users", "Permissions"],
                     lambda r: [link(reverse("role_edit", args=[r.pk]), r.name), r.n, len(r.permissions)], actions=[action("Add Role", reverse("role_new"))])


@requires("users.manage")
def role_edit(request, pk=None):
    obj = get_object_or_404(Role, pk=pk) if pk else None
    form = f.RoleForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        role = form.save(commit=False)
        valid = set(PERMISSION_LABELS)
        role.permissions = [c for c in request.POST.getlist("perm") if c in valid]
        if obj and request.user.role_id == obj.pk and "users.manage" not in role.permissions and not request.user.is_superuser:
            messages.error(request, t("You cannot remove your own access to user management."))
        else:
            role.save()
            saved(request, role, "updated" if pk else "created", ",".join(role.permissions))
            return redirect("role_list")
    current = set(obj.permissions) if obj else set()
    groups = [(t(g), [{"code": c, "label": t(PERMISSION_LABELS[c]), "on": c in current} for c in codes]) for g, codes in PERMISSIONS.items()]
    return render(request, "role_form.html", {"form": form, "groups": groups, "title": t("Edit Role") if pk else t("Add Role"), "role": obj})


@post_only
@requires("users.manage")
def role_delete(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if role.user_set.exists():
        messages.error(request, t("This role is still assigned to users."))
    else:
        record(request.user, "deleted", role)
        role.delete()
        messages.success(request, t("Deleted."))
    return redirect("role_list")


# ---- Settings ----

@requires("settings.manage")
def settings_view(request):
    cfg = SystemSettings.get()
    form = f.SettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        saved(request, form.save(), "settings_updated")
        return redirect("settings")
    return render(request, "settings.html", {"form": form, "cfg": cfg, "countries": Country.objects.all(), "doctypes": DocumentType.objects.all(),
                                             "categories_empty": not Subtype.objects.exists(), "activity": ActivityLog.objects.select_related("user")[:15]})


def reading_limits(request, scope="all", pk=None):
    """Edit the limits that mark a reading red on a maintenance report: for everything (Settings), for one model, or for one unit.
    A unit's own limit wins over its model's, and a model's over Settings. Every change goes to the Activity Log."""
    user = request.user
    if scope == "unit":
        obj = get_object_or_404(units_for(user).select_related("model"), pk=pk) if user.has("equipment.edit") else None
        above, back, subject = limits_for(model=obj.model) if obj else {}, obj and obj.get_absolute_url(), obj and obj.name
    elif scope == "model":
        obj = get_object_or_404(EquipmentModel, pk=pk) if user.has("manufacturers.edit") else None
        above, back, subject = limits_for(), obj and reverse("model_detail", args=[obj.pk]), obj and obj.model_number
    else:
        obj = SystemSettings.get() if user.has("settings.manage") else None
        above, back, subject = resolved_limits(), reverse("settings"), ""
    if not obj:
        raise PermissionDenied
    stored = obj.reading_limits if isinstance(obj.reading_limits, dict) else {}
    pair_text = lambda key, low, high: limit_text(low, high, UNITS[LIMIT_DEFS[key][1]]) or "—"

    class Form(f.Bilingual, forms.Form):
        pass
    form = Form(request.POST or None)
    form.sections = []
    for key, (label, kind, *_rest) in LIMIT_DEFS.items():
        low, high, source = above[key]
        # Settings shows the limits in force, ready to change. A model or unit shows only what it sets itself: empty means "as above".
        shown = (stored.get(key) or [low, high]) if scope == "all" else (stored.get(key) or [None, None])
        hint = t("From the manuals") if scope == "all" else t("Used when left empty")
        form.fields[f"{key}_low"] = forms.FloatField(required=False, initial=shown[0], label=f"{t('Minimum')} ({UNITS[kind]})",
                                                     help_text=f"{hint}: {pair_text(key, low, high)}" + (f" · {source}" if source and scope != "all" else ""))
        form.fields[f"{key}_high"] = forms.FloatField(required=False, initial=shown[1], label=f"{t('Maximum')} ({UNITS[kind]})")
        form.sections.append((label, [f"{key}_low", f"{key}_high"]))
    if request.method == "POST" and form.is_valid():
        new, changed = {}, []
        for key, (label, *_rest) in LIMIT_DEFS.items():
            low, high = form.cleaned_data[f"{key}_low"], form.cleaned_data[f"{key}_high"]
            if low is not None and high is not None and low > high:
                form.add_error(f"{key}_low", t("The minimum is higher than the maximum."))
                continue
            # Settings keeps only what differs from the manuals; a model or unit keeps only what it fills in.
            keep = [low, high] != list(above[key][:2]) if scope == "all" else low is not None or high is not None
            if keep:
                new[key] = [low, high]
            before, after = stored.get(key), new.get(key)
            if before != after:
                words = lambda pair: pair_text(key, *pair) if pair else ("as in the manuals" if scope == "all" else "as above")
                changed.append(f"{label}: {words(before)} → {words(after)}")
        if not form.errors:
            if changed:
                obj.reading_limits = new
                obj.save(update_fields=["reading_limits"])
                record(user, "limits_changed", obj, "\n".join(changed))
                messages.success(request, t("Saved successfully."))
            return redirect(back)
    intro = {"all": "These limits apply to every unit. A reading outside its limit is shown in red on the maintenance report. Empty both boxes to switch a limit off.",
             "model": "Fill in only the limits that differ for this model. Empty boxes follow Settings. A single unit can still have its own.",
             "unit": "Fill in only the limits that differ for this unit. Empty boxes follow its model, then Settings."}[scope]
    return form_page(request, form, "Reading limits", back, submit="Save", subject=subject, intro=t(intro))


@post_only
@requires("settings.manage")
def run_reminders_now(request):
    created = run_reminders()
    messages.success(request, t("Reminder check finished. New notifications:") + f" {created}")
    return redirect("settings")


@post_only
@requires("settings.manage")
def load_templates(request):
    from ..templates_data import load_suggested
    added = load_suggested()
    record(request.user, "templates_loaded", SystemSettings.get(), str(added))
    messages.success(request, t("Suggested subtypes and technical fields loaded. Items added:") + f" {added}")
    return redirect("catalog")


def _lookup_edit(request, form_class, model, pk, title):
    obj = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved(request, form.save(), "updated" if pk else "created")
        return redirect("settings")
    return form_page(request, form, title, reverse("settings"))


@requires("settings.manage")
def country_edit(request, pk=None):
    return _lookup_edit(request, f.CountryForm, Country, pk, "Edit Country" if pk else "Add Country")


@requires("settings.manage")
def doctype_edit(request, pk=None):
    return _lookup_edit(request, f.DocumentTypeForm, DocumentType, pk, "Edit Document Type" if pk else "Add Document Type")


@requires("settings.manage")
def activity(request):
    return list_page(request, "Activity Log", ActivityLog.objects.select_related("user"), ["Date", "User", "Action", "Record", "Details"],
                     lambda a: [timezone.localtime(a.created_at).strftime("%Y-%m-%d %H:%M"), str(a.user) if a.user else "—", a.action, f"{a.target_type}: {a.target_label}",
                                a.details.replace("\n", " · ")[:2000 if a.action in ("report_edited", "limits_changed") else 120]], per_page=50)  # a report edit lists every old → new value in full

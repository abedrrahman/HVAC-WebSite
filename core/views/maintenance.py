"""Contracts, schedules, visits and the calendar."""
import calendar as pycal
import hashlib
import json
from urllib.parse import quote
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from .. import forms as f
from ..i18n import t
from ..phone_report import report_view
from ..report_pdf import build_report_pdf
from ..models import (ActivityLog, Attachment, Category, Client, Contract, Country, FieldReport, Schedule, SystemSettings, Unit, User, Visit, VisitReport, add_months)
from ..services import (can_edit_report, close_open_contract_visits, dispatch_visits, technicians, contracts_for, copy_for_renewal, field_reports_for, follow_schedule_people, limits_for, new_attachment,
                        notify_report, record, report_texts, set_contract_units,
                        sync_contract_visits, unit_conflicts, visit_from_report, visits_for)
from .common import action, badge, fmt, form_page, link, list_page, post_only, requires, saved


# ---- Contracts ----

class ContractFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    client = forms.ModelChoiceField(Client.objects.all(), label="Client", required=False)
    status = forms.ChoiceField(label="Contract Status", required=False, choices=[("", "—"), *Contract.STATUS_LABELS.items()])


@requires("contracts.view")
def contract_list(request):
    qs = contracts_for(request.user).select_related("client")
    form = ContractFilter(request.GET or None)
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            qs = qs.filter(Q(number__icontains=d["q"]) | Q(title__icontains=d["q"]) | Q(client__name_en__icontains=d["q"]) | Q(client__name_ar__icontains=d["q"]))
        if d["client"]:
            qs = qs.filter(client=d["client"])
        if d["status"]:
            qs = qs.with_status(d["status"])
    acts = [action("Add Contract", reverse("contract_new"))] if request.user.has("contracts.edit") else []
    return list_page(request, "Maintenance Contracts", qs, ["Contract Number", "Contract Title", "Client", "Contract Start Date", "Contract End Date", "Contract Status"],
                     None, filters=form, actions=acts,
                     card=lambda c: {"url": c.get_absolute_url(), "icon": "contract", "name": f"{c.number} — {c.title}", "meta": f"{c.client.name} · {fmt(c.start_date)} → {fmt(c.end_date)}", "badge": badge(c.display_status, c.status_label)})


@requires("contracts.edit")
def contract_edit(request, pk=None):
    obj = get_object_or_404(contracts_for(request.user), pk=pk) if pk else None
    initial = {"currency": SystemSettings.get().default_currency, "client": request.GET.get("client")} if not pk else None
    form = f.ContractForm(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.save()
        saved(request, c, "updated" if pk else "created")
        if pk:
            sync_contract_visits(c)
            return redirect(c)
        return redirect("contract_equipment", pk=c.pk)
    return form_page(request, form, "Edit Contract" if pk else "Add Contract", obj.get_absolute_url() if obj else reverse("contract_list"))


def _contract_or_404(request, pk):
    return get_object_or_404(contracts_for(request.user).select_related("client__country"), pk=pk)


@requires("contracts.view")
def contract_detail(request, pk):
    c = _contract_or_404(request, pk)
    u = request.user
    units = c.active_units.select_related("category", "model__manufacturer")
    schedules = c.schedules.select_related("unit", "engineer", "technician") if u.has("schedules.view") else []
    individual = {s.unit_id: s for s in schedules if s.scope == "unit" and s.is_active}
    general = next((s for s in schedules if s.scope == "general" and s.is_active), None)
    rows = []
    open_visits = {}
    for v in c.visits.open().filter(scheduled_date__gte=timezone.localdate()).order_by("scheduled_date"):
        open_visits.setdefault(v.unit_id, v)
    for x in units:
        s = individual.get(x.pk)
        covered = general and (general.include_new_units or general.covered_units.filter(pk=x.pk).exists())
        rows.append({"unit": x, "schedule": s or (general if covered else None), "next": open_visits.get(x.pk)})
    return render(request, "contract_detail.html", {
        "c": c, "unit_rows": rows, "schedules": schedules, "removed": c.equipment_links.filter(removed_at__isnull=False).select_related("unit"),
        "visits": visits_for(u).filter(contract=c).select_related("unit", "technician").order_by("-scheduled_date")[:50] if u.has("visits.view") else [],
        "attachment_form": f.AttachmentForm(kinds=[Attachment.Kind.CONTRACT]), "attachments": c.attachments.all(), "duration": c.duration_months,
        "renewals": c.renewals.all(),
    })


@requires("visits.manage")
def contract_dispatch(request, pk):
    """One button for a whole contract: every unit under it gets a visit, all started and sent to one technician's phone.
    A unit's next planned visit (within a month) is used when there is one, so no duplicate is made."""
    c = _contract_or_404(request, pk)
    units = list(c.active_units.filter(is_active=True).order_by("name_en"))
    form = ContractDispatchForm(request.POST or None, initial={"scheduled_date": timezone.localdate(), "visit_type": Visit.Type.PREVENTIVE})
    if request.method == "POST" and form.is_valid():
        when, kind = form.cleaned_data["scheduled_date"], form.cleaned_data["visit_type"]
        visits = []
        with transaction.atomic():
            for unit in units:
                v = (Visit.objects.filter(status__in=["scheduled", "rescheduled", "in_progress"]).select_for_update().filter(contract=c, unit=unit, scheduled_date__lte=when + timedelta(days=31)).order_by("scheduled_date").first()
                     or Visit.objects.create(unit=unit, client=c.client, contract=c, scheduled_date=when, visit_type=kind))
                visits.append(v)
            dispatch_visits(visits, form.cleaned_data["technician"], request.user, contract=c)
        messages.success(request, t("Visits sent to the technician's phone:") + f" {len(visits)}")
        return redirect(c)
    return form_page(request, form, "Send all units to a technician", c.get_absolute_url(), submit="Start and send", subject=f"{c.number} · {c.client}",
                     intro=t("A started visit is made for every unit under this contract and sent to the technician's phone.") + f" ({len(units)})")


@requires("contracts.edit")
def contract_equipment(request, pk):
    """Two steps: choose units (individually or all), then review and confirm before anything is saved."""
    c = _contract_or_404(request, pk)
    units = list(Unit.objects.filter(client=c.client, is_active=True).select_related("category", "subtype", "model"))
    chosen_ids = {x.pk for x in c.active_units}
    if request.method == "POST":
        ids = {int(x) for x in request.POST.getlist("units") if x.isdigit()}
        chosen = [x for x in units if x.pk in ids]
        if request.POST.get("step") == "confirm":
            try:
                set_contract_units(c, chosen, request.user)
            except ValidationError as e:
                messages.error(request, e.messages[0])
                return redirect("contract_equipment", pk=c.pk)
            messages.success(request, t("Saved successfully."))
            return redirect(c)
        current = {x.pk for x in c.active_units}
        return render(request, "contract_equipment_review.html", {
            "c": c, "chosen": chosen, "added": [x for x in chosen if x.pk not in current], "removed": list(c.active_units.exclude(pk__in=ids)),
            "conflicts": unit_conflicts(c, chosen),
        })
    return render(request, "contract_equipment.html", {"c": c, "units": units, "chosen_ids": chosen_ids, "conflicts": {x.unit_id: x.contract for x in unit_conflicts(c, units)}})


@post_only
@requires("contracts.edit")
def contract_action(request, pk, verb):
    c = _contract_or_404(request, pk)
    if verb == "activate":
        conflicts = unit_conflicts(c, c.active_units)
        if conflicts:
            messages.error(request, t("Already covered by another contract in these dates:") + " " + ", ".join(f"{x.unit.name} ({x.contract.number})" for x in conflicts))
            return redirect(c)
        c.status = "active"
    elif verb == "suspend":
        c.status = "suspended"
    elif verb == "terminate":
        c.status = "terminated"
    else:
        return redirect(c)
    with transaction.atomic():
        c.save(update_fields=["status"])
        if verb == "activate":
            sync_contract_visits(c)
        else:
            close_open_contract_visits(c, f"Contract {verb}ed" if verb == "suspend" else "Contract terminated")
    record(request.user, f"contract_{verb}", c)
    messages.success(request, t("Saved successfully."))
    return redirect(c)


@requires("contracts.edit")
def contract_renew(request, pk):
    old = _contract_or_404(request, pk)
    start = old.end_date + timedelta(days=1)
    initial = {"number": f"{old.number}-R", "title": old.title, "client": old.client_id, "start_date": start, "end_date": add_months(start, max(old.duration_months, 1)) - timedelta(days=1),
               "value": old.value, "currency": old.currency, "description": old.description}
    form = f.ContractForm(request.POST or None, initial=initial)
    form.fields["client"].disabled = True
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                new = form.save(commit=False)
                new.renewed_from = old
                new.status = "draft"
                new.save()
                copy_for_renewal(old, new, request.user)
        except ValidationError as e:
            messages.error(request, e.messages[0])
            return redirect(old)
        record(request.user, "contract_renewed", new, old.number)
        messages.success(request, t("Renewal created as a draft with the same equipment and schedules. Activate it when it is signed."))
        return redirect(new)
    return form_page(request, form, "Renew Contract", old.get_absolute_url(), subject=old.number)


@post_only
@requires("contracts.edit")
def contract_attach(request, pk):
    c = _contract_or_404(request, pk)
    form = f.AttachmentForm(request.POST, request.FILES, kinds=[Attachment.Kind.CONTRACT])
    if form.is_valid():
        new_attachment(form.cleaned_data["file"], request.user, "contract", contract=c)
        record(request.user, "attachment_added", c)
        messages.success(request, t("Saved successfully."))
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(c)


# ---- Schedules ----

@requires("schedules.manage")
def schedule_edit(request, contract, pk=None):
    c = _contract_or_404(request, contract)
    obj = get_object_or_404(Schedule, pk=pk, contract=c) if pk else None
    old_people = (obj.engineer_id, obj.technician_id) if obj else None  # read now: validating the form overwrites them
    form = f.ScheduleForm(request.POST or None, instance=obj, contract=c, initial={"scope": request.GET.get("scope"), "unit": request.GET.get("unit")} if not pk else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            s = form.save()
            sync_contract_visits(c)
            if old_people:
                follow_schedule_people(s, *old_people)
        saved(request, s.contract, "schedule_saved", s.scope)
        return redirect(c)
    return form_page(request, form, "Edit Schedule" if pk else "Add Schedule", c.get_absolute_url(), subject=c.number,
                     intro=t("An individual schedule replaces the general schedule for that unit. Completed visits are never changed."))


@post_only
@requires("schedules.manage")
def schedule_delete(request, pk):
    s = get_object_or_404(Schedule, pk=pk)
    c = _contract_or_404(request, s.contract_id)
    with transaction.atomic():
        s.is_active = False  # kept, because generated visits still point at it
        s.save(update_fields=["is_active"])
        sync_contract_visits(c)
    record(request.user, "schedule_removed", c, s.scope)
    messages.success(request, t("Schedule removed. Untouched future visits were cleared and history was kept."))
    return redirect(c)


# ---- Visits ----

def add_visit_filters(form, user):
    form.fields["client"].queryset = Client.objects.filter(pk__in=visits_for(user).values("client_id"))
    form.fields["contract"].queryset = contracts_for(user)
    staff = User.objects.filter(is_active=True)
    form.fields["engineer"].queryset = staff
    form.fields["technician"].queryset = staff


class VisitFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    client = forms.ModelChoiceField(Client.objects.all(), label="Client", required=False)
    country = forms.ModelChoiceField(Country.objects.all(), label="Country", required=False)
    category = forms.ModelChoiceField(Category.objects.all(), label="Equipment Type", required=False)
    contract = forms.ModelChoiceField(Contract.objects.all(), label="Contract", required=False)
    engineer = forms.ModelChoiceField(User.objects.all(), label="Engineer", required=False)
    technician = forms.ModelChoiceField(User.objects.all(), label="Technician", required=False)
    status = forms.ChoiceField(label="Visit Status", required=False, choices=[("", "—"), *Visit.STATUS_LABELS.items()])
    start = forms.DateField(label="From", required=False)
    end = forms.DateField(label="To", required=False)
    quick = forms.ChoiceField(label="Quick filter", required=False, choices=[("", "—"), ("week", "Due this week"), ("month", "Due this month")])


def week_bounds(day):
    start = day - timedelta(days=(day.weekday() + 1) % 7)  # weeks start on Sunday, as in Jordan
    return start, start + timedelta(days=6)


def filter_visits(request):
    qs = visits_for(request.user).select_related("unit__category", "unit__model", "client", "contract", "engineer", "technician")
    form = VisitFilter(request.GET or None)
    add_visit_filters(form, request.user)
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            q = d["q"]
            qs = qs.filter(Q(number__icontains=q) | Q(unit__name_en__icontains=q) | Q(unit__name_ar__icontains=q) | Q(unit__serial_number__icontains=q)
                           | Q(client__name_en__icontains=q) | Q(client__name_ar__icontains=q) | Q(contract__number__icontains=q))
        for key, lookup in {"client": "client", "country": "client__country", "category": "unit__category", "contract": "contract", "engineer": "engineer", "technician": "technician"}.items():
            if d[key]:
                qs = qs.filter(**{lookup: d[key]})
        if d["status"]:
            qs = qs.with_status(d["status"])
        if d["start"]:
            qs = qs.filter(scheduled_date__gte=d["start"])
        if d["end"]:
            qs = qs.filter(scheduled_date__lte=d["end"])
        today = timezone.localdate()
        if d["quick"] == "week":
            qs = qs.open().filter(scheduled_date__gte=today, scheduled_date__lte=week_bounds(today)[1])
        elif d["quick"] == "month":
            qs = qs.open().filter(scheduled_date__gte=today, scheduled_date__lte=today.replace(day=pycal.monthrange(today.year, today.month)[1]))
    return qs, form


@requires("visits.view")
def visit_list(request):
    qs, form = filter_visits(request)
    if not request.GET or request.GET.get("view") == "board":
        # Each column is its own query with a cap, so years of visits never crowd out today's work; the list view holds the rest.
        today = timezone.localdate()
        sources = [("overdue", "Overdue", qs.with_status("overdue").order_by("scheduled_date", "pk"), 50),
                   ("todo", "To Do", qs.open().filter(scheduled_date__gte=today).order_by("scheduled_date", "pk"), 50),
                   ("in_progress", "In Progress", qs.filter(status="in_progress").order_by("scheduled_date", "pk"), 50),
                   ("completed", "Completed", qs.filter(status="completed").order_by("-actual_date", "-scheduled_date", "-pk"), 30)]
        columns = [(key, label, list(q[:cap]), q.count()) for key, label, q, cap in sources]
        return render(request, "visits_board.html", {"title": t("Maintenance Visits"), "columns": columns, "form": form, "filtered": len(request.GET) > 1})
    acts = [action("Board View", reverse("visit_list"), "calendar", False)]
    if request.user.has("visits.manage"):
        acts.append(action("Add Visit", reverse("visit_new")))
    return list_page(request, "Maintenance Visits", qs.order_by("-scheduled_date", "pk"),
                     ["Visit Number", "Client", "Equipment Unit", "Serial Number", "Contract Number", "Scheduled Date", "Assigned Technician", "Visit Status"],
                     lambda v: [link(v), v.client.name, link(v.unit), v.unit.serial_number or "—", v.contract.number if v.contract else "—", fmt(v.scheduled_date), str(v.technician) if v.technician else "—", badge(v.display_status, v.status_label)],
                     filters=form, actions=acts)


@requires("visits.manage")
def visit_edit(request, pk=None):
    obj = get_object_or_404(visits_for(request.user), pk=pk) if pk else None
    if obj and obj.status == "completed":
        messages.error(request, t("Completed visits are permanent records and cannot be edited."))
        return redirect(obj)
    form = f.VisitForm(request.POST or None, instance=obj, user=request.user, initial={"unit": request.GET.get("unit"), "scheduled_date": request.GET.get("date")} if not pk else None)
    if request.method == "POST" and form.is_valid():
        v = form.save()
        saved(request, v, "updated" if pk else "created")
        return redirect(v)
    return form_page(request, form, "Edit Visit" if pk else "Add Visit", obj.get_absolute_url() if obj else reverse("visit_list"))


class DispatchForm(f.BaseForm):
    technician = forms.ModelChoiceField(label="Assigned Technician", queryset=User.objects.none())

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["technician"].queryset = User.objects.filter(pk__in=[u.pk for u in technicians()]).order_by("first_name", "username")


class ContractDispatchForm(DispatchForm):
    scheduled_date = forms.DateField(label="Visit date")
    visit_type = forms.ChoiceField(label="Visit Type", choices=Visit.Type.choices)


def _visit_or_404(request, pk):
    return get_object_or_404(visits_for(request.user).select_related("unit__category", "unit__model__manufacturer", "client", "contract", "engineer", "technician", "schedule"), pk=pk)


@requires("visits.view")
def visit_detail(request, pk):
    v = _visit_or_404(request, pk)
    phone = FieldReport.objects.filter(visit=v).first()
    hidden = bool(phone) and not field_reports_for(request.user).filter(pk=phone.pk).exists()
    return render(request, "visit_detail.html", {
        "v": v, "today": timezone.localdate().isoformat(), "report_hidden": hidden,
        "report": None if hidden else getattr(v, "report", None), "attachments": [] if hidden else v.attachments.all(),
        "phone_report": None if hidden else phone,
        "attachment_form": f.AttachmentForm(kinds=[Attachment.Kind.REPORT, Attachment.Kind.PHOTO, Attachment.Kind.OTHER]),
        "reschedule_form": f.RescheduleForm(initial={"scheduled_date": v.scheduled_date}), "cancel_form": f.CancelForm(),
        "complete_form": f.CompleteForm(initial={"actual_date": timezone.localdate(), "notes": v.notes}),
    })


def _form_errors(request, form):
    messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))


@post_only
@requires("visits.manage")
def visit_reschedule(request, pk):
    v = _visit_or_404(request, pk)
    form = f.RescheduleForm(request.POST)
    if not v.is_open:
        messages.error(request, t("This visit is closed."))
    elif form.is_valid():
        old = v.scheduled_date
        new_date = form.cleaned_data["scheduled_date"]
        if new_date < timezone.localdate():
            messages.error(request, t("The new date cannot be in the past."))
            return redirect(v)
        if Visit.objects.filter(unit=v.unit, scheduled_date=new_date, status__in=["scheduled", "rescheduled", "in_progress"]).exclude(pk=v.pk).exists():
            messages.error(request, t("This unit already has an open visit on that date."))
            return redirect(v)
        v.scheduled_date = new_date
        v.status = "rescheduled"
        if form.cleaned_data["reason"]:
            v.notes = f"{v.notes}\n{old.isoformat()} → {v.scheduled_date.isoformat()}: {form.cleaned_data['reason']}".strip()
        v.save()
        record(request.user, "visit_rescheduled", v, f"{old} -> {v.scheduled_date}")
        messages.success(request, t("Saved successfully."))
    else:
        _form_errors(request, form)
    return redirect(v)


@post_only
@requires("visits.manage")
def visit_cancel(request, pk):
    v = _visit_or_404(request, pk)
    form = f.CancelForm(request.POST)
    if not v.is_open:
        messages.error(request, t("This visit is closed."))
    elif form.is_valid():
        v.status = "cancelled"
        v.cancel_reason = form.cleaned_data["reason"]
        v.save()
        record(request.user, "visit_cancelled", v, v.cancel_reason)
        messages.success(request, t("Saved successfully."))
    else:
        _form_errors(request, form)
    return redirect(v)


@requires("visits.update")
def visit_start(request, pk):
    """Start Visit: choose the technician who receives it on his phone, then start it."""
    v = _visit_or_404(request, pk)
    if v.status not in ("scheduled", "rescheduled"):
        return redirect(v)
    form = DispatchForm(request.POST or None, initial={"technician": v.technician_id})
    if request.method == "POST" and form.is_valid():
        dispatch_visits([v], form.cleaned_data["technician"], request.user)
        messages.success(request, t("The visit was started and sent to the technician's phone."))
        return redirect(v)
    return form_page(request, form, "Start Visit", v.get_absolute_url(), submit="Start and send", subject=f"{v.number} · {v.unit}",
                     intro=t("The technician gets the visit on his phone the next time the app syncs (within a minute when online)."))


@post_only
@requires("visits.update")
def visit_complete(request, pk):
    v = _visit_or_404(request, pk)
    form = f.CompleteForm(request.POST)
    if not v.is_open:
        messages.error(request, t("This visit is closed."))
    elif form.is_valid():
        if form.cleaned_data["actual_date"] > timezone.localdate():
            messages.error(request, t("The actual visit date cannot be in the future."))
            return redirect(v)
        v.status = "completed"
        v.actual_date = form.cleaned_data["actual_date"]
        v.notes = form.cleaned_data["notes"]
        v.save()
        record(request.user, "visit_completed", v)
        messages.success(request, t("Visit completed and saved to the equipment history."))
    else:
        _form_errors(request, form)
    return redirect(v)


@requires("visits.attach")
def visit_report(request, pk):
    v = _visit_or_404(request, pk)
    phone = FieldReport.objects.filter(visit=v).first()
    if phone:  # its text is written from the phone report's readings: editing it here would be overwritten, so there is one place to edit
        if can_edit_report(request.user, phone):
            return redirect("maintenance_report_edit", pk=phone.pk)
        messages.info(request, t("This report came from the phone app. It is changed on its report page, by the foreman or the engineer whose turn it is."))
        return redirect(v)
    report = getattr(v, "report", None)
    form = f.VisitReportForm(request.POST or None, instance=report)
    if request.method == "POST" and form.is_valid():
        r = form.save(commit=False)
        r.visit, r.author = v, request.user
        r.save()
        saved(request, v, "visit_report_saved")
        return redirect(v)
    return form_page(request, form, "Maintenance Report", v.get_absolute_url(), subject=str(v))


@post_only
@requires("visits.attach")
def visit_attach(request, pk):
    v = _visit_or_404(request, pk)
    form = f.AttachmentForm(request.POST, request.FILES, kinds=[Attachment.Kind.REPORT, Attachment.Kind.PHOTO, Attachment.Kind.OTHER])
    if form.is_valid():
        new_attachment(form.cleaned_data["file"], request.user, form.cleaned_data["kind"], visit=v)
        record(request.user, "attachment_added", v)
        messages.success(request, t("Saved successfully."))
    else:
        _form_errors(request, form)
    return redirect(v)


# ---- Maintenance reports ----

APP_TYPE_WORDS = {"ahu": "Air Handling", "chiller": "Chiller", "fcu": "Fan Coil", "split": "Split", "vrf": "Split", "boiler": "Other", "cooling_tower": "Other", "custom": "Other"}


def _report_file(visit_or_pending):
    """The PDF to open for a row, if one was uploaded."""
    return next((a for a in visit_or_pending.attachments.all() if a.kind == Attachment.Kind.REPORT), None)


@requires("visits.view")
def maintenance_reports(request):
    """Completed visits as reports: those under a contract, and the daily ones outside any contract.
    Daily reports also hold what the phone app sent for sites or units that are not in the database yet."""
    u = request.user
    done = visits_for(u).filter(status="completed").select_related("unit", "client", "contract", "technician", "field_report").prefetch_related("attachments").order_by("-actual_date", "-pk")
    mine = set(field_reports_for(u).values_list("pk", flat=True))
    shown = lambda v: not hasattr(v, "field_report") or v.field_report.pk in mine  # a visit without a phone report is always shown
    rows = lambda qs: [{"visit": v, "report": getattr(v, "field_report", None) if shown(v) else None, "file": _report_file(v) if shown(v) else None,
                        "hidden": not shown(v)} for v in qs[:100]]
    waiting = [{"report": r, "file": _report_file(r)} for r in field_reports_for(u).filter(visit__isnull=True).select_related("uploaded_by").prefetch_related("attachments")[:100]]
    return render(request, "maintenance_reports.html", {
        "contract_rows": rows(done.filter(contract__isnull=False)), "daily_rows": rows(done.filter(contract__isnull=True)), "waiting": waiting,
        "contract_total": done.filter(contract__isnull=False).count(), "daily_total": done.filter(contract__isnull=True).count(),
        "can_add": u.has("equipment.edit") and u.has("clients.edit"),
    })


ROW_PARTS = {  # the report's lists: title, columns, and the column a row cannot be without
    "materials": ("Parts Used", [("description", "Description"), ("qty", "Quantity"), ("unit", "Unit")], "description"),
    "attention_notes": ("Recommendations", [("unit_no", "Unit"), ("note", "Notes")], "note"),
}


def _phone_report(request, pk):
    return get_object_or_404(field_reports_for(request.user).select_related("visit__unit", "visit__client", "uploaded_by"), pk=pk)


@requires("visits.view")
def maintenance_report_view(request, pk):
    """One phone report as a page: every reading in its own box, the superheat of each circuit, and the photos."""
    fr = _phone_report(request, pk)
    files = list((fr.visit or fr).attachments.all())  # once filed, the PDF and photos belong to the visit
    return render(request, "field_report.html", {
        "fr": fr, "view": report_view(fr.data, limits_for(unit=fr.visit.unit if fr.visit else None)),
        "photos": [a for a in files if a.kind == Attachment.Kind.PHOTO], "pdf": next((a for a in files if a.kind == Attachment.Kind.REPORT), None),
        "signatures": [a for a in files if a.kind == Attachment.Kind.SIGNATURE],
        "can_add": not fr.visit_id and request.user.has("equipment.edit") and request.user.has("clients.edit"),
        "can_edit": can_edit_report(request.user, fr), "step": _next_step(request.user, fr),
        "can_email": fr.status == FieldReport.Status.APPROVED and (request.user.has("reports.approve") or request.user.has("users.manage")),
        "mailto": _mailto(fr) if fr.status == FieldReport.Status.APPROVED else "",
        "edited": ActivityLog.objects.filter(action="report_edited", target_type="FieldReport", target_id=fr.pk).exists(),
    })


@requires("visits.view")
def maintenance_report_edit(request, pk):
    """Change a phone report's readings, notes, parts and recommendations. What changed, from what to what, goes into the activity log."""
    fr = _phone_report(request, pk)
    if not can_edit_report(request.user, fr):
        raise PermissionDenied
    items = [r for r in fr.data.get("checklist_items") or [] if isinstance(r, dict)]
    texts = {"required_maintenance": "Actions Taken", "general_notes": "Notes"}
    both = lambda r, fallback: " · ".join(dict.fromkeys(x for x in (r.get("label_en"), r.get("label_ar")) if x)) or r.get("field_key") or str(fallback)
    lists = {key: [r for r in fr.data.get(key) or [] if isinstance(r, dict)] + [{}, {}] for key in ROW_PARTS}  # two empty rows to add new ones

    class Form(forms.Form):
        pass
    form = Form(request.POST or None)
    for i, r in enumerate(items):
        form.fields[f"item_{i}"] = forms.CharField(required=False, max_length=500, initial=r.get("value", ""), label=both(r, i + 1))
    for key, label in texts.items():
        form.fields[key] = forms.CharField(required=False, max_length=5000, initial=fr.data.get(key, ""), label=t(label), widget=forms.Textarea(attrs={"rows": 3}))
    for key, (title, columns, _need) in ROW_PARTS.items():
        for i, row in enumerate(lists[key]):
            for column, name in columns:
                form.fields[f"{key}_{i}_{column}"] = forms.CharField(required=False, max_length=500, initial=row.get(column, ""), label=f"{t(title)} {i + 1} · {t(name)}")
    # A fingerprint of the report as this form was opened: the form sends every field back, so saving over someone else's change would undo it.
    stamp = hashlib.md5(json.dumps(fr.data, sort_keys=True).encode()).hexdigest()
    form.fields["stamp"] = forms.CharField(widget=forms.HiddenInput, initial=stamp, required=False)
    back = reverse("maintenance_report_view", args=[fr.pk])
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["stamp"] != stamp:
            messages.error(request, t("Someone else changed this report while you were editing. Nothing was saved: check the values and enter your changes again."))
            return redirect("maintenance_report_edit", pk=fr.pk)
        changed = []
        note = lambda label, old, new: changed.append(f"{label}: {old or '—'} → {new or '—'}")
        for i, r in enumerate(items):
            old, new = str(r.get("value", "") or ""), form.cleaned_data[f"item_{i}"].strip()
            if old != new:
                note(r.get("label_en") or r.get("field_key") or i + 1, old, new)
                r["value"] = new
        for key, label in texts.items():
            old, new = str(fr.data.get(key, "") or ""), form.cleaned_data[key].strip()
            if old != new:
                note(label, old, new)
                fr.data[key] = new
        for key, (title, columns, need) in ROW_PARTS.items():
            line = lambda row: " | ".join(str(row.get(c, "") or "") for c, _ in columns) if str(row.get(need, "") or "").strip() else ""
            kept = []
            for i, row in enumerate(lists[key]):
                new = {**row, **{c: form.cleaned_data[f"{key}_{i}_{c}"].strip() for c, _ in columns}}
                if line(row) != line(new):
                    note(f"{title} {i + 1}", line(row), line(new))  # an emptied row is removed; a filled empty row is added
                if line(new):
                    kept.append(new)
            fr.data[key] = kept
        if changed:
            with transaction.atomic():
                fr.save(update_fields=["data"])
                if fr.visit_id:  # the visit's own report text follows the readings
                    VisitReport.objects.filter(visit_id=fr.visit_id).update(**report_texts(fr.data))
                record(request.user, "report_edited", fr, "\n".join(changed))
            messages.success(request, t("Saved successfully."))
        return redirect(back)
    return form_page(request, form, "Edit report", back, submit="Save", subject=str(fr.visit or fr),
                     intro=t("Every change is written to the activity log with the old and the new value. Empty a row's main box to delete it; fill an empty row to add one."))


STEPS = {  # status now -> (permission, new status, button, log action, who is told next)
    FieldReport.Status.RECEIVED: ("reports.review", FieldReport.Status.ENGINEER, "Approve and send to engineer", "report_sent_to_engineer", "reports.approve"),
    FieldReport.Status.ENGINEER: ("reports.approve", FieldReport.Status.APPROVED, "Approve", "report_approved", None),
}


def _next_step(user, fr):
    """The approval button this user should see on the report, if it is their turn."""
    step = STEPS.get(fr.status)
    return step[2] if step and (user.has(step[0]) or user.has("users.manage")) else ""


@post_only
@requires("visits.view")
def maintenance_report_step(request, pk):
    """The foreman's "Approve and send to engineer" and the engineer's "Approve". Each is logged; the next person is notified."""
    fr = _phone_report(request, pk)
    step = STEPS.get(fr.status)
    if not _next_step(request.user, fr) or request.POST.get("status") != fr.status:
        raise PermissionDenied
    _code, new, _button, action, tell = step
    who = "foreman" if new == FieldReport.Status.ENGINEER else "engineer"
    with transaction.atomic():
        # The status in the filter makes two people pressing at once safe: only the first one moves the report on.
        moved = FieldReport.objects.filter(pk=fr.pk, status=fr.status).update(status=new, **{f"{who}_by": request.user, f"{who}_at": timezone.now()})
        if moved:
            record(request.user, action, fr)
            if tell:
                what = f"{fr.visit.unit if fr.visit else fr} · {request.user}"
                notify_report(fr, tell, new, f"Maintenance report approved by the foreman, waiting for your approval: {what}",
                              f"تقرير صيانة وافق عليه رئيس العمال وينتظر موافقتك: {what}", actor=request.user)
    messages.success(request, t("Saved successfully.") if moved else t("Someone else already did this step."))
    return redirect("maintenance_report_view", pk=fr.pk)


def _report_subject(fr):
    unit = fr.visit.unit if fr.visit else None
    return " – ".join(x for x in ("Maintenance report", unit.name if unit else fr.site or fr.client_name, fr.machine_model, date_format(fr.report_date, "j M Y")) if x)


def _mailto(fr):
    """A new email in the user's mail program (the new Outlook), with the client's address, subject and message filled in.
    shortcut: a mailto link cannot carry the PDF, so the button downloads it to be dragged in; Microsoft Graph could attach it, if ever needed."""
    client = fr.visit.client if fr.visit else None
    name = client.name_en if client else fr.client_name or fr.site or "Sir/Madam"
    name_ar = (client.name_ar or name) if client else name
    unit = fr.visit.unit.name if fr.visit else fr.machine_model
    body = (f"Dear {name},\n\nPlease find attached the maintenance report for {unit}, visit date {date_format(fr.report_date, 'j F Y')}.\n\n"
            "Best regards,\nAladdin Sweiti Engineering Est. (ASEE)\n\n"
            f"السادة {name_ar} المحترمين،\n\nمرفق تقرير الصيانة للوحدة {unit} بتاريخ {fr.report_date:%d/%m/%Y}.\n\n"
            "مع التحية،\nمؤسسة علاء الدين السويطي الهندسية")
    to = client.email if client and client.email else ""
    return f"mailto:{quote(to)}?subject={quote(_report_subject(fr))}&body={quote(body)}"


@requires("visits.view")
def maintenance_report_pdf(request, pk):
    """The approved report as a PDF, with every change made on the website; the phone's signed PDF follows it."""
    fr = _phone_report(request, pk)
    if fr.status != FieldReport.Status.APPROVED:
        raise PermissionDenied
    files = list((fr.visit or fr).attachments.all())
    content = build_report_pdf(fr, report_view(fr.data, limits_for(unit=fr.visit.unit if fr.visit else None)),
                               [a for a in files if a.kind == Attachment.Kind.PHOTO], next((a for a in files if a.kind == Attachment.Kind.REPORT), None),
                               [a for a in files if a.kind == Attachment.Kind.SIGNATURE])
    if request.GET.get("email"):
        record(request.user, "report_email_prepared", fr, _report_subject(fr))
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="ASEE_Report_{fr.visit.number if fr.visit else fr.pk}.pdf"'
    return response


@requires("equipment.edit", "clients.edit")
def maintenance_report_add(request, pk):
    """The button on a waiting daily report: add its site and unit to the database, and file the report as a completed visit on that unit."""
    fr = get_object_or_404(field_reports_for(request.user).filter(visit__isnull=True), pk=pk)
    word = APP_TYPE_WORDS.get(fr.machine_type, "")
    initial = {"client": Client.objects.filter(name_en__iexact=fr.client_name.strip()).first() if fr.client_name.strip() else None,
               "new_client_name": fr.client_name or fr.site, "country": Country.objects.filter(code="JO").first(),
               "unit_name": " ".join(x for x in (fr.machine_model, fr.site) if x)[:200] or fr.serial_no, "model_number": fr.machine_model, "serial_number": fr.serial_no,
               "category": Category.objects.filter(name_en__icontains=word, is_active=True).first() if word else None}
    form = f.FieldReportAddForm(request.POST or None, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            unit = form.save()
            visit = visit_from_report(unit, fr.data, fr.uploaded_by or request.user, fr.report_date, fr.app_report_id)
            fr.attachments.update(visit=visit, field_report=None)
            fr.visit = visit
            fr.save(update_fields=["visit"])
            record(request.user, "created", unit, f"From phone report {fr.app_report_id}")
        messages.success(request, t("Site and unit added. The report is now a completed visit."))
        return redirect(visit)
    return form_page(request, form, "Add site and unit to the database", reverse("maintenance_reports") + "#daily", submit="Add to database", subject=str(fr),
                     intro=t("These details come from the report sent by the phone app. Correct them, then add. Only what is missing is created."))


# ---- Calendar ----

@requires("visits.view")
def calendar_view(request):
    qs, form = filter_visits(request)
    view = request.GET.get("view", "month")
    if view not in {"month", "week", "day", "list"}:
        view = "month"
    today = timezone.localdate()
    try:
        anchor = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        anchor = today

    if view == "day":
        start = end = anchor
        prev, nxt = anchor - timedelta(days=1), anchor + timedelta(days=1)
    elif view == "week":
        start, end = week_bounds(anchor)
        prev, nxt = anchor - timedelta(days=7), anchor + timedelta(days=7)
    else:
        first = anchor.replace(day=1)
        last = anchor.replace(day=pycal.monthrange(anchor.year, anchor.month)[1])
        prev, nxt = add_months(first, -1), add_months(first, 1)
        if view == "month":
            start, end = week_bounds(first)[0], week_bounds(last)[1]
        else:
            start, end = first, last

    has_quick = form.is_valid() and (form.cleaned_data["quick"] or form.cleaned_data["start"] or form.cleaned_data["end"])
    visits = qs if has_quick and view == "list" else qs.filter(scheduled_date__gte=start, scheduled_date__lte=end)
    by_day = {}
    for v in visits.order_by("scheduled_date", "pk"):
        by_day.setdefault(v.scheduled_date, []).append(v)

    days = []
    d = start
    while d <= end and view != "list":
        days.append({"date": d, "visits": by_day.get(d, []), "today": d == today, "in_month": d.month == anchor.month})
        d += timedelta(days=1)
    query = request.GET.copy()
    for key in ("view", "date"):
        query.pop(key, None)
    weekdays = [days[i]["date"] for i in range(7)] if view in ("month", "week") else []
    return render(request, "calendar.html", {
        "view": view, "views": [("month", "Monthly View"), ("week", "Weekly View"), ("day", "Daily View"), ("list", "List View")],
        "anchor": anchor, "prev": prev.isoformat(), "next": nxt.isoformat(), "today": today.isoformat(), "days": days, "weeks": [days[i:i + 7] for i in range(0, len(days), 7)],
        "weekdays": weekdays, "list_visits": list(visits.order_by("scheduled_date", "pk")) if view in ("list",) else [],
        "filters": form, "query": query.urlencode(), "range_start": start, "range_end": end,
        "statuses": Visit.STATUS_LABELS.items(),
    })

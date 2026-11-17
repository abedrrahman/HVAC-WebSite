"""Business rules: data scoping, document inheritance, visit generation, reminders."""
import hashlib
import logging
import re
import smtplib
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone, translation
from django.utils.formats import date_format

from .i18n import t
from .models import (ActivityLog, AlarmCode, Attachment, Client, alarm_key, Contract, ContractEquipment, Document, DocumentAssignment,
                     FieldDefinition, FieldReport, Notification, Schedule, SystemSettings, TechnicalValue, Unit, User, Visit, VisitReport, add_months)

log = logging.getLogger(__name__)


def record(user, action, obj, details=""):
    ActivityLog.objects.create(user=user if getattr(user, "pk", None) else None, action=action,
                               target_type=type(obj).__name__, target_id=getattr(obj, "pk", None),
                               target_label=str(obj)[:255], details=details)


# ---- Data scope: users without scope.all only see what they are assigned to ----

def visits_for(user):
    qs = Visit.objects.all()
    return qs if user.has("scope.all") else qs.filter(Q(engineer=user) | Q(technician=user))


def units_for(user):
    qs = Unit.objects.all()
    if user.has("scope.all"):
        return qs
    return qs.filter(pk__in=visits_for(user).values("unit_id"))


def clients_for(user):
    qs = Client.objects.all()
    return qs if user.has("scope.all") else qs.filter(pk__in=units_for(user).values("client_id"))


def contracts_for(user):
    qs = Contract.objects.all()
    return qs if user.has("scope.all") else qs.filter(pk__in=visits_for(user).exclude(contract=None).values("contract_id"))


def reviews_reports(user):
    """Foremen, engineers and administrators see every phone report from the moment it arrives."""
    return user.has("reports.review") or user.has("reports.approve") or user.has("users.manage")


def field_reports_for(user):
    """Phone reports this user may open: all of them for the reviewers; for everyone else their own uploads,
    and approved reports within their usual scope. A report is not shown to others before the engineer approves it."""
    qs = FieldReport.objects.all()
    if reviews_reports(user):
        return qs
    approved = qs.filter(status=FieldReport.Status.APPROVED)
    if not user.has("scope.all"):
        approved = approved.filter(visit__in=visits_for(user))
    return qs.filter(Q(uploaded_by=user) | Q(pk__in=approved.values("pk")))


def can_edit_report(user, fr):
    """Whose turn it is: the foreman while it waits for the foreman, the engineer after that. Once approved, only an administrator."""
    if user.has("users.manage"):
        return True
    if fr.status == FieldReport.Status.RECEIVED:
        return user.has("reports.review")
    return fr.status == FieldReport.Status.ENGINEER and user.has("reports.approve")


def notify_report(fr, code, step, en, ar, actor=None):
    """Tells everyone holding the permission that a report waits for them. The step is in the key, so a retried upload never notifies twice."""
    rows = [Notification(user=u, kind=Notification.Kind.REPORT, dedupe_key=f"report:{fr.pk}:{step}", message_en=en, message_ar=ar, field_report=fr)
            for u in _users_with(code) if u != actor]
    Notification.objects.bulk_create(rows, ignore_conflicts=True)


def documents_for(user):
    qs = Document.objects.all()
    if user.has("scope.all"):
        return qs
    # Type-wide documents are general engineering references; unit documents follow the unit's visibility.
    return qs.filter(Q(assignments__level__in=["model", "subtype", "category", "manufacturer", "all"]) | Q(assignments__unit__in=units_for(user))).distinct()


def attachment_allowed(user, att):
    if att.unit_id:
        return user.has("equipment.view") and units_for(user).filter(pk=att.unit_id).exists()
    if att.visit_id:
        if not (user.has("visits.view") and visits_for(user).filter(pk=att.visit_id).exists()):
            return False
        # A visit filed from a phone report keeps its files hidden from others until the engineer approves the report.
        return not FieldReport.objects.filter(visit_id=att.visit_id).exclude(pk__in=field_reports_for(user).values("pk")).exists()
    if att.contract_id:
        return user.has("contracts.view") and contracts_for(user).filter(pk=att.contract_id).exists()
    if att.field_report_id:
        return user.has("visits.view") and field_reports_for(user).filter(pk=att.field_report_id).exists()
    return user.has("manufacturers.view")


# ---- Document inheritance: computed on read, so future units inherit with no extra rows ----

def documents_for_unit(unit):
    """One entry per document, with the most specific assignment as its source."""
    return documents_for_units([unit])


def documents_for_units(units):
    """The documents that reach any of these units, in one query. A unit page with many units stays fast."""
    if not units:
        return []
    q = (Q(unit__in=[u.pk for u in units]) | Q(model_id__in={u.model_id for u in units}) | Q(category_id__in={u.category_id for u in units})
         | Q(manufacturer_id__in={u.model.manufacturer_id for u in units}) | Q(subtype_id__in={u.subtype_id for u in units if u.subtype_id}) | Q(level="all"))
    links = (DocumentAssignment.objects.filter(q, document__is_archived=False)
             .select_related("document__doc_type", "document__manufacturer", "model", "subtype", "category", "manufacturer"))
    rank = {"unit": 0, "model": 1, "subtype": 2, "category": 3, "manufacturer": 4, "all": 5}
    best = {}
    for link in sorted(links, key=lambda a: rank[a.level]):
        best.setdefault(link.document_id, link)
    return sorted(best.values(), key=lambda a: (rank[a.level], a.document.name_en.lower()))


def units_reached(document):
    """Units a document is available to, directly or by inheritance."""
    links = document.assignments.all()
    if any(a.level == "all" for a in links):
        return Unit.objects.filter(is_active=True)
    q = Q(pk__in=[a.unit_id for a in links if a.unit_id])
    q |= Q(model_id__in=[a.model_id for a in links if a.model_id])
    q |= Q(subtype_id__in=[a.subtype_id for a in links if a.subtype_id])
    q |= Q(category_id__in=[a.category_id for a in links if a.category_id])
    q |= Q(model__manufacturer_id__in=[a.manufacturer_id for a in links if a.manufacturer_id])
    return Unit.objects.filter(q, is_active=True)


def in_category(documents, category):
    """Documents that reach units of this category, at any link level."""
    return documents.filter(Q(assignments__category=category) | Q(assignments__subtype__category=category) | Q(assignments__unit__category=category)
                            | Q(assignments__model__category=category) | Q(assignments__manufacturer__models__category=category) | Q(assignments__level="all")).distinct()


def find_alarms(controller, text):
    """Exact code first (d4.1 also brings the D4 group, d3.S the d3 entry); otherwise words from a code or name."""
    text = text.strip()
    typed = alarm_key(text)
    alarms = AlarmCode.objects.filter(controller=controller).select_related("source_document")
    keys = {typed, alarm_key(text.split(".")[0]), re.sub(r"(?<=[A-Z])0+(?=\d)", "", typed)}  # E01 is printed as E1 in most manuals
    hot_water = re.fullmatch(r"(d\d+(?:\.\d+)?)\.?\s*s", text, re.I)  # the display adds S during a hot-water cycle
    if hot_water:
        keys |= {alarm_key(hot_water[1]), alarm_key(hot_water[1].split(".")[0]), "DXXS"}
    drive = re.fullmatch(r"(1[679]0)[-\s]*(\d{2})", text)  # fan or pump drive alarm: main code plus a sub-code
    if drive:
        keys |= {f"{drive[1]}YY", f"DRIVE{drive[2]}"}
    family = re.fullmatch(r"(700|130)\d{1,2}", text)  # the service guides print these groups as 700-n and 130-nn
    if family:
        keys.add("700N" if family[1] == "700" else "130NN")
    keys.discard("")
    exact = sorted(alarms.filter(key__in=keys), key=lambda a: a.key != typed)
    if exact or not text:
        return exact
    words = Q(code__icontains=text) | Q(name_en__icontains=text) | Q(name_ar__icontains=text)
    if len(typed) >= 3:
        words |= Q(key__contains=typed) | Q(key__contains=re.sub(r"\d", "X", typed))  # the manual prints "CIRCUIT X", the screen a number
    return list(alarms.filter(words)[:30])


def unit_document_scopes(unit):
    """(level, label, target, units reached) for each way a document added on this unit can be shared."""
    active = Unit.objects.filter(is_active=True)
    model = unit.model
    rows = [("unit", f"{t('This unit only')}: {unit.name}", unit, 1),
            ("model", f"{t('All units of this model')}: {model.model_number}", model, active.filter(model=model).count())]
    if unit.subtype_id:
        rows.append(("subtype", f"{t('All units in this subcategory')}: {unit.subtype.name}", unit.subtype, active.filter(subtype_id=unit.subtype_id).count()))
    rows += [("category", f"{t('All units in this main category')}: {unit.category.name}", unit.category, active.filter(category_id=unit.category_id).count()),
             ("manufacturer", f"{t('All units of this manufacturer')}: {model.manufacturer.name}", model.manufacturer, active.filter(model__manufacturer_id=model.manufacturer_id).count()),
             ("all", t("All units in the app"), None, active.count())]
    return rows


def hash_file(f):
    h = hashlib.sha256()
    for chunk in f.chunks():
        h.update(chunk)
    f.seek(0)
    return h.hexdigest()


# ---- Technical specifications ----

def format_value(field, raw):
    if raw in (None, "", []):
        return ""
    if field.field_type == "boolean":
        return t("Yes") if raw else t("No")
    if field.field_type == "multiselect":
        return ", ".join(raw)
    if field.field_type == "date":
        return date_format(date.fromisoformat(raw), "j F Y")
    if isinstance(raw, dict):
        return f"{raw.get('value', '')} {raw.get('unit', '')}".strip()
    return str(raw)


def spec_rows(unit=None, model=None):
    """Rows for display. A unit shows its own value, otherwise the shared value from its model."""
    target = unit or model
    model_id = unit.model_id if unit else model.pk
    defs = FieldDefinition.applying_to(target.category_id, target.subtype_id)
    model_vals = {v.field_id: v.value for v in TechnicalValue.objects.filter(model_id=model_id)}
    unit_vals = {v.field_id: v.value for v in TechnicalValue.objects.filter(unit=unit)} if unit else {}
    rows = []
    for d in defs:
        own = d.pk in unit_vals
        raw = unit_vals.get(d.pk, model_vals.get(d.pk))
        rows.append({"label": d.name, "value": format_value(d, raw), "from_model": bool(unit and not own and raw not in (None, ""))})
    return rows


# ---- Contracts ----

def unit_conflicts(contract, units, start=None, end=None):
    """Units already on another live contract with overlapping dates."""
    start, end = start or contract.start_date, end or contract.end_date
    return (ContractEquipment.objects.filter(unit__in=units, removed_at__isnull=True,
                                             contract__status__in=["active", "suspended"],
                                             contract__start_date__lte=end, contract__end_date__gte=start)
            .exclude(contract=contract).select_related("unit", "contract"))


def set_contract_units(contract, units, user):
    """Make the contract cover exactly these units. Removed units keep their record and visit history."""
    conflicts = unit_conflicts(contract, units)
    if conflicts:
        names = ", ".join(f"{c.unit.name} ({c.contract.number})" for c in conflicts)
        raise ValidationError(t("Already covered by another contract in these dates:") + " " + names)
    with transaction.atomic():
        wanted = {u.pk for u in units}
        current = contract.equipment_links.filter(removed_at__isnull=True)
        for link in current:
            if link.unit_id not in wanted:
                link.removed_at = timezone.now()
                link.save(update_fields=["removed_at"])
                record(user, "contract_unit_removed", contract, link.unit.name_en)
        have = {link.unit_id for link in current}
        for unit in units:
            if unit.pk not in have:
                ContractEquipment.objects.create(contract=contract, unit=unit)
                record(user, "contract_unit_added", contract, unit.name_en)
        sync_contract_visits(contract)


# ---- Visit generation ----

def schedule_for_unit(unit, schedules):
    """An individual schedule always wins over the general one for that unit."""
    general = None
    for s in schedules:
        if s.scope == "unit" and s.unit_id == unit.pk:
            return s
        if s.scope == "general":
            general = s
    if general and (general.include_new_units or unit.pk in general._covered):
        return general
    return None


@transaction.atomic
def sync_contract_visits(contract):
    """Bring future generated visits in line with the schedules. Idempotent: safe to call after any change.
    Completed or worked-on visits are never modified; only untouched future ones are replaced."""
    Contract.objects.select_for_update().get(pk=contract.pk)  # one sync at a time per contract
    today = timezone.localdate()
    desired = {}
    if contract.in_force:
        schedules = list(contract.schedules.filter(is_active=True).prefetch_related("covered_units").select_related("contract"))
        for s in schedules:
            s._covered = {u.pk for u in s.covered_units.all()}
        for unit in contract.active_units.filter(is_active=True):
            s = schedule_for_unit(unit, schedules)
            if s:
                for d in s.planned_dates():
                    if d >= today:
                        desired[(unit.pk, d)] = s

    future = contract.visits.filter(planned_date__gte=today, schedule__isnull=False).select_related("schedule").prefetch_related("attachments")
    for v in future:
        s = desired.get((v.unit_id, v.planned_date))
        if s is None:
            if v.is_untouched():
                v.delete()
        elif v.schedule_id != s.pk and v.status == "scheduled":
            # People follow the new schedule unless someone reassigned this visit by hand.
            if v.engineer_id == v.schedule.engineer_id:
                v.engineer_id = s.engineer_id
            if v.technician_id == v.schedule.technician_id:
                v.technician_id = s.technician_id
            v.schedule, v.source = s, s.scope
            v.save(update_fields=["schedule", "source", "engineer", "technician"])

    # Skip any date the unit already has a planned visit or a live visit on, so overlaps never duplicate.
    unit_ids = {k[0] for k in desired}
    taken = set(Visit.objects.filter(unit_id__in=unit_ids, planned_date__isnull=False).values_list("unit_id", "planned_date"))
    taken |= set(Visit.objects.filter(unit_id__in=unit_ids).exclude(status="cancelled").values_list("unit_id", "scheduled_date"))
    units = {u.pk: u for u in Unit.objects.filter(pk__in=unit_ids)}
    for (unit_id, d), s in sorted(desired.items(), key=lambda kv: kv[0][1]):
        if (unit_id, d) not in taken:
            Visit.objects.create(unit=units[unit_id], client_id=contract.client_id, contract=contract, schedule=s, source=s.scope,
                                 planned_date=d, scheduled_date=d, engineer=s.engineer, technician=s.technician)


def follow_schedule_people(schedule, old_engineer_id, old_technician_id):
    """After a schedule's people change, its open future visits follow. A visit reassigned by hand keeps its person."""
    visits = schedule.visits.open().filter(scheduled_date__gte=timezone.localdate())
    if old_engineer_id != schedule.engineer_id:
        visits.filter(engineer_id=old_engineer_id).update(engineer_id=schedule.engineer_id)
    if old_technician_id != schedule.technician_id:
        visits.filter(technician_id=old_technician_id).update(technician_id=schedule.technician_id)


def sync_unit_contracts(unit):
    """After a unit is archived or restored, so its future visits go away or come back."""
    for contract in Contract.objects.filter(equipment_links__unit=unit, equipment_links__removed_at__isnull=True):
        sync_contract_visits(contract)


def close_open_contract_visits(contract, reason):
    """After suspension or termination: untouched future visits go; worked-on open ones are cancelled with the reason."""
    sync_contract_visits(contract)
    today = timezone.localdate()
    contract.visits.filter(status__in=["scheduled", "rescheduled"], scheduled_date__gte=today).update(status="cancelled", cancel_reason=reason)


def copy_for_renewal(old, new, user):
    set_contract_units(new, list(old.active_units.filter(is_active=True)), user)
    months = old.duration_months
    for s in old.schedules.filter(is_active=True):
        covered = list(s.covered_units.all())
        s.pk = None
        s.contract = new
        s.start_date = new.start_date
        # Old dates fall outside the new term, so shift them by the term length.
        s.manual_dates = [add_months(date.fromisoformat(d), months).isoformat() for d in s.manual_dates] if s.mode == "manual" else []
        s.end_date = None
        s.save()
        s.covered_units.set(covered)
    sync_contract_visits(new)


# ---- Reminders ----

def _when(days, lang):
    if lang == "ar":
        return "اليوم" if days == 0 else f"بعد {days} {'يوم' if days == 1 or days > 10 else 'أيام'}"
    return "today" if days == 0 else f"in {days} day{'' if days == 1 else 's'}"


def _d(value, lang):
    with translation.override(lang):
        return date_format(value, "j F Y")


def visit_messages(v, days):
    kind_en = v.get_visit_type_display()
    with translation.override("ar"):
        kind_ar = t(kind_en)
        unit_ar, client_ar = v.unit.name, v.client.name
    en = (f"Reminder: {kind_en} for {v.unit.name_en} at {v.client.name_en} is scheduled {_when(days, 'en')}, "
          f"on {_d(v.scheduled_date, 'en')}.")
    ar = f"تذكير: {kind_ar} للوحدة {unit_ar} لدى {client_ar} مجدولة {_when(days, 'ar')}، بتاريخ {_d(v.scheduled_date, 'ar')}."
    return en, ar


def contract_messages(c, days):
    with translation.override("ar"):
        client_ar = c.client.name
    verb = "expires today" if days == 0 else f"will expire {_when(days, 'en')}"
    en = f"Attention: Maintenance Contract {c.number} for {c.client.name_en} {verb}. Please review and initiate the renewal process."
    ar = f"تنبيه: عقد الصيانة {c.number} الخاص بـ {client_ar} ينتهي {_when(days, 'ar')}. يرجى المراجعة والبدء بإجراءات التجديد."
    return en, ar


def _users_with(code):
    return [u for u in User.objects.filter(is_active=True).select_related("role") if u.has(code)]


def run_reminders(today=None):
    """Create any reminder that has come due. Catches up after downtime because it selects by window, not by an
    exact date, and the unique (user, dedupe_key) constraint means re-running can never duplicate."""
    today = today or timezone.localdate()
    cfg = SystemSettings.get()
    before = Notification.objects.count()  # bulk_create with ignore_conflicts cannot report how many rows it really added

    if cfg.visit_reminders_enabled:
        watchers = _users_with("notifications.visits")
        visits = (Visit.objects.open().filter(unit__is_active=True, scheduled_date__gte=today, scheduled_date__lte=today + timedelta(days=cfg.visit_reminder_days))
                  .select_related("unit", "client", "engineer", "technician"))
        for v in visits:
            en, ar = visit_messages(v, (v.scheduled_date - today).days)
            people = {u.pk: u for u in watchers}
            for person in (v.engineer, v.technician):
                if person and person.is_active:
                    people[person.pk] = person
            # The date is in the key so that a rescheduled visit is reminded again for its new date.
            key = f"visit:{v.pk}:{v.scheduled_date.isoformat()}"
            rows = [Notification(user=u, kind="visit", dedupe_key=key, message_en=en, message_ar=ar, visit=v) for u in people.values()]
            Notification.objects.bulk_create(rows, ignore_conflicts=True)

    if cfg.contract_reminders_enabled:
        watchers = _users_with("notifications.contracts")
        contracts = (Contract.objects.in_force().filter(end_date__lte=today + timedelta(days=cfg.contract_reminder_days))
                     .exclude(renewals__status="active").select_related("client"))
        for c in contracts:
            en, ar = contract_messages(c, (c.end_date - today).days)
            key = f"contract:{c.pk}:{c.end_date.isoformat()}"
            rows = [Notification(user=u, kind="contract", dedupe_key=key, message_en=en, message_ar=ar, contract=c) for u in watchers]
            Notification.objects.bulk_create(rows, ignore_conflicts=True)

    if cfg.email_enabled:
        send_pending_emails()
    SystemSettings.objects.filter(pk=cfg.pk).update(last_reminder_run=timezone.now())
    return Notification.objects.count() - before


def send_pending_emails():
    """Retries on the next run after a failure, because emailed_at stays empty."""
    week_ago = timezone.now() - timedelta(days=7)
    pending = Notification.objects.filter(emailed_at__isnull=True, created_at__gte=week_ago).exclude(user__email="").select_related("user")
    for n in pending:
        body = n.message_ar if n.user.language == "ar" else n.message_en
        try:
            send_mail(t(n.get_kind_display()), body, settings.DEFAULT_FROM_EMAIL, [n.user.email])
        except smtplib.SMTPRecipientsRefused:  # one wrong address must not stop everyone else's reminders
            log.warning("Email address refused for notification %s", n.pk)
            continue
        except Exception:
            log.exception("Email failed for notification %s", n.pk)
            return
        Notification.objects.filter(pk=n.pk).update(emailed_at=timezone.now())


# ---- Catalog reassignment before deletion ----

def _merge_fields(defs, to_category, to_subtype=None):
    for f in defs:
        # Only the same type merges: a text value moved into a date field could not be displayed.
        twin = FieldDefinition.objects.filter(category=to_category, subtype=to_subtype, name_en__iexact=f.name_en, field_type=f.field_type).exclude(pk=f.pk).first()
        if twin:
            for v in f.values.all():
                # Keep the target's existing value if both exist; never overwrite silently.
                if v.unit_id and twin.values.filter(unit_id=v.unit_id).exists() or v.model_id and twin.values.filter(model_id=v.model_id).exists():
                    v.delete()
                else:
                    v.field = twin
                    v.save(update_fields=["field"])
            f.delete()
        else:
            f.category, f.subtype = to_category, to_subtype
            f.save(update_fields=["category", "subtype"])


@transaction.atomic
def reassign_subtype(source, target):
    source.units.update(subtype=target, category=target.category)
    source.models.update(subtype=target, category=target.category)
    for a in source.doc_assignments.all():
        if DocumentAssignment.objects.filter(document=a.document, subtype=target).exists():
            a.delete()
        else:
            a.subtype = target
            a.save(update_fields=["subtype"])
    _merge_fields(source.fields.all(), target.category, target)


@transaction.atomic
def reassign_category(source, target):
    for sub in source.subtypes.all():
        twin = target.subtypes.filter(name_en__iexact=sub.name_en).first()
        if twin:
            reassign_subtype(sub, twin)
            sub.delete()
        else:
            sub.category = target
            sub.save(update_fields=["category"])
            sub.units.update(category=target)
            sub.models.update(category=target)
            sub.fields.update(category=target)
    source.units.update(category=target)
    source.models.update(category=target)
    for a in source.doc_assignments.all():
        if DocumentAssignment.objects.filter(document=a.document, category=target).exists():
            a.delete()
        else:
            a.category = target
            a.save(update_fields=["category"])
    _merge_fields(source.fields.filter(subtype__isnull=True), target)
    for m in source.manufacturers.all():
        m.categories.add(target)
        m.categories.remove(source)


APP_VISIT_TYPES = {"Preventive": "preventive", "Corrective": "corrective", "Emergency": "emergency", "Commissioning": "commissioning", "Startup": "commissioning"}


def limits_for(unit=None, model=None):
    """The reading limits that apply to a unit (or a model, or everything): the manuals, then Settings, the model and the unit, each replacing the one before."""
    from .phone_report import resolved_limits
    model = unit.model if unit else model
    layers = [(SystemSettings.get().reading_limits, "Changed in Settings")]
    if model:
        layers.append((model.reading_limits, f"Set for model {model.model_number}"))
    if unit:
        layers.append((unit.reading_limits, "Set for this unit"))
    return resolved_limits(layers)


def technicians():
    """People who can receive visits on their phone: active users allowed to update visits."""
    return [u for u in User.objects.filter(is_active=True).select_related("role").order_by("first_name", "username") if u.has("visits.update")]


def dispatch_visits(visits, technician, actor, contract=None):
    """Start the visits and hand them to the technician. They reach his phone on its next sync; he gets one notification
    (one for the whole contract, not one per unit). Logged on each visit."""
    today = timezone.localdate()
    for v in visits:
        late = v.scheduled_date < today  # a missed visit sent now is being worked on today, not overdue; planned_date keeps the original date
        v.technician, v.status, v.scheduled_date = technician, "in_progress", max(v.scheduled_date, today)
        v.save(update_fields=["technician", "status", "scheduled_date"])
        record(actor, "visit_started", v, f"Sent to {technician}" + (f", date moved to {today}" if late else ""))
    if not visits:
        return
    if contract:
        en = f"Contract {contract.number} ({contract.client}): {len(visits)} unit(s) sent to your phone"
        ar = f"العقد {contract.number} ({contract.client}): تم إرسال {len(visits)} وحدة إلى هاتفك"
        key, target = f"dispatch:contract:{contract.pk}:{timezone.now():%Y%m%d%H%M%S}", {"contract": contract}
    else:
        v = visits[0]
        en, ar = f"Visit {v.number} sent to your phone: {v.unit} · {v.client}", f"تم إرسال الزيارة {v.number} إلى هاتفك: {v.unit} · {v.client}"
        key, target = f"dispatch:visit:{v.pk}:{technician.pk}", {"visit": v}
    Notification.objects.bulk_create([Notification(user=technician, kind=Notification.Kind.VISIT, dedupe_key=key[:80], message_en=en, message_ar=ar, **target)],
                                     ignore_conflicts=True)


def report_texts(data):
    """The four text boxes of a visit report, written from a phone report's readings, work, materials and notes."""
    text = lambda key, limit=5000: str(data.get(key, "") or "")[:limit]
    rows = lambda key, limit: [r for r in (data.get(key) if isinstance(data.get(key), list) else [])[:limit] if isinstance(r, dict)]
    lines = lambda key, limit, fmt, need: "\n".join(fmt(r) for r in rows(key, limit) if str(r.get(need, "")).strip())
    readings = lines("checklist_items", 300, lambda r: f"{r.get('label_en', '')}: {r.get('value', '')}", "value")
    attention = lines("attention_notes", 100, lambda r: f"{r.get('unit_no', '')} - {r.get('note', '')}".strip(" -"), "note")
    materials = lines("materials", 200, lambda r: f"{r.get('description', '')}: {r.get('qty', '')} {r.get('unit', '')}".strip(), "description")
    findings = "\n\n".join(x for x in (text("general_notes"), readings and "Readings:\n" + readings) if x) or "Report uploaded from the phone app."
    return {"findings": findings[:20000], "actions_taken": text("required_maintenance"), "parts_used": materials[:5000], "recommendations": attention[:5000]}


def visit_from_report(unit, data, user, when, report_id, open_visit=None):
    """A phone-app report becomes a completed visit on the unit, with its readings, work, materials and notes as the visit report."""
    text = lambda key, limit=5000: str(data.get(key, "") or "")[:limit]
    if open_visit:
        visit = open_visit
        visit.status, visit.actual_date, visit.app_report_id = "completed", when, report_id
        visit.save(update_fields=["status", "actual_date", "app_report_id"])
    else:
        visit = Visit.objects.create(unit=unit, client_id=unit.client_id, visit_type=APP_VISIT_TYPES.get(text("maintenance_type", 30), "preventive"), scheduled_date=when,
                                     actual_date=when, status="completed", technician=user, app_report_id=report_id)
    VisitReport.objects.update_or_create(visit=visit, defaults={**report_texts(data), "author": user})
    return visit


def new_attachment(upload, user, kind, **owner):
    return Attachment.objects.create(kind=kind, file=upload, original_name=upload.name[:255], content_type=upload.content_type or "",
                                     size=upload.size, uploaded_by=user, **owner)


def is_general_schedule_duplicate(contract, scope, unit, exclude_pk=None):
    qs = Schedule.objects.filter(contract=contract, scope=scope, is_active=True)
    if scope == "unit":
        qs = qs.filter(unit=unit)
    return qs.exclude(pk=exclude_pk).exists()

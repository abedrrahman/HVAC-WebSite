"""Equipment units, categories, subtypes, technical fields, clients, manufacturers and models."""
from urllib.parse import urlencode

import segno
from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import format_html

from .. import forms as f
from ..i18n import t
from ..models import (Attachment, Category, Client, Country, EquipmentModel, FieldDefinition, Manufacturer, ReportForm, Subtype, Unit)
from ..report_forms import TYPE_LABELS, report_type
from ..services import (clients_for, contracts_for, documents_for, new_attachment, reassign_category, reassign_subtype, record, spec_rows,
                        sync_unit_contracts, units_for, visits_for)
from .common import (action, active_badge, badge, form_page, link, list_page, post_only, requires, saved, toggle_active, try_delete)
from .documents import document_rows_for_unit, document_rows_for_units


# ---- JSON options for dependent selects ----

def options(request, kind):
    # Model, document and visit forms use these dropdowns too, so any of their view rights is enough.
    if not any(request.user.has(p) for p in ("equipment.view", "manufacturers.view", "documents.view", "visits.view")):
        raise PermissionDenied
    g = request.GET
    if kind == "filter_subtypes":  # list filters: no parent chosen means every option
        qs = Subtype.objects.all()
        if g.get("category"):
            qs = qs.filter(category_id=g["category"])
    elif kind == "filter_models":
        qs = EquipmentModel.objects.order_by("model_number")
        for key in ("category", "subtype", "manufacturer"):
            if g.get(key):
                qs = qs.filter(**{f"{key}_id": g[key]})
        return JsonResponse([{"id": x.pk, "label": x.model_number} for x in qs], safe=False)
    elif kind == "subtypes":
        qs = Subtype.objects.filter(category_id=g.get("category") or None, is_active=True)
    elif kind == "models":
        qs = EquipmentModel.objects.filter(is_active=True, manufacturer_id=g.get("manufacturer_pick") or g.get("manufacturer") or g.get("scope_manufacturer") or None)
        if g.get("category"):
            qs = qs.filter(category_id=g["category"])
        return JsonResponse([{"id": x.pk, "label": x.model_number} for x in qs], safe=False)
    elif kind == "units":
        qs = units_for(request.user).filter(client_id=g.get("client") or None, is_active=True)
    else:
        return JsonResponse([], safe=False)
    return JsonResponse([{"id": x.pk, "label": x.name} for x in qs], safe=False)


# ---- Equipment units ----

class UnitFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    country = forms.ModelChoiceField(Country.objects.all(), label="Country", required=False)
    client = forms.ModelChoiceField(Client.objects.all(), label="Client", required=False)
    category = forms.ModelChoiceField(Category.objects.all(), label="Main Equipment Type", required=False)
    subtype = forms.ModelChoiceField(Subtype.objects.all(), label="Subtype", required=False)
    manufacturer = forms.ModelChoiceField(Manufacturer.objects.all(), label="Manufacturer", required=False)
    model = forms.ModelChoiceField(EquipmentModel.objects.order_by("model_number"), label="Model", required=False)
    status = forms.ChoiceField(label="Equipment Status", required=False, choices=[("", "—"), *Unit.Status.choices, ("archived", "Archived")])

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        f.depends(self.fields["subtype"], "id_category", "filter_subtypes")
        f.depends(self.fields["model"], "id_category,id_subtype,id_manufacturer", "filter_models")
        self.fields["model"].label_from_instance = lambda x: x.model_number

    def narrow_choices(self):
        """After validation, so a stale combination in the URL cannot discard the other filters."""
        d = self.cleaned_data
        if d["category"]:
            self.fields["subtype"].queryset = Subtype.objects.filter(category=d["category"])
        models = EquipmentModel.objects.order_by("model_number")
        for key in ("category", "subtype", "manufacturer"):
            if d[key]:
                models = models.filter(**{key: d[key]})
        self.fields["model"].queryset = models


def _add_equipment(user, **prefill):
    query = urlencode(prefill)
    return {"url": reverse("unit_new") + (f"?{query}" if query else ""), "label": t("Add Equipment")} if user.has("equipment.edit") else None


def _tone(category):
    return category.pk % 6


def _equipment_group(category, title, back_url, back_label, stats, items, add):
    return {"title": title, "tone": _tone(category), "vt": f"type-{category.pk}", "icon_name": "equipment", "back_url": back_url, "back_label": back_label,
            "stats": stats, "items": items, "add": add}


@requires("equipment.view")
def unit_type(request, pk):
    category = get_object_or_404(Category, pk=pk)
    counts = dict(units_for(request.user).filter(is_active=True, category=category).order_by().values_list("subtype").annotate(n=Count("pk")))
    units_label = t("Equipment Units")
    items = [{"name": s.name, "n": counts[s.pk], "url": reverse("unit_subtype", args=[pk, s.pk]), "names": f"{s.name_en} {s.name_ar}"} for s in Subtype.objects.filter(pk__in=counts)]
    items.sort(key=lambda i: -i["n"])
    if None in counts:
        label = t("No subtype")
        items.append({"name": label, "n": counts[None], "url": reverse("unit_subtype", args=[pk, 0]), "names": label})
    for i in items:
        i["meta"] = f"{i['n']} · {units_label}"
    unit_total = sum(counts.values())
    return render(request, "group.html", {"unit_total": unit_total, **_equipment_group(
        category, category.name, reverse("unit_list"), t("All equipment types"), [(unit_total, units_label), (len(items), t("Subtypes"))], items, _add_equipment(request.user, category=pk))})


@requires("equipment.view")
def unit_subtype(request, pk, sub):
    category = get_object_or_404(Category, pk=pk)
    units = units_for(request.user).filter(is_active=True, category=category).select_related("client", "model")
    if sub:
        subtype = get_object_or_404(Subtype, pk=sub, category=category)
        units, heading, prefill = units.filter(subtype=subtype), subtype.name, {"category": pk, "subtype": sub}
    else:
        units, heading, prefill = units.filter(subtype__isnull=True), t("No subtype"), {"category": pk}
    rows = [{"unit": u, "url": u.get_absolute_url(), "name": u.name, "meta": f"{u.client.name} · {u.model.model_number}", "icon": "equipment", "badge": badge(u.status, u.get_status_display()),
             "names": f"{u.name_en} {u.name_ar} {u.serial_number} {u.client.name_en} {u.client.name_ar} {u.model.model_number}"} for u in units.order_by(Lower("name_en"))]
    return render(request, "group.html", {"units": rows, **_equipment_group(
        category, heading, reverse("unit_type", args=[pk]), category.name, [(len(rows), t("Equipment Units"))], rows, _add_equipment(request.user, **prefill))})


@requires("equipment.view")
def unit_list(request):
    if not request.GET:  # any search, filter or ?view=list goes to the full table below
        counts = units_for(request.user).filter(is_active=True).order_by().values_list("category").annotate(n=Count("pk"), subs=Count("subtype", distinct=True))
        totals = {c: (n, subs) for c, n, subs in counts}
        units_label, subtypes_label = t("Equipment Units"), t("Subtypes")
        cards = sorted(({"category": c, "units": totals[c.pk][0], "name": c.name, "tone": _tone(c), "url": reverse("unit_type", args=[c.pk]), "vt": f"type-{c.pk}",
                         "stats": [(totals[c.pk][0], units_label), (totals[c.pk][1], subtypes_label)]} for c in Category.objects.filter(pk__in=totals)), key=lambda c: -c["units"])
        actions = [action("Add Equipment", reverse("unit_new"))] if request.user.has("equipment.edit") else []
        actions.append(action("All equipment", reverse("unit_list") + "?view=list", "", False))
        if request.user.has("catalog.manage"):
            actions.append(action("Categories & Subtypes", reverse("catalog"), "settings", False))
        return render(request, "hub.html", {"title": t("Equipment"), "sub": t("Choose a type to see its equipment"), "cards": cards, "actions": actions, "icon_name": "equipment"})
    qs = units_for(request.user).select_related("client__country", "category", "subtype", "model__manufacturer")
    form = UnitFilter(request.GET)
    form.fields["client"].queryset = clients_for(request.user)
    status = ""
    if form.is_valid():
        d = form.cleaned_data
        status = d["status"]
        if d["q"]:
            q = d["q"]
            qs = qs.filter(Q(name_en__icontains=q) | Q(name_ar__icontains=q) | Q(serial_number__icontains=q) | Q(model__model_number__icontains=q)
                           | Q(client__name_en__icontains=q) | Q(client__name_ar__icontains=q) | Q(location__icontains=q) | Q(project__icontains=q))
        for key, lookup in {"country": "client__country", "client": "client", "category": "category", "subtype": "subtype", "manufacturer": "model__manufacturer", "model": "model"}.items():
            if d[key]:
                qs = qs.filter(**{lookup: d[key]})
        form.narrow_choices()
        if status and status != "archived":
            qs = qs.filter(status=status)
    qs = qs.filter(is_active=status != "archived")
    actions = []
    if request.user.has("equipment.edit"):
        actions.append(action("Add Equipment", reverse("unit_new")))
    if request.user.has("catalog.manage"):
        actions.append(action("Categories & Subtypes", reverse("catalog"), "settings", False))
    two = lambda top, small: format_html('{}<div class="meta">{}</div>', top, small) if small else top  # country and subtype ride under their parent, so the table fits without sideways scrolling
    return list_page(request, "Equipment", qs, ["Equipment Name", "Client", "Project", "Main Equipment Type", "Model", "Serial Number", "Equipment Status"],
                     lambda u: [link(u), two(link(u.client), u.client.country.name), u.project or "—", two(u.category.name, u.subtype.name if u.subtype else ""),
                                two(u.model.model_number, u.model.manufacturer.name), u.serial_number or "—", badge(u.status, u.get_status_display())],
                     filters=form, actions=actions, keep_view="list", back=(reverse("unit_list"), t("All equipment types")),
                     phone_card=lambda u: {"url": u.get_absolute_url(), "icon": "equipment", "name": u.name, "meta": f"{u.client.name} · {u.model.model_number}", "badge": badge(u.status, u.get_status_display())})


@requires("equipment.edit")
def unit_edit(request, pk=None):
    unit = get_object_or_404(Unit, pk=pk) if pk else None
    if unit and not units_for(request.user).filter(pk=pk).exists():
        raise PermissionDenied
    initial = {k: request.GET[k] for k in ("client", "category", "subtype", "manufacturer_pick") if k in request.GET}
    form = f.UnitForm(request.POST or None, instance=unit, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        saved(request, obj, "updated" if pk else "created")
        return redirect("unit_specs", pk=obj.pk) if not pk else redirect(obj)
    return form_page(request, form, "Edit Equipment" if pk else "Add Equipment", unit.get_absolute_url() if unit else reverse("unit_list"))


def _unit_or_404(request, pk):
    return get_object_or_404(units_for(request.user).select_related("client__country", "category", "subtype", "model__manufacturer"), pk=pk)


@requires("equipment.view")
def unit_detail(request, pk):
    unit = _unit_or_404(request, pk)
    u = request.user
    visits = visits_for(u).filter(unit=unit).select_related("contract", "engineer", "technician")
    today_open = visits.filter(status__in=["scheduled", "rescheduled"]).order_by("scheduled_date")
    links = unit.contract_links.filter(removed_at__isnull=True).select_related("contract")
    active_contract = next((l.contract for l in links if l.contract.in_force), None)
    schedule = unit.schedules.filter(contract=active_contract, is_active=True).first() if active_contract else None
    if active_contract and not schedule:
        schedule = active_contract.schedules.filter(scope="general", is_active=True).first()
    history = visits.filter(status="completed").order_by("-actual_date", "-scheduled_date")
    photos = unit.attachments.all()
    return render(request, "unit_detail.html", {
        "unit": unit, "tone": _tone(unit.category), "specs": spec_rows(unit=unit), "documents": document_rows_for_unit(unit) if u.has("documents.view") else None,
        "active_contract": active_contract if u.has("contracts.view") else None, "schedule": schedule if u.has("schedules.view") else None,
        "next_visit": today_open.first(), "upcoming_visits": today_open[:20], "history": history,
        "contract_history": unit.contract_links.select_related("contract").order_by("-added_at") if u.has("contracts.view") else [],
        "photos": photos, "photo_form": f.AttachmentForm(kinds=[Attachment.Kind.PHOTO, Attachment.Kind.NAMEPLATE, Attachment.Kind.INSTALLATION, Attachment.Kind.OTHER], images_only=True),
        "has_report_form": report_type(unit) in TYPE_LABELS,
        "unit_own_form": ReportForm.objects.filter(Q(rows__isnull=False) | Q(materials__isnull=False), unit=unit).exists(),
    })


@requires("equipment.edit")
def unit_specs(request, pk):
    unit = _unit_or_404(request, pk)
    form = f.SpecForm(request.POST or None, owner=unit)
    if request.method == "POST" and form.is_valid():
        form.save()
        saved(request, unit, "specs_updated")
        return redirect(unit)
    intro = t("Empty fields use the value from the model.") if form.defs else t("No technical fields are configured for this equipment type yet.")
    return form_page(request, form, "Technical Information", unit.get_absolute_url(), intro=intro, subject=unit.name)


@post_only
@requires("equipment.edit")
def unit_photo(request, pk):
    unit = _unit_or_404(request, pk)
    form = f.AttachmentForm(request.POST, request.FILES, kinds=[Attachment.Kind.PHOTO, Attachment.Kind.NAMEPLATE, Attachment.Kind.INSTALLATION, Attachment.Kind.OTHER], images_only=True)
    if form.is_valid():
        new_attachment(form.cleaned_data["file"], request.user, form.cleaned_data["kind"], unit=unit)
        record(request.user, "photo_added", unit)
        messages.success(request, t("Saved successfully."))
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(unit.get_absolute_url() + "#images")


@post_only
@requires("equipment.delete")
def unit_archive(request, pk):
    unit = _unit_or_404(request, pk)
    response = toggle_active(request, unit, unit)
    sync_unit_contracts(unit)
    return response


@post_only
@requires("equipment.delete")
def unit_delete(request, pk):
    unit = _unit_or_404(request, pk)
    return try_delete(request, unit, "unit_list", unit)


@requires("equipment.view")
def unit_qr(request, pk):
    unit = _unit_or_404(request, pk)
    base = settings.SITE_URL or request.build_absolute_uri("/").rstrip("/")
    response = HttpResponse(content_type="image/svg+xml")
    segno.make(f"{base}{reverse('unit_scan', args=[unit.qr_token])}", error="m").save(response, kind="svg", scale=6, border=2)
    return response


def unit_scan(request, token):
    """QR target. Middleware has already enforced sign-in; the scope check happens in unit_detail."""
    unit = get_object_or_404(Unit, qr_token=token)
    return redirect(unit)


# ---- Categories, subtypes and technical fields ----

@requires("catalog.manage")
def catalog(request):
    cats = Category.objects.annotate(n_sub=Count("subtypes", distinct=True), n_units=Count("units", distinct=True), n_fields=Count("fields", distinct=True)).order_by("sort_order", "name_en")
    return list_page(request, "Equipment Categories & Subtypes", cats, ["Main Equipment Type", "Subtypes", "Technical Fields", "Equipment Units", "Status"],
                     lambda c: [link(c), c.n_sub, c.n_fields, c.n_units, active_badge(c.is_active)], per_page=100,
                     actions=[action("Add Category", reverse("category_new"))])


@requires("catalog.manage")
def category_detail(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    fields_all = cat.fields.select_related("subtype")
    return render(request, "category_detail.html", {"cat": cat, "subtypes": cat.subtypes.annotate(n=Count("units")), "fields": fields_all})


def _simple_edit(request, form_class, obj, title, back, **form_kw):
    form = form_class(request.POST or None, instance=obj, **form_kw)
    if request.method == "POST" and form.is_valid():
        saved_obj = form.save()
        saved(request, saved_obj, "updated" if obj else "created")
        return redirect(back or saved_obj)
    return form_page(request, form, title, back or reverse("catalog"))


@requires("catalog.manage")
def category_edit(request, pk=None):
    obj = get_object_or_404(Category, pk=pk) if pk else None
    return _simple_edit(request, f.CategoryForm, obj, "Edit Category" if pk else "Add Category", obj.get_absolute_url() if obj else None)


@requires("catalog.manage")
def subtype_edit(request, pk=None):
    obj = get_object_or_404(Subtype, pk=pk) if pk else None
    category = obj.category if obj else get_object_or_404(Category, pk=request.GET.get("category") or request.POST.get("category"))
    form = f.SubtypeForm(request.POST or None, instance=obj, initial={"category": category.pk})
    if request.method == "POST" and form.is_valid():
        sub = form.save()
        saved(request, sub, "updated" if pk else "created")
        return redirect(sub)
    return form_page(request, form, "Edit Subtype" if pk else "Add Subtype", category.get_absolute_url())


def _deletion_guard(request, obj, kind, targets, reassign, counts, back):
    """Linked records must be reassigned or the item archived first. Nothing related is ever destroyed."""
    linked = sum(c for _, c in counts)
    if request.method == "POST":
        choice = request.POST.get("choice")
        if choice == "archive":
            obj.is_active = False
            obj.save(update_fields=["is_active"])
            record(request.user, "archived", obj)
            messages.success(request, t("Archived. Existing records keep their link and it is no longer offered for new records."))
            return redirect(back)
        if choice == "reassign":
            target = targets.filter(pk=request.POST.get("target") or None).first()
            if not target:
                messages.error(request, t("Select where to move the linked records."))
                return redirect(request.path)
            with transaction.atomic():
                reassign(obj, target)
                record(request.user, "reassigned", obj, f"to {target.name_en}")
                obj.delete()
            messages.success(request, t("Records moved and item deleted."))
            return redirect(back)
        if choice == "delete" and linked == 0:
            record(request.user, "deleted", obj)
            obj.delete()
            messages.success(request, t("Deleted."))
            return redirect(back)
    return render(request, "delete_guard.html", {"obj": obj, "kind": kind, "counts": [(t(l), c) for l, c in counts if c], "linked": linked, "targets": targets, "back": obj.get_absolute_url()})


@requires("catalog.manage")
def category_delete(request, pk):
    c = get_object_or_404(Category, pk=pk)
    counts = [("Equipment Units", c.units.count()), ("Models", c.models.count()), ("Subtypes", c.subtypes.count()), ("Documents", c.doc_assignments.count()), ("Technical Fields", c.fields.count())]
    return _deletion_guard(request, c, t("Category"), Category.objects.exclude(pk=pk).filter(is_active=True), reassign_category, counts, reverse("catalog"))


@requires("catalog.manage")
def subtype_delete(request, pk):
    s = get_object_or_404(Subtype, pk=pk)
    counts = [("Equipment Units", s.units.count()), ("Models", s.models.count()), ("Documents", s.doc_assignments.count()), ("Technical Fields", s.fields.count())]
    return _deletion_guard(request, s, t("Subtype"), s.category.subtypes.exclude(pk=pk).filter(is_active=True), reassign_subtype, counts, s.category.get_absolute_url())


@post_only
@requires("catalog.manage")
def subtype_toggle(request, pk):
    s = get_object_or_404(Subtype, pk=pk)
    return toggle_active(request, s, s.category)


@requires("catalog.manage")
def field_edit(request, category, pk=None):
    cat = get_object_or_404(Category, pk=category)
    obj = get_object_or_404(FieldDefinition, pk=pk, category=cat) if pk else None
    form = f.FieldDefinitionForm(request.POST or None, instance=obj, category=cat)
    if request.method == "POST" and form.is_valid():
        field = form.save(commit=False)
        field.category = cat
        if not pk:
            last = cat.fields.order_by("-sort_order").first()
            field.sort_order = last.sort_order + 1 if last else 1
        field.save()
        saved(request, field, "updated" if pk else "created")
        return redirect(cat)
    return form_page(request, form, "Edit Technical Field" if pk else "Add Technical Field", cat.get_absolute_url(), subject=cat.name)


@post_only
@requires("catalog.manage")
def field_move(request, pk, direction):
    field = get_object_or_404(FieldDefinition, pk=pk)
    siblings = list(field.category.fields.order_by("sort_order", "pk"))
    i = siblings.index(field)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(siblings):
        siblings[i], siblings[j] = siblings[j], siblings[i]
        with transaction.atomic():
            for n, s in enumerate(siblings, 1):
                FieldDefinition.objects.filter(pk=s.pk).update(sort_order=n)
    return redirect(field.category)


@post_only
@requires("catalog.manage")
def field_delete(request, pk):
    field = get_object_or_404(FieldDefinition, pk=pk)
    cat = field.category
    if field.values.exists():
        field.is_active = False
        field.save(update_fields=["is_active"])
        messages.warning(request, t("This field holds saved values, so it was deactivated instead of deleted."))
    else:
        record(request.user, "deleted", field)
        field.delete()
        messages.success(request, t("Deleted."))
    return redirect(cat)


# ---- Clients ----

class ClientFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    country = forms.ModelChoiceField(Country.objects.all(), label="Country", required=False)
    status = forms.ChoiceField(label="Status", required=False, choices=[("", "Active"), ("archived", "Archived")])


def _ranked_clients(user):
    return clients_for(user).select_related("country").annotate(n_units=Count("units", filter=Q(units__is_active=True))).order_by("-n_units", Lower("name_en"))


@requires("clients.view")
def client_country(request, pk):
    country = get_object_or_404(Country, pk=pk)
    sites = list(_ranked_clients(request.user).filter(country=country, is_active=True, n_units__gt=0))
    units_label = t("Equipment Units")
    items = [{"url": s.get_absolute_url(), "name": s.name, "names": f"{s.name_en} {s.name_ar}", "meta": f"{s.n_units} · {units_label}"} for s in sites]
    add = {"url": f"{reverse('client_new')}?country={country.pk}", "label": t("Add Client")} if request.user.has("clients.edit") else None
    return render(request, "group.html", {"title": country.name, "code": country.code, "vt": f"country-{country.pk}", "back_url": reverse("client_list"), "back_label": t("All countries"),
                                          "stats": [(len(sites), t("Sites")), (sum(s.n_units for s in sites), units_label)], "items": items, "sites": sites, "add": add})


@requires("clients.view")
def client_list(request):
    qs = _ranked_clients(request.user)
    can_add = request.user.has("clients.edit")
    if not request.GET:  # any search, filter or ?view=list goes to the full table below
        totals = {}
        for pk, n in qs.filter(is_active=True, n_units__gt=0).values_list("country", "n_units"):
            row = totals.setdefault(pk, [0, 0])
            row[0] += 1
            row[1] += n
        sites_label, units_label = t("Sites"), t("Equipment Units")
        cards = [{"country": c, "sites": totals[c.pk][0], "units": totals[c.pk][1], "name": c.name, "code": c.code, "url": reverse("client_country", args=[c.pk]), "vt": f"country-{c.pk}",
                  "stats": [(totals[c.pk][0], sites_label), (totals[c.pk][1], units_label)]} for c in Country.objects.filter(pk__in=totals)]
        actions = [action("Add Client", reverse("client_new"))] if can_add else []
        actions.append(action("All clients", reverse("client_list") + "?view=list", "", False))
        return render(request, "hub.html", {"title": t("Clients"), "sub": t("Choose a country to see its sites"), "cards": sorted(cards, key=lambda c: -c["sites"]),
                                            "actions": actions, "feature_first": True})
    form = ClientFilter(request.GET)
    archived = searching = False
    if form.is_valid():
        d = form.cleaned_data
        archived = d["status"] == "archived"
        searching = bool(d["q"])
        if d["q"]:
            qs = qs.filter(Q(name_en__icontains=d["q"]) | Q(name_ar__icontains=d["q"]) | Q(city__icontains=d["q"]) | Q(contact_person__icontains=d["q"]))
        if d["country"]:
            qs = qs.filter(country=d["country"])
    qs = qs.filter(is_active=not archived)
    if not archived and not searching:  # search still finds them, or a new client could not be reached to add its first unit
        qs = qs.filter(n_units__gt=0)
    acts = [action("Add Client", reverse("client_new"))] if can_add else []
    return list_page(request, "Clients", qs, ["Client Name", "Country", "City", "Contact Person", "Phone Number", "Equipment Units"],
                     lambda c: [link(c), c.country.name, c.city or "—", c.contact_person or "—", c.phone or "—", c.n_units], filters=form, actions=acts, keep_view="list", back=(reverse("client_list"), t("All countries")))


@requires("clients.edit")
def client_edit(request, pk=None):
    obj = get_object_or_404(clients_for(request.user), pk=pk) if pk else None
    initial = {"country": request.GET.get("country") or Country.objects.filter(code="JO").values_list("pk", flat=True).first()} if not pk else None
    form = f.ClientForm(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.save()
        saved(request, c, "updated" if pk else "created")
        return redirect(c)
    return form_page(request, form, "Edit Client" if pk else "Add Client", obj.get_absolute_url() if obj else reverse("client_list"))


@requires("clients.view")
def client_detail(request, pk):
    client = get_object_or_404(clients_for(request.user).select_related("country"), pk=pk)
    u = request.user
    units = list(units_for(u).filter(client=client, is_active=True).select_related("category", "subtype", "model__manufacturer")) if u.has("equipment.view") else []
    groups = {}
    for x in units:
        groups.setdefault(x.category.name, []).append(x)
    docs = {row["doc"].pk: row for row in document_rows_for_units(units)} if u.has("documents.view") else {}
    return render(request, "client_detail.html", {
        "client": client, "groups": groups, "unit_count": len(units),
        "contracts": contracts_for(u).filter(client=client) if u.has("contracts.view") else None,
        "history": visits_for(u).filter(client=client, status="completed").select_related("unit", "contract").order_by("-actual_date") if u.has("visits.view") else None,
        "documents": list(docs.values()),
    })


@post_only
@requires("clients.edit")
def client_archive(request, pk):
    c = get_object_or_404(clients_for(request.user), pk=pk)
    return toggle_active(request, c, c)


@post_only
@requires("clients.edit")
def client_delete(request, pk):
    c = get_object_or_404(clients_for(request.user), pk=pk)
    return try_delete(request, c, "client_list", c)


# ---- Manufacturers and models ----

class ManufacturerFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    category = forms.ModelChoiceField(Category.objects.all(), label="Main Equipment Type", required=False)
    status = forms.ChoiceField(label="Status", required=False, choices=[("", "Active"), ("archived", "Archived")])


@requires("manufacturers.view")
def manufacturer_list(request):
    if not request.GET:  # any search, filter or ?view=list goes to the full table below
        rows = Manufacturer.objects.filter(is_active=True).annotate(n_models=Count("models", distinct=True), n_units=Count("models__units", filter=Q(models__units__is_active=True), distinct=True)).order_by("-n_units", Lower("name_en"))
        cards = [{"name": x.name, "url": x.get_absolute_url(), "tone": x.pk % 6, "names": f"{x.name_en} {x.name_ar}", "meta": f"{x.n_models} · {t('Models')} — {x.n_units} · {t('Equipment Units')}"} for x in rows]
        actions = [action("Add Manufacturer", reverse("manufacturer_new"))] if request.user.has("manufacturers.edit") else []
        actions.append(action("All manufacturers", reverse("manufacturer_list") + "?view=list", "", False))
        return render(request, "hub.html", {"title": t("Manufacturers"), "cards": cards, "actions": actions, "compact": True})
    qs = Manufacturer.objects.select_related("country").annotate(n_models=Count("models", distinct=True)).order_by("name_en")
    form = ManufacturerFilter(request.GET or None)
    archived = False
    if form.is_valid():
        d = form.cleaned_data
        archived = d["status"] == "archived"
        if d["q"]:
            qs = qs.filter(Q(name_en__icontains=d["q"]) | Q(name_ar__icontains=d["q"]) | Q(models__model_number__icontains=d["q"])).distinct()
        if d["category"]:
            qs = qs.filter(categories=d["category"])
    qs = qs.filter(is_active=not archived)
    acts = [action("Add Manufacturer", reverse("manufacturer_new"))] if request.user.has("manufacturers.edit") else []
    return list_page(request, "Manufacturers", qs, ["Manufacturer Name", "Country of Origin", "Website", "Models"],
                     lambda x: [link(x), x.country.name if x.country else "—", x.website or "—", x.n_models], filters=form, actions=acts, keep_view="list", back=(reverse("manufacturer_list"), t("All manufacturers")))


@requires("manufacturers.edit")
def manufacturer_edit(request, pk=None):
    obj = get_object_or_404(Manufacturer, pk=pk) if pk else None
    form = f.ManufacturerForm(request.POST or None, request.FILES or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        x = form.save()
        logo = form.cleaned_data.get("logo")
        if logo:
            new_attachment(logo, request.user, Attachment.Kind.LOGO, manufacturer=x)
        saved(request, x, "updated" if pk else "created")
        return redirect(x)
    return form_page(request, form, "Edit Manufacturer" if pk else "Add Manufacturer", obj.get_absolute_url() if obj else reverse("manufacturer_list"), multipart=True)


@requires("manufacturers.view")
def manufacturer_detail(request, pk):
    x = get_object_or_404(Manufacturer, pk=pk)
    return render(request, "manufacturer_detail.html", {"m": x, "models": x.models.select_related("category", "subtype").annotate(n=Count("units"))})


@post_only
@requires("manufacturers.edit")
def manufacturer_archive(request, pk):
    x = get_object_or_404(Manufacturer, pk=pk)
    return toggle_active(request, x, x)


@post_only
@requires("manufacturers.edit")
def manufacturer_delete(request, pk):
    x = get_object_or_404(Manufacturer, pk=pk)
    return try_delete(request, x, "manufacturer_list", x)


@requires("manufacturers.edit")
def model_edit(request, pk=None):
    obj = get_object_or_404(EquipmentModel, pk=pk) if pk else None
    form = f.EquipmentModelForm(request.POST or None, instance=obj, initial={"manufacturer": request.GET.get("manufacturer")} if not pk else None)
    if request.method == "POST" and form.is_valid():
        x = form.save()
        x.manufacturer.categories.add(x.category)
        saved(request, x, "updated" if pk else "created")
        return redirect(x)
    return form_page(request, form, "Edit Model" if pk else "Add Model", obj.get_absolute_url() if obj else reverse("manufacturer_list"))


@requires("manufacturers.view")
def model_detail(request, pk):
    x = get_object_or_404(EquipmentModel.objects.select_related("manufacturer", "category", "subtype"), pk=pk)
    return render(request, "model_detail.html", {
        "model": x, "specs": spec_rows(model=x), "units": units_for(request.user).filter(model=x, is_active=True).select_related("client"),
        "documents": documents_for(request.user).filter(model=x, is_archived=False).select_related("doc_type") if request.user.has("documents.view") else None,
        "images": x.attachments.all(), "image_form": f.AttachmentForm(kinds=[Attachment.Kind.PRODUCT], images_only=True),
    })


@requires("manufacturers.edit")
def model_specs(request, pk):
    x = get_object_or_404(EquipmentModel, pk=pk)
    form = f.SpecForm(request.POST or None, owner=x)
    if request.method == "POST" and form.is_valid():
        form.save()
        saved(request, x, "specs_updated")
        return redirect(x)
    intro = t("Values entered here are shared by every unit of this model.") if form.defs else t("No technical fields are configured for this equipment type yet.")
    return form_page(request, form, "General Technical Specifications", x.get_absolute_url(), intro=intro, subject=str(x))


@post_only
@requires("manufacturers.edit")
def model_image(request, pk):
    x = get_object_or_404(EquipmentModel, pk=pk)
    form = f.AttachmentForm(request.POST, request.FILES, kinds=[Attachment.Kind.PRODUCT], images_only=True)
    if form.is_valid():
        new_attachment(form.cleaned_data["file"], request.user, "product", model=x)
        messages.success(request, t("Saved successfully."))
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(x)


@post_only
@requires("manufacturers.edit")
def model_archive(request, pk):
    x = get_object_or_404(EquipmentModel, pk=pk)
    return toggle_active(request, x, x)


@post_only
@requires("manufacturers.edit")
def model_delete(request, pk):
    x = get_object_or_404(EquipmentModel, pk=pk)
    return try_delete(request, x, x.manufacturer, x)

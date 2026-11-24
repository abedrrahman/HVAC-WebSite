"""Phone report forms on the website: the default form of each equipment type, and a unit's own form."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from ..i18n import t
from ..models import ReportForm
from ..report_forms import (CATALOG, SETUP_CHOICES, category_type, SETUP_COUNTS, TYPE_LABELS, WITH_AIR_FANS, WITH_COMPRESSORS, clean_setup, default_rows, effective, new_key,
                            report_type, same_rows, type_form, with_new_rows)
from ..services import record, units_for
from .common import post_only, requires

NEW_ROWS = 3  # empty lines at the end of each list for adding


def _target(request, rtype=None, pk=None):
    """(type, unit or None, form or None, back url, title subject). Type forms need Settings; a unit's form needs equipment editing."""
    user = request.user
    if pk:
        if not user.has("equipment.edit"):
            raise PermissionDenied
        unit = get_object_or_404(units_for(user).select_related("category"), pk=pk)
        rtype = report_type(unit)
        if rtype not in TYPE_LABELS:
            raise Http404
        return rtype, unit, ReportForm.objects.filter(unit=unit).first(), unit.get_absolute_url(), unit.name
    if not user.has("settings.manage"):
        raise PermissionDenied
    if rtype not in TYPE_LABELS:
        raise Http404
    return rtype, None, type_form(rtype), reverse("report_forms"), t(TYPE_LABELS[rtype])


def _changes(old, new):
    """Old → new for the activity log, box by box."""
    before = {r["key"]: r for r in old}
    out = []
    for r in new:
        o = before.pop(r["key"], None)
        name = r["label_en"] or r["label_ar"]
        if o is None:
            out.append(f"added: {name}")
            continue
        if (o["label_en"], o["label_ar"]) != (r["label_en"], r["label_ar"]):
            out.append(f"renamed: {o['label_en'] or o['label_ar']} → {name}")
        if o["hidden"] != r["hidden"]:
            out.append(f"{'hidden' if r['hidden'] else 'shown'}: {name}")
    out += [f"removed: {o['label_en'] or o['label_ar']}" for o in before.values()]
    if [r["key"] for r in old if r["key"] in {x["key"] for x in new}] != [r["key"] for r in new if r["key"] in {x["key"] for x in old}]:
        out.append("order changed")
    return out


def _rows_from_post(post):
    rows = []
    for i in range(min(int(post.get("rows") or 0), 300)):
        key, custom = post.get(f"r{i}-key", "")[:60], post.get(f"r{i}-custom") == "1"
        en, ar = post.get(f"r{i}-en", "").strip()[:120], post.get(f"r{i}-ar", "").strip()[:120]
        if not key or (custom and (post.get(f"r{i}-delete") or not (en or ar))):
            continue  # a custom box is removed by ticking Delete or emptying both names
        try:
            pos = float(post.get(f"r{i}-pos") or i + 1)
        except ValueError:
            pos = i + 1
        rows.append((pos, i, {"key": key, "label_en": en, "label_ar": ar, "hidden": not post.get(f"r{i}-show"), "custom": custom}))
    for j in range(NEW_ROWS):
        en, ar = post.get(f"n{j}-en", "").strip()[:120], post.get(f"n{j}-ar", "").strip()[:120]
        if en or ar:
            try:
                pos = float(post.get(f"n{j}-pos") or 10000 + j)
            except ValueError:
                pos = 10000 + j
            rows.append((pos, 1000 + j, {"key": new_key(), "label_en": en, "label_ar": ar, "hidden": False, "custom": True}))
    return [r for _, _, r in sorted(rows, key=lambda x: (x[0], x[1]))]


def _materials_from_post(post):
    out = []
    for i in range(min(int(post.get("materials") or 0), 100) + NEW_ROWS):
        d, u = post.get(f"m{i}-d", "").strip()[:200], post.get(f"m{i}-u", "").strip()[:30]
        if d:
            out.append({"description": d, "unit": u})
    return out


def report_form_edit(request, rtype=None, pk=None):
    rtype, unit, form, back, subject = _target(request, rtype, pk)
    tf = type_form(rtype)
    base_rows = with_new_rows(rtype, tf.rows) if tf and tf.rows is not None else default_rows(rtype)  # what a unit follows without its own form
    base_materials = tf.materials if tf and tf.materials is not None else []
    current = effective(rtype, unit) if unit else {"rows": base_rows, "materials": base_materials, "setup": {}}
    if request.method == "POST":
        rows, materials = _rows_from_post(request.POST), _materials_from_post(request.POST)
        setup = clean_setup({k: request.POST.get(f"s-{k}", "") for k in [*SETUP_CHOICES, *SETUP_COUNTS]}) if unit else {}
        log = _changes(current["rows"], rows)
        if materials != current["materials"]:
            log.append("parts: " + (", ".join(f"{m['description']} {m['unit']}".strip() for m in materials) or "none"))
        if unit and setup != current["setup"]:
            log.append("unit setup: " + (", ".join(f"{k}={v}" for k, v in setup.items()) or "left to the phone"))
        if log:
            form = form or ReportForm(unit=unit, report_type="" if unit else rtype)
            # A unit whose boxes or parts match its type keeps following the type, so later type changes reach it too.
            form.rows = None if unit and same_rows(rows, base_rows) else rows
            form.materials = None if unit and materials == base_materials else materials
            form.setup = setup if unit else {}
            form.web_changed_at, form.source, form.updated_by = timezone.now(), "web", request.user
            form.save()
            record(request.user, "report_form_changed", unit or form, f"{t(TYPE_LABELS[rtype])}\n" + "\n".join(log))
            messages.success(request, t("Saved. New reports on the phones use this form after their next sync; reports already open keep theirs."))
        return redirect(request.path)
    phases = {r["key"]: r.get("phase", "") for r in CATALOG.get(rtype, [])}
    for r in current["rows"]:
        r["phase"] = "" if r["custom"] else phases.get(r["key"], "")
    show_setup = bool(unit)
    return render(request, "report_form.html", {
        "title": t("Report form"), "subject": subject, "back": back, "rtype": rtype, "type_label": TYPE_LABELS[rtype], "unit": unit,
        "rows": current["rows"], "materials": current["materials"], "setup": current.get("setup", {}), "new_rows": range(NEW_ROWS),
        "material_new": range(len(current["materials"]), len(current["materials"]) + NEW_ROWS),
        "own": bool(unit and (current.get("own_rows") or current.get("own_materials"))), "changed": bool(form and (form.rows is not None or form.materials is not None)),
        "form": form, "show_setup": show_setup, "choices": SETUP_CHOICES,
        "counts": [(k, top) for k, top in SETUP_COUNTS.items() if (k in ("compressors", "circuits") and rtype in WITH_COMPRESSORS) or (k.endswith("_fans") and rtype in WITH_AIR_FANS)],
        "count_range": range(0, 6), "type_url": reverse("report_form_type", args=[rtype]) if request.user.has("settings.manage") else "",
    })


@post_only
def report_form_reset(request, rtype=None, pk=None):
    """Back to the default: a unit follows its type's form again; a type goes back to the phone's built-in form. Setup is kept."""
    rtype, unit, form, back, subject = _target(request, rtype, pk)
    if form and (form.rows is not None or form.materials is not None):
        form.rows = form.materials = None
        form.web_changed_at, form.source, form.updated_by = timezone.now(), "web", request.user
        form.save()
        record(request.user, "report_form_reset", unit or form, t(TYPE_LABELS[rtype]))
        messages.success(request, t("The form is back to the default. New reports use it after the next sync."))
    return redirect(request.path.rsplit("reset/", 1)[0])


@requires("settings.manage")
def report_forms(request):
    """Every equipment type's form, and the units that have their own."""
    forms = {f.report_type: f for f in ReportForm.objects.filter(unit__isnull=True).select_related("updated_by")}
    own = [f for f in ReportForm.objects.filter(unit__isnull=False).select_related("unit__client", "unit__category", "updated_by") if f.rows is not None or f.materials is not None]
    types = [{"key": k, "label": label, "form": forms.get(k), "changed": bool(forms.get(k) and forms[k].rows is not None or forms.get(k) and forms[k].materials is not None),
              "units": [f for f in own if report_type(f.unit) == k]} for k, label in TYPE_LABELS.items()]
    return render(request, "report_forms.html", {"title": t("Report forms"), "types": types})


def report_form_units(request, rtype):
    """One form for several units of the same type: pick the units, then edit the form once; each picked unit gets it as its own.
    Each unit keeps its own setup (counts, phase, refrigerant), which differs from unit to unit."""
    from ..models import Category
    if not request.user.has("equipment.edit"):
        raise PermissionDenied
    if rtype not in TYPE_LABELS:
        raise Http404
    categories = [c.pk for c in Category.objects.all() if category_type(c.name_en) == rtype]
    pool = units_for(request.user).filter(is_active=True, category_id__in=categories).select_related("client").order_by("client__name_en", "name_en")
    ids = [int(x) for x in request.POST.getlist("units") or request.GET.getlist("units") if str(x).isdigit()]
    units = list(pool.filter(pk__in=ids))
    back = reverse("report_forms") if request.user.has("settings.manage") else reverse("unit_list")
    if not units:  # step 1: choose the units
        if request.method == "POST":
            messages.error(request, t("Choose at least one unit."))
        groups = {}
        for u in pool:
            groups.setdefault(u.client.name, []).append(u)
        return render(request, "report_form_units.html", {"title": t("Report form for several units"), "subject": t(TYPE_LABELS[rtype]), "groups": groups, "back": back})
    tf = type_form(rtype)
    base_rows = with_new_rows(rtype, tf.rows) if tf and tf.rows is not None else default_rows(rtype)
    base_materials = tf.materials if tf and tf.materials is not None else []
    forms = {f.unit_id: f for f in ReportForm.objects.filter(unit__in=units)}
    current = [effective(rtype, u) for u in units]
    # The picked units start from the form they share; units with different forms start from their type's form.
    rows = current[0]["rows"] if all(same_rows(c["rows"], current[0]["rows"]) for c in current) else base_rows
    materials = current[0]["materials"] if all(c["materials"] == current[0]["materials"] for c in current) else base_materials
    if request.method == "POST":
        reset = bool(request.POST.get("reset"))
        new_rows, new_materials = (None, None) if reset else (_rows_from_post(request.POST), _materials_from_post(request.POST))
        log = ["back to the type's form"] if reset else _changes(rows, new_rows) + (["parts: " + (", ".join(f"{m['description']} {m['unit']}".strip() for m in new_materials) or "none")] if new_materials != materials else [])
        if log:
            now = timezone.now()
            for u in units:
                form = forms.get(u.pk) or ReportForm(unit=u)
                form.rows = None if reset or same_rows(new_rows, base_rows) else new_rows
                form.materials = None if reset or new_materials == base_materials else new_materials
                form.web_changed_at, form.source, form.updated_by = now, "web", request.user
                form.save()
                record(request.user, "report_form_reset" if reset else "report_form_changed", u, f"{t(TYPE_LABELS[rtype])} · {len(units)} units\n" + "\n".join(log))
            messages.success(request, t("The form is back to the default. New reports use it after the next sync.") if reset else
                             t("Saved. New reports on the phones use this form after their next sync; reports already open keep theirs.") + f" ({len(units)})")
        return redirect(f"{request.path}?" + "&".join(f"units={u.pk}" for u in units))
    phases = {r["key"]: r.get("phase", "") for r in CATALOG.get(rtype, [])}
    rows = [{**r, "phase": "" if r["custom"] else phases.get(r["key"], "")} for r in rows]
    return render(request, "report_form.html", {
        "title": t("Report form for several units"), "subject": t(TYPE_LABELS[rtype]), "back": request.path, "rtype": rtype, "type_label": TYPE_LABELS[rtype],
        "unit": None, "multi": units, "rows": rows, "materials": materials, "setup": {}, "new_rows": range(NEW_ROWS),
        "material_new": range(len(materials), len(materials) + NEW_ROWS), "changed": any(f.rows is not None or f.materials is not None for f in forms.values()),
        "form": None, "show_setup": False, "choices": SETUP_CHOICES, "counts": [], "count_range": range(0),
        "type_url": reverse("report_form_type", args=[rtype]) if request.user.has("settings.manage") else "",
    })

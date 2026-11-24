"""Phone report forms and reading limits on the website: the default of each equipment type, one unit's own, or the same for several units."""
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .. import forms as f
from ..i18n import t
from ..models import Category, ReportForm, SystemSettings
from ..phone_report import LIMIT_DEFS, UNITS, limit_text, resolved_limits
from ..report_forms import (CATALOG, SETUP_CHOICES, SETUP_COUNTS, TYPE_LABELS, WITH_AIR_FANS, WITH_COMPRESSORS, category_type, clean_setup, default_rows,
                            effective, new_key, report_type, same_rows, type_form, with_new_rows)
from ..services import limits_for, record, units_for
from .common import post_only, requires

NEW_ROWS = 3  # empty lines at the end of each list for adding


def _target(request, rtype=None, pk=None):
    """(type, unit or None, form or None, back url, title subject). Type forms need Settings; a unit's form needs equipment editing."""
    user = request.user
    if pk:
        if not user.has("equipment.edit"):
            raise PermissionDenied
        unit = get_object_or_404(units_for(user).select_related("category", "model__category"), pk=pk)
        rtype = report_type(unit)
        if rtype not in TYPE_LABELS:
            raise Http404
        return rtype, unit, ReportForm.objects.filter(unit=unit).first(), unit.get_absolute_url(), unit.name
    if not user.has("settings.manage"):
        raise PermissionDenied
    if rtype not in TYPE_LABELS:
        raise Http404
    return rtype, None, type_form(rtype), reverse("report_forms"), t(TYPE_LABELS[rtype])


def _type_base(rtype, tf):
    """What a unit follows without a form of its own: its type's boxes and parts, or the phone's built-in boxes."""
    rows = with_new_rows(rtype, tf.rows) if tf and tf.rows is not None else default_rows(rtype)
    return rows, (tf.materials if tf and tf.materials is not None else [])


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


def _number(text, fallback):
    try:
        n = float(text)
    except (TypeError, ValueError):
        return fallback
    return n if n == n else fallback  # "nan" is not a place in the list


def _rows_from_post(post, rtype):
    standard = {r["key"]: r for r in CATALOG.get(rtype, [])}
    rows = []
    for i in range(min(int(_number(post.get("rows"), 0)), 300)):
        key, custom = post.get(f"r{i}-key", "")[:60], post.get(f"r{i}-custom") == "1"
        en, ar = post.get(f"r{i}-en", "").strip()[:120], post.get(f"r{i}-ar", "").strip()[:120]
        if not key or (custom and (post.get(f"r{i}-delete") or not (en or ar))):
            continue  # a custom box is removed by ticking Delete or emptying both names
        if not custom and not (en or ar) and key in standard:
            en, ar = standard[key]["label_en"], standard[key]["label_ar"]  # a standard box never goes nameless: emptied, it gets the app's name back
        rows.append((_number(post.get(f"r{i}-pos"), i + 1), i, {"key": key, "label_en": en, "label_ar": ar, "hidden": not post.get(f"r{i}-show"), "custom": custom}))
    for j in range(NEW_ROWS):
        en, ar = post.get(f"n{j}-en", "").strip()[:120], post.get(f"n{j}-ar", "").strip()[:120]
        if en or ar:
            rows.append((_number(post.get(f"n{j}-pos"), 10000 + j), 1000 + j, {"key": new_key(), "label_en": en, "label_ar": ar, "hidden": False, "custom": True}))
    return [r for _, _, r in sorted(rows, key=lambda x: (x[0], x[1]))]


def _materials_from_post(post):
    out = []
    for i in range(min(int(_number(post.get("materials"), 0)), 100) + NEW_ROWS):
        d, u = post.get(f"m{i}-d", "").strip()[:200], post.get(f"m{i}-u", "").strip()[:30]
        if d:
            out.append({"description": d, "unit": u})
    return out


class LimitsForm(f.Bilingual, forms.Form):
    pass


def _limits_form(data, above, stored):
    """Every reading limit as a minimum and a maximum box. Empty boxes follow the level above, shown under each limit."""
    form = LimitsForm(data, prefix="lim")
    form.sections = []
    for key, (label, kind, *_rest) in LIMIT_DEFS.items():
        low, high, source = above[key]
        shown = stored.get(key) or [None, None]
        form.fields[f"{key}_low"] = forms.FloatField(required=False, initial=shown[0], label=f"{t('Minimum')} ({UNITS[kind]})",
                                                     help_text=f"{t('Used when left empty')}: {limit_text(low, high, UNITS[kind]) or '—'}" + (f" · {source}" if source else ""))
        form.fields[f"{key}_high"] = forms.FloatField(required=False, initial=shown[1], label=f"{t('Maximum')} ({UNITS[kind]})")
        form.sections.append((label, [f"{key}_low", f"{key}_high"]))
    return form


def _limits_from(form):
    """The limits filled in, or None when a minimum is above its maximum (the error is on the form)."""
    new = {}
    for key in LIMIT_DEFS:
        low, high = form.cleaned_data[f"{key}_low"], form.cleaned_data[f"{key}_high"]
        if low is not None and high is not None and low > high:
            form.add_error(f"{key}_low", t("The minimum is higher than the maximum."))
        elif low is not None or high is not None:
            new[key] = [low, high]
    return None if form.errors else new


def _limits_log(before, after):
    words = lambda key, pair: limit_text(*pair, UNITS[LIMIT_DEFS[key][1]]) if pair else "as above"
    return [f"{LIMIT_DEFS[k][0]}: {words(k, before.get(k))} → {words(k, after.get(k))}" for k in LIMIT_DEFS if before.get(k) != after.get(k)]


def _settings_limits():
    return resolved_limits([(SystemSettings.get().reading_limits, "Changed in Settings")])


def report_form_edit(request, rtype=None, pk=None):
    """One page for a report form and its reading limits: a type's (Save all) or one unit's. Save to multiple units goes on to choose units."""
    rtype, unit, form, back, subject = _target(request, rtype, pk)
    tf = type_form(rtype)
    base_rows, base_materials = _type_base(rtype, tf)
    current = effective(rtype, unit) if unit else {"rows": base_rows, "materials": base_materials, "setup": {}}
    stored = dict((unit.reading_limits if unit else tf.limits if tf else {}) or {})
    above = limits_for(model=unit.model) if unit else _settings_limits()  # what an empty limit box follows here
    limits = _limits_form(request.POST or None, above, stored)
    if request.method == "POST":
        if request.POST.get("action") == "multi":
            return _pick(request, rtype, request.POST)
        new_limits = _limits_from(limits) if limits.is_valid() else None
        if new_limits is not None:
            rows = _rows_from_post(request.POST, rtype) if "rows" in request.POST else current["rows"]
            materials = _materials_from_post(request.POST) if "materials" in request.POST else current["materials"]
            setup = clean_setup({k: request.POST.get(f"s-{k}", "") for k in [*SETUP_CHOICES, *SETUP_COUNTS]}) if unit else {}
            log = _changes(current["rows"], rows)
            if materials != current["materials"]:
                log.append("parts: " + (", ".join(f"{m['description']} {m['unit']}".strip() for m in materials) or "none"))
            if unit and "s-phase" in request.POST and setup != current["setup"]:
                log.append("unit setup: " + (", ".join(f"{k}={v}" for k, v in setup.items()) or "left to the phone"))
            limit_log = _limits_log(stored, new_limits)
            if log or (limit_log and not unit):
                form = form or ReportForm(unit=unit, report_type="" if unit else rtype)
                if log:
                    # A unit whose boxes or parts match its type keeps following the type, so later type changes reach it too.
                    form.rows = None if unit and same_rows(rows, base_rows) else rows
                    form.materials = None if unit and materials == base_materials else materials
                    form.setup = setup if unit and "s-phase" in request.POST else form.setup
                    form.web_changed_at, form.source, form.updated_by = timezone.now(), "web", request.user
                if not unit:
                    form.limits = new_limits
                form.save()
            if unit and limit_log:
                unit.reading_limits = new_limits
                unit.save(update_fields=["reading_limits"])
            if log:
                record(request.user, "report_form_changed", unit or form, f"{t(TYPE_LABELS[rtype])}\n" + "\n".join(log))
            if limit_log:
                record(request.user, "limits_changed", unit or form, f"{t(TYPE_LABELS[rtype])}\n" + "\n".join(limit_log))
            if log or limit_log:
                messages.success(request, t("Saved. New reports on the phones use this form after their next sync; reports already open keep theirs."))
            return redirect(request.path)
    phases = {r["key"]: r.get("phase", "") for r in CATALOG.get(rtype, [])}
    rows = [{**r, "phase": "" if r["custom"] else phases.get(r["key"], "")} for r in current["rows"]]
    return render(request, "report_form.html", {
        "title": t("Report form"), "subject": subject, "back": back, "rtype": rtype, "type_label": TYPE_LABELS[rtype], "unit": unit,
        "rows": rows, "materials": current["materials"], "setup": current.get("setup", {}), "new_rows": range(NEW_ROWS),
        "material_new": range(len(current["materials"]), len(current["materials"]) + NEW_ROWS),
        "own": bool(unit and (current.get("own_rows") or current.get("own_materials"))), "changed": bool(form and (form.rows is not None or form.materials is not None)),
        "form": form, "limits_form": limits, "own_limits": bool(stored), "choices": SETUP_CHOICES,
        "counts": [(k, top) for k, top in SETUP_COUNTS.items() if (k in ("compressors", "circuits") and rtype in WITH_COMPRESSORS) or (k.endswith("_fans") and rtype in WITH_AIR_FANS)],
        "count_range": range(0, 6), "type_url": reverse("report_form_type", args=[rtype]) if request.user.has("settings.manage") else "",
        "multi_url": reverse("report_form_units", args=[rtype]),
    })


def _pool(request, rtype):
    """Active units of this type the user may see, by client."""
    categories = [c.pk for c in Category.objects.all() if category_type(c.name_en) == rtype]
    return units_for(request.user).filter(is_active=True, category_id__in=categories).select_related("client").order_by("client__name_en", "name_en")


def _pick(request, rtype, carried, error=False):
    """Step two of Save to multiple units: choose the units. The form as edited rides along unchanged in hidden fields."""
    if error:
        messages.error(request, t("Choose at least one unit."))
    groups = {}
    for u in _pool(request, rtype):
        groups.setdefault(u.client.name, []).append(u)
    keep = [(k, v) for k in carried for v in carried.getlist(k) if k not in ("csrfmiddlewaretoken", "action", "units", "origin")]
    origin = carried.get("origin") or request.path
    return render(request, "report_form_units.html", {"title": t("Save to multiple units"), "subject": t(TYPE_LABELS[rtype]), "groups": groups,
                                                      "carried": keep, "origin": origin, "action": reverse("report_form_units", args=[rtype])})


@post_only
def report_form_units(request, rtype):
    """Save to multiple units: the form's boxes and parts become each chosen unit's own form, and the limits filled in are added to each
    unit's own limits (an empty box never removes a unit's limit). Each unit keeps its own setup, which differs from unit to unit."""
    if not request.user.has("equipment.edit"):
        raise PermissionDenied
    if rtype not in TYPE_LABELS:
        raise Http404
    ids = [int(x) for x in request.POST.getlist("units") if x.isdigit()]
    units = list(_pool(request, rtype).filter(pk__in=ids))
    if not units:
        return _pick(request, rtype, request.POST, error=True)
    tf = type_form(rtype)
    base_rows, base_materials = _type_base(rtype, tf)
    rows, materials = _rows_from_post(request.POST, rtype), _materials_from_post(request.POST)
    limits = _limits_form(request.POST, _settings_limits(), {})
    new_limits = _limits_from(limits) if limits.is_valid() else None
    if new_limits is None:
        messages.error(request, t("The minimum is higher than the maximum."))
        return _pick(request, rtype, request.POST)
    own = {x.unit_id: x for x in ReportForm.objects.filter(unit__in=units)}
    now = timezone.now()
    for u in units:
        uf = own.get(u.pk) or ReportForm(unit=u)
        uf.rows = None if same_rows(rows, base_rows) else rows
        uf.materials = None if materials == base_materials else materials
        uf.web_changed_at, uf.source, uf.updated_by = now, "web", request.user
        uf.save()
        record(request.user, "report_form_changed", u, f"{t(TYPE_LABELS[rtype])} · saved to {len(units)} units")
        merged = {**(u.reading_limits or {}), **new_limits}
        if merged != (u.reading_limits or {}):
            record(request.user, "limits_changed", u, "\n".join(_limits_log(u.reading_limits or {}, merged)))
            u.reading_limits = merged
            u.save(update_fields=["reading_limits"])
    messages.success(request, t("Saved. New reports on the phones use this form after their next sync; reports already open keep theirs.") + f" ({len(units)})")
    origin = request.POST.get("origin", "")
    return redirect(origin if url_has_allowed_host_and_scheme(origin, allowed_hosts={request.get_host()}) else reverse("report_form_type", args=[rtype]))


@post_only
def report_form_reset(request, rtype=None, pk=None):
    """Back to the default: a unit follows its type's form again; a type goes back to the phone's built-in form. Setup and limits are kept."""
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
    by_type = {x.report_type: x for x in ReportForm.objects.filter(unit__isnull=True).select_related("updated_by")}
    own = [x for x in ReportForm.objects.filter(unit__isnull=False).select_related("unit__client", "unit__category", "updated_by") if x.rows is not None or x.materials is not None]
    types = [{"key": k, "label": label, "form": by_type.get(k), "changed": bool(by_type.get(k) and (by_type[k].rows is not None or by_type[k].materials is not None or by_type[k].limits)),
              "units": [x for x in own if report_type(x.unit) == k]} for k, label in TYPE_LABELS.items()]
    return render(request, "report_forms.html", {"title": t("Report forms"), "types": types})

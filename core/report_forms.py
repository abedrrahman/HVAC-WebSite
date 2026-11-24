"""Phone report forms: the boxes a new report on the phone starts with, changed from the website.
Each equipment type has a default form; one unit can have its own. Without a change on the website the phone uses its
built-in form, listed in report_form_catalog.json (exported from the app by tools/export_form_catalog.mjs)."""
import json
import re
import secrets
from pathlib import Path

from django.utils import timezone
from django.utils.dateparse import parse_datetime

CATALOG = json.loads(Path(__file__).with_name("report_form_catalog.json").read_text(encoding="utf-8"))["types"]
TYPE_LABELS = {  # the phone's equipment types, in the order the website lists them
    "chiller": "Chillers", "ahu": "Air Handling Units (AHU)", "fcu": "Fan Coil Units (FCU)", "split": "Split Units",
    "vrf": "VRF / VRV Systems", "rtu": "Rooftop Units", "ccu": "Close Control Units (CCU)", "dehumidifier": "Dehumidifiers",
    "cooling_tower": "Cooling Towers", "boiler": "Boilers",
}
WITH_COMPRESSORS = {"chiller", "fcu", "split", "rtu", "dehumidifier", "ccu"}
WITH_AIR_FANS = {"ahu", "rtu", "dehumidifier"}
REFRIGERANTS = ["R-22", "R-134a", "R-410A", "R-407C", "R-32", "R-404A", "Other"]
SETUP_CHOICES = {"phase": ["1Ph", "3Ph"], "refrigerant": REFRIGERANTS, "temp_unit": ["C", "F", "K"], "pressure_unit": ["bar", "psi", "kPa", "MPa"]}
SETUP_COUNTS = {"compressors": 5, "circuits": 3, "supply_fans": 5, "extract_fans": 5, "return_fans": 5}  # highest number the phone allows


def report_type(unit):
    """The phone's equipment type for a unit, from its main category: the same rule as machineTypeFor in the app."""
    return category_type(unit.category.name_en)


def category_type(name):
    c = name.lower()
    if "air handling" in c or "ahu" in c:
        return "ahu"
    if "chiller" in c:
        return "chiller"
    if "fan coil" in c or "fcu" in c:
        return "fcu"
    if "vrf" in c or "vrv" in c:
        return "vrf"
    if "cooling tower" in c:
        return "cooling_tower"
    if "boiler" in c:
        return "boiler"
    if re.search(r"roof ?top|rtu", c):
        return "rtu"
    if "dehumid" in c:
        return "dehumidifier"
    if re.search(r"close control|ccu|crac|precision", c):
        return "ccu"
    if re.search(r"split|ducted", c):
        return "split"
    return "custom"


def default_rows(rtype):
    return [{"key": r["key"], "label_en": r["label_en"], "label_ar": r["label_ar"], "hidden": False, "custom": False} for r in CATALOG.get(rtype, [])]


def with_new_rows(rtype, rows):
    """A saved form plus any standard box the phone gained after it was saved, placed after the box it follows."""
    rows = [dict(r) for r in rows]
    have = {r["key"] for r in rows}
    before = None
    for r in default_rows(rtype):
        if r["key"] not in have:
            at = next((i + 1 for i, x in enumerate(rows) if x["key"] == before), 0 if before is None else len(rows))
            rows.insert(at, r)
        before = r["key"]
    return rows


def type_form(rtype):
    from .models import ReportForm
    return ReportForm.objects.filter(report_type=rtype, unit__isnull=True).first()


def unit_form(unit):
    from .models import ReportForm
    return ReportForm.objects.filter(unit=unit).first()


def effective(rtype, unit=None):
    """The form a new report starts with: the unit's own rows and parts if set, else the type's, else the phone's built-in form."""
    tf, uf = type_form(rtype), unit_form(unit) if unit else None
    rows = (uf.rows if uf and uf.rows is not None else tf.rows if tf and tf.rows is not None else None)
    materials = (uf.materials if uf and uf.materials is not None else tf.materials if tf and tf.materials is not None else [])
    return {"rows": with_new_rows(rtype, rows) if rows is not None else default_rows(rtype), "materials": materials,
            "setup": dict(uf.setup) if uf else {}, "own_rows": bool(uf and uf.rows is not None), "own_materials": bool(uf and uf.materials is not None)}


def last_web_change(rtype, unit):
    """When the website last changed this unit's form, through its own form or its type's. Reports started before it do not change it."""
    stamps = [f.web_changed_at for f in (type_form(rtype), unit_form(unit)) if f and f.web_changed_at]
    return max(stamps) if stamps else None


def new_key():
    return "custom_" + secrets.token_hex(4)


def clean_rows(rows):
    """Rows as the website keeps them; anything else the phone sent is dropped. Returns None when the list is unusable."""
    if not isinstance(rows, list):
        return None
    out, seen = [], set()
    for r in rows[:200]:
        if not isinstance(r, dict):
            continue
        custom = bool(r.get("custom"))
        en, ar = str(r.get("label_en") or "")[:120].strip(), str(r.get("label_ar") or "")[:120].strip()
        key = str(r.get("key") or "")[:60].strip() or (new_key() if custom else "")
        if not key or key in seen or (custom and not (en or ar)):
            continue
        seen.add(key)
        out.append({"key": key, "label_en": en, "label_ar": ar, "hidden": bool(r.get("hidden")), "custom": custom})
    return out


def clean_materials(materials):
    if not isinstance(materials, list):
        return None
    return [{"description": str(m.get("description") or "")[:200].strip(), "unit": str(m.get("unit") or "")[:30].strip()}
            for m in materials[:100] if isinstance(m, dict) and str(m.get("description") or "").strip()]


def clean_setup(setup):
    """Only known keys with allowed values; a missing key lets the phone decide."""
    if not isinstance(setup, dict):
        return {}
    out = {}
    for key, choices in SETUP_CHOICES.items():
        if str(setup.get(key) or "") in choices:
            out[key] = str(setup[key])
    for key, top in SETUP_COUNTS.items():
        value = str(setup.get(key, "")).strip()
        if value.isdigit() and int(value) <= top:
            out[key] = int(value)
    return out


def same_rows(a, b):
    """Two forms show the same boxes in the same order with the same names (a custom box's own id does not count)."""
    pick = lambda rows: [("custom" if r["custom"] else r["key"], r["label_en"], r["label_ar"], r["hidden"]) for r in rows]
    return pick(a) == pick(b)


def sync_payload(units):
    """What the phone needs: every changed type form, and the forms of the units it receives."""
    from .models import ReportForm
    stamp = lambda f: (f.web_changed_at or f.updated_at).isoformat()
    types = {f.report_type: {"rows": with_new_rows(f.report_type, f.rows) if f.rows is not None else None, "materials": f.materials, "changed_at": stamp(f)}
             for f in ReportForm.objects.filter(unit__isnull=True)}
    by_id = {u.pk: u for u in units}
    forms = {}
    for f in ReportForm.objects.filter(unit_id__in=by_id):
        rtype = report_type(by_id[f.unit_id])
        forms[str(f.unit_id)] = {"report_type": rtype, "rows": with_new_rows(rtype, f.rows) if f.rows is not None else None,
                                 "materials": f.materials, "setup": f.setup, "changed_at": stamp(f)}
    return {"types": types, "units": forms}


def update_from_report(unit, data, user):
    """A finished phone report brings its form back: the unit's setup, and its own boxes and parts when they differ from its type's form.
    A report started before the website last changed the form changes nothing: the website's change is newer.
    Returns what happened, for the activity log, or "" when nothing changed."""
    from .models import ReportForm
    form = data.get("form") if isinstance(data, dict) else None
    if not isinstance(form, dict):
        return ""  # an older phone app sends no form
    rtype = report_type(unit)
    started = parse_datetime(str(data.get("created_at") or ""))
    changed = last_web_change(rtype, unit)
    if changed and (not started or started <= changed):
        return "ignored: the form was changed on the website after this report started"
    rows, materials, setup = clean_rows(form.get("rows")), clean_materials(form.get("materials")), clean_setup(form.get("setup"))
    tf = type_form(rtype)
    type_rows = with_new_rows(rtype, tf.rows) if tf and tf.rows is not None else default_rows(rtype)
    type_materials = tf.materials if tf and tf.materials is not None else []
    uf = unit_form(unit) or ReportForm(unit=unit)
    new_rows = None if rows is None or same_rows(rows, type_rows) else rows
    new_materials = None if materials is None or materials == type_materials else materials
    if rows is None:
        new_rows = uf.rows  # the phone sent no rows: keep what the unit had
    if materials is None:
        new_materials = uf.materials
    if (uf.pk and uf.rows == new_rows and uf.materials == new_materials and uf.setup == setup) or (not uf.pk and new_rows is None and new_materials is None and not setup):
        return ""
    uf.rows, uf.materials, uf.setup, uf.source, uf.updated_by = new_rows, new_materials, setup, "app", user
    uf.save()
    return "own form" if new_rows is not None or new_materials is not None else "setup only"

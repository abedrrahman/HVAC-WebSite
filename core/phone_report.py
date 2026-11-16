"""Turns a phone-app report (its JSON) into what the report page shows: one box per reading, grouped by
compressor and circuit, and the superheat of every circuit that has a suction pressure and a suction temperature.
The readings are whatever the app sent: boxes the technician added or deleted simply appear or are absent."""
import math
import re

# Dew-point pressure in bar absolute, from CoolProp 8.0, at every 5 °C from -50 to 65.
PT_TEMPS = range(-50, 66, 5)
PT_BAR = {
    "R410A": [1.086, 1.386, 1.748, 2.181, 2.693, 3.294, 3.993, 4.8, 5.727, 6.783, 7.981, 9.332, 10.848, 12.543, 14.429, 16.521, 18.834, 21.383, 24.186, 27.261, 30.63, 34.316, 38.348, 42.763],
    "R32": [1.101, 1.406, 1.774, 2.214, 2.734, 3.346, 4.058, 4.881, 5.826, 6.906, 8.131, 9.514, 11.069, 12.808, 14.746, 16.896, 19.275, 21.898, 24.783, 27.948, 31.412, 35.199, 39.332, 43.843],
    "R134A": [0.295, 0.391, 0.512, 0.661, 0.844, 1.064, 1.327, 1.639, 2.006, 2.433, 2.928, 3.497, 4.146, 4.884, 5.717, 6.654, 7.702, 8.87, 10.166, 11.599, 13.179, 14.915, 16.818, 18.898],
    "R22": [0.645, 0.829, 1.052, 1.32, 1.639, 2.014, 2.453, 2.962, 3.548, 4.218, 4.98, 5.841, 6.809, 7.893, 9.1, 10.439, 11.919, 13.548, 15.336, 17.292, 19.427, 21.751, 24.275, 27.012],
    "R407C": [0.502, 0.661, 0.857, 1.097, 1.387, 1.735, 2.147, 2.632, 3.198, 3.853, 4.607, 5.469, 6.449, 7.556, 8.803, 10.199, 11.758, 13.491, 15.412, 17.535, 19.876, 22.453, 25.286, 28.398],
    "R404A": [0.81, 1.037, 1.31, 1.636, 2.022, 2.475, 3.002, 3.61, 4.307, 5.102, 6.003, 7.018, 8.157, 9.429, 10.844, 12.412, 14.145, 16.053, 18.149, 20.447, 22.961, 25.709, 28.712, 32.0],
}
ATMOSPHERE_BAR = 1.013  # technicians read gauge pressure
TO_BAR = {"bar": 1, "psi": 1 / 14.5038, "kPa": 0.01, "MPa": 10}
# General guide for the zones of the meter, in K. Change here if ASEE uses other limits.
SUPERHEAT_LOW, SUPERHEAT_HIGH, METER_MAX = 4, 12, 20


def saturation_temp(refrigerant, bar_abs):
    """Dew temperature in °C at an absolute pressure, or None for an unknown refrigerant or a pressure outside the table."""
    table = PT_BAR.get(re.sub(r"[^A-Z0-9]", "", str(refrigerant).upper()))
    if not table or not table[0] <= bar_abs <= table[-1]:
        return None
    for i, t in enumerate(PT_TEMPS):
        if bar_abs <= table[i + 1]:
            # ln(pressure) is close to a straight line over 5 °C, so the error stays under 0.1 K
            return t + 5 * (math.log(bar_abs) - math.log(table[i])) / (math.log(table[i + 1]) - math.log(table[i]))


def _number(value):
    found = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", "."))
    return float(found.group()) if found else None


def superheat(refrigerant, pressure, pressure_unit, temp, temp_unit, band=(None, None)):
    """Suction temperature minus the saturation temperature at the suction pressure. Returns the meter's values, or a reason it cannot be worked out.
    band is the normal range in K; a missing end falls back to the general guide."""
    p, ts = _number(pressure), _number(temp)
    if p is None or ts is None:
        return None
    low, high = (SUPERHEAT_LOW if band[0] is None else band[0]), (SUPERHEAT_HIGH if band[1] is None else band[1])
    fahrenheit = temp_unit == "F"
    out = {"refrigerant": refrigerant, "pressure": pressure, "temp": temp}
    if re.sub(r"[^A-Z0-9]", "", str(refrigerant).upper()) not in PT_BAR:
        return {**out, "problem": "No pressure table for this refrigerant."}
    sat = saturation_temp(refrigerant, p * TO_BAR.get(pressure_unit, 1) + ATMOSPHERE_BAR)
    if sat is None:
        return {**out, "problem": "The suction pressure is outside the refrigerant's range. Check the reading and its unit."}
    kelvin = ((ts - 32) / 1.8 if fahrenheit else ts) - sat
    zone = "low" if kelvin < low else "high" if kelvin > high else "normal"
    top = max(METER_MAX, high * 1.5)  # the meter always has room to show "high"
    percent = lambda x: f"{min(max(x, 0), top) / top * 100:.1f}"
    # Text, not numbers: the Arabic page would write 6,4 and break the meter's CSS position.
    return {**out, "value": f"{kelvin * (1.8 if fahrenheit else 1):.1f}", "unit": "°F" if fahrenheit else "K",
            "saturation": f"{sat * 1.8 + 32 if fahrenheit else sat:.1f}", "zone": zone,
            "zone_label": {"low": "Low", "normal": "Normal", "high": "High"}[zone],
            "position": percent(kelvin), "low_width": percent(low), "normal_width": percent(high - low)}


# ---- Operating limits. The starting values come from the manuals stored in the system (document and page). ----
# Stored in °C, bar gauge, V, A, K and %. Settings, a model and a single unit can each replace a limit: see resolved_limits.
# The manual values describe a standard unit: one with the low-temperature (glycol) option runs below the water limits on purpose,
# which is what a model's or unit's own limit is for.
SRC_WATER = "AQUACIAT LD/ILD 150-600 Instruction Manual p.26; Carrier 30RBS/30RBSY IOM p.28"
SRC_COND = "30GX / 30HXC Installation Manual p.20"
SRC_VOLT = "30GX / 30HXC Installation Manual p.13 (400 V, range 360-440)"
SRC_VOLT_1PH = "AQUACIAT 2 Evolution Instruction Manual p.18 (rated voltage ±10 %, for 220-230 V)"
SRC_IMBALANCE = "AQUACIAT LD/ILD 150-600 Instruction Manual p.24"
SRC_HP = "AQUACIATPOWER BV Instruction Manual p.25; AQUACIAT LD/ILD 150A-600A and AQUACIATPOWER ILD Unit Service Guides (high-pressure switch)"
SRC_SUPERHEAT = "General guide; the controllers' superheat setpoint is 5 K (SmartVu service guides) or 7 K (Connect 2 settings)"
SRC_TABLE = "Refrigerant pressure table (no saturation temperature between -50 and 65 °C at this pressure)"
UNITS = {"temp": "°C", "pressure": "bar", "volt": "V", "amp": "A", "kelvin": "K", "percent": "%"}
LIMIT_DEFS = {  # limit key: label, kind, lowest, highest, source. No value means the manuals give none: it is checked only once someone sets it.
    "evap_water_in": ("Evaporator water in", "temp", 7.5, 30, SRC_WATER),
    "evap_water_out": ("Evaporator water out", "temp", 5, 20, SRC_WATER),
    "water_difference": ("Evaporator water in/out difference", "kelvin", 3, 10, SRC_WATER),
    "cond_water_in": ("Condenser water in", "temp", 20, 45, SRC_COND),
    "cond_water_out": ("Condenser water out", "temp", 25, 50, SRC_COND),
    "voltage_1ph": ("Voltage, single-phase", "volt", 198, 253, SRC_VOLT_1PH),
    "voltage_3ph": ("Voltage, three-phase", "volt", 360, 440, SRC_VOLT),
    "voltage_imbalance": ("Voltage imbalance", "percent", None, 2, SRC_IMBALANCE),
    "amps": ("Compressor current", "amp", None, None, ""),
    "oil_pressure": ("Oil pressure", "pressure", None, None, ""),
    "suction_temp": ("Suction temperature", "temp", None, None, ""),
    "discharge_temp": ("Discharge temperature", "temp", None, None, ""),
    "superheat": ("Superheat (normal range)", "kelvin", SUPERHEAT_LOW, SUPERHEAT_HIGH, SRC_SUPERHEAT),
    "discharge_R410A": ("Discharge pressure, R410A", "pressure", None, 41.5, SRC_HP),
    "discharge_R32": ("Discharge pressure, R32", "pressure", None, 44.2, SRC_HP),
    "discharge_R407C": ("Discharge pressure, R407C", "pressure", None, 29.0, SRC_HP),
    "discharge_R134A": ("Discharge pressure, R134a", "pressure", None, None, ""),
    "discharge_R22": ("Discharge pressure, R22", "pressure", None, None, ""),
    "discharge_R404A": ("Discharge pressure, R404A", "pressure", None, None, ""),
}
READING_LIMIT = {  # reading key without its "compressor_1_" or "circuit_1_" prefix -> the limit that applies to it
    "evap_water_in": "evap_water_in", "evap_water_out": "evap_water_out", "cond_water_in": "cond_water_in", "cond_water_out": "cond_water_out",
    "voltage_1ph": "voltage_1ph", "voltage_l1l2": "voltage_3ph", "voltage_l1l3": "voltage_3ph", "voltage_l2l3": "voltage_3ph",
    "amps": "amps", "oil_pressure": "oil_pressure", "suction_temp": "suction_temp", "discharge_temp": "discharge_temp",
}


def resolved_limits(layers=()):
    """The limits that apply: the manuals' values, then each layer on top. A layer is (stored limits, where they were set),
    given widest first: Settings, then the model, then the unit. Returns limit key -> (lowest, highest, source)."""
    out = {key: (low, high, source) for key, (_label, _kind, low, high, source) in LIMIT_DEFS.items()}
    number = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    for stored, origin in layers:
        for key, pair in (stored if isinstance(stored, dict) else {}).items():
            if key in out and isinstance(pair, (list, tuple)) and len(pair) == 2:
                out[key] = (number(pair[0]), number(pair[1]), origin)
    return out


def _measured(value):
    """The value as a number, only when it is nothing but a number."""
    found = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*", str(value or ""))
    return float(found.group(1).replace(",", ".")) if found else None


def limit_text(low, high, unit):
    show = lambda x: f"{round(x, 1):g}"
    if low is not None and high is not None:
        return f"{show(low)}–{show(high)} {unit}"
    return f"max {show(high)} {unit}" if high is not None else f"min {show(low)} {unit}" if low is not None else ""


def _outside(number, low, high):
    return (low is not None and number < low) or (high is not None and number > high)


def _limit_for(key, value, refrigerant, temp_unit, pressure_unit, limits):
    """(limit text in the report's own unit, outside the limit?, source) for one reading, or None when no limit is set for it."""
    number = _measured(value)
    if number is None:
        return None
    name, factor = re.sub(r"[^A-Z0-9]", "", str(refrigerant).upper()), TO_BAR.get(pressure_unit, 1)
    limit_key = f"discharge_{name}" if key == "discharge_pressure" else READING_LIMIT.get(key)
    low, high, source = limits.get(limit_key, (None, None, ""))
    if low is not None or high is not None:
        kind = LIMIT_DEFS[limit_key][1]
        if kind == "temp" and temp_unit == "F":  # the report is in °F: compare in °C, show the limit in °F
            shown, base, unit = (lambda x: None if x is None else x * 1.8 + 32), (number - 32) / 1.8, "°F"
        elif kind == "pressure":
            shown, base, unit = (lambda x: None if x is None else x / factor), number * factor, pressure_unit
        else:
            shown, base, unit = (lambda x: x), number, UNITS[kind]
        return limit_text(shown(low), shown(high), unit), _outside(base, low, high), source
    if key in ("suction_pressure", "discharge_pressure") and name in PT_BAR and saturation_temp(name, number * factor + ATMOSPHERE_BAR) is None:
        return "", True, SRC_TABLE  # a typing slip such as 440 bar
    return None


def _unit_for(key, value, temp_unit, pressure_unit):
    """The unit shown beside a reading. The same key rules as the app, so both show a value the same way."""
    if not key or _measured(value) is None:  # "Single Phase (1Ph)" holds a digit but is not a measurement
        return ""
    if "temp" in key or re.search(r"water_(in|out)$", key):
        return "°" + temp_unit
    if "pressure" in key:
        return pressure_unit
    if "amps" in key:
        return "A"
    return "V" if "voltage" in key else ""


def _derived(raw, temp_unit, limits):
    """Checks that no single reading shows: the water temperature difference and the voltage imbalance."""
    out = []

    def add(key, label, label_ar, value, unit):
        low, high, source = limits[key]
        out.append({"label": label, "label_ar": label_ar, "value": f"{value:.1f}", "unit": unit, "limit": limit_text(low, high, unit),
                    "bad": _outside(value, low, high), "source": source if low is not None or high is not None else ""})
    water_in, water_out = _measured(raw.get("evap_water_in")), _measured(raw.get("evap_water_out"))
    if water_in is not None and water_out is not None:
        add("water_difference", "Evap. water in/out difference", "فرق حرارة ماء المبخر", (water_in - water_out) / (1.8 if temp_unit == "F" else 1), "K")
    volts = [_measured(raw.get(k)) for k in ("voltage_l1l2", "voltage_l1l3", "voltage_l2l3")]
    if all(v is not None for v in volts) and sum(volts):
        average = sum(volts) / 3
        add("voltage_imbalance", "Voltage imbalance", "عدم توازن الفولتية", 100 * max(abs(v - average) for v in volts) / average, "%")
    return out


def report_view(data, limits=None):
    """The report as groups of reading boxes (General, Compressor n, Circuit n), each circuit with its superheat.
    A reading outside a limit from the manuals is marked "bad", with the limit and where it comes from."""
    limits = limits or resolved_limits()
    rows = lambda key: [r for r in (data.get(key) if isinstance(data.get(key), list) else []) if isinstance(r, dict)]
    text = lambda row, key: str(row.get(key, "") or "").strip()
    temp_unit, pressure_unit = ("F" if data.get("temp_unit") == "F" else "C"), str(data.get("pressure_unit") or "bar")
    items = [r for r in rows("checklist_items") if text(r, "value")]
    refrigerant = next((text(r, "value") for r in items if text(r, "field_key") in ("refrigerant_type", "refrigerant")), "")

    groups = {}
    for r in items:
        key = text(r, "field_key")
        part = re.match(r"(compressor|circuit)_(\d+)_", key)
        kind, number = (part.group(1), int(part.group(2))) if part else ("general", 0)
        short = re.sub(r"^(compressor|circuit)_\d+_", "", key)
        group = groups.setdefault((kind, number), {"kind": kind, "number": number, "readings": [], "raw": {}})
        label = text(r, "label_en") or text(r, "label_ar") or key  # both languages are shown; a custom box may have only one
        limit, bad, source = _limit_for(short, text(r, "value"), refrigerant, temp_unit, pressure_unit, limits) or ("", False, "")
        group["readings"].append({"label": label, "label_ar": text(r, "label_ar") if text(r, "label_ar") != label else "", "value": text(r, "value"),
                                  "unit": _unit_for(key, text(r, "value"), temp_unit, pressure_unit), "limit": limit, "bad": bad, "source": source})
        group["raw"][short] = text(r, "value")
    for group in groups.values():
        raw = group.pop("raw")
        group["readings"] += _derived(raw, temp_unit, limits)
        group["superheat"] = superheat(refrigerant, raw.get("suction_pressure"), pressure_unit, raw.get("suction_temp"), temp_unit, limits["superheat"][:2]) if refrigerant else None
        group["pressure_unit"], group["temp_unit"] = pressure_unit, "°" + temp_unit
    order = {"general": 0, "compressor": 1, "circuit": 2}
    readings = [r for g in groups.values() for r in g["readings"]]
    return {
        "groups": sorted(groups.values(), key=lambda g: (order[g["kind"]], g["number"])),
        "outside": sum(r["bad"] for r in readings), "sources": sorted({r["source"] for r in readings if r["source"]}),
        "materials": [r for r in rows("materials") if text(r, "description")],
        "attention": [r for r in rows("attention_notes") if text(r, "note")],
        "notes": str(data.get("general_notes") or "").strip(), "work": str(data.get("required_maintenance") or "").strip(),
        "technician": str(data.get("technician") or "").strip(),
    }

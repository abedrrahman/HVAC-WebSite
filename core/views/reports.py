"""All reports share one shape: columns plus rows, shown as a table and exportable to Excel or PDF."""
import io
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from django import forms
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import forms as f
from ..i18n import t
from ..models import Client, Contract, Unit, Visit
from ..services import clients_for, contracts_for, units_for, visits_for
from .common import fmt, requires, table


def _units(req):
    return units_for(req.user).filter(is_active=True).select_related("client__country", "category", "subtype", "model__manufacturer")


UNIT_COLS = ["Equipment Name", "Client", "Project", "Country", "Main Equipment Type", "Subtype", "Manufacturer", "Model", "Serial Number", "Equipment Status"]


def _unit_row(u):
    return [u.name, u.client.name, u.project, u.client.country.name, u.category.name, u.subtype.name if u.subtype else "", u.model.manufacturer.name, u.model.model_number, u.serial_number, t(u.get_status_display())]


def _unit_report(order):
    def build(req, flt):
        qs = _units(req).order_by(*order)
        if flt.get("client"):
            qs = qs.filter(client=flt["client"])
        return UNIT_COLS, [_unit_row(u) for u in qs]
    return build


CONTRACT_COLS = ["Contract Number", "Contract Title", "Client", "Contract Start Date", "Contract End Date", "Contract Value", "Currency", "Contract Status", "Equipment Units"]


def _contract_report(status):
    def build(req, flt):
        qs = contracts_for(req.user)
        qs = (qs.in_force() if status == "active" else qs.with_status(status)).select_related("client").order_by("end_date")
        if flt.get("client"):
            qs = qs.filter(client=flt["client"])
        if flt.get("start"):  # contract reports are about expiry, so the dates apply to the end date
            qs = qs.filter(end_date__gte=flt["start"])
        if flt.get("end"):
            qs = qs.filter(end_date__lte=flt["end"])
        return CONTRACT_COLS, [[c.number, c.title, c.client.name, fmt(c.start_date), fmt(c.end_date), c.value if c.value is not None else "", c.currency, t(c.status_label), c.active_units.count()] for c in qs]
    return build


VISIT_COLS = ["Visit Number", "Client", "Equipment Unit", "Serial Number", "Contract Number", "Scheduled Date", "Actual Visit Date", "Assigned Engineer", "Assigned Technician", "Visit Status"]


def _visit_row(v):
    return [v.number, v.client.name, v.unit.name, v.unit.serial_number, v.contract.number if v.contract else "", fmt(v.scheduled_date), fmt(v.actual_date) if v.actual_date else "", str(v.engineer or ""), str(v.technician or ""), t(v.status_label)]


def _visits(req, flt):
    qs = visits_for(req.user).select_related("client", "unit", "contract", "engineer", "technician")
    if flt.get("client"):
        qs = qs.filter(client=flt["client"])
    if flt.get("start"):
        qs = qs.filter(scheduled_date__gte=flt["start"])
    if flt.get("end"):
        qs = qs.filter(scheduled_date__lte=flt["end"])
    return qs


def _visit_report(selector):
    def build(req, flt):
        return VISIT_COLS, [_visit_row(v) for v in selector(_visits(req, flt))]
    return build


def _history(req, flt):
    return _visits(req, flt).filter(status="completed").order_by("-actual_date")


REPORTS = {
    "equipment-client": ("Equipment by client", "equipment.view", _unit_report(["client__name_en", "name_en"])),
    "equipment-country": ("Equipment by country", "equipment.view", _unit_report(["client__country__name_en", "client__name_en"])),
    "equipment-category": ("Equipment by category", "equipment.view", _unit_report(["category__sort_order", "subtype__name_en", "name_en"])),
    "equipment-manufacturer": ("Equipment by manufacturer", "equipment.view", _unit_report(["model__manufacturer__name_en", "model__model_number"])),
    "contracts-active": ("Active maintenance contracts", "contracts.view", _contract_report("active")),
    "contracts-expired": ("Expired contracts", "contracts.view", _contract_report("expired")),
    "contracts-expiring": ("Contracts approaching expiration", "contracts.view", _contract_report("expiring")),
    "visits-scheduled": ("Scheduled maintenance visits", "visits.view", _visit_report(lambda q: q.open().filter(scheduled_date__gte=timezone.localdate()).order_by("scheduled_date"))),
    "visits-completed": ("Completed maintenance visits", "visits.view", _visit_report(lambda q: q.filter(status="completed").order_by("-actual_date"))),
    "visits-overdue": ("Overdue maintenance visits", "visits.overdue", _visit_report(lambda q: q.with_status("overdue").order_by("scheduled_date"))),
    "history-equipment": ("Maintenance history by equipment", "visits.view", lambda r, fl: (VISIT_COLS, [_visit_row(v) for v in _history(r, fl).order_by("unit__name_en", "-actual_date")])),
    "history-client": ("Maintenance history by client", "visits.view", lambda r, fl: (VISIT_COLS, [_visit_row(v) for v in _history(r, fl).order_by("client__name_en", "-actual_date")])),
}


class ReportFilter(f.BaseForm):
    client = forms.ModelChoiceField(Client.objects.all(), label="Client", required=False)
    start = forms.DateField(label="From", required=False)
    end = forms.DateField(label="To", required=False)


@requires("reports.view")
def report_list(request):
    return render(request, "reports.html", {"reports": [(k, t(v[0])) for k, v in REPORTS.items() if request.user.has(v[1])]})


@requires("reports.view")
def report_view(request, key):
    if key not in REPORTS:
        raise Http404
    title, perm, build = REPORTS[key]
    if not request.user.has(perm):
        raise PermissionDenied
    form = ReportFilter(request.GET or None)
    form.fields["client"].queryset = clients_for(request.user)
    if key.startswith("equipment"):  # equipment has no date to filter on, so do not offer one
        del form.fields["start"], form.fields["end"]
    flt = form.cleaned_data if form.is_valid() else {}
    cols, rows = build(request, flt)
    export = request.GET.get("export")
    if export == "xlsx":
        return _xlsx(t(title), [t(c) for c in cols], rows, key)
    if export == "pdf":
        return _pdf(t(title), [t(c) for c in cols], rows, key, request.LANGUAGE_CODE == "ar")
    query = request.GET.copy()
    query.pop("export", None)
    return render(request, "report.html", {"title": t(title), "table": table(cols, rows), "filters": form, "query": query.urlencode(), "count": len(rows), "key": key})


def _xlsx(title, cols, rows, key):
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31].replace("/", "-")
    ws.sheet_view.rightToLeft = any("\u0600" <= ch <= "\u06ff" for ch in title)
    ws.append(cols)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="D1003C")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in rows:
        # A leading quote stops spreadsheet apps treating client-entered text such as "=1+1" as a formula.
        ws.append(["'" + c if isinstance(c, str) and c.startswith(("=", "+", "-", "@")) else c for c in row])
    for i, col in enumerate(ws.columns, 1):
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(width + 3, 45)
    ws.freeze_panes = "A2"
    buffer = io.BytesIO()
    wb.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{key}.xlsx"'
    return response


FONT_CANDIDATES = [Path(p) for p in (r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\arial.ttf",
                                     "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]


def _pdf_font():
    # shortcut: uses a system font with Arabic glyphs; ship a TTF with the app if deploying where none of these exist.
    for path in FONT_CANDIDATES:
        if path.exists():
            if "HvacFont" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("HvacFont", str(path)))
            return "HvacFont"
    return "Helvetica"


def _shape(text):
    text = str(text if text is not None else "")
    if any("\u0600" <= ch <= "\u06ff" for ch in text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def _pdf(title, cols, rows, key, rtl):
    font = _pdf_font()
    align = 2 if rtl else 0
    cell = ParagraphStyle("cell", fontName=font, fontSize=7.5, leading=9.5, alignment=align)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white)
    heading = ParagraphStyle("title", fontName=font, fontSize=14, leading=18, alignment=align, textColor=colors.HexColor("#16181D"))
    from xml.sax.saxutils import escape

    def para(v, style):
        return Paragraph(escape(_shape(v)), style)

    data = [[para(c, head) for c in cols]] + [[para(c, cell) for c in r] for r in rows]
    if rtl:
        data = [list(reversed(r)) for r in data]
    page = landscape(A4)
    logo = Image(str(settings.BASE_DIR / "static" / "logo.png"), width=120, height=120 * 387 / 1361, hAlign="RIGHT" if rtl else "LEFT")
    width = (page[0] - 48) / max(len(cols), 1)
    tbl = Table(data, colWidths=[width] * len(cols), repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D1003C")), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E0E0E6")),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F9")]), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    buffer = io.BytesIO()
    stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
    SimpleDocTemplate(buffer, pagesize=page, leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24, title=key).build(
        [logo, Spacer(1, 8), para(title, heading), para(f"{stamp} · {len(rows)}", cell), Spacer(1, 8), tbl])
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{key}.pdf"'
    return response

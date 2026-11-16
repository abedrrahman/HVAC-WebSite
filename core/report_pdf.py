"""The approved phone report as a PDF: the readings as corrected on the website, with their limits, the superheat,
notes, parts, recommendations, photos and the two approvals. The phone's own PDF follows it, because only that one
carries the technician's and the client's signatures."""
import io
import logging
from xml.sax.saxutils import escape

import pypdfium2 as pdfium
from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .i18n import AR
from .views.reports import _pdf_font, _shape

log = logging.getLogger(__name__)
BRAND, BAD, LINE, MUTED = colors.HexColor("#D1003C"), colors.HexColor("#B42318"), colors.HexColor("#E0E0E6"), colors.HexColor("#636874")
FOOTER = "Aladdin Sweiti Engineering Est. · Amman, Jordan · Tel +962 6 585 5959 · info@asee-eng.com · www.asee-eng.com"
PAGE_W, PAGE_H = A4
MARGIN = 32
WIDTH = PAGE_W - 2 * MARGIN


def both(en):
    """A fixed label in English and Arabic, as the rest of the system shows it."""
    return f"{en} · {AR[en]}" if AR.get(en) else en


def build_report_pdf(fr, view, photos, phone_pdf):
    font = _pdf_font()
    base = ParagraphStyle("base", fontName=font, fontSize=8.5, leading=11)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=9.5, textColor=MUTED)
    red = ParagraphStyle("red", parent=base, textColor=BAD)
    head = ParagraphStyle("head", parent=base, textColor=colors.white)
    title = ParagraphStyle("title", parent=base, fontSize=15, leading=20)
    section = ParagraphStyle("section", parent=base, fontSize=10.5, leading=14, spaceBefore=10, spaceAfter=4, textColor=BRAND)

    def para(text, style=base):
        # Arabic is shaped line by line, so a reading's two languages never swap places across lines.
        return Paragraph("<br/>".join(escape(_shape(line)) for line in str(text if text is not None else "").split("\n")), style)

    def table(rows, widths, header=None, red_rows=()):
        data = ([[para(h, head) for h in header]] if header else []) + rows
        tbl = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.25, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), BRAND))
        shift = 1 if header else 0
        style += [("BACKGROUND", (0, r + shift), (-1, r + shift), colors.HexColor("#FAE3E1")) for r in red_rows]
        tbl.setStyle(TableStyle(style))
        return tbl

    visit = fr.visit
    client = visit.client if visit else None
    when = lambda moment: timezone.localtime(moment).strftime("%d/%m/%Y %H:%M") if moment else "—"
    info = [
        ("Client", " · ".join(dict.fromkeys(x for x in (client.name_en, client.name_ar) if x)) if client else fr.client_name or "—"),
        ("Site", fr.site or "—"), ("Equipment Unit", visit.unit.name if visit else "—"), ("Equipment Model", fr.machine_model or "—"),
        ("Serial Number", fr.serial_no or "—"), ("Visit Number", visit.number if visit else "—"),
        ("Actual Visit Date", fr.report_date.strftime("%d/%m/%Y")), ("Visit Type", fr.maintenance_type or "—"),
        ("Assigned Technician", view["technician"] or str(fr.uploaded_by or "—")),
    ]
    pairs = [info[i:i + 2] for i in range(0, len(info), 2)]
    info_rows = [sum(([para(both(label), small), para(value)] for label, value in pair), []) + [para(""), para("")] * (2 - len(pair)) for pair in pairs]

    story = [Image(str(settings.BASE_DIR / "static" / "logo.png"), width=130, height=130 * 387 / 1361, hAlign="LEFT"), Spacer(1, 6),
             para(both("Maintenance Report"), title), Spacer(1, 6),
             table(info_rows, [WIDTH * w for w in (0.17, 0.33, 0.17, 0.33)])]
    if view["outside"]:
        story += [Spacer(1, 6), para(f"{view['outside']} reading(s) outside the limits are marked in red.", red)]

    names = {"general": "General readings", "compressor": "Compressor", "circuit": "Circuit"}
    for g in view["groups"]:
        heading = both(names[g["kind"]]) + (f" {g['number']}" if g["number"] else "")
        rows, red_rows = [], []
        for i, r in enumerate(g["readings"]):
            label = " · ".join(x for x in (r["label"], r["label_ar"]) if x)
            style = red if r["bad"] else base
            rows.append([para(label, style), para(f"{r['value']} {r['unit']}".strip(), style), para(r["limit"], small)])
            if r["bad"]:
                red_rows.append(i)
        story += [para(heading, section), table(rows, [WIDTH * 0.52, WIDTH * 0.22, WIDTH * 0.26], ["Reading · القراءة", "Value · القيمة", "Limit · الحد"], red_rows)]
        s = g["superheat"]
        if s:
            line = (s["problem"] if s.get("problem") else
                    f"Superheat {s['value']} {s['unit']} ({s['zone_label']}) · saturation {s['saturation']} {g['temp_unit']} "
                    f"at {s['pressure']} {g['pressure_unit']}, suction {s['temp']} {g['temp_unit']} · {s['refrigerant']}")
            story += [Spacer(1, 3), para(line, red if s.get("zone") in ("low", "high") or s.get("problem") else base)]

    for label, text in (("Actions Taken", view["work"]), ("Notes", view["notes"])):
        if text:
            story += [para(both(label), section), para(text)]
    if view["attention"]:
        story += [para(both("Recommendations"), section),
                  table([[para(a.get("unit_no") or "—"), para(a.get("note"))] for a in view["attention"]], [WIDTH * 0.25, WIDTH * 0.75])]
    if view["materials"]:
        story += [para(both("Parts Used"), section),
                  table([[para(m.get("description")), para(m.get("qty")), para(m.get("unit"))] for m in view["materials"]], [WIDTH * 0.6, WIDTH * 0.2, WIDTH * 0.2])]

    pictures = []
    for p in photos:
        try:
            w, h = ImageReader(p.file.path).getSize()
            box = (WIDTH - 12) / 2
            scale = min(box / w, 190 / h)
            pictures.append(Image(p.file.path, width=w * scale, height=h * scale))
        except Exception:  # a damaged photo must not stop the report
            log.warning("Photo %s left out of report %s", p.pk, fr.pk)
    if pictures:
        rows = [pictures[i:i + 2] + [""] * (2 - len(pictures[i:i + 2])) for i in range(0, len(pictures), 2)]
        grid = Table(rows, colWidths=[WIDTH / 2] * 2)
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story += [para(both("Photos"), section), grid]

    story += [para(both("Approvals"), section), table([
        [para(both("Foreman")), para(str(fr.foreman_by or "—")), para(when(fr.foreman_at))],
        [para(both("Engineer")), para(str(fr.engineer_by or "—")), para(when(fr.engineer_at))],
    ], [WIDTH * 0.3, WIDTH * 0.4, WIDTH * 0.3])]
    if phone_pdf:
        story += [Spacer(1, 6), para("The technician's original report with the signatures follows on the next pages.", small)]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 18, FOOTER)
        canvas.drawRightString(PAGE_W - MARGIN, 18, str(doc.page))
        canvas.restoreState()

    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=40,
                      title=f"Maintenance report {visit.number if visit else fr.pk}").build(story, onFirstPage=footer, onLaterPages=footer)
    content = buffer.getvalue()
    if not phone_pdf:
        return content
    try:  # the signed phone PDF is appended page by page
        merged, signed = pdfium.PdfDocument(content), pdfium.PdfDocument(phone_pdf.file.path)
        merged.import_pages(signed)
        out = io.BytesIO()
        merged.save(out)
        signed.close()
        merged.close()
        return out.getvalue()
    except Exception:
        log.exception("Could not add the phone PDF to report %s", fr.pk)
        return content

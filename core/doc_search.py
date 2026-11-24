"""The assistant behind Alarm Lookup. It only searches what is stored in this app: the alarm list and the text of the uploaded manuals."""
import difflib
import logging
import math
import re
from functools import lru_cache, reduce
from operator import and_, or_

import pypdfium2
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank, SearchVector
from django.db import transaction
from django.db.models import F, Q

from .models import AlarmCode, Document, DocumentPage, alarm_key
from .services import documents_for, find_alarms

log = logging.getLogger(__name__)

STOP = set("""a an the is are was were be been my our your i we you it its this that these those of to in on at for from with by and or not no
what why how when where who which do does did can could should would will has have had please help need problem problems issue issues alarm alarms
fault faults error errors code codes unit units machine chiller chillers system showing shows show give gives appears appeared keeps keep working work
works there get got after before very too about""".split())
AR_STOP = set("ما ماذا لماذا كيف هل في من على عن الى إلى لا لم مع هذا هذه هناك عندي عندنا يوجد مشكله مشاكل مشكلة عطل اعطال انذار إنذار الوحده الوحدة الجهاز تعمل يعمل شغال".split())


def _ar_normal(word):
    return word.translate(str.maketrans("أإآةى", "اااهي"))


# Lets an Arabic question reach the English manuals.
AR_TERMS = {_ar_normal(k): v for k, v in {
    "ضغط": "pressure", "مرتفع": "high", "عالي": "high", "منخفض": "low", "واطي": "low", "ضاغط": "compressor", "كمبروسر": "compressor", "مروحة": "fan", "مراوح": "fan",
    "مضخة": "pump", "ماء": "water", "مياه": "water", "تدفق": "flow", "حرارة": "temperature", "حساس": "sensor", "حساسات": "sensor", "تجمد": "freeze", "ثلج": "frost",
    "كهرباء": "power", "جهد": "voltage", "تيار": "current", "فاز": "phase", "طور": "phase", "زيت": "oil", "فريون": "refrigerant", "غاز": "refrigerant", "تسريب": "leak",
    "تهريب": "leak", "صمام": "valve", "فلتر": "filter", "مبخر": "evaporator", "مكثف": "condenser", "تبريد": "cooling", "تدفئة": "heating", "تشغيل": "start", "يشتغل": "start",
    "توقف": "stop", "ايقاف": "stop", "اتصال": "communication", "شبكة": "communication", "محرك": "motor", "موتور": "motor", "ضوضاء": "noise", "صوت": "noise", "اهتزاز": "vibration",
    "حمل": "load", "قاطع": "breaker", "بطارية": "battery", "ذوبان": "defrost",
}.items()}
AR_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال")
WORD = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-/]*|[ء-ي]+")


# ---- Reading the manuals ----

def page_texts(path):
    """(page number, text) for every page with readable text. Skips pages whose font hides the real letters."""
    pdf = pypdfium2.PdfDocument(path)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            finder = page.get_textpage()
            raw = finder.get_text_bounded()
            finder.close()
            page.close()
            noise = sum(ord(c) < 32 and c not in "\r\n\t" for c in raw)
            if len(raw) >= 20 and noise / len(raw) < 0.02:
                yield i + 1, " ".join(re.sub(r"[\x00-\x1f]", " ", raw).split())
    finally:
        pdf.close()


def index_document(doc):
    pages = []
    if doc.original_name.lower().endswith(".pdf") and doc.language != "ar":  # our Arabic PDFs use fonts whose text cannot be read back
        try:
            pages = list(page_texts(doc.file.path))
        except pypdfium2.PdfiumError:
            log.warning("Could not read document %s", doc.pk)
    with transaction.atomic():
        doc.pages.all().delete()
        DocumentPage.objects.bulk_create([DocumentPage(document=doc, number=n, text=text) for n, text in pages], batch_size=200)
        doc.pages.update(vector=SearchVector("text", config="english"))
        Document.objects.filter(pk=doc.pk).update(indexed_sha=doc.sha256)
    return len(pages)


def index_pending():
    """Reads new or replaced files. Called by the worker, so an upload becomes searchable within one cycle."""
    done = 0
    for doc in Document.objects.filter(Q(indexed_sha__isnull=True) | ~Q(indexed_sha=F("sha256"))):
        try:
            index_document(doc)
            done += 1
        except FileNotFoundError:  # one lost file must not hold back every manual after it; retried next cycle in case it returns
            log.warning("File missing for document %s; skipped", doc.pk)
        except OSError:
            log.exception("Document storage is unavailable; indexing resumes next cycle")
            break
    return done


# ---- Answering a question ----

SHORT = {"hi": "high", "lo": "low", "temp": "temperature", "temps": "temperature", "comp": "compressor", "press": "pressure", "evap": "evaporator", "cond": "condenser",
         "comm": "communication", "volt": "voltage", "volts": "voltage", "amp": "current", "amps": "current", "refrig": "refrigerant", "freon": "refrigerant"}


@lru_cache(maxsize=1)
def _vocabulary(alarm_count):
    """Every word the alarm names use. Rebuilt only when the number of alarms changes."""
    words = set()
    for name in AlarmCode.objects.values_list("name_en", flat=True):
        words.update(w for w in re.findall(r"[a-z]{4,}", name.lower()))
    return sorted(words)


def _spelling(word, vocabulary):
    """A mistyped word becomes the alarm word it is closest to; a word the alarms already use, a code or a short word is left alone."""
    if len(word) < 4 or not word.isalpha() or word in vocabulary:
        return word
    close = difflib.get_close_matches(word, vocabulary, n=1, cutoff=0.8)
    return close[0] if close else word


def parse(question):
    """English search terms (Arabic words translated where known) and Arabic stems for the Arabic alarm text."""
    terms, stems = [], []
    vocabulary = _vocabulary(AlarmCode.objects.count())
    for word in WORD.findall(question):
        if word[0] < "؀":
            low = word.lower().rstrip(".-/")
            low = _spelling(SHORT.get(low, low), vocabulary)
            if low and low not in STOP and low not in terms:
                terms.append(low)
            continue
        if word in AR_STOP or _ar_normal(word) in AR_STOP:
            continue
        stem = next((word[len(p):] for p in AR_PREFIXES if word.startswith(p) and len(word) - len(p) >= 2), word)
        if len(stem) >= 2 and stem not in stems:
            stems.append(stem)
        english = AR_TERMS.get(_ar_normal(stem)) or AR_TERMS.get(_ar_normal(word))
        if english and english not in terms:
            terms.append(english)
    return terms, stems


def _covered(text, terms):
    low = text.lower()
    return sum(t[:5] in low for t in terms)


def _best(qs, terms, text_of, boost, field="vector"):
    """Pages or alarms holding every term; otherwise those holding most of them. A question with few matching words gets no answer rather than a wrong one."""
    each = [SearchQuery(t, config="english") for t in terms]
    need = max(min(2, len(terms)), math.ceil(len(terms) * 0.6))
    for query in (reduce(and_, each), reduce(or_, each)):
        rows = list(qs.filter(**{field: query}).annotate(rank=SearchRank(F(field), query)).order_by("-rank")[:150])
        rows = [r for r in rows if _covered(text_of(r), terms) >= need]
        if rows:
            return sorted(rows, key=lambda r: (*boost(r), -r.rank))
    return []


def _alarm_text(a):
    return " ".join((a.code, a.name_en, a.causes_en, a.checks_en, a.action_en))


def search_alarms(question, terms, stems, controller):
    base = AlarmCode.objects.select_related("controller", "source_document")
    if controller:
        base = base.filter(controller=controller)
    found = list(find_alarms(controller, question)[:3]) if controller else []
    for t in terms:
        # "after 5 minutes" or "circuit 2" is not alarm 5 or 2: a bare short number is a code only when it is nearly all that was typed.
        if t.isdigit() and len(t) < 3 and len(terms) > 2:
            continue
        if re.search(r"\d", t) and len(t) <= 12:
            found += list(find_alarms(controller, t)[:3]) if controller else list(base.filter(key__in={alarm_key(t), re.sub(r"(?<=[A-Z])0+(?=\d)", "", alarm_key(t))})[:12])  # E01 is printed as E1
    if terms:  # first, because the English alarm text is complete and a word in the alarm's own name counts most
        vector = SearchVector("code", "name_en", weight="A", config="english") + SearchVector("causes_en", "checks_en", "action_en", weight="B", config="english")
        found += _best(base.annotate(vector=vector), terms, _alarm_text, lambda a: (-_covered(f"{a.code} {a.name_en}", terms), -_covered(_alarm_text(a), terms)))[:12]
    if stems:
        rows = list(base.filter(reduce(or_, [Q(name_ar__icontains=s) | Q(causes_ar__icontains=s) | Q(action_ar__icontains=s) for s in stems]))[:300])
        text = lambda a: f"{a.name_ar} {a.causes_ar} {a.action_ar}"
        found += sorted((a for a in rows if _covered(text(a), stems) >= min(2, len(stems))), key=lambda a: -_covered(text(a), stems))[:12]
    groups = {}  # the same alarm is printed for several controllers: show it once and name them all
    for a in found:
        entry = groups.setdefault((a.key, a.name_en.lower()), {"alarm": a, "controllers": [], "pks": set()})  # same code and name on two controllers is one alarm
        if a.pk not in entry["pks"]:
            entry["pks"].add(a.pk)
            if a.controller.name not in entry["controllers"]:
                entry["controllers"].append(a.controller.name)
    return list(groups.values())[:3]


def _parts(text):
    """Whole sentences when the text has them, otherwise the items of a comma list."""
    sentence = r"(?<=[.!?؟])\s+(?=[A-Z0-9؀-ۿ])|\n+"  # a capital must follow, so "temp. sensor" stays whole
    pieces = re.split(sentence, text) if re.search(sentence, text) else re.split(r"[,،;؛]\s*", text)
    cleaned = [re.sub(r"^and\s+", "", p.strip(" .،,;")) for p in pieces]
    return [p for p in cleaned if p and not p.isdigit()]  # manuals number their lists; the guide numbers the steps itself


def guide_steps(alarm):
    """The manual's own words for this alarm, cut into steps, then the reset. Returns (kind, text) pairs and the source note."""
    action, note = alarm.action.strip(), ""
    last = action.rsplit("\n", 1)
    if len(last) == 2 and re.fullmatch(r"\(.*\)", last[1].strip()):
        action, note = last[0].strip(), last[1].strip(" ()")
    generic = action.startswith("Check:")  # the manual only names a cause, so the cause is the step
    causes = re.split(r"probable cause:", alarm.causes, flags=re.I)[-1].strip() or (action[6:].strip() if generic else "")
    written = [] if generic else _parts(action)
    # With a written repair the causes would only repeat it, so they are shown above the steps instead.
    steps = [("check", p) for p in _parts(alarm.checks) if not re.match(r"error becomes|when value", p, re.I)] + ([("fix", p) for p in written] or [("cause", p) for p in _parts(causes)])
    if alarm.reset.strip():
        steps.append(("reset", alarm.reset.strip()))
    seen, unique = set(), []
    for kind, text in steps:
        if text.lower() not in seen:
            seen.add(text.lower())
            unique.append((kind, text))
    return unique, note


def is_exact(terms, groups, passages):
    """False when no result holds every word asked about, so the reply can say these are only the closest ones."""
    return not terms or any(_covered(_alarm_text(g["alarm"]), terms) == len(terms) for g in groups) or any(_covered(p["page"].text, terms) == len(terms) for p in passages)


def _manuals(user, model):
    """The manuals this user may read; with a model chosen, only those linked to it, its type, subtype or maker."""
    docs = documents_for(user).filter(is_archived=False)
    if model:
        q = Q(assignments__model=model) | Q(assignments__category=model.category) | Q(assignments__manufacturer=model.manufacturer) | Q(assignments__level="all")
        if model.subtype_id:
            q |= Q(assignments__subtype=model.subtype)
        docs = docs.filter(q)
    return docs


# A parameter asked for by its code (P103, C01, N04, Sp 01) or as "parameter 103" / "معامل 103".
PARAM_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,2})[ -]?(\d{1,4})(?![0-9])")
PARAM_WORD = re.compile(r"(?i)(?:param(?:eter)?s?|setting|معامل|باراميتر|برامتر)\D{0,3}(\d{1,4})")


def search_parameters(user, question, model):
    """The manual pages that define a parameter: its row in a parameter table, with the description, range and default as the snippet.
    A table row often prints only the number (103, not P103), so a page is trusted when the numbers around it are there too."""
    found = PARAM_WORD.search(question) or PARAM_CODE.search(question)
    if not found:
        return []
    letters, number = (found.group(1), found.group(2)) if found.re is PARAM_CODE else ("", found.group(1))
    n = int(number)
    own = rf"(?<![A-Za-z0-9.,]){re.escape(letters)}\s?0*{n}(?![0-9.,%])" if letters else None
    bare = rf"(?<![A-Za-z0-9.,]){n}(?![0-9.,%])"
    rows = []
    for page in DocumentPage.objects.filter(document__in=_manuals(user, model).values("pk"), text__iregex=rf"(^|[^0-9]){n}([^0-9]|$)").select_related("document")[:500]:
        text = page.text
        exact = len(re.findall(own, text, re.I)) if own else 0
        neighbours = sum(bool(re.search(rf"(?<![A-Za-z0-9.,])(?:{re.escape(letters)}\s?)?{k}(?![0-9.,%])", text, re.I)) for k in range(max(n - 5, 0), n + 6) if k != n)
        # With letters typed (C12), a page counts only if it is about such codes: the code itself, or several others like it (C11, C13).
        family = not letters or exact or len(set(re.findall(rf"(?<![A-Za-z0-9]){re.escape(letters)}\s?\d{{1,4}}(?![0-9])", text, re.I))) >= 3
        row = re.search(rf"(?:{own}|{bare})\s+[A-Za-z][a-z]", text, re.I) if own else re.search(rf"{bare}\s+[A-Za-z][a-z]", text)
        score = min(exact, 2) * 2 + neighbours * 4 + (4 if row else 0)  # a table with the numbers around it beats a page that only mentions the code
        if row and family and (exact or neighbours >= 2):
            end = re.search(rf"(?<![A-Za-z0-9.,])(?:{re.escape(letters)}\s?)?{n + 1}(?![0-9.,%])", text[row.start() + 1:], re.I)
            stop = row.start() + 1 + end.start() if end and end.start() < 260 else row.start() + 240
            snippet = text[row.start():stop].strip()
            code = re.match(r"\S+", snippet).group()
            rows.append((score, page.number, {"page": page, "snippet": "[[" + code + "]]" + snippet[len(code):]}))
    chosen, seen = [], set()
    for _, _, hit in sorted(rows, key=lambda r: (-r[0], r[1])):
        if hit["page"].document_id not in seen:  # one row per manual
            seen.add(hit["page"].document_id)
            chosen.append(hit)
    return chosen[:3]


def search_pages(user, terms, model):
    if not terms:
        return []
    docs = _manuals(user, model)
    fix_words = ("troubleshoot", "cause", "corrective", "remedy", "solution", "check")  # pages that explain a repair beat parameter tables
    rows = _best(DocumentPage.objects.filter(document__in=docs.values("pk")).select_related("document"), terms, lambda p: p.text,
                 lambda p: (-_covered(p.text, terms), -sum(w in p.text.lower() for w in fix_words)))
    chosen, seen_docs = [], set()
    for p in rows:  # one passage per manual
        if p.document_id not in seen_docs:
            seen_docs.add(p.document_id)
            chosen.append(p)
        if len(chosen) == 12:
            break
    query = reduce(or_, [SearchQuery(t, config="english") for t in terms])
    snippets = dict(DocumentPage.objects.filter(pk__in=[p.pk for p in chosen]).annotate(
        snippet=SearchHeadline("text", query, config="english", start_sel="[[", stop_sel="]]", min_words=20, max_words=45)).values_list("pk", "snippet"))
    unique = {}  # several manuals repeat the same paragraph: show it once
    for p in chosen:
        unique.setdefault(snippets[p.pk][:80], {"page": p, "snippet": snippets[p.pk]})
    return list(unique.values())[:4]

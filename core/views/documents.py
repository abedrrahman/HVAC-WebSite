"""Technical document library, three-level assignment, and permission-checked file delivery."""
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .. import forms as f
from ..doc_search import guide_steps, is_exact, parse, search_alarms, search_pages, search_parameters
from ..i18n import t
from ..models import (Attachment, Category, Document, DocumentAssignment, DocumentType, EquipmentModel, Manufacturer, Subtype, PREVIEWABLE)
from ..services import attachment_allowed, documents_for, documents_for_unit, documents_for_units, in_category, record, units_for, units_reached
from .common import action, fmt, form_page, link, list_page, post_only, requires, saved, try_delete


def source_label(a):
    """Where a document assignment comes from, as shown in lists."""
    if a.level == "all":
        return t("All Equipment Units")
    target = a.target.model_number if a.level == "model" else a.target.name
    return f"{t(a.get_level_display())}: {target}"


def _document_rows(links):
    return [{"doc": a.document, "inherited": a.level != "unit", "source": source_label(a)} for a in links]


def document_rows_for_unit(unit):
    return _document_rows(documents_for_unit(unit))


def document_rows_for_units(units):
    return _document_rows(documents_for_units(units))


class DocumentFilter(f.BaseForm):
    q = forms.CharField(label="Search", required=False)
    doc_type = forms.ModelChoiceField(DocumentType.objects.all(), label="Document Type", required=False)
    category = forms.ModelChoiceField(Category.objects.all(), label="Main Equipment Type", required=False)
    subtype = forms.ModelChoiceField(Subtype.objects.all(), label="Subtype", required=False)
    manufacturer = forms.ModelChoiceField(Manufacturer.objects.all(), label="Manufacturer", required=False)
    language = forms.ChoiceField(label="Language", required=False, choices=[("", "—"), *Document.Language.choices])
    show = forms.ChoiceField(label="Status", required=False, choices=[("", "Active"), ("unassigned", "Not assigned"), ("archived", "Archived")])


@requires("documents.view")
def document_type(request, pk):
    doc_type = get_object_or_404(DocumentType, pk=pk)
    docs = documents_for(request.user).filter(is_archived=False, doc_type=doc_type).select_related("manufacturer").order_by(Lower("name_en"))
    items = [{"url": d.get_absolute_url(), "name": d.name, "icon": "file", "names": f"{d.name_en} {d.name_ar} {d.original_name} {d.tags}",
              "meta": " · ".join(x for x in (d.manufacturer.name if d.manufacturer else "", t(d.get_language_display()), d.revision) if x)} for d in docs]
    add = {"url": reverse("document_upload"), "label": t("Upload Document")} if request.user.has("documents.upload") else None
    return render(request, "group.html", {"title": doc_type.name, "tone": pk % 6, "vt": f"doc-type-{pk}", "icon_name": "file", "back_url": reverse("document_list"),
                                          "back_label": t("All document types"), "stats": [(len(items), t("Documents"))], "items": items, "add": add})


@requires("documents.view")
def document_list(request):
    if not request.GET:  # any search, filter or ?view=list goes to the full table below
        counts = dict(documents_for(request.user).filter(is_archived=False).order_by().values_list("doc_type").annotate(n=Count("pk", distinct=True)))
        label = t("Documents")
        cards = sorted(({"name": x.name, "stats": [(counts[x.pk], label)], "tone": x.pk % 6, "url": reverse("document_type", args=[x.pk]), "vt": f"doc-type-{x.pk}"} for x in DocumentType.objects.filter(pk__in=counts)), key=lambda c: -c["stats"][0][0])
        actions = [action("Upload Document", reverse("document_upload"))] if request.user.has("documents.upload") else []
        actions.append(action("All documents", reverse("document_list") + "?view=list", "", False))
        return render(request, "hub.html", {"title": t("Technical Documents"), "sub": t("Choose a document type"), "cards": cards, "actions": actions, "icon_name": "file"})
    qs = documents_for(request.user).select_related("doc_type", "manufacturer", "model").prefetch_related(
        "assignments__unit", "assignments__model", "assignments__subtype", "assignments__category", "assignments__manufacturer")
    form = DocumentFilter(request.GET or None)
    show = ""
    if form.is_valid():
        d = form.cleaned_data
        show = d["show"]
        if d["q"]:
            q = d["q"]
            qs = qs.filter(Q(name_en__icontains=q) | Q(name_ar__icontains=q) | Q(tags__icontains=q) | Q(description_en__icontains=q)
                           | Q(description_ar__icontains=q) | Q(original_name__icontains=q) | Q(model__model_number__icontains=q))
        if d["doc_type"]:
            qs = qs.filter(doc_type=d["doc_type"])
        if d["manufacturer"]:
            qs = qs.filter(manufacturer=d["manufacturer"])
        if d["language"]:
            qs = qs.filter(language=d["language"])
        if d["category"]:
            qs = in_category(qs, d["category"])
        if d["subtype"]:
            qs = qs.filter(Q(assignments__subtype=d["subtype"]) | Q(assignments__unit__subtype=d["subtype"]) | Q(assignments__category=d["subtype"].category)
                           | Q(assignments__model__subtype=d["subtype"]) | Q(assignments__manufacturer__models__subtype=d["subtype"]) | Q(assignments__level="all")).distinct()
    qs = qs.filter(is_archived=show == "archived")
    if show == "unassigned":
        qs = qs.annotate(n=Count("assignments")).filter(n=0)
    acts = [action("Upload Document", reverse("document_upload"))] if request.user.has("documents.upload") else []

    def row(x):
        assigned = "; ".join(source_label(a) for a in list(x.assignments.all())[:2]) or t("Not assigned")
        return [link(x), x.doc_type.name, x.manufacturer.name if x.manufacturer else "—", x.get_language_display() and t(x.get_language_display()),
                x.revision or "—", assigned, fmt(x.uploaded_at),
                link(reverse("document_file", args=[x.pk]) + "?download=1", t("Download"))]
    return list_page(request, "Technical Documents", qs, ["Document Name", "Document Type", "Manufacturer", "Language", "Revision Number", "Assigned To", "Upload Date", ""], row, filters=form, actions=acts,
                     keep_view="list", back=(reverse("document_list"), t("All document types")))


def _assign(document, target, user):
    (level, value), = target.items()
    link_to = {} if level == "all" else {level: value}
    a, created = DocumentAssignment.objects.get_or_create(document=document, level=level, **link_to, defaults={"created_by": user})
    record(user, "document_assigned", document, f"{level}: {value}" if link_to else "all units")
    return created


@requires("documents.upload")
def unit_document_add(request, pk):
    unit = get_object_or_404(units_for(request.user).select_related("model__manufacturer", "subtype", "category"), pk=pk)
    number = unit.model.model_number
    seed = Document(manufacturer=unit.model.manufacturer, model=unit.model, name_en=f"{number} document", name_ar=f"مستند {number}")
    form = f.UnitDocumentForm(request.POST or None, request.FILES or None, instance=seed, unit=unit, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            doc = form.save(commit=False)
            doc.uploaded_by = request.user
            doc.save()
            _assign(doc, form.target, request.user)
        saved(request, doc, "uploaded")
        return redirect(unit.get_absolute_url() + "#documents")
    return form_page(request, form, "Add document", unit.get_absolute_url() + "#documents", multipart=True, submit="Confirm and Save", subject=unit.name,
                     intro=t("Details are filled in from this unit, and you can change them. Every choice except This unit only stays linked, so units added later get the document automatically."))


@requires("documents.upload")
def document_upload(request):
    form = f.DocumentUploadForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            doc = form.save(commit=False)
            doc.uploaded_by = request.user
            doc.save()
            _assign(doc, form.target, request.user)
        saved(request, doc, "uploaded")
        return redirect(doc)
    return form_page(request, form, "Upload Document", reverse("document_list"), multipart=True, submit="Confirm and Save",
                     intro=t("Choose where the document applies. Assigning to a subtype or main type also makes it available to every future unit under it."))


def _pick_model(text, pk=""):
    """The model the user means, or the candidates when what they typed fits several."""
    if pk.isdigit():
        return EquipmentModel.objects.filter(pk=pk).select_related("controller").first(), []
    if not text:
        return None, []
    found = EquipmentModel.objects.filter(model_number__iexact=text) or EquipmentModel.objects.filter(model_number__icontains=text)
    picks = list(found.select_related("manufacturer", "controller")[:30])
    return (picks[0], []) if len(picks) == 1 else (None, picks)


def _source(request_visible, doc, page):
    return {"name": doc.name, "page": page, "url": f'{reverse("document_file", args=[doc.pk])}#page={page}' if doc.pk in request_visible else ""}


@requires("documents.view")
def alarm_ask(request):
    """One chat reply. Everything in it comes from the alarm list or the stored manuals."""
    question = request.GET.get("q", "").strip()[:300]
    typed = request.GET.get("model", "").strip()
    model, picks = _pick_model(typed)
    if picks:
        return JsonResponse({"text": t("More than one model matches. Choose one:"), "picks": [m.model_number for m in picks]})
    if typed and not model:
        return JsonResponse({"text": t("No model matches what you typed.")})
    terms, stems = parse(question)
    if not terms and not stems:
        return JsonResponse({"text": t("Please describe the problem in a few more words, or type the alarm code.")})
    groups = search_alarms(question, terms, stems, model.controller if model and model.controller_id else None) if not model or model.controller_id else []
    params = search_parameters(request.user, question, model)  # a parameter asked by its code goes straight to its row in the manual
    shown = {p["page"].pk for p in params}
    passages = (params + [p for p in search_pages(request.user, terms, model) if p["page"].pk not in shown])[:5]
    visible = set(documents_for(request.user).filter(pk__in={g["alarm"].source_document_id for g in groups}).values_list("pk", flat=True)) | {p["page"].document_id for p in passages}
    kinds = {"cause": t("Check this possible cause"), "check": t("Check"), "fix": t("Do this"), "reset": t("Reset the alarm")}
    alarms = []
    for g in groups:
        a = g["alarm"]
        steps, note = guide_steps(a)
        alarms.append({"code": a.code, "name": a.name, "controllers": g["controllers"], "effect": "" if a.effect.strip().lower() == "none" else a.effect, "note": note,
                       "causes": a.causes if any(k == "fix" for k, _ in steps) else "",
                       "steps": [{"label": kinds[k], "text": x} for k, x in steps], "source": _source(visible, a.source_document, a.source_page)})
    found = [{"snippet": p["snippet"], "source": _source(visible, p["page"].document, p["page"].number)} for p in passages]
    if not alarms and not found:
        return JsonResponse({"text": t("I could not find this in the documents. Try other words, or choose the equipment model.")})
    intro = "Here is what I found in the documents:" if is_exact(terms, groups, passages) else "I did not find an exact match. These are the closest results in the documents:"
    return JsonResponse({"text": t(intro), "alarms": alarms, "passages": found,
                         "tip": "" if model else t("Tip: choose the model above for a more exact answer."),
                         "labels": {"alarms": t("Other possible alarms"), "passages": t("More help from the manuals"), "page": t("Page"), "controller": t("Controller"), "step": t("Step"), "of": t("of"),
                                    "next": t("Done, next step"), "fixed": t("This fixed it"), "effect": t("What the unit does"), "causes": t("Possible causes"), "ask": t("Is the problem solved?"), "yes": t("Yes, solved"), "no": t("No, still there"),
                                    "solved": t("Good. The problem is solved."), "stuck": t("These are all the steps the manuals give for this alarm. Open the source page below, or try another possible alarm."),
                                    "none": t("The manuals give no repair steps for this alarm. Open the source page below."), "guide": t("Guide me through this one"), "back": t("Previous step"), "source": t("Source")}})


@requires("documents.view")
def alarm_lookup(request):
    """The chat page. A unit page links here with its model filled in."""
    model, _ = _pick_model("", request.GET.get("model_pk", ""))
    return render(request, "alarm_lookup.html", {"models": EquipmentModel.objects.filter(is_active=True).order_by("model_number"), "model_text": model.model_number if model else ""})


def _document_or_404(request, pk):
    return get_object_or_404(documents_for(request.user).select_related("doc_type", "manufacturer", "model", "uploaded_by"), pk=pk)


def _can_edit(user, doc):
    return user.has("documents.manage") or (user.has("documents.upload") and doc.uploaded_by_id == user.pk)


@requires("documents.view")
def document_detail(request, pk):
    doc = _document_or_404(request, pk)
    ext = doc.original_name.rsplit(".", 1)[-1].lower()
    return render(request, "document_detail.html", {
        "doc": doc, "assignments": [{"a": a, "label": source_label(a)} for a in doc.assignments.select_related("unit__client", "model", "subtype__category", "category", "manufacturer")],
        "reach": units_reached(doc).count(), "can_edit": _can_edit(request.user, doc), "assign_form": f.AssignmentForm(user=request.user),
        "is_image": ext in {"png", "jpg", "jpeg", "gif", "webp"}, "can_frame": ext in {"pdf", "txt"},
        "tags": [x.strip() for x in doc.tags.split(",") if x.strip()],
    })


@requires("documents.upload")
def document_edit(request, pk):
    doc = _document_or_404(request, pk)
    if not _can_edit(request.user, doc):
        raise PermissionDenied
    old_file = doc.file.name
    form = f.DocumentForm(request.POST or None, request.FILES or None, instance=doc)
    if request.method == "POST" and form.is_valid():
        new = form.save()
        if new.file.name != old_file:
            new.file.storage.delete(old_file)  # one shared file, so every assignment now serves the new version
        saved(request, new, "updated")
        return redirect(new)
    return form_page(request, form, "Edit Document", doc.get_absolute_url(), multipart=True, subject=doc.name)


@post_only
@requires("documents.upload")
def assignment_add(request, pk):
    doc = _document_or_404(request, pk)
    if not _can_edit(request.user, doc):
        raise PermissionDenied
    form = f.AssignmentForm(request.POST, user=request.user)
    if form.is_valid():
        if _assign(doc, form.target, request.user):
            messages.success(request, t("Saved successfully."))
        else:
            messages.info(request, t("The document is already assigned there."))
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(doc)


@post_only
@requires("documents.upload")
def assignment_remove(request, pk):
    a = get_object_or_404(DocumentAssignment.objects.select_related("document"), pk=pk)
    doc = _document_or_404(request, a.document_id)
    if not _can_edit(request.user, doc):
        raise PermissionDenied
    record(request.user, "document_unassigned", doc, f"{a.level}: {a.target or 'all units'}")
    a.delete()  # removes only this link; the document and its other assignments are untouched
    messages.success(request, t("Assignment removed. The document itself was kept."))
    return redirect(doc)


@post_only
@requires("documents.manage")
def document_archive(request, pk):
    doc = _document_or_404(request, pk)
    doc.is_archived = not doc.is_archived
    doc.save(update_fields=["is_archived"])
    record(request.user, "archived" if doc.is_archived else "restored", doc)
    messages.success(request, t("Saved successfully."))
    return redirect(doc)


@post_only
@requires("documents.manage")
def document_delete(request, pk):
    doc = _document_or_404(request, pk)
    if doc.assignments.exists():
        messages.error(request, t("This document is still assigned to equipment. Remove its assignments first."))
        return redirect(doc)
    stored, storage = doc.file.name, doc.file.storage
    response = try_delete(request, doc, "document_list", doc)  # alarms keep their source manual, so the database may refuse
    if doc.pk is None:  # Django clears the pk only when the row is really gone
        storage.delete(stored)
    return response


def _serve(request, field, name, can_inline):
    try:
        handle = field.open("rb")
    except FileNotFoundError:
        raise Http404
    inline = can_inline and "download" not in request.GET
    # as_attachment for everything else means uploaded HTML or SVG can never run in our origin.
    response = FileResponse(handle, as_attachment=not inline, filename=name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@requires("documents.view")
def document_file(request, pk):
    doc = _document_or_404(request, pk)
    return _serve(request, doc.file, doc.original_name, doc.can_preview)


def attachment_file(request, pk):
    att = get_object_or_404(Attachment, pk=pk)
    if not attachment_allowed(request.user, att):
        raise PermissionDenied
    return _serve(request, att.file, att.original_name, att.original_name.rsplit(".", 1)[-1].lower() in PREVIEWABLE)

"""Shared page helpers: permission decorator, generic list and form pages, table cells."""
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.html import format_html

from ..i18n import t
from ..services import record


def requires(*codes):
    """The user must hold every listed permission code."""
    def deco(view):
        @wraps(view)
        def wrapped(request, *a, **k):
            if not all(request.user.has(c) for c in codes):
                raise PermissionDenied
            return view(request, *a, **k)
        return wrapped
    return deco


def link(obj_or_url, text=None):
    if text is None:
        return format_html('<a href="{}">{}</a>', obj_or_url.get_absolute_url(), str(obj_or_url))
    return format_html('<a href="{}">{}</a>', obj_or_url, text)


def badge(status, label):
    return format_html('<span class="badge s-{}">{}</span>', status, t(label))


def active_badge(is_active):
    return badge("completed", "Active") if is_active else badge("cancelled", "Archived")


def fmt(d):
    return date_format(d, "j M Y") if d else "—"


def table(columns, rows, empty="No records found."):
    return {"columns": [t(c) for c in columns], "rows": rows, "empty": t(empty)}


def list_page(request, title, qs, columns, row, filters=None, actions=(), tabs=None, intro=None, per_page=25, keep_view=None, back=None, card=None, phone_card=None):
    """card replaces the table with cards; phone_card keeps the table and swaps to cards only on phones."""
    page = Paginator(qs, per_page).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "list.html", {
        "phone_cards": [phone_card(x) for x in page] if phone_card else None,
        "title": t(title), "table": table(columns, [row(x) for x in page] if not card else []), "cards": [card(x) for x in page] if card else None, "page": page, "filters": filters,
        "actions": actions, "query": query.urlencode(), "tabs": tabs, "intro": intro, "keep_view": keep_view, "back": back,
    })


def action(label, url, icon="plus", primary=True):
    return {"label": t(label), "url": url, "icon": icon, "primary": primary}


def form_page(request, form, title, back, multipart=False, submit="Save", intro=None, template="form.html", **extra):
    return render(request, template, {"form": form, "title": t(title), "back": back, "multipart": multipart,
                                      "submit": t(submit), "intro": intro, **extra})


def saved(request, obj, action_name="updated", details=""):
    record(request.user, action_name, obj, details)
    messages.success(request, t("Saved successfully."))


def try_delete(request, obj, back_ok, back_blocked):
    """Database PROTECT is the safety net: anything still linked cannot disappear."""
    label = str(obj)
    try:
        with transaction.atomic():  # a blocked delete must also take back its log entry
            record(request.user, "deleted", obj)
            obj.delete()
    except ProtectedError:
        messages.error(request, t("This record is linked to other records and cannot be deleted. Archive it instead."))
        return redirect(back_blocked)
    messages.success(request, t("Deleted:") + " " + label)
    return redirect(back_ok)


def toggle_active(request, obj, back):
    obj.is_active = not obj.is_active
    obj.save(update_fields=["is_active"])
    record(request.user, "activated" if obj.is_active else "archived", obj)
    messages.success(request, t("Saved successfully."))
    return redirect(back)


def post_only(view):
    @wraps(view)
    def wrapped(request, *a, **k):
        if request.method != "POST":
            return redirect(reverse("dashboard"))
        return view(request, *a, **k)
    return wrapped

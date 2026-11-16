from django import template
from django.utils.formats import date_format
from django.utils.html import format_html

from core.i18n import t as translate

register = template.Library()


@register.simple_tag(name="t")
def t_tag(text):
    return translate(text)


@register.filter
def tr(value):
    return translate(str(value))


@register.filter
def can(user, code):
    return user.has(code)


@register.filter
def longdate(value):
    return date_format(value, "j F Y") if value else "—"


@register.simple_tag
def badge(status, label):
    return format_html('<span class="badge s-{}">{}</span>', status, translate(label))


@register.filter
def filesize(n):
    n = n or 0
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(n // 1024, 1)} KB"


@register.filter
def dict_get(d, key):
    return d.get(key)


@register.simple_tag
def icon(name, size=18):
    return format_html('<svg class="icon" width="{0}" height="{0}" aria-hidden="true"><use href="#i-{1}"></use></svg>', size, name)

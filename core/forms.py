from datetime import date
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from . import models as m
from .i18n import t
from .services import clients_for, contracts_for, hash_file, is_general_schedule_duplicate, unit_conflicts, unit_document_scopes, units_for

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "xlsm", "csv", "ppt", "pptx", "txt", "rtf", "png", "jpg", "jpeg", "gif", "webp",
    "bmp", "tif", "tiff", "dwg", "dxf", "zip", "rar", "7z", "msg",
}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def validate_upload(f, allowed=ALLOWED_EXTENSIONS):
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    if ext not in allowed:
        raise ValidationError(t("This file type is not allowed."))
    if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(t("The file is too large.") + f" (max {settings.MAX_UPLOAD_MB} MB)")


class Bilingual:
    """Translates labels and fixed choices for the active language, and gives dates a native picker."""
    sections = ()  # optional (title, [field names]) pairs that group a long form

    def section_rows(self):
        rows = [(title, [self[n] for n in names if n in self.fields and not self[n].is_hidden]) for title, names in self.sections]
        used = {n for _, names in self.sections for n in names}
        rows[-1][1].extend(f for f in self.visible_fields() if f.name not in used)  # a field added later must never vanish from the form
        return [r for r in rows if r[1]]

    def localize_fields(self):
        for f in self.fields.values():
            if f.label:
                f.label = t(str(f.label))
            if f.help_text:
                f.help_text = t(str(f.help_text))
            if isinstance(f, forms.ModelChoiceField):
                f.empty_label = "—"
            elif isinstance(f, forms.ChoiceField):
                f.choices = [(k, t(str(v))) for k, v in f.choices]
            if isinstance(f, forms.DateField):
                f.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
            if isinstance(f.widget, forms.Textarea):
                f.widget.attrs.setdefault("rows", 3)


class BaseForm(Bilingual, forms.Form):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.localize_fields()


class BaseModelForm(Bilingual, forms.ModelForm):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.localize_fields()


def depends(field, parent_id, source):
    """Marks a select to be refilled by app.js whenever its parent select changes."""
    field.widget.attrs.update({"data-parent": parent_id, "data-source": source})


class LoginForm(Bilingual, AuthenticationForm):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["username"].label = t("Username")
        self.fields["password"].label = t("Password")


class ChangePasswordForm(Bilingual, PasswordChangeForm):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["old_password"].label = t("Current password")
        self.fields["new_password1"].label = t("New password")
        self.fields["new_password1"].help_text = ""
        self.fields["new_password2"].label = t("Confirm new password")


class CountryForm(BaseModelForm):
    class Meta:
        model = m.Country
        fields = ["name_en", "name_ar", "code", "sort_order", "is_active"]


class DocumentTypeForm(BaseModelForm):
    class Meta:
        model = m.DocumentType
        fields = ["name_en", "name_ar", "is_active"]


class CategoryForm(BaseModelForm):
    class Meta:
        model = m.Category
        fields = ["name_en", "name_ar", "description_en", "description_ar", "sort_order", "is_active"]


class SubtypeForm(BaseModelForm):
    class Meta:
        model = m.Subtype
        fields = ["category", "name_en", "name_ar", "description_en", "description_ar", "is_active"]


class FieldDefinitionForm(BaseModelForm):
    class Meta:
        model = m.FieldDefinition
        fields = ["subtype", "name_en", "name_ar", "field_type", "units", "options", "required", "is_active"]

    def __init__(self, *a, category=None, **k):
        super().__init__(*a, **k)
        self.category = category
        self.fields["subtype"].queryset = category.subtypes.all()

    def clean(self):
        data = super().clean()
        if data.get("field_type") in ("select", "multiselect") and not data.get("options", "").strip():
            self.add_error("options", t("Enter at least one option."))
        if data.get("field_type") == "measurement" and not data.get("units", "").strip():
            self.add_error("units", t("Enter at least one measurement unit."))
        if self.instance.pk and "field_type" in self.changed_data and self.instance.values.exists():
            # Saved values are not converted, and a text value in a date field could not be displayed.
            self.add_error("field_type", t("This field already holds saved values, so its type cannot be changed. Add a new field instead."))
        return data


class ClientForm(BaseModelForm):
    sections = (("General Information", ["name_en", "name_ar", "country", "city", "address"]), ("Contact Information", ["contact_person", "phone", "email"]), ("Notes", ["notes"]))

    class Meta:
        model = m.Client
        fields = ["name_en", "name_ar", "country", "city", "address", "contact_person", "phone", "email", "notes"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["name_en"].label = t("Client Name")
        self.fields["name_ar"].label = t("Arabic Client Name")
        self.fields["country"].queryset = m.Country.objects.filter(Q(is_active=True) | Q(pk=self.instance.country_id))


class ManufacturerForm(BaseModelForm):
    logo = forms.FileField(label="Logo", required=False)

    class Meta:
        model = m.Manufacturer
        fields = ["name_en", "name_ar", "country", "website", "contact_info", "description_en", "description_ar", "categories"]
        widgets = {"categories": forms.CheckboxSelectMultiple}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["name_en"].label = t("Manufacturer Name")
        self.fields["name_ar"].label = t("Arabic Name")
        self.fields["categories"].queryset = m.Category.objects.filter(is_active=True)

    def clean_logo(self):
        f = self.cleaned_data.get("logo")
        if f:
            validate_upload(f, IMAGE_EXTENSIONS)
        return f


class EquipmentModelForm(BaseModelForm):
    class Meta:
        model = m.EquipmentModel
        fields = ["manufacturer", "model_number", "category", "subtype", "controller", "description_en", "description_ar"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        data = self.data or None
        self.fields["manufacturer"].queryset = m.Manufacturer.objects.filter(Q(is_active=True) | Q(pk=self.instance.manufacturer_id))
        self.fields["category"].queryset = m.Category.objects.filter(Q(is_active=True) | Q(pk=self.instance.category_id))
        cat = (data and data.get("category")) or self.initial.get("category") or self.instance.category_id
        self.fields["subtype"].queryset = m.Subtype.objects.filter(category_id=cat or None)
        depends(self.fields["subtype"], "id_category", "subtypes")


class UnitForm(BaseModelForm):
    sections = (("General Information", ["name_en", "name_ar", "client", "project", "location", "status"]), ("Equipment Details", ["category", "subtype", "manufacturer_pick", "model", "serial_number"]),
                ("Installation & Warranty", ["installed_on", "warranty_start", "warranty_end"]), ("Notes", ["notes"]))

    class Meta:
        model = m.Unit
        fields = ["name_en", "name_ar", "client", "category", "subtype", "model", "serial_number", "project", "location",
                  "installed_on", "warranty_start", "warranty_end", "status", "notes"]

    manufacturer_pick = forms.ModelChoiceField(m.Manufacturer.objects.none(), label="Manufacturer")
    field_order = ["name_en", "name_ar", "client", "category", "subtype", "manufacturer_pick", "model", "serial_number"]

    def __init__(self, *a, user=None, **k):
        super().__init__(*a, **k)
        d = self.data or None
        inst = self.instance
        self.fields["name_en"].label = t("Equipment Name") + " (" + t("English") + ")"
        self.fields["name_ar"].label = t("Equipment Name") + " (" + t("Arabic") + ")"
        clients = clients_for(user) if user else m.Client.objects.all()  # a limited user only picks clients they can already see
        self.fields["client"].queryset = clients.filter(Q(is_active=True) | Q(pk=inst.client_id))
        if inst.pk and inst.contract_links.filter(removed_at__isnull=True, contract__status__in=["draft", "active", "suspended"],
                                                  contract__end_date__gte=timezone.localdate()).exists():
            self.fields["client"].disabled = True  # the contract and its visits belong to the current client
            self.fields["client"].help_text = t("The client cannot be changed while the unit is on a live contract. Remove it from the contract first.")
        self.fields["category"].queryset = m.Category.objects.filter(Q(is_active=True) | Q(pk=inst.category_id))
        self.fields["manufacturer_pick"].queryset =m.Manufacturer.objects.filter(is_active=True) | m.Manufacturer.objects.filter(models__units=inst.pk or None)
        self.fields["manufacturer_pick"].queryset = self.fields["manufacturer_pick"].queryset.distinct()
        if inst.pk:
            self.fields["manufacturer_pick"].initial = inst.model.manufacturer_id
        cat = (d and d.get("category")) or self.initial.get("category") or inst.category_id
        man = (d and d.get("manufacturer_pick")) or self.initial.get("manufacturer_pick") or (inst.model.manufacturer_id if inst.pk else None)
        sub = (d and d.get("subtype")) or self.initial.get("subtype") or inst.subtype_id
        self.fields["subtype"].queryset = m.Subtype.objects.filter(Q(category_id=cat or None, is_active=True) | Q(pk=inst.subtype_id))
        models_qs = m.EquipmentModel.objects.filter(manufacturer_id=man or None, category_id=cat or None)
        self.fields["model"].queryset = models_qs.select_related("manufacturer")
        depends(self.fields["subtype"], "id_category", "subtypes")
        self.fields["model"].widget.attrs.update({"data-parent": "id_manufacturer_pick,id_category", "data-source": "models"})

    def clean(self):
        d = super().clean()
        cat, sub, model = d.get("category"), d.get("subtype"), d.get("model")
        if sub and cat and sub.category_id != cat.pk:
            self.add_error("subtype", t("This subtype does not belong to the selected type."))
        if model and cat and model.category_id != cat.pk:
            self.add_error("model", t("This model does not belong to the selected type."))
        if model and sub and model.subtype_id and model.subtype_id != sub.pk:
            self.add_error("model", t("This model does not belong to the selected subtype."))
        serial = d.get("serial_number")
        twin = model and serial and m.Unit.objects.filter(model=model, serial_number=serial).exclude(pk=self.instance.pk).first()
        if twin:
            self.add_error("serial_number", f"{t('This serial number is already used by another unit of this model:')} {twin.name}")
        ws, we = d.get("warranty_start"), d.get("warranty_end")
        if ws and we and we < ws:
            self.add_error("warranty_end", t("The end date must be after the start date."))
        return d


def plain_number(d):
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class SpecForm(BaseForm):
    """Built at runtime from the field definitions of a category and subtype. Saves to a unit or a model."""
    def __init__(self, *a, owner=None, **k):
        super().__init__(*a, **k)
        self.owner = owner
        self.defs = list(m.FieldDefinition.applying_to(owner.category_id, owner.subtype_id))
        self.values = {v.field_id: v.value for v in owner.tech_values.all()}
        is_unit = isinstance(owner, m.Unit)
        inherited = {}
        if is_unit:
            inherited = {v.field_id: v.value for v in owner.model.tech_values.all()}
        for d in self.defs:
            self._add(d, self.values.get(d.pk), inherited.get(d.pk), is_unit)
        self.localize_fields()

    def _add(self, d, val, inherited, is_unit):
        # Units inherit from the model, so a required field only demands input if the model has no value.
        req = d.required and inherited in (None, "")
        name, attrs, label = f"f_{d.pk}", {}, d.name
        if inherited not in (None, "", []):
            attrs["placeholder"] = t("From model:") + " " + (f"{inherited.get('value')} {inherited.get('unit')}" if isinstance(inherited, dict) else str(inherited))
        unit_hint = f" ({', '.join(d.unit_list)})" if d.unit_list and d.field_type != "measurement" else ""
        kw = {"label": label + unit_hint, "required": req}
        ft = d.field_type
        if ft == "integer":
            field = forms.IntegerField(widget=forms.NumberInput(attrs=attrs), **kw)
        elif ft in ("decimal", "measurement"):
            field = forms.DecimalField(max_digits=18, decimal_places=4, widget=forms.NumberInput(attrs={**attrs, "step": "any"}), **kw)
        elif ft == "date":
            field = forms.DateField(**kw)
        elif ft == "select":
            field = forms.ChoiceField(choices=[("", "—")] + [(o, o) for o in d.option_list], **kw)
        elif ft == "multiselect":
            field = forms.MultipleChoiceField(choices=[(o, o) for o in d.option_list], widget=forms.CheckboxSelectMultiple, **kw)
        elif ft == "boolean":
            field = forms.ChoiceField(choices=[("", "—"), ("1", t("Yes")), ("0", t("No"))], **kw)
        else:
            field = forms.CharField(max_length=500, widget=forms.TextInput(attrs=attrs), **kw)
        if isinstance(val, dict):
            field.initial = val.get("value")
        elif ft == "boolean" and val is not None:
            field.initial = "1" if val else "0"
        else:
            field.initial = val
        self.fields[name] = field
        if ft == "measurement":
            cur = val.get("unit") if isinstance(val, dict) else (d.unit_list[0] if d.unit_list else "")
            self.fields[f"{name}_unit"] = forms.ChoiceField(label=f"{d.name} — {t('Unit of measure')}", choices=[(u, u) for u in d.unit_list], initial=cur, required=False)

    def save(self):
        fk = {"unit": self.owner} if isinstance(self.owner, m.Unit) else {"model": self.owner}
        for d in self.defs:
            raw = self.cleaned_data.get(f"f_{d.pk}")
            if d.field_type == "measurement" and raw not in (None, ""):
                value = {"value": plain_number(raw), "unit": self.cleaned_data.get(f"f_{d.pk}_unit", "")}
            elif isinstance(raw, Decimal):
                value = plain_number(raw)
            elif isinstance(raw, date):
                value = raw.isoformat()
            elif d.field_type == "boolean":
                value = {"1": True, "0": False}.get(raw)
            else:
                value = raw
            if value in (None, "", []):
                m.TechnicalValue.objects.filter(field=d, **fk).delete()
            else:
                m.TechnicalValue.objects.update_or_create(field=d, **fk, defaults={"value": value})


class AttachmentForm(BaseForm):
    kind = forms.ChoiceField(label="Type", choices=[])
    file = forms.FileField(label="File")

    def __init__(self, *a, kinds=(), images_only=False, **k):
        super().__init__(*a, **k)
        self.images_only = images_only
        self.fields["kind"].choices = [(x.value, t(x.label)) for x in kinds]
        if images_only:
            self.fields["file"].widget.attrs["accept"] = "image/*"

    def clean_file(self):
        f = self.cleaned_data["file"]
        validate_upload(f, IMAGE_EXTENSIONS if self.images_only else ALLOWED_EXTENSIONS)
        return f


def assignment_fields():
    """The link levels. app.js shows only the selectors relevant to the chosen level."""
    return {
        "level": forms.ChoiceField(label=t("Assignment Level"), initial="unit", widget=forms.RadioSelect,
                                   choices=[(k, t(str(v))) for k, v in m.DocumentAssignment.Level.choices]),
        "scope_manufacturer": forms.ModelChoiceField(m.Manufacturer.objects.filter(is_active=True), label=t("Manufacturer"), required=False, empty_label="—",
                                                     widget=forms.Select(attrs={"data-levels": "model,manufacturer"})),
        "scope_model": forms.ModelChoiceField(m.EquipmentModel.objects.none(), label=t("Model"), required=False, empty_label="—",
                                              widget=forms.Select(attrs={"data-levels": "model"})),
        "category": forms.ModelChoiceField(m.Category.objects.filter(is_active=True), label=t("Main Equipment Type"), required=False,
                                           empty_label="—", widget=forms.Select(attrs={"data-levels": "subtype,category"})),
        "subtype": forms.ModelChoiceField(m.Subtype.objects.none(), label=t("Subtype"), required=False, empty_label="—",
                                          widget=forms.Select(attrs={"data-levels": "subtype"})),
        "client": forms.ModelChoiceField(m.Client.objects.filter(is_active=True), label=t("Client"), required=False, empty_label="—",
                                         widget=forms.Select(attrs={"data-levels": "unit"})),
        "unit": forms.ModelChoiceField(m.Unit.objects.none(), label=t("Equipment Unit"), required=False, empty_label="—",
                                       widget=forms.Select(attrs={"data-levels": "unit"})),
    }


WIDE_LEVELS = {"model", "manufacturer", "all"}


def allowed_levels(user):
    """Reaching whole models, manufacturers or the entire app needs the permission to manage any document."""
    return [x for x in m.DocumentAssignment.Level.values if x not in WIDE_LEVELS or user.has("documents.manage")]


def add_assignment_fields(form, user):
    form.fields.update(assignment_fields())
    d = form.data or {}
    allowed = allowed_levels(user)
    form.fields["level"].choices = [c for c in form.fields["level"].choices if c[0] in allowed]
    form.fields["subtype"].queryset = m.Subtype.objects.filter(category_id=d.get("category") or None)
    form.fields["unit"].queryset = m.Unit.objects.filter(client_id=d.get("client") or None, is_active=True)
    depends(form.fields["subtype"], "id_category", "subtypes")
    depends(form.fields["unit"], "id_client", "units")
    if "model" in allowed:
        form.fields["scope_model"].queryset = m.EquipmentModel.objects.filter(manufacturer_id=d.get("scope_manufacturer") or None).select_related("manufacturer")
        depends(form.fields["scope_model"], "id_scope_manufacturer", "models")
    else:
        del form.fields["scope_model"], form.fields["scope_manufacturer"]


def assignment_target(form, data):
    level = data.get("level")
    if not level:
        return {}
    if level == "all":
        return {"all": None}
    field = {"model": "scope_model", "manufacturer": "scope_manufacturer"}.get(level, level)
    if not data.get(field):
        form.add_error(field, t("This field is required."))
    return {level: data.get(field)}


class AssignmentForm(BaseForm):
    def __init__(self, *a, user, **k):
        super().__init__(*a, **k)
        add_assignment_fields(self, user)

    def clean(self):
        d = super().clean()
        self.target = assignment_target(self, d)
        return d


class DocumentForm(BaseModelForm):
    class Meta:
        model = m.Document
        fields = ["file", "name_en", "name_ar", "doc_type", "language", "manufacturer", "model", "revision", "tags", "description_en", "description_ar"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._setup_document()

    def _setup_document(self):
        d = self.data or {}
        inst = self.instance
        self.fields["name_en"].label = t("Document Name") + " (" + t("English") + ")"
        self.fields["name_ar"].label = t("Document Name") + " (" + t("Arabic") + ")"
        self.fields["doc_type"].queryset = m.DocumentType.objects.filter(Q(is_active=True) | Q(pk=inst.doc_type_id))
        man = d.get("manufacturer") or inst.manufacturer_id
        self.fields["model"].queryset = m.EquipmentModel.objects.filter(manufacturer_id=man or None).select_related("manufacturer")
        self.fields["model"].widget.attrs.update({"data-parent": "id_manufacturer", "data-source": "models"})
        if inst.pk:
            self.fields["file"].required = False
            self.fields["file"].label = t("Replace file (updates it everywhere it is assigned)")
            self.fields["file"].widget = forms.FileInput()

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and hasattr(f, "chunks") and "file" in self.changed_data:
            validate_upload(f)
            self.new_hash = hash_file(f)
            dup = m.Document.objects.filter(sha256=self.new_hash, is_archived=False).exclude(pk=self.instance.pk).first()
            if dup:
                raise ValidationError(t("This exact file is already in the library as:") + f" {dup.name}")
        return f

    def clean(self):
        d = super().clean()
        man, model = d.get("manufacturer"), d.get("model")
        if model and man and model.manufacturer_id != man.pk:
            self.add_error("model", t("This model does not belong to the selected manufacturer."))
        return d

    def save(self, commit=True):
        doc = super().save(commit=False)
        if "file" in self.changed_data and hasattr(self, "new_hash"):
            f = self.cleaned_data["file"]
            doc.original_name, doc.content_type, doc.size, doc.sha256 = f.name[:255], f.content_type or "", f.size, self.new_hash
        if commit:
            doc.save()
        return doc


class DocumentUploadForm(DocumentForm):
    """Upload fields plus the required assignment step, in the order of the upload workflow."""
    def __init__(self, *a, user, **k):
        super().__init__(*a, **k)
        add_assignment_fields(self, user)

    def clean(self):
        d = super().clean()
        self.target = assignment_target(self, d)
        return d


class UnitDocumentForm(DocumentForm):
    """A document added from a unit's page: details come from the unit, and the choice of who gets it defaults to this unit only."""
    def __init__(self, *a, unit, user, **k):
        super().__init__(*a, **k)
        allowed = allowed_levels(user)
        self.scopes = {level: target for level, _, target, _ in unit_document_scopes(unit)}
        self.fields["scope"] = forms.ChoiceField(label=t("Who gets this document"), initial="unit", widget=forms.RadioSelect, choices=[
            (level, f"{label} — " + (t("This will show on 1 unit") if n == 1 else t("This will show on %d units") % n))
            for level, label, _, n in unit_document_scopes(unit) if level in allowed])
        self.order_fields(["file", "doc_type", "language", "scope"])

    def clean(self):
        d = super().clean()
        level = d.get("scope")
        self.target = {level: self.scopes[level]} if level else {}
        return d


class ContractForm(BaseModelForm):
    sections = (("General Information", ["number", "title", "client", "description"]), ("Period & Value", ["start_date", "end_date", "value", "currency"]), ("Notes", ["notes"]))

    class Meta:
        model = m.Contract
        fields = ["number", "title", "client", "start_date", "end_date", "value", "currency", "description", "notes"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["client"].queryset = m.Client.objects.filter(Q(is_active=True) | Q(pk=self.instance.client_id))
        if self.instance.pk:
            self.fields["client"].disabled = True  # the covered units belong to this client

    def clean_currency(self):
        return self.cleaned_data["currency"].upper()

    def clean(self):
        d = super().clean()
        s, e = d.get("start_date"), d.get("end_date")
        if s and e and e < s:
            self.add_error("end_date", t("The end date must be after the start date."))
        elif s and e and self.instance.pk:
            conflicts = unit_conflicts(self.instance, self.instance.active_units, s, e)
            if conflicts:
                self.add_error("end_date", t("Already covered by another contract in these dates:") + " " + ", ".join(f"{c.unit.name} ({c.contract.number})" for c in conflicts))
        return d


FREQUENCIES = [("1", "Monthly"), ("2", "Every 2 months"), ("3", "Every 3 months"), ("4", "Every 4 months"), ("6", "Every 6 months"),
               ("12", "Every 12 months"), ("custom", "Custom interval"), ("manual", "Manually selected dates")]


class ScheduleForm(BaseModelForm):
    frequency = forms.ChoiceField(label="Frequency", choices=FREQUENCIES, initial="3")
    custom_value = forms.IntegerField(label="Repeat every", min_value=1, max_value=365, required=False)
    custom_unit = forms.ChoiceField(label="Interval unit", choices=m.Schedule.IntervalUnit.choices, required=False)
    dates = forms.CharField(label="Visit dates", required=False, widget=forms.Textarea(attrs={"rows": 4, "placeholder": "2026-12-20"}),
                            help_text="One date per line, as YYYY-MM-DD.")

    class Meta:
        model = m.Schedule
        fields = ["scope", "unit", "start_date", "end_date", "engineer", "technician", "include_new_units", "notes", "is_active"]

    field_order = ["scope", "unit", "frequency", "custom_value", "custom_unit", "dates", "start_date", "end_date", "engineer", "technician", "include_new_units", "notes", "is_active"]

    def __init__(self, *a, contract=None, **k):
        super().__init__(*a, **k)
        self.contract = contract
        self.fields["unit"].queryset = contract.active_units
        self.fields["start_date"].required = False
        staff = m.User.objects.filter(is_active=True)
        self.fields["engineer"].queryset = staff
        self.fields["technician"].queryset = staff
        i = self.instance
        if i.pk:
            if i.mode == "manual":
                self.fields["frequency"].initial = "manual"
                self.fields["dates"].initial = "\n".join(i.manual_dates)
            elif i.interval_unit == "month" and str(i.interval_value) in {"1", "2", "3", "4", "6", "12"}:
                self.fields["frequency"].initial = str(i.interval_value)
            else:
                self.fields["frequency"].initial = "custom"
                self.fields["custom_value"].initial = i.interval_value
                self.fields["custom_unit"].initial = i.interval_unit
        else:
            self.fields["start_date"].initial = contract.start_date

    def clean(self):
        d = super().clean()
        scope, unit, freq = d.get("scope"), d.get("unit"), d.get("frequency")
        if scope == "unit" and not unit:
            self.add_error("unit", t("This field is required."))
        if scope == "general":
            d["unit"] = None
        if d.get("is_active", True) and scope and is_general_schedule_duplicate(self.contract, scope, d.get("unit"), self.instance.pk):
            self.add_error("scope", t("An active schedule already exists at this level. Edit it instead of adding another."))
        if freq == "manual":
            try:
                parsed = sorted({date.fromisoformat(x.strip()) for x in d.get("dates", "").split() if x.strip()})
            except ValueError:
                parsed = None
                self.add_error("dates", t("Enter valid dates as YYYY-MM-DD, one per line."))
            if parsed is not None:
                if not parsed:
                    self.add_error("dates", t("This field is required."))
                elif parsed[0] < self.contract.start_date or parsed[-1] > self.contract.end_date:
                    self.add_error("dates", t("All dates must fall inside the contract period."))
                else:
                    self.manual = [x.isoformat() for x in parsed]
                    d["start_date"] = parsed[0]
        else:
            if not d.get("start_date"):
                self.add_error("start_date", t("This field is required."))
            elif not self.contract.start_date <= d["start_date"] <= self.contract.end_date:
                self.add_error("start_date", t("All dates must fall inside the contract period."))
            if freq == "custom" and not d.get("custom_value"):
                self.add_error("custom_value", t("This field is required."))
        return d

    def save(self, commit=True):
        s = super().save(commit=False)
        d = self.cleaned_data
        s.contract = self.contract
        s.unit = d.get("unit")
        freq = d["frequency"]
        if freq == "manual":
            s.mode, s.manual_dates = "manual", self.manual
        else:
            s.mode, s.manual_dates = "interval", []
            s.interval_value = int(d["custom_value"]) if freq == "custom" else int(freq)
            s.interval_unit = (d.get("custom_unit") or "month") if freq == "custom" else "month"
        s.save()
        if s.scope == "general":
            # The snapshot is what "do not auto-include new units" compares against.
            if not s.include_new_units and not s.covered_units.exists():
                s.covered_units.set(self.contract.active_units)
        return s


class VisitForm(BaseModelForm):
    sections = (("General Information", ["unit", "contract", "visit_type", "scheduled_date"]), ("Assigned Team", ["engineer", "technician"]), ("Notes", ["description", "notes"]))

    class Meta:
        model = m.Visit
        fields = ["unit", "contract", "visit_type", "scheduled_date", "engineer", "technician", "description", "notes"]

    def __init__(self, *a, user=None, **k):
        super().__init__(*a, **k)
        staff = m.User.objects.filter(is_active=True)
        self.fields["engineer"].queryset = staff
        self.fields["technician"].queryset = staff
        # A limited user may only plan visits on units and contracts they can already see.
        units, contracts = (units_for(user), contracts_for(user)) if user else (m.Unit.objects.all(), m.Contract.objects.all())
        self.fields["unit"].queryset = units.filter(Q(is_active=True) | Q(pk=self.instance.unit_id)).select_related("client").order_by(Lower("client__name_en"), Lower("name_en"))  # labels start with the client
        self.fields["unit"].label_from_instance = lambda u: f"{u.client.name} — {u.name}"
        self.fields["contract"].queryset = contracts
        self.fields["contract"].required = False
        if self.instance.pk and self.instance.schedule_id:
            self.fields["unit"].disabled = True
            self.fields["contract"].disabled = True
            self.fields["scheduled_date"].disabled = True  # the date changes through Reschedule, which keeps the record

    def clean(self):
        c = super().clean()
        u, contract = c.get("unit"), c.get("contract")
        if u and contract and u.client_id != contract.client_id:
            self.add_error("contract", t("This contract belongs to a different client."))
        return c

    def save(self, commit=True):
        v = super().save(commit=False)
        v.client_id = v.unit.client_id
        if commit:
            v.save()
        return v


class FieldReportAddForm(BaseForm):
    """Adds the site and unit named in a phone-app report to the database. Filled in from the report; everything can be corrected first."""
    sections = (("Site", ["client", "new_client_name", "country"]), ("Equipment Details", ["unit_name", "category", "manufacturer_name", "model_number", "serial_number"]))
    client = forms.ModelChoiceField(m.Client.objects.none(), label="Existing client", required=False, help_text="Choose the site if it is already in the database.")
    new_client_name = forms.CharField(label="Or new client name", max_length=200, required=False)
    country = forms.ModelChoiceField(m.Country.objects.filter(is_active=True), label="Country", required=False)
    unit_name = forms.CharField(label="Equipment Name", max_length=200)
    category = forms.ModelChoiceField(m.Category.objects.filter(is_active=True), label="Main Equipment Type")
    manufacturer_name = forms.CharField(label="Manufacturer", max_length=200)
    model_number = forms.CharField(label="Model Number", max_length=120)
    serial_number = forms.CharField(label="Serial Number", max_length=120, required=False)

    def __init__(self, *a, user, **k):
        super().__init__(*a, **k)
        self.fields["client"].queryset = clients_for(user).filter(is_active=True)

    def clean(self):
        d = super().clean()
        client, name = d.get("client"), (d.get("new_client_name") or "").strip()
        if not client and not name:
            self.add_error("new_client_name", t("Choose an existing client or type a new client name."))
        if not client and name and not d.get("country"):
            self.add_error("country", t("This field is required."))
        if not client and name and d.get("country"):
            client = m.Client.objects.filter(name_en__iexact=name, country=d["country"]).first()  # typed a name that already exists
        man = m.Manufacturer.objects.filter(name_en__iexact=(d.get("manufacturer_name") or "").strip()).first()
        model = man and m.EquipmentModel.objects.filter(manufacturer=man, model_number__iexact=(d.get("model_number") or "").strip()).first()
        if model and d.get("category") and model.category_id != d["category"].pk:
            self.add_error("category", f"{t('This model already exists under another type:')} {model.category.name}")
        if client and d.get("unit_name") and m.Unit.objects.filter(client=client, name_en=d["unit_name"]).exists():
            self.add_error("unit_name", t("This client already has a unit with this name."))
        if model and d.get("serial_number") and m.Unit.objects.filter(model=model, serial_number=d["serial_number"]).exists():
            self.add_error("serial_number", t("This serial number is already used by another unit of this model:") + f" {model.model_number}")
        self.found = {"client": client, "manufacturer": man, "model": model}
        return d

    def save(self):
        """Creates only what is missing: client, manufacturer, model, then the unit. Call inside a transaction."""
        d, f = self.cleaned_data, self.found
        client = f["client"] or m.Client.objects.create(name_en=d["new_client_name"].strip(), country=d["country"])
        man = f["manufacturer"] or m.Manufacturer.objects.create(name_en=d["manufacturer_name"].strip())
        model = f["model"] or m.EquipmentModel.objects.create(manufacturer=man, model_number=d["model_number"].strip(), category=d["category"])
        man.categories.add(d["category"])
        return m.Unit.objects.create(name_en=d["unit_name"], client=client, category=d["category"], subtype=model.subtype, model=model, serial_number=d.get("serial_number", ""))


class RescheduleForm(BaseForm):
    scheduled_date = forms.DateField(label="New date")
    reason = forms.CharField(label="Reason", max_length=300, required=False)


class CancelForm(BaseForm):
    reason = forms.CharField(label="Reason", max_length=300)


class CompleteForm(BaseForm):
    actual_date = forms.DateField(label="Actual Visit Date")
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea)


class VisitReportForm(BaseModelForm):
    class Meta:
        model = m.VisitReport
        fields = ["findings", "actions_taken", "parts_used", "recommendations"]


class UserForm(BaseModelForm):
    new_password = forms.CharField(label="Password", required=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                                   help_text="Leave empty to keep the current password. The user must change it at next sign-in.")

    class Meta:
        model = m.User
        fields = ["username", "first_name", "last_name", "email", "phone", "role", "language", "is_active"]
        labels = {"username": "Username", "first_name": "First name", "last_name": "Last name", "email": "Email", "is_active": "Active"}
        help_texts = {"username": "", "is_active": ""}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["role"].required = True
        if not self.instance.pk:
            self.fields["new_password"].required = True
            self.fields["new_password"].help_text = t("The user must change it at next sign-in.")

    def clean_new_password(self):
        p = self.cleaned_data.get("new_password")
        if p:
            validate_password(p)
        return p

    def save(self, commit=True):
        u = super().save(commit=False)
        p = self.cleaned_data.get("new_password")
        if p:
            u.set_password(p)
            u.must_change_password = True
            u.api_token_hash = None  # a password reset also signs the phone app out (a lost phone must stop working)
        if commit:
            u.save()
        return u


class RoleForm(BaseModelForm):
    class Meta:
        model = m.Role
        fields = ["name_en", "name_ar"]


class SettingsForm(BaseModelForm):
    class Meta:
        model = m.SystemSettings
        fields = ["visit_reminders_enabled", "visit_reminder_days", "contract_reminders_enabled", "contract_reminder_days", "email_enabled", "default_currency"]

import calendar
import re
import uuid
from datetime import date, timedelta

from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import get_language


def lang_pick(en, ar):
    return ar if (get_language() or "en").startswith("ar") and ar else en


class Named(models.Model):
    name_en = models.CharField("Name (English)", max_length=200)
    name_ar = models.CharField("Name (Arabic)", max_length=200, blank=True)

    class Meta:
        abstract = True

    @property
    def name(self):
        return lang_pick(self.name_en, self.name_ar)

    def __str__(self):
        return self.name


class Described(models.Model):
    description_en = models.TextField("Description (English)", blank=True)
    description_ar = models.TextField("Description (Arabic)", blank=True)

    class Meta:
        abstract = True

    @property
    def description(self):
        return lang_pick(self.description_en, self.description_ar)


class Role(Named):
    permissions = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.name


class User(AbstractUser):
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Role")
    phone = models.CharField("Phone", max_length=50, blank=True)
    language = models.CharField("Language", max_length=2, default="en", choices=[("en", "English"), ("ar", "Arabic")])
    must_change_password = models.BooleanField(default=False)
    api_token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)  # the phone app's sign-in; only the hash is kept

    def has(self, code):
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        if not hasattr(self, "_codes"):
            self._codes = set(self.role.permissions) if self.role_id else set()
        return code in self._codes

    def __str__(self):
        return self.get_full_name() or self.username


class Country(Named):
    code = models.CharField("Code", max_length=3, blank=True)
    is_active = models.BooleanField("Active", default=True)
    sort_order = models.PositiveIntegerField("Display order", default=100)

    class Meta:
        ordering = ["sort_order", "name_en"]
        constraints = [models.UniqueConstraint(fields=["name_en"], name="uniq_country_name")]


class Client(Named):
    country = models.ForeignKey(Country, on_delete=models.PROTECT, verbose_name="Country")
    city = models.CharField("City", max_length=120, blank=True)
    address = models.CharField("Address", max_length=300, blank=True)
    contact_person = models.CharField("Contact Person", max_length=150, blank=True)
    phone = models.CharField("Phone Number", max_length=50, blank=True)
    email = models.EmailField("Email", blank=True)
    notes = models.TextField("Additional Notes", blank=True)
    is_active = models.BooleanField("Active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [Lower("name_en")]
        constraints = [models.UniqueConstraint(fields=["name_en", "country"], name="uniq_client_country")]

    def get_absolute_url(self):
        return reverse("client_detail", args=[self.pk])


class Category(Named, Described):
    is_active = models.BooleanField("Active", default=True)
    sort_order = models.PositiveIntegerField("Display order", default=100)

    class Meta:
        ordering = ["sort_order", "name_en"]
        constraints = [models.UniqueConstraint(fields=["name_en"], name="uniq_category_name")]

    def get_absolute_url(self):
        return reverse("category_detail", args=[self.pk])


class Subtype(Named, Described):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="subtypes", verbose_name="Main Equipment Type")
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        ordering = ["name_en"]
        constraints = [models.UniqueConstraint(fields=["category", "name_en"], name="uniq_subtype_name")]

    def get_absolute_url(self):
        return reverse("category_detail", args=[self.category_id])


class FieldDefinition(Named):
    class Type(models.TextChoices):
        TEXT = "text", "Text"
        INTEGER = "integer", "Number"
        DECIMAL = "decimal", "Decimal"
        MEASUREMENT = "measurement", "Measurement (number with unit)"
        DATE = "date", "Date"
        SELECT = "select", "Dropdown"
        MULTISELECT = "multiselect", "Multiple selection"
        BOOLEAN = "boolean", "Yes / No"

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="fields", verbose_name="Main Equipment Type")
    subtype = models.ForeignKey(Subtype, null=True, blank=True, on_delete=models.PROTECT, related_name="fields",
                                verbose_name="Subtype", help_text="Leave empty to apply to every subtype in the category.")
    field_type = models.CharField("Field type", max_length=20, choices=Type.choices, default=Type.TEXT)
    units = models.CharField("Measurement units", max_length=100, blank=True,
                             help_text="Comma separated, e.g. kW, TR. Used with number, decimal and measurement fields.")
    options = models.TextField("Options", blank=True,
                               help_text="For dropdown and multiple selection: one option per line.")
    required = models.BooleanField("Required", default=False)
    is_active = models.BooleanField("Active", default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]

    @property
    def option_list(self):
        return [o.strip() for o in self.options.splitlines() if o.strip()]

    @property
    def unit_list(self):
        return [u.strip() for u in self.units.split(",") if u.strip()]

    @classmethod
    def applying_to(cls, category_id, subtype_id):
        return cls.objects.filter(Q(subtype__isnull=True) | Q(subtype_id=subtype_id), category_id=category_id, is_active=True)


class Manufacturer(Named, Described):
    country = models.ForeignKey(Country, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Country of Origin")
    website = models.URLField("Website", blank=True)
    contact_info = models.TextField("Contact Information", blank=True)
    categories = models.ManyToManyField(Category, blank=True, related_name="manufacturers", verbose_name="Related Equipment Categories")
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        ordering = ["name_en"]
        constraints = [models.UniqueConstraint(fields=["name_en"], name="uniq_manufacturer_name")]

    def get_absolute_url(self):
        return reverse("manufacturer_detail", args=[self.pk])

    @property
    def logo(self):
        return self.attachments.filter(kind=Attachment.Kind.LOGO).order_by("-pk").first()


class EquipmentModel(Described):
    manufacturer = models.ForeignKey(Manufacturer, on_delete=models.PROTECT, related_name="models", verbose_name="Manufacturer")
    model_number = models.CharField("Model Number", max_length=120)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="models", verbose_name="Main Equipment Type")
    subtype = models.ForeignKey(Subtype, null=True, blank=True, on_delete=models.PROTECT, related_name="models", verbose_name="Subtype")
    controller = models.ForeignKey("Controller", null=True, blank=True, on_delete=models.PROTECT, related_name="models", verbose_name="Controller")
    is_active = models.BooleanField("Active", default=True)
    reading_limits = models.JSONField(default=dict, blank=True, editable=False)  # this model's own reading limits; an empty dict follows Settings

    class Meta:
        ordering = ["manufacturer__name_en", "model_number"]
        constraints = [models.UniqueConstraint(fields=["manufacturer", "model_number"], name="uniq_model_number")]

    def __str__(self):
        return f"{self.manufacturer.name} {self.model_number}"

    def get_absolute_url(self):
        return reverse("model_detail", args=[self.pk])


def new_token():
    return uuid.uuid4().hex


class Unit(Named):
    class Status(models.TextChoices):
        OPERATIONAL = "operational", "Operational"
        UNDER_MAINTENANCE = "maintenance", "Under Maintenance"
        OUT_OF_SERVICE = "out_of_service", "Out of Service"
        DECOMMISSIONED = "decommissioned", "Decommissioned"

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="units", verbose_name="Client")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="units", verbose_name="Main Equipment Type")
    subtype = models.ForeignKey(Subtype, null=True, blank=True, on_delete=models.PROTECT, related_name="units", verbose_name="Subtype")
    model = models.ForeignKey(EquipmentModel, on_delete=models.PROTECT, related_name="units", verbose_name="Model")
    serial_number = models.CharField("Serial Number", max_length=120, blank=True)
    location = models.CharField("Installation Location", max_length=200, blank=True)
    project = models.CharField("Project", max_length=200, blank=True)
    installed_on = models.DateField("Installation Date", null=True, blank=True)
    warranty_start = models.DateField("Warranty Start Date", null=True, blank=True)
    warranty_end = models.DateField("Warranty End Date", null=True, blank=True)
    status = models.CharField("Equipment Status", max_length=20, choices=Status.choices, default=Status.OPERATIONAL)
    notes = models.TextField("Additional Notes", blank=True)
    is_active = models.BooleanField("Active", default=True)
    qr_token = models.CharField(max_length=32, unique=True, default=new_token, editable=False)
    reading_limits = models.JSONField(default=dict, blank=True, editable=False)  # this unit's own reading limits; an empty dict follows its model
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [Lower("name_en")]
        constraints = [
            models.UniqueConstraint(fields=["client", "name_en"], name="uniq_unit_name_client"),
            models.UniqueConstraint(fields=["model", "serial_number"], condition=~Q(serial_number=""), name="uniq_unit_serial"),
        ]

    def get_absolute_url(self):
        return reverse("unit_detail", args=[self.pk])

    @property
    def manufacturer(self):
        return self.model.manufacturer


class TechnicalValue(models.Model):
    """A spec value on a model (shared defaults) or on a unit (its own value, wins over the model's)."""
    field = models.ForeignKey(FieldDefinition, on_delete=models.PROTECT, related_name="values")
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.CASCADE, related_name="tech_values")
    model = models.ForeignKey(EquipmentModel, null=True, blank=True, on_delete=models.CASCADE, related_name="tech_values")
    value = models.JSONField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(unit__isnull=False, model__isnull=True) | Q(unit__isnull=True, model__isnull=False),
                                   name="techvalue_one_owner"),
            models.UniqueConstraint(fields=["field", "unit"], condition=Q(unit__isnull=False), name="uniq_value_unit"),
            models.UniqueConstraint(fields=["field", "model"], condition=Q(model__isnull=False), name="uniq_value_model"),
        ]


class DocumentType(Named):
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        ordering = ["pk"]


def upload_path(_instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    # Random names: never trust or expose the client's file name on disk.
    return f"{timezone.now():%Y/%m}/{uuid.uuid4().hex}.{ext}"


class Document(Named, Described):
    class Language(models.TextChoices):
        EN = "en", "English"
        AR = "ar", "Arabic"
        BOTH = "both", "English & Arabic"
        OTHER = "other", "Other"

    doc_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="documents", verbose_name="Document Type")
    manufacturer = models.ForeignKey(Manufacturer, null=True, blank=True, on_delete=models.PROTECT, related_name="documents", verbose_name="Manufacturer")
    model = models.ForeignKey(EquipmentModel, null=True, blank=True, on_delete=models.PROTECT, related_name="documents", verbose_name="Model")
    language = models.CharField("Language", max_length=10, choices=Language.choices, default=Language.EN)
    revision = models.CharField("Revision Number", max_length=40, blank=True)
    tags = models.CharField("Tags", max_length=300, blank=True, help_text="Comma separated")
    file = models.FileField("File", upload_to=upload_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True, blank=True)
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="documents", verbose_name="Uploaded By")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_archived = models.BooleanField(default=False)
    indexed_sha = models.CharField(max_length=64, null=True, blank=True, editable=False)  # file version whose text is in DocumentPage

    class Meta:
        ordering = ["-uploaded_at"]

    def get_absolute_url(self):
        return reverse("document_detail", args=[self.pk])

    @property
    def can_preview(self):
        return self.original_name.rsplit(".", 1)[-1].lower() in PREVIEWABLE


PREVIEWABLE = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "txt"}


class DocumentPage(models.Model):
    """The text of one PDF page, so the assistant can search inside the manuals."""
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    number = models.PositiveIntegerField()
    text = models.TextField()
    vector = SearchVectorField(null=True)

    class Meta:
        indexes = [GinIndex(fields=["vector"], name="doc_page_vector")]
        constraints = [models.UniqueConstraint(fields=["document", "number"], name="uniq_document_page")]


class Controller(Named):
    """A control system, such as Connect 2. Alarm lists belong to the controller, not to one unit size."""

    class Meta:
        ordering = ["name_en"]
        constraints = [models.UniqueConstraint(fields=["name_en"], name="uniq_controller_name")]


def alarm_key(text):
    """Letters and digits only, so "D4.1", "d4 1" and "d41" are the same code."""
    return re.sub(r"[^0-9A-Za-z]", "", text).upper()


def _bilingual(field):
    return property(lambda self: lang_pick(getattr(self, f"{field}_en"), getattr(self, f"{field}_ar")))


class AlarmCode(models.Model):
    """One alarm exactly as a manual gives it. A blank field means the manual does not say."""
    controller = models.ForeignKey(Controller, on_delete=models.PROTECT, related_name="alarms")
    code = models.CharField("Alarm code", max_length=200)
    key = models.CharField(max_length=200, db_index=True, editable=False)
    section = models.CharField("Section", max_length=120, blank=True)
    name_en = models.CharField(max_length=300)
    name_ar = models.CharField(max_length=300, blank=True)
    causes_en = models.TextField(blank=True)
    causes_ar = models.TextField(blank=True)
    checks_en = models.TextField(blank=True)
    checks_ar = models.TextField(blank=True)
    effect_en = models.TextField(blank=True)
    effect_ar = models.TextField(blank=True)
    action_en = models.TextField(blank=True)
    action_ar = models.TextField(blank=True)
    reset_en = models.TextField(blank=True)
    reset_ar = models.TextField(blank=True)
    source_document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="alarms")
    source_page = models.PositiveIntegerField()

    name = _bilingual("name")
    causes = _bilingual("causes")
    checks = _bilingual("checks")
    effect = _bilingual("effect")
    action = _bilingual("action")
    reset = _bilingual("reset")

    class Meta:
        ordering = ["key", "source_document_id"]
        constraints = [models.UniqueConstraint(fields=["controller", "key", "source_document", "name_en"], name="uniq_alarm_per_manual")]  # manuals sometimes print two alarms under one code

    def __str__(self):
        return self.code

    def save(self, *a, **k):
        self.key = alarm_key(self.code)
        super().save(*a, **k)


ASSIGNMENT_TARGETS = ("unit", "model", "subtype", "category", "manufacturer")


def _assignment_only(level):
    """The link's own target is set and every other target is empty."""
    return Q(level=level, **{f"{x}__isnull": x != level for x in ASSIGNMENT_TARGETS})


class DocumentAssignment(models.Model):
    class Level(models.TextChoices):
        UNIT = "unit", "Specific Equipment Unit"
        MODEL = "model", "All Units of a Model"
        SUBTYPE = "subtype", "Equipment Subtype"
        CATEGORY = "category", "Main Equipment Type"
        MANUFACTURER = "manufacturer", "All Units of a Manufacturer"
        ALL = "all", "All Equipment Units"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="assignments")
    level = models.CharField("Assignment Level", max_length=12, choices=Level.choices)
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.PROTECT, related_name="doc_assignments")
    model = models.ForeignKey(EquipmentModel, null=True, blank=True, on_delete=models.PROTECT, related_name="doc_assignments")
    subtype = models.ForeignKey(Subtype, null=True, blank=True, on_delete=models.PROTECT, related_name="doc_assignments")
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT, related_name="doc_assignments")
    manufacturer = models.ForeignKey(Manufacturer, null=True, blank=True, on_delete=models.PROTECT, related_name="doc_assignments")
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(name="assignment_matches_level", condition=(
                _assignment_only("unit") | _assignment_only("model") | _assignment_only("subtype")
                | _assignment_only("category") | _assignment_only("manufacturer") | _assignment_only("all"))),
            models.UniqueConstraint(fields=["document", "unit"], condition=Q(level="unit"), name="uniq_assign_unit"),
            models.UniqueConstraint(fields=["document", "model"], condition=Q(level="model"), name="uniq_assign_model"),
            models.UniqueConstraint(fields=["document", "subtype"], condition=Q(level="subtype"), name="uniq_assign_subtype"),
            models.UniqueConstraint(fields=["document", "category"], condition=Q(level="category"), name="uniq_assign_category"),
            models.UniqueConstraint(fields=["document", "manufacturer"], condition=Q(level="manufacturer"), name="uniq_assign_manufacturer"),
            models.UniqueConstraint(fields=["document"], condition=Q(level="all"), name="uniq_assign_all"),
        ]

    @property
    def target(self):
        return self.unit or self.model or self.subtype or self.category or self.manufacturer


class ContractQuerySet(models.QuerySet):
    def with_status(self, status):
        today = timezone.localdate()
        soon = today + timedelta(days=SystemSettings.get().contract_reminder_days)
        if status == "expired":
            return self.filter(status="active", end_date__lt=today)
        if status == "expiring":
            return self.filter(status="active", end_date__gte=today, end_date__lte=soon)
        if status == "active":
            return self.filter(status="active", end_date__gt=soon)
        return self.filter(status=status)

    def in_force(self):
        return self.filter(status="active", end_date__gte=timezone.localdate())


class Contract(models.Model):
    class Base(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        TERMINATED = "terminated", "Terminated"

    STATUS_LABELS = {"draft": "Draft", "active": "Active", "expiring": "Expiring Soon", "expired": "Expired",
                     "suspended": "Suspended", "terminated": "Terminated"}

    number = models.CharField("Contract Number", max_length=60, unique=True)
    title = models.CharField("Contract Title", max_length=200)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="contracts", verbose_name="Client")
    start_date = models.DateField("Contract Start Date")
    end_date = models.DateField("Contract End Date")
    value = models.DecimalField("Contract Value", max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField("Currency", max_length=3, default="JOD")
    # Stores only the states people set; "expiring" and "expired" are derived from dates so they can never go stale.
    status = models.CharField(max_length=12, choices=Base.choices, default=Base.DRAFT)
    description = models.TextField("Contract Description", blank=True)
    notes = models.TextField("Additional Notes", blank=True)
    renewed_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="renewals")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ContractQuerySet.as_manager()

    class Meta:
        ordering = ["-start_date", "number"]
        constraints = [models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="contract_dates_ok")]

    def __str__(self):
        return self.number

    def get_absolute_url(self):
        return reverse("contract_detail", args=[self.pk])

    @property
    def display_status(self):
        if self.status != "active":
            return self.status
        today = timezone.localdate()
        if self.end_date < today:
            return "expired"
        if (self.end_date - today).days <= SystemSettings.get().contract_reminder_days:
            return "expiring"
        return "active"

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.display_status]

    @property
    def in_force(self):
        return self.status == "active" and self.end_date >= timezone.localdate()

    @property
    def duration_months(self):
        d = self.end_date + timedelta(days=1)
        return (d.year - self.start_date.year) * 12 + d.month - self.start_date.month

    @property
    def active_units(self):
        return Unit.objects.filter(contract_links__contract=self, contract_links__removed_at__isnull=True)


class ContractEquipment(models.Model):
    """Removal sets removed_at instead of deleting, which keeps the history of what a contract covered."""
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="equipment_links")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="contract_links")
    added_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["contract", "unit"], condition=Q(removed_at__isnull=True), name="uniq_contract_unit_active")]


def add_months(d, months):
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class Schedule(models.Model):
    class Scope(models.TextChoices):
        GENERAL = "general", "General (all contract equipment)"
        UNIT = "unit", "Individual equipment unit"

    class Mode(models.TextChoices):
        INTERVAL = "interval", "Regular interval"
        MANUAL = "manual", "Manually selected dates"

    class IntervalUnit(models.TextChoices):
        MONTH = "month", "Months"
        WEEK = "week", "Weeks"
        DAY = "day", "Days"

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="schedules")
    scope = models.CharField("Schedule level", max_length=10, choices=Scope.choices)
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.PROTECT, related_name="schedules", verbose_name="Equipment Unit")
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.INTERVAL)
    interval_value = models.PositiveIntegerField(default=3)
    interval_unit = models.CharField(max_length=5, choices=IntervalUnit.choices, default=IntervalUnit.MONTH)
    manual_dates = models.JSONField(default=list, blank=True)
    start_date = models.DateField("First visit date")
    end_date = models.DateField("Last date (optional)", null=True, blank=True)
    include_new_units = models.BooleanField("Automatically include units added to the contract later", default=True)
    covered_units = models.ManyToManyField(Unit, blank=True, related_name="general_schedules")
    engineer = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="Assigned Engineer")
    technician = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="Assigned Technician")
    is_active = models.BooleanField("Active", default=True)
    notes = models.TextField("Notes", blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["contract"], condition=Q(scope="general", is_active=True), name="one_active_general_schedule"),
            models.UniqueConstraint(fields=["contract", "unit"], condition=Q(scope="unit", is_active=True), name="one_active_unit_schedule"),
            models.CheckConstraint(condition=Q(scope="general", unit__isnull=True) | Q(scope="unit", unit__isnull=False), name="schedule_scope_ok"),
        ]

    @property
    def frequency_label(self):
        if self.mode == "manual":
            return ("manual", len(self.manual_dates))
        return (self.interval_unit, self.interval_value)

    def planned_dates(self):
        """All dates within this schedule's window and the contract's coverage."""
        last = min(self.end_date or self.contract.end_date, self.contract.end_date)
        if self.mode == self.Mode.MANUAL:
            dates = sorted({date.fromisoformat(d) for d in self.manual_dates})
            return [d for d in dates if self.contract.start_date <= d <= last]
        result, i = [], 0
        step = max(self.interval_value, 1)
        while True:
            if self.interval_unit == self.IntervalUnit.MONTH:
                d = add_months(self.start_date, i * step)  # from the start each time, so 31 Jan never drifts to 28th forever
            else:
                d = self.start_date + timedelta(days=i * step * (7 if self.interval_unit == self.IntervalUnit.WEEK else 1))
            if d > last:
                return result
            if d >= self.contract.start_date:  # the contract's start may have moved after this schedule was saved
                result.append(d)
            i += 1


class VisitQuerySet(models.QuerySet):
    OPEN = ["scheduled", "rescheduled"]

    def open(self):
        return self.filter(status__in=self.OPEN)

    def with_status(self, status):
        today = timezone.localdate()
        soon = today + timedelta(days=7)
        if status in ("in_progress", "completed", "cancelled"):
            return self.filter(status=status)
        if status == "overdue":
            return self.open().filter(scheduled_date__lt=today)
        if status == "rescheduled":
            return self.filter(status="rescheduled", scheduled_date__gte=today)
        if status == "due":
            return self.filter(status="scheduled", scheduled_date=today)
        if status == "upcoming":
            return self.filter(status="scheduled", scheduled_date__gt=today, scheduled_date__lte=soon)
        return self.filter(status="scheduled", scheduled_date__gt=soon)


class Visit(models.Model):
    class Base(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        RESCHEDULED = "rescheduled", "Rescheduled"
        CANCELLED = "cancelled", "Cancelled"

    class Type(models.TextChoices):
        PREVENTIVE = "preventive", "Preventive Maintenance"
        CORRECTIVE = "corrective", "Corrective Maintenance"
        EMERGENCY = "emergency", "Emergency"
        INSPECTION = "inspection", "Inspection"
        COMMISSIONING = "commissioning", "Commissioning"

    STATUS_LABELS = {"scheduled": "Scheduled", "upcoming": "Upcoming", "due": "Due", "in_progress": "In Progress",
                     "completed": "Completed", "overdue": "Overdue", "rescheduled": "Rescheduled", "cancelled": "Cancelled"}

    number = models.CharField("Visit Number", max_length=30, unique=True, blank=True, null=True)
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="visits", verbose_name="Equipment Unit")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="visits", verbose_name="Client")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.PROTECT, related_name="visits", verbose_name="Contract Number")
    schedule = models.ForeignKey(Schedule, null=True, blank=True, on_delete=models.SET_NULL, related_name="visits")
    source = models.CharField(max_length=20, default="manual")  # manual | general | unit: what produced the visit, kept after schedule edits
    planned_date = models.DateField(null=True, blank=True)  # date the schedule produced; survives rescheduling so it is not regenerated
    scheduled_date = models.DateField("Scheduled Date")
    actual_date = models.DateField("Actual Visit Date", null=True, blank=True)
    engineer = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="engineer_visits", verbose_name="Assigned Engineer")
    technician = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="technician_visits", verbose_name="Assigned Technician")
    visit_type = models.CharField("Visit Type", max_length=20, choices=Type.choices, default=Type.PREVENTIVE)
    status = models.CharField(max_length=12, choices=Base.choices, default=Base.SCHEDULED)
    description = models.TextField("Description", blank=True)
    notes = models.TextField("Notes", blank=True)
    cancel_reason = models.CharField("Reason", max_length=300, blank=True)
    app_report_id = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)  # the phone app's report id, so a retried upload never duplicates
    created_at = models.DateTimeField(auto_now_add=True)

    objects = VisitQuerySet.as_manager()

    class Meta:
        ordering = ["scheduled_date", "pk"]
        indexes = [models.Index(fields=["scheduled_date", "status"])]
        constraints = [models.UniqueConstraint(fields=["unit", "planned_date"], condition=Q(planned_date__isnull=False), name="uniq_generated_visit")]

    def __str__(self):
        return self.number or f"#{self.pk}"

    def get_absolute_url(self):
        return reverse("visit_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.number:
            self.number = f"MV-{self.scheduled_date.year}-{self.pk:06d}"
            Visit.objects.filter(pk=self.pk).update(number=self.number)

    @property
    def display_status(self):
        if self.status in ("in_progress", "completed", "cancelled"):
            return self.status
        today = timezone.localdate()
        if self.scheduled_date < today:
            return "overdue"
        if self.status == "rescheduled":
            return "rescheduled"
        if self.scheduled_date == today:
            return "due"
        return "upcoming" if self.scheduled_date <= today + timedelta(days=7) else "scheduled"

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.display_status]

    @property
    def is_open(self):
        return self.status in ("scheduled", "rescheduled", "in_progress")

    @property
    def source_label(self):
        return {"general": "General contract schedule", "unit": "Individual equipment schedule"}.get(self.source, "Manually created")

    def is_untouched(self):
        """True when nobody has worked on it, so a schedule change may safely replace it."""
        return (self.status == "scheduled" and self.schedule_id is not None and not self.notes
                and not self.description and not self.attachments.exists() and not hasattr(self, "report"))


class VisitReport(models.Model):
    visit = models.OneToOneField(Visit, on_delete=models.PROTECT, related_name="report")
    findings = models.TextField("Findings")
    actions_taken = models.TextField("Actions Taken", blank=True)
    parts_used = models.TextField("Parts Used", blank=True)
    recommendations = models.TextField("Recommendations", blank=True)
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)


class FieldReport(models.Model):
    """A report as the phone app sent it, kept whole so its page can show every reading.
    With no visit it is waiting under Daily Reports for its site and unit to be added; with one, it is that completed visit's report."""
    app_report_id = models.CharField(max_length=64, unique=True)
    data = models.JSONField()
    client_name = models.CharField(max_length=200, blank=True)
    site = models.CharField(max_length=200, blank=True)
    machine_type = models.CharField(max_length=30, blank=True)
    machine_model = models.CharField(max_length=120, blank=True)
    serial_no = models.CharField(max_length=120, blank=True)
    maintenance_type = models.CharField(max_length=30, blank=True)
    report_date = models.DateField()
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="field_reports")
    created_at = models.DateTimeField(auto_now_add=True)
    visit = models.OneToOneField(Visit, null=True, blank=True, on_delete=models.SET_NULL, related_name="field_report")  # set once it is in the database

    class Status(models.TextChoices):  # received -> the foreman sends it to the engineer -> the engineer approves it, and it is locked
        RECEIVED = "received", "Waiting for the foreman"
        ENGINEER = "engineer", "Waiting for the engineer"
        APPROVED = "approved", "Approved"
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    foreman_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    foreman_at = models.DateTimeField(null=True, blank=True)
    engineer_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    engineer_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-report_date", "-pk"]

    def __str__(self):
        return f"{self.client_name or self.site or '—'} · {self.machine_model or self.serial_no or '—'}"


class Attachment(models.Model):
    """Every private non-library file: photos, logos, contract papers, visit reports."""
    class Kind(models.TextChoices):
        PHOTO = "photo", "Equipment photograph"
        NAMEPLATE = "nameplate", "Nameplate photograph"
        INSTALLATION = "installation", "Installation photograph"
        OTHER = "other", "Other"
        PRODUCT = "product", "Product image"
        LOGO = "logo", "Logo"
        CONTRACT = "contract", "Contract document"
        REPORT = "report", "Maintenance report"
        SIGNATURE = "signature", "Signature"

    kind = models.CharField("Type", max_length=20, choices=Kind.choices, default=Kind.OTHER)
    file = models.FileField("File", upload_to=upload_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.BigIntegerField(default=0)
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    model = models.ForeignKey(EquipmentModel, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    manufacturer = models.ForeignKey(Manufacturer, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    visit = models.ForeignKey(Visit, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    field_report = models.ForeignKey(FieldReport, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    @property
    def is_image(self):
        return self.original_name.rsplit(".", 1)[-1].lower() in {"png", "jpg", "jpeg", "gif", "webp"}


@receiver(post_delete, sender=Attachment)
def delete_attachment_file(sender, instance, **kwargs):
    """Covers cascades too, such as deleting a unit with photos. Waits for commit so a rollback keeps the file."""
    if instance.file:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))


class Notification(models.Model):
    class Kind(models.TextChoices):
        VISIT = "visit", "Maintenance Visit Reminder"
        CONTRACT = "contract", "Contract Expiration Reminder"
        REPORT = "report", "Maintenance report from the phone"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    dedupe_key = models.CharField(max_length=80)
    message_en = models.TextField()
    message_ar = models.TextField()
    visit = models.ForeignKey(Visit, null=True, blank=True, on_delete=models.CASCADE)
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE)
    field_report = models.ForeignKey(FieldReport, null=True, blank=True, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["user", "dedupe_key"], name="uniq_reminder_per_user")]
        indexes = [models.Index(fields=["user", "read_at"])]

    @property
    def message(self):
        return lang_pick(self.message_en, self.message_ar)

    @property
    def target_url(self):
        if self.field_report_id:
            return reverse("maintenance_report_view", args=[self.field_report_id])
        return (self.visit or self.contract).get_absolute_url() if (self.visit or self.contract) else reverse("notifications")


class ActivityLog(models.Model):
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=40)
    target_type = models.CharField(max_length=40)
    target_id = models.BigIntegerField(null=True)
    target_label = models.CharField(max_length=255, blank=True)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class SystemSettings(models.Model):
    visit_reminders_enabled = models.BooleanField("Send maintenance visit reminders", default=True)
    visit_reminder_days = models.PositiveIntegerField("Days before a visit", default=7)
    contract_reminders_enabled = models.BooleanField("Send contract expiration reminders", default=True)
    contract_reminder_days = models.PositiveIntegerField("Days before contract expiry", default=30)
    email_enabled = models.BooleanField("Also send reminders by email", default=False)
    default_currency = models.CharField("Default currency", max_length=3, default="JOD")
    reading_limits = models.JSONField(default=dict, blank=True, editable=False)  # limit key -> [lowest, highest]; replaces the manuals' values for every unit
    last_reminder_run = models.DateTimeField(null=True, blank=True)
    last_backup_run = models.DateTimeField(null=True, blank=True)
    last_backup_status = models.CharField(max_length=300, blank=True)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]

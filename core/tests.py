import io
import json
import os
import re
import smtplib
import subprocess
import tempfile
import zipfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import psycopg
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from reportlab.pdfgen import canvas

from .doc_search import guide_steps, index_pending, page_texts
from .i18n import AR
from .management.commands import backup, worker
from .management.commands.bootstrap import seed_reference_data
from .models import (ActivityLog, AlarmCode, Attachment, Category, Client, Contract, Controller, Country, Document, DocumentAssignment, DocumentPage, DocumentType, EquipmentModel, FieldDefinition, FieldReport,
                     Manufacturer, Notification, Role, Schedule, Subtype, SystemSettings, TechnicalValue, Unit, User, Visit, add_months)
from .services import (close_open_contract_visits, documents_for_unit, find_alarms, reassign_subtype, run_reminders, send_pending_emails, set_contract_units, spec_rows,
                       sync_contract_visits, units_reached)
from .templates_data import load_suggested

TEMP_FILES = tempfile.mkdtemp()


@override_settings(PRIVATE_ROOT=TEMP_FILES, MEDIA_ROOT=TEMP_FILES, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": TEMP_FILES}},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_reference_data()  # not the bootstrap command: that would overwrite the real FIRST_LOGIN.txt
        cls.today = timezone.localdate()
        role = lambda n: Role.objects.get(name_en=n)
        cls.admin = User.objects.create_superuser("admin", password="x", role=role("Administrator"))
        cls.engineer = User.objects.create_user("eng", password="x", role=role("Engineer"))
        cls.tech = User.objects.create_user("tech", password="x", role=role("Technician"))
        cls.other_tech = User.objects.create_user("tech2", password="x", role=role("Technician"))
        cls.viewer = User.objects.create_user("view", password="x", role=role("Viewer"))
        # Approves phone reports and nothing more: shows the approval order now that engineers hold every permission but user management.
        cls.approver = User.objects.create_user("appr", password="x", role=Role.objects.create(name_en="Approver", permissions=["visits.view", "scope.all", "reports.approve"]))
        cls.jordan = Country.objects.get(code="JO")
        cls.chillers = Category.objects.get(name_en="Chillers")
        cls.ahu = Category.objects.get(name_en="Air Handling Units (AHU)")
        cls.screw = Subtype.objects.create(category=cls.chillers, name_en="Air-Cooled Screw")
        cls.scroll = Subtype.objects.create(category=cls.chillers, name_en="Air-Cooled Scroll")
        cls.ciat = Manufacturer.objects.create(name_en="CIAT")
        cls.lxv = EquipmentModel.objects.create(manufacturer=cls.ciat, model_number="LXV 1450", category=cls.chillers, subtype=cls.screw)
        cls.sc = EquipmentModel.objects.create(manufacturer=cls.ciat, model_number="SC 200", category=cls.chillers, subtype=cls.scroll)
        cls.ahu_model = EquipmentModel.objects.create(manufacturer=cls.ciat, model_number="AHU 9", category=cls.ahu)
        cls.client_a = Client.objects.create(name_en="Hikma", name_ar="الحكمة", country=cls.jordan)
        cls.client_b = Client.objects.create(name_en="Other Co", country=cls.jordan)
        cls.c1 = cls.unit("Chiller 01", cls.lxv, "SER-123456")
        cls.c2 = cls.unit("Chiller 02", cls.lxv, "SER-777")
        cls.a1 = Unit.objects.create(name_en="AHU 01", client=cls.client_a, category=cls.ahu, model=cls.ahu_model, serial_number="AHU-1")
        cls.b1 = Unit.objects.create(name_en="Chiller B", client=cls.client_b, category=cls.chillers, subtype=cls.scroll, model=cls.sc, serial_number="B-1")
        cls.doc_type = DocumentType.objects.first()

    @classmethod
    def unit(cls, name, model, serial):
        return Unit.objects.create(name_en=name, client=cls.client_a, category=model.category, subtype=model.subtype, model=model, serial_number=serial)

    def contract(self, number="MC-1", months=24, client=None, status="active"):
        start = self.today
        return Contract.objects.create(number=number, title="T", client=client or self.client_a, start_date=start,
                                       end_date=add_months(start, months) - timedelta(days=1), status=status)

    def doc(self, name="Manual", content=b"%PDF-1.4 test", ext="pdf"):
        return Document.objects.create(name_en=name, doc_type=self.doc_type, original_name=f"{name}.{ext}",
                                       file=SimpleUploadedFile(f"{name}.{ext}", content), size=len(content))


class DocumentInheritance(Base):
    def assign(self, doc, **target):
        (level, value), = target.items()
        return DocumentAssignment.objects.create(document=doc, level=level, **{level: value})

    def test_three_levels(self):
        unit_doc, sub_doc, cat_doc = self.doc("U"), self.doc("S"), self.doc("C")
        self.assign(unit_doc, unit=self.c1)
        self.assign(sub_doc, subtype=self.screw)
        self.assign(cat_doc, category=self.chillers)
        levels = lambda u: {a.document.name_en: a.level for a in documents_for_unit(u)}
        self.assertEqual(levels(self.c1), {"U": "unit", "S": "subtype", "C": "category"})
        self.assertEqual(levels(self.c2), {"S": "subtype", "C": "category"})
        self.assertEqual(levels(self.b1), {"C": "category"})  # other subtype, other client: category only
        self.assertEqual(levels(self.a1), {})

    def test_future_units_inherit_without_new_rows(self):
        doc = self.doc()
        self.assign(doc, subtype=self.screw)
        rows = DocumentAssignment.objects.count()
        new = self.unit("Chiller 03", self.lxv, "SER-3")
        self.assertIn(doc, [a.document for a in documents_for_unit(new)])
        self.assertEqual(DocumentAssignment.objects.count(), rows)

    def test_most_specific_source_wins_and_no_duplicate(self):
        doc = self.doc()
        self.assign(doc, unit=self.c1)
        self.assign(doc, category=self.chillers)
        links = documents_for_unit(self.c1)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].level, "unit")

    def test_reach_count(self):
        doc = self.doc()
        self.assign(doc, category=self.chillers)
        self.assertEqual(units_reached(doc).count(), 3)

    def test_removing_one_assignment_keeps_document_and_others(self):
        doc = self.doc()
        first = self.assign(doc, unit=self.c1)
        self.assign(doc, subtype=self.scroll)
        self.client.force_login(self.admin)
        self.client.post(reverse("assignment_remove", args=[first.pk]))
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())
        self.assertEqual(doc.assignments.count(), 1)

    def test_assigned_document_cannot_be_deleted(self):
        doc = self.doc()
        self.assign(doc, unit=self.c1)
        self.client.force_login(self.admin)
        self.client.post(reverse("document_delete", args=[doc.pk]))
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())
        doc.assignments.all().delete()
        self.client.post(reverse("document_delete", args=[doc.pk]))
        self.assertFalse(Document.objects.filter(pk=doc.pk).exists())

    def test_upload_requires_target_and_rejects_bad_types_and_duplicates(self):
        self.client.force_login(self.admin)
        data = {"name_en": "New", "doc_type": self.doc_type.pk, "language": "en"}
        pdf = lambda: SimpleUploadedFile("a.pdf", b"%PDF unique")
        r = self.client.post(reverse("document_upload"), {**data, "file": pdf(), "level": "subtype"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Document.objects.filter(name_en="New").exists())
        r = self.client.post(reverse("document_upload"), {**data, "file": SimpleUploadedFile("x.exe", b"MZ"), "level": "category", "category": self.chillers.pk})
        self.assertFalse(Document.objects.filter(name_en="New").exists())
        r = self.client.post(reverse("document_upload"), {**data, "file": pdf(), "level": "category", "category": self.chillers.pk})
        self.assertEqual(r.status_code, 302)
        doc = Document.objects.get(name_en="New")
        self.assertEqual(doc.assignments.get().category, self.chillers)
        self.client.post(reverse("document_upload"), {**data, "name_en": "Dup", "file": pdf(), "level": "category", "category": self.chillers.pk})
        self.assertFalse(Document.objects.filter(name_en="Dup").exists())

    def test_replacing_file_updates_everywhere(self):
        doc = self.doc()
        self.assign(doc, category=self.chillers)
        self.client.force_login(self.admin)
        r = self.client.post(reverse("document_edit", args=[doc.pk]), {"name_en": "Manual", "doc_type": self.doc_type.pk, "language": "en",
                                                                         "file": SimpleUploadedFile("v2.pdf", b"%PDF version two")})
        self.assertEqual(r.status_code, 302)
        served = self.client.get(reverse("document_file", args=[doc.pk]))
        self.assertEqual(b"".join(served.streaming_content), b"%PDF version two")


class UnitSerial(Base):
    def test_changing_model_onto_a_used_serial_names_the_other_unit(self):
        self.client.force_login(self.admin)
        clash = Unit.objects.create(name_en="Twin", client=self.client_b, category=self.chillers, subtype=self.scroll, model=self.sc, serial_number="SER-777")
        r = self.client.post(reverse("unit_edit", args=[clash.pk]), {"name_en": "Twin", "client": self.client_b.pk, "category": self.chillers.pk, "subtype": self.screw.pk, "manufacturer_pick": self.ciat.pk,
                                                                     "model": self.lxv.pk, "serial_number": "SER-777", "status": "operational"})
        self.assertContains(r, "already used by another unit of this model: Chiller 02")
        self.assertNotContains(r, "uniq_unit_serial")
        clash.refresh_from_db()
        self.assertEqual(clash.model, self.sc)


class EquipmentFilters(Base):
    def setUp(self):
        self.client.force_login(self.admin)

    def options(self, kind, **query):
        return [x["label"] for x in self.client.get(f"/options/{kind}/", query).json()]

    def test_dropdown_data_needs_a_view_right(self):
        role = Role.objects.create(name_en="Nothing", permissions=[])
        nobody = User.objects.create_user("nobody", password="x", role=role)
        self.client.force_login(nobody)
        self.assertEqual(self.client.get("/options/filter_models/").status_code, 403)

    def test_subtypes_follow_the_main_type(self):
        self.assertEqual(self.options("filter_subtypes", category=self.chillers.pk), ["Air-Cooled Screw", "Air-Cooled Scroll"])
        self.assertEqual(self.options("filter_subtypes", category=self.ahu.pk), [])
        self.assertIn("Air-Cooled Screw", self.options("filter_subtypes", category=""))

    def test_models_follow_the_subtype_and_main_type(self):
        self.assertEqual(self.options("filter_models", category=self.chillers.pk, subtype=self.scroll.pk), ["SC 200"])
        self.assertEqual(self.options("filter_models", category=self.ahu.pk, subtype=""), ["AHU 9"])
        self.assertEqual(self.options("filter_models", category="", subtype="", manufacturer=""), ["AHU 9", "LXV 1450", "SC 200"])

    def test_form_options_still_need_a_parent(self):
        self.assertEqual(self.options("subtypes", category=""), [])
        self.assertEqual(self.options("models", category=self.chillers.pk), [])

    def test_list_filters_by_model(self):
        r = self.client.get(reverse("unit_list"), {"model": self.sc.pk})
        self.assertContains(r, "Chiller B")
        self.assertNotContains(r, "Chiller 01")

    def test_page_opens_with_narrowed_dropdowns(self):
        fields = self.client.get(reverse("unit_list"), {"category": self.chillers.pk, "subtype": self.scroll.pk}).context["filters"].fields
        self.assertEqual(list(fields["subtype"].queryset), [self.screw, self.scroll])
        self.assertEqual(list(fields["model"].queryset), [self.sc])

    def test_mismatched_url_keeps_the_filters_working(self):
        r = self.client.get(reverse("unit_list"), {"category": self.ahu.pk, "subtype": self.screw.pk})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Chiller 01")


class AlarmLookup(Base):
    def setUp(self):
        self.ctrl = Controller.objects.create(name_en="Connect 2")
        self.lxv.controller = self.ctrl
        self.lxv.save()
        self.manual = self.doc("Controller manual")
        DocumentAssignment.objects.create(document=self.manual, level="unit", unit=self.c1)
        self.alarm = self.add("d4.1", "Outdoor sensor", causes_en="Sensor open", causes_ar="الحسّاس مفتوح")
        self.add("CIRCUIT X OFF LP FAULT", "LP fault")
        self.add("D4", "Temperature sensor fault", action_en="Contact your installer.")
        self.add("dx.xS", "Fault during DHW")
        self.add("d3", "Water flow")

    def add(self, code, name, **k):
        return AlarmCode.objects.create(controller=self.ctrl, code=code, name_en=name, source_document=self.manual, source_page=26, **k)

    def codes(self, code):
        return [x.code for x in find_alarms(self.ctrl, code)]

    def test_code_ignores_case_and_punctuation_and_brings_the_group(self):
        self.assertEqual(self.codes(code="D4.1"), ["d4.1", "D4"])
        self.assertEqual(self.codes(code="d4 1")[0], "d4.1")

    def test_message_with_a_real_circuit_number_is_found(self):
        self.assertEqual(self.codes(code="circuit 2 off lp fault"), ["CIRCUIT X OFF LP FAULT"])

    def test_hot_water_suffix_finds_the_fault(self):
        self.assertEqual(sorted(self.codes(code="d3. S")), ["d3", "dx.xS"])

    def test_drive_alarm_finds_main_code_and_sub_code(self):
        self.add("160-YY", "Fan failure")
        self.add("Drive 13", "drive overload")
        self.assertEqual(sorted(self.codes(code="160-13")), ["160-YY", "Drive 13"])

    def test_alarm_group_printed_with_letters_is_found_by_number(self):
        self.add("130-nn", "Service maintenance alert")
        self.assertEqual(self.codes(code="13002"), ["130-nn"])

    def test_unknown_code_finds_nothing_and_unknown_model_is_said_plainly(self):
        self.assertEqual(self.codes("zz9"), [])
        self.client.force_login(self.admin)
        self.assertIn("No model matches", self.client.get(reverse("alarm_ask"), {"q": "d4.1", "model": "NOPE"}).json()["text"])

    def test_page_opens_with_the_unit_model_filled_in_and_in_arabic(self):
        self.client.force_login(self.admin)
        self.client.cookies["lang"] = "ar"
        page = self.client.get(reverse("alarm_lookup"), {"model_pk": self.lxv.pk})
        self.assertContains(page, 'value="LXV 1450"')
        self.assertContains(page, "البحث عن الإنذارات")

    def test_needs_permission_to_view_documents(self):
        role = self.viewer.role
        role.permissions = [p for p in role.permissions if p != "documents.view"]
        role.save()
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("alarm_lookup")).status_code, 403)

    def test_unit_page_links_to_lookup(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.c1.get_absolute_url()), f'{reverse("alarm_lookup")}?model_pk={self.lxv.pk}')


class AlarmChat(Base):
    def setUp(self):
        self.ctrl = Controller.objects.create(name_en="Connect 2")
        self.lxv.controller = self.ctrl
        self.lxv.save()
        self.manual = self.doc("Service manual")
        DocumentAssignment.objects.create(document=self.manual, level="all")
        self.alarm = AlarmCode.objects.create(controller=self.ctrl, code="15001", name_en="High discharge pressure", name_ar="ضغط تصريف مرتفع", causes_en="Dirty condenser", action_en="Clean the condenser coil.", source_document=self.manual, source_page=12)
        self.page(self.manual, 40, "If the compressor stops on high pressure, inspect the condenser fan and clean the coil.")
        self.page(self.manual, 41, "Pressure gauge installation guide for the workshop.")

    def page(self, doc, number, text):
        p = DocumentPage.objects.create(document=doc, number=number, text=text)
        DocumentPage.objects.filter(pk=p.pk).update(vector=SearchVector("text", config="english"))

    def ask(self, q, model="", user=None, lang=None):
        self.client.force_login(user or self.admin)
        if lang:
            self.client.cookies["lang"] = lang
        return self.client.get(reverse("alarm_ask"), {"q": q, "model": model}).json()

    def test_a_parameter_code_finds_its_row_in_the_manuals_table(self):
        self.page(self.manual, 17, "COOLING CONTROL 1 103 Temperature setpoint in cooling mode 8,0 to 45,0°C 25,0°C P07 > 0 1 104 Temperature proportional band "
                                   "in cooling mode 1,0 to 10,0 K 2,0 K P07 > 0 1 105 Temperature integral time P07 > 0 1 106 Temperature setpoint in heating mode P08 > 0 P06 = 1")
        self.page(self.manual, 18, "The cooling setpoint must stay below P103 + P104 when the heater runs.")  # only mentions it
        for q in ("P103", "parameter 103", "معامل 103"):
            first = self.ask(q)["passages"][0]
            self.assertEqual((first["source"]["page"], first["snippet"][:12]), (17, "[[103]] Temp"), q)
        self.assertEqual(self.ask("C12").get("passages", []), [])  # a number alone on a page of other numbers is not a parameter C12

    def test_a_described_problem_finds_alarm_and_manual_page_with_source(self):
        data = self.ask("my unit has high pressure on the compressor")
        self.assertEqual(data["alarms"][0]["code"], "15001")
        self.assertEqual([s["text"] for s in data["alarms"][0]["steps"]], ["Clean the condenser coil"])
        self.assertEqual(data["alarms"][0]["causes"], "Dirty condenser")
        self.assertEqual(data["passages"][0]["source"]["page"], 40)
        self.assertTrue(data["passages"][0]["source"]["url"].endswith("#page=40"))
        self.assertIn("[[", data["passages"][0]["snippet"])

    def test_misspelled_and_shortened_words_still_find_the_alarm(self):
        self.assertEqual(self.ask("hi discharg presure")["alarms"][0]["code"], "15001")

    def test_a_number_inside_a_sentence_is_not_an_alarm_code_but_e01_is_e1(self):
        AlarmCode.objects.create(controller=self.ctrl, code="5", name_en="Sensor failure", source_document=self.manual, source_page=1)
        AlarmCode.objects.create(controller=self.ctrl, code="E1", name_en="Phase loss", source_document=self.manual, source_page=1)
        self.assertEqual([a["code"] for a in self.ask("high discharge pressure after 5 minutes")["alarms"]], ["15001"])
        self.assertEqual([a["code"] for a in self.ask("5")["alarms"]], ["5"])
        self.assertEqual([a["code"] for a in self.ask("E01", model="LXV 1450")["alarms"]], ["E1"])

    def test_the_app_gets_the_alarm_list_once_and_the_bot_in_arabic(self):
        self.client.post(reverse("api_login"), {"username": "eng", "password": "x"}, content_type="application/json")
        cache.clear()
        auth = {"HTTP_AUTHORIZATION": "Token " + self.client.post(reverse("api_login"), {"username": "eng", "password": "x"}, content_type="application/json").json()["token"]}
        first = self.client.get(reverse("api_alarms"), **auth).json()
        self.assertEqual(dict(zip(first["fields"], first["alarms"][0]))["code"], "15001")
        self.assertIsNone(self.client.get(reverse("api_alarms"), {"have": first["version"]}, **auth).json()["alarms"])
        AlarmCode.objects.filter(code="15001").update(name_ar="نص مصحح")  # a corrected text must reach the phones: same count, same ids
        self.assertEqual(dict(zip(first["fields"], self.client.get(reverse("api_alarms"), {"have": first["version"]}, **auth).json()["alarms"][0]))["name_ar"], "نص مصحح")
        AlarmCode.objects.filter(code="15001").update(name_ar="ضغط تصريف مرتفع")
        answer =self.client.get(reverse("api_alarm_ask"), {"q": "high pressure", "lang": "ar"}, **auth).json()
        self.assertEqual((answer["alarms"][0]["code"], answer["alarms"][0]["name"]), ("15001", "ضغط تصريف مرتفع"))
        self.assertEqual(self.client.get(reverse("api_alarm_ask"), {"q": "x"}).status_code, 401)

    def steps(self, **fields):
        a = AlarmCode(controller=self.ctrl, code="1", name_en="x", source_document=self.manual, source_page=1, **fields)
        return guide_steps(a)

    def test_cause_only_alarm_becomes_one_step_per_cause_plus_reset(self):
        steps, note = self.steps(causes_en="Measured signal = 0 V dc Probable cause: Defective transducer, wiring fault", reset_en="Manual",
                                 action_en="Check: Defective transducer, wiring fault.\n(Based on the cause stated in the manual.)")
        self.assertEqual(steps, [("cause", "Defective transducer"), ("cause", "wiring fault"), ("reset", "Manual")])
        self.assertEqual(note, "Based on the cause stated in the manual.")

    def test_written_repair_becomes_one_step_per_sentence(self):
        steps, _ = self.steps(action_en="Check the direction of rotation of the fans. Clean the coil. Check and adjust the charge.\n(Instruction manual, section 27.)")
        self.assertEqual([t for _, t in steps], ["Check the direction of rotation of the fans", "Clean the coil", "Check and adjust the charge"])

    def test_manual_numbering_and_abbreviations_do_not_make_false_steps(self):
        steps, _ = self.steps(action_en="1. Check discharge temp. sensor (TD).\n2. Check NHC board.", checks_en="Error becomes definite after retrying operation for 4 times")
        self.assertEqual([t for _, t in steps], ["Check discharge temp. sensor (TD)", "Check NHC board"])

    def test_alarm_without_any_repair_has_no_invented_steps(self):
        self.assertEqual(self.steps()[0], [])

    def test_alarm_code_is_found_exactly(self):
        self.assertEqual(self.ask("alarm 15001")["alarms"][0]["name"], "High discharge pressure")

    def test_nothing_outside_the_documents_is_invented(self):
        data = self.ask("how do I bake bread")
        self.assertNotIn("alarms", data)
        self.assertIn("could not find", data["text"])

    def test_arabic_question_reaches_english_manuals_and_arabic_alarms(self):
        data = self.ask("ضغط مرتفع في الضاغط", lang="ar")
        self.assertEqual(data["alarms"][0]["name"], "ضغط تصريف مرتفع")
        self.assertEqual(data["passages"][0]["source"]["page"], 40)

    def test_archived_manual_is_not_searched(self):
        Document.objects.filter(pk=self.manual.pk).update(is_archived=True)
        self.assertFalse(self.ask("inspect condenser fan coil").get("passages"))

    def test_manual_the_user_cannot_open_gives_no_passage(self):
        self.manual.assignments.all().delete()
        DocumentAssignment.objects.create(document=self.manual, level="unit", unit=self.c1)
        role = self.tech.role
        role.permissions = [p for p in role.permissions if p != "scope.all"]
        role.save()
        self.assertFalse(self.ask("compressor high pressure", user=User.objects.get(pk=self.tech.pk)).get("passages"))

    def test_model_limits_alarms_to_its_controller(self):
        other = Controller.objects.create(name_en="Other")
        AlarmCode.objects.create(controller=other, code="99", name_en="High oil pressure", source_document=self.manual, source_page=3)
        names = [a["name"] for a in self.ask("high pressure", model="LXV 1450")["alarms"]]
        self.assertEqual(names, ["High discharge pressure"])

    def test_stop_words_alone_ask_for_more(self):
        self.assertIn("few more words", self.ask("the alarm is")["text"])

    def test_pdf_text_is_read_page_by_page(self):
        path = Path(TEMP_FILES) / "probe.pdf"
        c = canvas.Canvas(str(path))
        c.drawString(72, 700, "Compressor discharge pressure check procedure")
        c.showPage()
        c.save()
        try:
            self.assertEqual([n for n, _ in page_texts(str(path))], [1])
            self.assertIn("Compressor", next(page_texts(str(path)))[1])
        finally:
            path.unlink()

    def test_manual_with_alarms_is_refused_cleanly_and_its_file_kept(self):
        self.manual.assignments.all().delete()
        self.client.force_login(self.admin)
        r = self.client.post(reverse("document_delete", args=[self.manual.pk]), follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Archive it instead")
        self.assertTrue(Document.objects.filter(pk=self.manual.pk).exists())
        self.assertTrue(self.manual.file.storage.exists(self.manual.file.name))
        self.assertFalse(ActivityLog.objects.filter(action="deleted", target_id=self.manual.pk).exists())

    def test_missing_file_does_not_block_other_manuals_from_indexing(self):
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer)
        c.drawString(72, 700, "Readable compressor manual page")
        c.showPage()
        c.save()
        readable = self.doc("Readable", buffer.getvalue())
        lost = self.doc("Lost")  # newest, so it is tried first
        lost.file.storage.delete(lost.file.name)
        Document.objects.exclude(pk__in=[readable.pk, lost.pk]).update(indexed_sha="")
        index_pending()
        self.assertEqual(readable.pages.count(), 1)
        self.assertIsNone(Document.objects.get(pk=lost.pk).indexed_sha)  # tried again next cycle in case the file comes back

    def test_page_opens_as_chat(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("alarm_lookup")), "data-chat")


class CardPages(Base):
    def test_documents_open_on_types_and_the_table_stays_reachable(self):
        doc = self.doc("Grouped manual")
        self.client.force_login(self.admin)
        hub = self.client.get(reverse("document_list"))
        self.assertContains(hub, reverse("document_type", args=[doc.doc_type_id]))
        self.assertContains(self.client.get(reverse("document_type", args=[doc.doc_type_id])), "Grouped manual")
        self.assertContains(self.client.get(reverse("document_list"), {"view": "list"}), 'class="back-link"')

    def test_visits_board_sorts_by_state_and_hides_cancelled(self):
        today = timezone.localdate()
        late = Visit.objects.create(unit=self.c1, client=self.c1.client, scheduled_date=today - timedelta(days=3))
        Visit.objects.create(unit=self.c1, client=self.c1.client, scheduled_date=today + timedelta(days=30))
        Visit.objects.create(unit=self.c1, client=self.c1.client, scheduled_date=today, status="cancelled")
        self.client.force_login(self.admin)
        columns = {k: v for k, _, v, _ in self.client.get(reverse("visit_list")).context["columns"]}
        self.assertEqual([v.pk for v in columns["overdue"]], [late.pk])
        self.assertEqual(len(columns["todo"]), 1)
        self.assertContains(self.client.get(reverse("visit_list"), {"view": "list"}), "Board")

    def test_board_columns_are_capped_but_counted_and_newest_completed_first(self):
        today = timezone.localdate()
        Visit.objects.bulk_create([Visit(unit=self.c1, client=self.c1.client, scheduled_date=today + timedelta(days=i)) for i in range(1, 61)])
        Visit.objects.bulk_create([Visit(unit=self.c1, client=self.c1.client, scheduled_date=today - timedelta(days=i), actual_date=today - timedelta(days=i), status="completed") for i in range(1, 41)])
        self.client.force_login(self.admin)
        columns = {k: (v, n) for k, _, v, n in self.client.get(reverse("visit_list")).context["columns"]}
        self.assertEqual((len(columns["todo"][0]), columns["todo"][1]), (50, 60))
        self.assertEqual(columns["todo"][0][0].scheduled_date, today + timedelta(days=1))  # the nearest work is never cut off
        self.assertEqual((len(columns["completed"][0]), columns["completed"][1]), (30, 40))
        self.assertEqual(columns["completed"][0][0].actual_date, today - timedelta(days=1))

    def test_grouped_form_keeps_every_field(self):
        self.client.force_login(self.admin)
        form = self.client.get(reverse("unit_new")).context["form"]
        self.assertEqual({f.name for _, fields in form.section_rows() for f in fields}, {f.name for f in form.visible_fields()})

    def test_pdf_report_with_logo_is_built(self):
        self.client.force_login(self.admin)
        key = self.client.get(reverse("report_list")).context["reports"][0][0]
        self.assertEqual(self.client.get(reverse("report_view", args=[key]), {"export": "pdf"}).content[:4], b"%PDF")


class PhoneApp(Base):
    """The app's connection: token sign-in, scoped download, report upload that is safe to retry."""
    def token(self, username):
        cache.clear()
        r = self.client.post(reverse("api_login"), {"username": username, "password": "x"}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        return {"HTTP_AUTHORIZATION": "Token " + r.json()["token"]}

    def test_sign_in_needs_the_right_password_and_a_token_is_required(self):
        cache.clear()
        bad = self.client.post(reverse("api_login"), {"username": "eng", "password": "nope"}, content_type="application/json")
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(self.client.get(reverse("api_sync")).status_code, 401)
        self.assertEqual(self.client.get(reverse("api_sync"), HTTP_AUTHORIZATION="Token wrong").status_code, 401)

    def test_download_holds_units_clients_and_document_links(self):
        doc = Document.objects.create(name_en="LX manual", doc_type=self.doc_type, file="x.pdf", original_name="x.pdf")
        DocumentAssignment.objects.create(document=doc, level="model", model=self.lxv)
        data = self.client.get(reverse("api_sync"), **self.token("eng")).json()
        units = {u["id"]: u for u in data["units"]}
        self.assertEqual(set(units), {self.c1.pk, self.c2.pk, self.a1.pk, self.b1.pk})
        self.assertEqual(units[self.c1.pk]["document_ids"], [doc.pk])
        self.assertEqual(units[self.a1.pk]["document_ids"], [])
        self.assertIn(f"/documents/{doc.pk}/file/", data["documents"][0]["url"])
        tunnel = self.client.get(reverse("api_sync"), HTTP_X_FORWARDED_PROTO="https", **self.token("eng")).json()  # Android refuses http links
        self.assertTrue(tunnel["documents"][0]["url"].startswith("https://"))
        # The app keeps manuals on the phone: the file comes with the token, and only if the user may see the document.
        from django.core.files.base import ContentFile
        doc.file.save("m.pdf", ContentFile(b"%PDF-1.4"))
        file_url = reverse("api_document_file", args=[doc.pk])
        self.assertEqual((self.client.get(file_url).status_code, self.client.get(reverse("api_document_file", args=[0]), **self.token("eng")).status_code), (401, 404))
        self.assertEqual((tunnel["documents"][0]["file_name"], "attachment" in self.client.get(file_url, **self.token("eng")).get("Content-Disposition", "x")), ("x.pdf", True))
        self.assertEqual({c["name_en"] for c in data["clients"]}, {"Hikma", "Other Co"})

    def test_technician_downloads_only_assigned_units(self):
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        data = self.client.get(reverse("api_sync"), **self.token("tech")).json()
        self.assertEqual([u["id"] for u in data["units"]], [self.c1.pk])
        self.assertEqual([c["name_en"] for c in data["clients"]], ["Hikma"])
        self.assertEqual(len(data["visits"]), 1)

    def test_uploaded_report_completes_the_visit_once(self):
        visit = Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        auth = self.token("tech")
        report = {"id": "r-1", "unit_id": self.c1.pk, "visit_id": visit.pk, "date": f"{self.today.year}-{self.today.month}-{self.today.day}", "maintenance_type": "Preventive", "general_notes": "All fine",
                  "required_maintenance": "Cleaned filters", "checklist_items": [{"label_en": "Voltage", "value": "400"}], "materials": [{"description": "Filter", "qty": "2", "unit": "pcs"}]}
        send = lambda: self.client.post(reverse("api_report"), {"report": json.dumps(report), "pdf": SimpleUploadedFile("r.pdf", b"%PDF-1.4")}, **auth)
        first, again = send(), send()
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertTrue(again.json()["already_uploaded"])
        visit.refresh_from_db()
        self.assertEqual((visit.status, visit.actual_date, Visit.objects.count()), ("completed", self.today, 1))
        self.assertIn("Voltage: 400", visit.report.findings)
        self.assertEqual((visit.report.parts_used, visit.attachments.count()), ("Filter: 2 pcs", 1))

    def test_the_visit_sent_to_the_phone_comes_before_an_older_missed_one(self):
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today - timedelta(days=30), technician=self.tech)
        sent = Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today + timedelta(days=5), technician=self.tech, status="in_progress")
        data = self.client.get(reverse("api_sync"), **self.token("tech")).json()
        self.assertEqual(data["visits"][0]["id"], sent.pk)  # the app reports on the first visit listed for a unit

    def test_a_report_sent_twice_at_once_answers_already_uploaded(self):
        from .views import api
        FieldReport.objects.create(app_report_id="r-dup", data={}, report_date=self.today)  # the other copy, saved while this one was checked
        real, calls = api._already_uploaded, []
        # The first check misses the other copy, as it would in a real race; the check after the database refuses finds it.
        with mock.patch.object(api, "_already_uploaded", lambda request, rid: real(request, rid) if calls.append(rid) or len(calls) > 1 else None):
            r = self.client.post(reverse("api_report"), {"report": json.dumps({"id": "r-dup", "unit_id": self.c1.pk})}, **self.token("eng"))
        self.assertEqual((r.status_code, r.json()["already_uploaded"], Visit.objects.filter(app_report_id="r-dup").count()), (200, True, 0))

    def test_a_model_page_lists_only_documents_the_user_may_open(self):
        sales = User.objects.create_user("sales", password="x", role=Role.objects.create(name_en="Sales", permissions=["manufacturers.view", "documents.view"]))
        shared, private = self.doc("Model manual"), self.doc("Hikma commissioning")
        Document.objects.filter(pk__in=[shared.pk, private.pk]).update(model=self.lxv)
        DocumentAssignment.objects.create(document=shared, level="model", model=self.lxv)
        DocumentAssignment.objects.create(document=private, level="unit", unit=self.c1)  # one client's unit only
        self.client.force_login(sales)
        page = self.client.get(reverse("model_detail", args=[self.lxv.pk]))
        self.assertEqual([d.pk for d in page.context["documents"]], [shared.pk])

    def test_report_forms_are_changed_on_the_web_reach_the_phone_and_follow_finished_reports(self):
        from .models import ReportForm
        from .report_forms import default_rows
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("report_form_type", args=["chiller"])).status_code, 403)  # every chiller's form needs Settings
        self.client.force_login(self.admin)
        page = self.client.get(reverse("report_form_type", args=["chiller"]))
        rows = page.context["rows"]
        self.assertEqual([r["key"] for r in rows], [r["key"] for r in default_rows("chiller")])  # the phone's built-in boxes to start from
        post = {"rows": len(rows), "materials": 0}
        for i, r in enumerate(rows):
            post.update({f"r{i}-key": r["key"], f"r{i}-custom": "0", f"r{i}-pos": i + 1, f"r{i}-en": r["label_en"], f"r{i}-ar": r["label_ar"], f"r{i}-show": "on"})
        oil = next(i for i, r in enumerate(rows) if r["key"] == "oil_pressure")
        post.update({f"r{oil}-en": "Oil Pressure (bar)", f"r{oil}-pos": "0.5", "n0-en": "Vibration", "n0-ar": "الاهتزاز", "m0-d": "Oil filter", "m0-u": "pcs"})
        del post[f"r{rows.index(next(r for r in rows if r['key'] == 'machine_load'))}-show"]  # switch a standard box off
        self.client.post(reverse("report_form_type", args=["chiller"]), post)
        form = ReportForm.objects.get(report_type="chiller", unit__isnull=True)
        self.assertEqual((form.rows[0]["label_en"], form.rows[-1]["label_en"], form.rows[-1]["custom"], form.materials), ("Oil Pressure (bar)", "Vibration", True, [{"description": "Oil filter", "unit": "pcs"}]))
        self.assertTrue(next(r for r in form.rows if r["key"] == "machine_load")["hidden"])
        self.assertIn("renamed: Oil Pressure → Oil Pressure (bar)", ActivityLog.objects.get(action="report_form_changed").details)

        # A unit follows its type until it is given something of its own; going back to the default undoes that.
        unit_url = reverse("unit_report_form", args=[self.c1.pk])
        same = self.client.get(unit_url).context
        post = {"rows": len(same["rows"]), "materials": 1, "m0-d": "Oil filter", "m0-u": "pcs", "s-compressors": "2", "s-phase": "3Ph"}
        for i, r in enumerate(same["rows"]):
            post.update({f"r{i}-key": r["key"], f"r{i}-custom": "1" if r["custom"] else "0", f"r{i}-pos": i + 1, f"r{i}-en": r["label_en"], f"r{i}-ar": r["label_ar"]})
            if not r["hidden"]:
                post[f"r{i}-show"] = "on"
        self.client.post(unit_url, post)
        own = ReportForm.objects.get(unit=self.c1)
        self.assertEqual((own.rows, own.materials, own.setup), (None, None, {"phase": "3Ph", "compressors": 2}))  # same boxes as the type: still follows it
        self.client.post(unit_url, {**post, "n0-en": "Pump vibration"})
        own.refresh_from_db()
        self.assertEqual(own.rows[-1]["label_en"], "Pump vibration")
        sync = self.client.get(reverse("api_sync"), **self.token("eng")).json()
        self.assertEqual((sync["units"][0]["report_type"] in ("chiller", "ahu"), sync["report_forms"]["types"]["chiller"]["rows"][0]["label_en"],
                          sync["report_forms"]["units"][str(self.c1.pk)]["rows"][-1]["label_en"]), (True, "Oil Pressure (bar)", "Pump vibration"))
        self.client.force_login(self.admin)
        self.client.post(unit_url + "reset/")
        own.refresh_from_db()
        self.assertEqual((own.rows, own.setup["compressors"]), (None, 2))  # back to the type's boxes; the unit's setup stays

        # A finished phone report brings its form back, unless the website changed the form after the report started.
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        phone_rows = [*ReportForm.objects.get(report_type="chiller").rows, {"key": "custom_ab12", "label_en": "Fan belt", "label_ar": "سير المروحة", "hidden": False, "custom": True}]
        report = lambda rid, started: {"id": rid, "unit_id": self.c1.pk, "created_at": started.isoformat(),
                                        "form": {"rows": phone_rows, "materials": [{"description": "Oil filter", "unit": "pcs"}], "setup": {"compressors": 3, "refrigerant": "R-410A"}}}
        old = report("r-f1", own.web_changed_at - timedelta(hours=1))
        self.client.post(reverse("api_report"), {"report": json.dumps(old)}, **self.token("tech"))
        own.refresh_from_db()
        self.assertEqual((own.rows, own.setup["compressors"]), (None, 2))  # started before the website's change: the website wins
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        self.client.post(reverse("api_report"), {"report": json.dumps(report("r-f2", timezone.now() + timedelta(minutes=1)))}, **self.token("tech"))
        own.refresh_from_db()
        self.assertEqual((own.rows[-1]["label_en"], own.setup, own.source), ("Fan belt", {"refrigerant": "R-410A", "compressors": 3}, "app"))

    def test_save_all_and_save_to_multiple_units_with_limits(self):
        from .models import ReportForm
        from .services import limits_for
        self.client.force_login(self.admin)
        url = reverse("report_form_type", args=["chiller"])
        page = self.client.get(url).context
        post = {"rows": len(page["rows"]), "materials": 0, "origin": url}
        for i, r in enumerate(page["rows"]):
            post.update({f"r{i}-key": r["key"], f"r{i}-custom": "0", f"r{i}-pos": i + 1, f"r{i}-en": r["label_en"], f"r{i}-ar": r["label_ar"], f"r{i}-show": "on"})
        post.update({"r0-en": "", "r0-ar": ""})  # a standard box emptied by mistake keeps the app's name
        self.client.post(url, {**post, "lim-amps_high": "70"})  # Save all: the type's limits reach every chiller without its own
        self.assertIsNone(ReportForm.objects.get(report_type="chiller", unit__isnull=True).rows)  # the emptied name came back, so no box changed
        self.assertEqual(limits_for(unit=self.c2)["amps"], (None, 70.0, "Set for Chillers"))
        self.assertEqual(limits_for(unit=self.a1)["amps"][1], None)  # an AHU is another type
        self.assertNotIn("chiller", self.client.get(reverse("api_sync"), **self.token("eng")).json()["report_forms"]["types"])  # limits alone are not a form change for the phone

        # Save to multiple units: the edited form rides along to the unit choice, then goes to the chosen units only.
        Unit.objects.filter(pk=self.c2.pk).update(reading_limits={"oil_pressure": [1.0, 5.0]})
        pick = self.client.post(url, {**post, "action": "multi", "n0-en": "Belt tension", "lim-amps_high": "40"})
        carried = dict(pick.context["carried"])
        self.assertEqual((carried["n0-en"], carried["lim-amps_high"]), ("Belt tension", "40"))
        self.assertNotIn(self.a1.pk, {u.pk for us in pick.context["groups"].values() for u in us})
        done = self.client.post(reverse("report_form_units", args=["chiller"]), {**carried, "units": [self.c1.pk, self.c2.pk, self.a1.pk], "origin": url})
        self.assertRedirects(done, url, fetch_redirect_response=False)
        forms = {x.unit_id: x for x in ReportForm.objects.filter(unit__isnull=False)}
        self.assertEqual(sorted(forms), [self.c1.pk, self.c2.pk])
        self.assertEqual(forms[self.c1.pk].rows[-1]["label_en"], "Belt tension")
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.reading_limits, {"oil_pressure": [1.0, 5.0], "amps": [None, 40.0]})  # added to its own; nothing it had is removed

    def test_an_odd_report_start_date_never_blocks_the_upload(self):
        from .models import ReportForm
        ReportForm.objects.create(report_type="chiller", rows=None, web_changed_at=timezone.now())
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        for rid, started in (("r-d1", "2026-11-22 10:00"), ("r-d2", "2026-13-45T00:00:00")):
            r = self.client.post(reverse("api_report"), {"report": json.dumps({"id": rid, "unit_id": self.c1.pk, "created_at": started, "form": {"rows": []}})}, **self.token("tech"))
            self.assertIn(r.status_code, (200, 201))

    def test_report_for_a_unit_out_of_scope_waits_and_touches_nothing(self):
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        r = self.client.post(reverse("api_report"), {"report": json.dumps({"id": "r-2", "unit_id": self.b1.pk})}, **self.token("tech"))
        self.assertTrue(r.json()["pending"])
        self.assertFalse(Visit.objects.filter(unit=self.b1).exists())

    def test_unknown_site_report_waits_then_one_button_adds_site_unit_and_visit(self):
        report = {"id": "r-3", "client": "New Bakery", "site": "Zarqa", "machine_model": "LD-300", "serial_no": "NB-1", "type": "chiller", "date": "2026-7-2",
                  "maintenance_type": "Corrective", "required_maintenance": "Replaced contactor"}
        send = lambda: self.client.post(reverse("api_report"), {"report": json.dumps(report), "pdf": SimpleUploadedFile("r.pdf", b"%PDF-1.4")}, **self.token("eng"))
        self.assertEqual((send().status_code, send().json()["already_uploaded"], FieldReport.objects.count()), (201, True, 1))
        waiting = FieldReport.objects.get()
        self.client.force_login(self.admin)
        page = self.client.get(reverse("maintenance_reports"))
        self.assertEqual([row["report"] for row in page.context["waiting"]], [waiting])
        r = self.client.post(reverse("maintenance_report_add", args=[waiting.pk]), {"new_client_name": "New Bakery", "country": self.jordan.pk, "unit_name": "Chiller LD-300",
                                                                                   "category": self.chillers.pk, "manufacturer_name": "ciat", "model_number": "LD-300", "serial_number": "NB-1"})
        unit = Unit.objects.get(name_en="Chiller LD-300")
        visit = unit.visits.get()
        self.assertRedirects(r, visit.get_absolute_url())
        self.assertEqual((unit.client.name_en, unit.model.manufacturer, visit.status, visit.visit_type, visit.actual_date), ("New Bakery", self.ciat, "completed", "corrective", date(2026, 7, 2)))
        self.assertEqual((visit.report.actions_taken, visit.attachments.count(), FieldReport.objects.filter(visit__isnull=True).count()), ("Replaced contactor", 1, 0))
        self.assertEqual(self.client.get(reverse("maintenance_report_add", args=[waiting.pk])).status_code, 404)  # the button cannot run twice

    def test_saturation_temperature_and_superheat(self):
        from .phone_report import saturation_temp, superheat
        self.assertAlmostEqual(saturation_temp("R-22", 4.98), 0, places=1)
        self.assertAlmostEqual(saturation_temp("R410A", 9.0), 3.8, delta=0.15)  # between two table rows; CoolProp gives 3.82
        self.assertIsNone(saturation_temp("R22", 441))
        s = superheat("R-410A", "8", "bar", "10", "C")  # 9.013 bar abs boils at 3.9 °C
        self.assertEqual((s["value"], s["unit"], s["zone"]), ("6.1", "K", "normal"))
        f = superheat("R410A", "116", "psi", "50", "F")  # the same reading in psi and °F
        self.assertEqual((f["value"], f["unit"], f["zone"]), ("11.0", "°F", "normal"))
        self.assertIn("problem", superheat("R22", "440", "bar", "35", "C"))
        self.assertIn("problem", superheat("Other", "4", "bar", "10", "C"))
        self.assertIsNone(superheat("R22", "", "bar", "10", "C"))

    def test_report_page_has_a_box_per_reading_superheat_and_photos(self):
        item = lambda key, label, value: {"field_key": key, "label_en": label, "label_ar": "", "value": value}
        report = {"id": "r-4", "unit_id": self.c1.pk, "date": "2026-7-2", "temp_unit": "C", "pressure_unit": "bar", "checklist_items": [
            item("refrigerant_type", "Refrigerant Type", "R410A"), item("circuit_2_suction_pressure", "Circuit 2 Suction Pressure", "8"),
            item("circuit_2_suction_temp", "Circuit 2 Suction Temp", "10"), item("compressor_1_amps", "Compressor 1 Amps", "30"),
            item("", "Added by technician", "Yes"), item("compressor_1_voltage_l1l2", "Compressor 1 L1-L2", "")]}
        gif = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        r = self.client.post(reverse("api_report"), {"report": json.dumps(report), "photo": SimpleUploadedFile("p.gif", gif, "image/gif")}, **self.token("eng"))
        self.assertEqual(r.status_code, 201)
        fr = FieldReport.objects.get(app_report_id="r-4")  # a matched unit keeps the full readings too
        self.client.force_login(self.admin)
        page = self.client.get(reverse("maintenance_report_view", args=[fr.pk]))
        groups = page.context["view"]["groups"]
        self.assertEqual([(g["kind"], g["number"], len(g["readings"])) for g in groups], [("general", 0, 2), ("compressor", 1, 1), ("circuit", 2, 2)])
        self.assertEqual(groups[2]["superheat"]["value"], "6.1")
        self.assertEqual([r["label"] for r in groups[2]["readings"]], ["Suction Pressure", "Suction Temp"])  # the heading already says Circuit 2
        self.client.cookies["lang"] = "ar"  # Arabic writes decimals with a comma; the meter's position must not
        self.assertContains(self.client.get(reverse("maintenance_report_view", args=[fr.pk])), "inset-inline-start:30.6%")
        del self.client.cookies["lang"]
        self.assertContains(page, reverse("attachment_file", args=[page.context["photos"][0].pk]))
        self.assertContains(self.client.get(fr.visit.get_absolute_url()), reverse("maintenance_report_view", args=[fr.pk]))
        self.client.force_login(self.tech)  # someone else's report stays hidden from a restricted user
        self.assertEqual(self.client.get(reverse("maintenance_report_view", args=[fr.pk])).status_code, 404)

    def test_report_goes_foreman_then_engineer_then_is_locked_and_every_step_is_logged(self):
        import io
        import pypdfium2 as pdfium
        from reportlab.pdfgen import canvas
        signed = io.BytesIO()
        page = canvas.Canvas(signed)
        page.drawString(50, 750, "signed by the client")
        page.save()
        foreman = User.objects.create_user("fore", password="x", role=Role.objects.get(name_en="Foreman"))
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        report = {"id": "r-5", "unit_id": self.c1.pk, "general_notes": "ok", "checklist_items": [
            {"field_key": "oil_pressure", "label_en": "Oil Pressure", "value": "2"}, {"field_key": "compressor_1_amps", "label_en": "Compressor 1 Amps", "value": "30"}]}
        self.client.post(reverse("api_report"), {"report": json.dumps(report), "pdf": SimpleUploadedFile("r.pdf", signed.getvalue(), "application/pdf")}, **self.token("tech"))
        fr = FieldReport.objects.get(app_report_id="r-5")
        told = lambda: set(Notification.objects.filter(field_report=fr).values_list("user__username", flat=True))
        self.assertEqual((fr.status, told()), ("received", {"fore", "admin"}))  # the foremen are told first, the engineers not yet
        view, url, step = (reverse(n, args=[fr.pk]) for n in ("maintenance_report_view", "maintenance_report_edit", "maintenance_report_step"))
        phone_pdf = fr.visit.attachments.get(kind="report")

        self.client.force_login(self.viewer)  # before approval nobody else sees the report, its text or its files
        self.assertEqual(self.client.get(view).status_code, 404)
        self.assertContains(self.client.get(fr.visit.get_absolute_url()), "waiting for approval")
        self.assertEqual(self.client.get(reverse("attachment_file", args=[phone_pdf.pk])).status_code, 403)
        self.client.force_login(self.tech)  # the technician who sent it can read it, not change it
        self.assertEqual((self.client.get(view).status_code, self.client.get(url).status_code), (200, 403))
        self.client.force_login(self.engineer)  # not the engineer's turn yet
        self.assertEqual((self.client.get(url).status_code, self.client.post(step, {"status": "received"}).status_code), (403, 403))

        self.client.force_login(foreman)
        save = lambda **values: self.client.post(url, {"stamp": self.client.get(url).context["form"].fields["stamp"].initial, "item_1": "30", "general_notes": "ok", **values})
        same = {"materials_0_description": "Oil", "materials_0_qty": "5", "materials_0_unit": "L"}
        stale = self.client.get(url).context["form"].fields["stamp"].initial
        save(item_0="3.5", **same)
        fr.refresh_from_db()
        self.assertEqual(([i["value"] for i in fr.data["checklist_items"]], fr.data["materials"]), (["3.5", "30"], [{"description": "Oil", "qty": "5", "unit": "L"}]))
        self.assertIn("Oil Pressure: 3.5", fr.visit.report.findings)
        log = ActivityLog.objects.get(action="report_edited")
        self.assertEqual((log.user, log.details), (foreman, "Oil Pressure: 2 → 3.5\nParts Used 1: — → Oil | 5 | L"))
        save(item_0="3.5", **same)  # nothing changed: no new log line
        self.client.post(url, {"stamp": stale, "item_0": "2", "item_1": "30", "general_notes": "ok", **same})  # a form opened before that save cannot undo it
        fr.refresh_from_db()
        self.assertEqual((fr.data["checklist_items"][0]["value"], ActivityLog.objects.filter(action="report_edited").count()), ("3.5", 1))
        self.assertRedirects(self.client.get(reverse("visit_report", args=[fr.visit_id])), url)  # one place to edit: the hand-written form would be overwritten
        self.client.post(step, {"status": "received", "note": "Oil pressure was low, please check"})
        fr.refresh_from_db()
        self.assertEqual((fr.status, fr.foreman_by, told()), ("engineer", foreman, {"fore", "admin", "eng", "appr"}))
        self.assertEqual(fr.foreman_note, "Oil pressure was low, please check")  # the foreman's one-way note to the engineer
        self.assertEqual(self.client.get(url).status_code, 403)  # the foreman's turn is over

        self.client.force_login(self.engineer)
        save(item_0="4", **same)
        self.client.post(step, {"status": "engineer"})
        fr.refresh_from_db()
        self.assertEqual((fr.status, fr.engineer_by, fr.data["checklist_items"][0]["value"]), ("approved", self.engineer, "4"))
        self.assertEqual(self.client.get(url).status_code, 403)  # approved reports are locked
        self.assertEqual(self.client.post(step, {"status": "engineer"}).status_code, 403)
        page = self.client.get(view)
        self.assertContains(page, "mailto:?subject=Maintenance%20report")  # the client has no email yet, so the To line stays empty
        pdf = self.client.get(reverse("maintenance_report_pdf", args=[fr.pk]), {"email": "1"})
        merged = pdfium.PdfDocument(pdf.content)
        self.assertEqual((pdf["Content-Type"], len(merged) >= 2), ("application/pdf", True))  # the approved report, then the signed phone pages
        merged.close()
        with mock.patch("core.views.maintenance.MAX_EMAIL_PDF", 100):  # too big for an email: the same PDF comes inside a zip
            packed = self.client.get(reverse("maintenance_report_pdf", args=[fr.pk]))
        with zipfile.ZipFile(io.BytesIO(packed.content)) as z:
            self.assertEqual((packed["Content-Type"], z.namelist(), z.read(z.namelist()[0])[:5]), ("application/zip", [f"ASEE_Report_{fr.visit.number}.pdf"], b"%PDF-"))
        self.assertEqual(list(ActivityLog.objects.filter(target_type="FieldReport", target_id=fr.pk).order_by("pk").values_list("action", flat=True)),
                         ["report_edited", "report_sent_to_engineer", "report_edited", "report_approved", "report_email_prepared"])

        self.assertContains(self.client.get(view), "Oil pressure was low, please check")  # the engineer reads the foreman's note
        self.client.force_login(self.viewer)  # after approval everyone with the visit in scope can read it
        self.assertEqual((self.client.get(view).status_code, self.client.get(reverse("attachment_file", args=[phone_pdf.pk])).status_code), (200, 200))
        self.assertNotContains(self.client.get(view), "Oil pressure was low")  # the note stays between the foreman and the engineer
        self.client.force_login(self.admin)  # only an administrator can still change it
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_contract_is_sent_to_one_technicians_phone_and_his_reports_close_the_visits(self):
        c = self.contract()
        set_contract_units(c, [self.c1, self.c2], self.admin)
        planned = Visit.objects.create(unit=self.c1, client=self.client_a, contract=c, scheduled_date=self.today + timedelta(days=10))
        missed = Visit.objects.create(unit=self.c2, client=self.client_a, contract=c, scheduled_date=self.today - timedelta(days=7))
        self.client.force_login(self.engineer)
        send = reverse("contract_dispatch", args=[c.pk])
        self.assertEqual(self.client.get(send).status_code, 200)
        self.client.post(send, {"technician": self.tech.pk, "scheduled_date": self.today.isoformat(), "visit_type": "preventive"})
        self.client.post(send, {"technician": self.tech.pk, "scheduled_date": self.today.isoformat(), "visit_type": "preventive"})  # pressed twice: no duplicates
        sent = Visit.objects.filter(contract=c).order_by("unit__name_en")
        self.assertEqual([(v.unit, v.status, v.technician) for v in sent], [(self.c1, "in_progress", self.tech), (self.c2, "in_progress", self.tech)])
        self.assertIn(planned, sent)  # the unit's next planned visit is used, not a new one
        missed.refresh_from_db()  # a missed visit sent now is in progress today, not overdue
        self.assertEqual((missed.status, missed.scheduled_date, missed.display_status), ("in_progress", self.today, "in_progress"))
        self.assertEqual(Visit.objects.get(pk=planned.pk).scheduled_date, self.today + timedelta(days=10))  # a future date is kept
        self.assertEqual(Notification.objects.filter(user=self.tech, contract=c).count(), 1)  # one for the contract, not one per unit

        data = self.client.get(reverse("api_sync"), **self.token("tech")).json()
        mine = {v["unit_id"]: v for v in data["visits"]}
        self.assertEqual((set(mine), mine[self.c1.pk]["contract"]["number"], mine[self.c1.pk]["assigned_to_me"], mine[self.c1.pk]["status"]),
                         ({self.c1.pk, self.c2.pk}, "MC-1", True, "in_progress"))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        report = {"id": "r-9", "unit_id": self.c1.pk, "visit_id": planned.pk, "contract_batch_id": "b-1"}
        self.client.post(reverse("api_report"), {"report": json.dumps(report), "signature": [SimpleUploadedFile("signature_client.png", png, "image/png"),
                                                                                                  SimpleUploadedFile("signature_technician.png", png, "image/png")]}, **self.token("tech"))
        planned.refresh_from_db()
        self.assertEqual((planned.status, planned.attachments.filter(kind="signature").count()), ("completed", 2))  # the started visit is the one closed

        visit = Visit.objects.filter(unit=self.c2).first()
        self.client.force_login(self.engineer)
        self.client.post(reverse("visit_start", args=[Visit.objects.create(unit=self.a1, client=self.client_a, scheduled_date=self.today).pk]), {"technician": self.tech.pk})
        self.assertEqual(Visit.objects.get(unit=self.a1).status, "in_progress")
        self.assertTrue(Notification.objects.filter(user=self.tech, visit__unit=self.a1).exists())
        self.assertEqual(visit.status, "in_progress")

    def test_password_reset_signs_the_phone_out_and_a_serial_alone_does_not_pick_a_unit(self):
        auth = self.token("eng")
        self.client.force_login(self.admin)
        e = self.engineer
        self.client.post(reverse("user_edit", args=[e.pk]), {"username": "eng", "role": e.role_id, "language": e.language, "is_active": "on", "new_password": "A-new-pass-9431"})
        self.assertEqual(self.client.get(reverse("api_sync"), **auth).status_code, 401)  # a lost phone stops working
        e.set_password("x"); e.must_change_password = False; e.save()
        Unit.objects.filter(pk=self.c1.pk).update(serial_number="12345")
        send = lambda rid, model: self.client.post(reverse("api_report"), {"report": json.dumps({"id": rid, "serial_no": "12345", "machine_model": model})}, **self.token("eng")).json()["pending"]
        self.assertEqual((send("r-6", "Some other machine"), send("r-7", self.c1.model.model_number)), (True, False))

    def test_readings_outside_the_manuals_limits_are_marked(self):
        from .phone_report import report_view
        item = lambda key, value: {"field_key": key, "label_en": key, "value": value}
        view = report_view({"temp_unit": "C", "pressure_unit": "bar", "checklist_items": [
            item("refrigerant_type", "R410A"), item("evap_water_in", "12"), item("evap_water_out", "3"),  # outlet under 5 °C, difference 9 K is fine
            item("compressor_1_voltage_l1l2", "406"), item("compressor_1_voltage_l1l3", "380"), item("compressor_1_voltage_l2l3", "399"),  # each in 360-440, imbalance 3.8 %
            item("circuit_1_suction_pressure", "8"), item("circuit_1_discharge_pressure", "43"), item("circuit_2_suction_pressure", "440")]})
        marked = {r["label"]: (r["bad"], r["limit"]) for g in view["groups"] for r in g["readings"]}
        self.assertEqual((marked["evap_water_in"], marked["evap_water_out"]), ((False, "7.5–30 °C"), (True, "5–20 °C")))
        self.assertEqual((marked["Evap. water in/out difference"], marked["Voltage imbalance"][0], marked["compressor_1_voltage_l1l3"][0]), ((False, "3–10 K"), True, False))
        self.assertEqual((marked["circuit_1_suction_pressure"][0], marked["circuit_1_discharge_pressure"], marked["circuit_2_suction_pressure"][0]), (False, (True, "max 41.5 bar"), True))
        self.assertEqual(view["outside"], 4)
        psi = report_view({"temp_unit": "F", "pressure_unit": "psi", "checklist_items": [item("refrigerant_type", "R-410A"), item("evap_water_out", "44.6"), item("circuit_1_discharge_pressure", "500")]})
        self.assertEqual([(r["bad"], r["limit"]) for g in psi["groups"] for r in g["readings"]][1:], [(False, "41–68 °F"), (False, "max 601.9 psi")])

    def test_limits_follow_settings_then_model_then_unit_and_changes_are_logged(self):
        from .services import limits_for
        self.client.force_login(self.viewer)  # Settings limits need the settings permission
        self.assertEqual(self.client.get(reverse("reading_limits")).status_code, 403)
        self.client.force_login(self.admin)
        page = self.client.get(reverse("reading_limits"))
        every = {name: ("" if field.initial is None else field.initial) for name, field in page.context["form"].fields.items()}
        self.assertEqual((every["evap_water_out_low"], every["evap_water_out_high"], every["amps_high"]), (5, 20, ""))  # the manuals' values, ready to change
        self.client.post(reverse("reading_limits"), {**every, "evap_water_out_low": "4"})
        self.client.post(reverse("model_limits", args=[self.c1.model_id]), {"evap_water_out_low": "-5", "evap_water_out_high": "15", "amps_high": "60"})
        self.client.post(reverse("unit_report_form", args=[self.c1.pk]), {"lim-amps_high": "45"})  # a unit's limits live on its report form page
        got = lambda unit: {k: v[:2] for k, v in limits_for(unit=Unit.objects.get(pk=unit.pk)).items() if k in ("evap_water_out", "amps", "cond_water_in")}
        self.assertEqual(got(self.c1), {"evap_water_out": (-5, 15), "amps": (None, 45), "cond_water_in": (20, 45)})  # unit over model over Settings over manuals
        self.assertEqual(got(self.b1)["evap_water_out"], (4, 20))
        self.assertEqual(SystemSettings.get().reading_limits, {"evap_water_out": [4.0, 20.0]})  # only what differs from the manuals is kept
        self.assertEqual(list(ActivityLog.objects.filter(action="limits_changed").order_by("pk").values_list("details", flat=True)),
                         ["Evaporator water out: as in the manuals → 4–20 °C", "Evaporator water out: as above → -5–15 °C\nCompressor current: as above → max 60 A",
                          "Chillers\nCompressor current: as above → max 45 A"])
        report = {"id": "r-8", "unit_id": self.c1.pk, "checklist_items": [{"field_key": "evap_water_out", "label_en": "Out", "value": "-2"},
                                                                         {"field_key": "compressor_1_amps", "label_en": "Amps", "value": "50"}]}
        self.client.post(reverse("api_report"), {"report": json.dumps(report)}, **self.token("eng"))
        self.client.force_login(self.admin)
        view = self.client.get(reverse("maintenance_report_view", args=[FieldReport.objects.get(app_report_id="r-8").pk])).context["view"]
        self.assertEqual([(r["bad"], r["limit"], r["source"]) for g in view["groups"] for r in g["readings"]],
                         [(False, "-5–15 °C", f"Set for model {self.c1.model.model_number}"), (True, "max 45 A", "Set for this unit")])
        bad = self.client.post(reverse("unit_report_form", args=[self.c1.pk]), {"lim-oil_pressure_low": "9", "lim-oil_pressure_high": "2"})
        self.assertContains(bad, "The minimum is higher than the maximum.")

    def test_a_unit_is_shown_only_beside_a_number(self):
        from .phone_report import report_view
        item = lambda key, value: {"field_key": key, "label_en": key, "value": value}
        readings = report_view({"checklist_items": [item("compressor_1_voltage_type", "Single Phase (1Ph)"), item("compressor_1_voltage_1ph", "220")]})["groups"][0]["readings"]
        self.assertEqual([r["unit"] for r in readings], ["", "V"])


class RecordScope(Base):
    def test_restricted_user_cannot_edit_a_client_or_contract_out_of_scope(self):
        role = self.tech.role
        role.permissions = [p for p in role.permissions if p != "scope.all"] + ["clients.edit", "contracts.edit"]
        role.save()
        self.client.force_login(User.objects.get(pk=self.tech.pk))
        self.assertEqual(self.client.get(reverse("client_edit", args=[self.client_a.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("client_delete", args=[self.client_a.pk])).status_code, 404)
        self.assertTrue(Client.objects.filter(pk=self.client_a.pk).exists())

    def test_restricted_user_cannot_plan_a_visit_on_a_unit_out_of_scope(self):
        role = self.tech.role
        role.permissions = [p for p in role.permissions if p != "scope.all"] + ["visits.manage"]
        role.save()
        self.client.force_login(User.objects.get(pk=self.tech.pk))
        response = self.client.post(reverse("visit_new"), {"unit": self.a1.pk, "visit_type": "preventive", "scheduled_date": timezone.localdate().isoformat(),
                                                           "technician": self.tech.pk})
        self.assertIn("unit", response.context["form"].errors)
        self.assertFalse(Visit.objects.filter(unit=self.a1).exists())

    def test_client_page_query_count_does_not_grow_with_units(self):
        self.client.force_login(self.admin)
        url = reverse("client_detail", args=[self.client_a.pk])
        with CaptureQueriesContext(connection) as before:
            self.client.get(url)
        for i in range(6):
            Unit.objects.create(name_en=f"More {i}", client=self.client_a, category=self.chillers, model=self.lxv)
        with CaptureQueriesContext(connection) as after:
            self.client.get(url)
        self.assertEqual(len(before), len(after))


class UnitDocumentAdd(Base):
    def post(self, scope, user=None, unit=None, name="Added"):
        self.client.force_login(user or self.admin)
        return self.client.post(reverse("unit_document_add", args=[(unit or self.c1).pk]), {
            "file": SimpleUploadedFile("a.pdf", f"%PDF {scope} {name}".encode()), "doc_type": self.doc_type.pk, "language": "en", "scope": scope,
            "name_en": name, "manufacturer": self.ciat.pk, "model": self.lxv.pk})

    def on(self, unit, name="Added"):
        return name in [a.document.name_en for a in documents_for_unit(unit)]

    def test_form_is_filled_from_the_unit_and_shows_reach(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("unit_document_add", args=[self.c1.pk]))
        form = page.context["form"]
        self.assertEqual(form["manufacturer"].value(), self.ciat.pk)
        self.assertEqual(form["model"].value(), self.lxv.pk)
        self.assertEqual(form["name_en"].value(), "LXV 1450 document")
        self.assertEqual(form["scope"].value(), "unit")
        self.assertContains(page, "This will show on 2 units")
        self.assertContains(page, "This will show on 4 units")

    def test_default_is_this_unit_only(self):
        self.assertEqual(self.post("unit").status_code, 302)
        doc = Document.objects.get(name_en="Added")
        self.assertEqual((doc.assignments.get().level, doc.assignments.get().unit, doc.uploaded_by), ("unit", self.c1, self.admin))
        self.assertTrue(self.on(self.c1))
        self.assertFalse(self.on(self.c2))
        self.assertFalse(self.on(self.unit("Chiller 03", self.lxv, "SER-3")))

    def assert_live(self, scope, reached, not_reached):
        self.assertEqual(self.post(scope).status_code, 302)
        doc = Document.objects.get(name_en="Added")
        rows = DocumentAssignment.objects.count()
        self.assertEqual(doc.assignments.get().level, scope)
        new = self.unit("Chiller 03", self.lxv, "SER-3")
        self.assertTrue(self.on(new))
        self.assertEqual(DocumentAssignment.objects.count(), rows)  # a live link, not copies
        for u in reached:
            self.assertTrue(self.on(u), u)
        for u in not_reached:
            self.assertFalse(self.on(u), u)
        self.assertIn(new, units_reached(doc))
        return doc

    def test_model_link_reaches_future_units(self):
        self.assert_live("model", [self.c2], [self.b1, self.a1])

    def test_subtype_link_reaches_future_units(self):
        self.assert_live("subtype", [self.c2], [self.b1, self.a1])

    def test_category_link_reaches_future_units(self):
        self.assert_live("category", [self.c2, self.b1], [self.a1])

    def test_manufacturer_link_reaches_future_units(self):
        doc = self.assert_live("manufacturer", [self.c2, self.b1, self.a1], [])
        other = EquipmentModel.objects.create(manufacturer=Manufacturer.objects.create(name_en="Other"), model_number="X", category=self.chillers)
        self.assertFalse(self.on(self.unit("Other 1", other, "O-1")))
        self.assertEqual(units_reached(doc).filter(model=other).count(), 0)

    def test_all_units_link_reaches_everything_new(self):
        doc = self.assert_live("all", [self.c2, self.b1, self.a1], [])
        other = EquipmentModel.objects.create(manufacturer=Manufacturer.objects.create(name_en="Other"), model_number="X", category=self.ahu)
        self.assertTrue(self.on(self.unit("Other 1", other, "O-1")))
        self.assertEqual(units_reached(doc).count(), Unit.objects.filter(is_active=True).count())

    def test_linked_at_several_levels_shows_once(self):
        doc = self.doc("Twice")
        for level, target in [("unit", {"unit": self.c1}), ("model", {"model": self.lxv}), ("manufacturer", {"manufacturer": self.ciat}), ("all", {})]:
            DocumentAssignment.objects.create(document=doc, level=level, **target)
        links = [a for a in documents_for_unit(self.c1) if a.document == doc]
        self.assertEqual([a.level for a in links], ["unit"])

    def test_wide_scopes_need_the_manage_permission(self):
        uploader = User.objects.create_user("upl", password="x", role=Role.objects.create(name_en="Uploader", permissions=[
            "equipment.view", "equipment.edit", "documents.view", "documents.upload", "scope.all"]))  # may upload, may not manage documents
        r = self.post("all", user=uploader)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Document.objects.filter(name_en="Added").exists())
        self.assertEqual(self.post("subtype", user=uploader).status_code, 302)  # sharing by subtype needs only upload rights

    def test_viewer_cannot_add(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("unit_document_add", args=[self.c1.pk])).status_code, 403)

    def test_oversized_upload_is_rejected(self):
        self.client.force_login(self.admin)
        with self.settings(MAX_UPLOAD_MB=0):
            r = self.post("unit")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Document.objects.filter(name_en="Added").exists())

    def test_document_page_adds_and_removes_wide_links(self):
        self.client.force_login(self.admin)
        doc = self.doc("Scoped")
        url = reverse("assignment_add", args=[doc.pk])
        self.client.post(url, {"level": "all"})
        self.client.post(url, {"level": "manufacturer", "scope_manufacturer": self.ciat.pk})
        self.client.post(url, {"level": "model", "scope_manufacturer": self.ciat.pk, "scope_model": self.lxv.pk})
        self.client.post(url, {"level": "model"})  # no model chosen: nothing saved
        self.assertEqual(sorted(doc.assignments.values_list("level", flat=True)), ["all", "manufacturer", "model"])
        page = self.client.get(doc.get_absolute_url())
        self.assertContains(page, "All Equipment Units")
        self.assertContains(page, "LXV 1450")
        self.client.post(reverse("assignment_remove", args=[doc.assignments.get(level="all").pk]))
        self.assertEqual(doc.assignments.count(), 2)

    def test_arabic_page_is_translated(self):
        self.client.force_login(self.admin)
        self.client.cookies["lang"] = "ar"
        page = self.client.get(reverse("unit_document_add", args=[self.c1.pk]))
        self.assertContains(page, "من يحصل على هذا المستند")
        self.assertContains(page, "جميع وحدات هذا الطراز")

    def test_technician_sees_app_wide_document(self):
        doc = self.doc("Wide")
        DocumentAssignment.objects.create(document=doc, level="all")
        self.client.force_login(self.tech)
        self.assertEqual(self.client.get(doc.get_absolute_url()).status_code, 200)


class FileAndDataPermissions(Base):
    def test_anonymous_is_redirected_everywhere(self):
        doc = self.doc()
        for url in (reverse("document_file", args=[doc.pk]), reverse("unit_detail", args=[self.c1.pk]), reverse("dashboard")):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_technician_sees_only_assigned(self):
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=self.today, technician=self.tech)
        secret = self.doc("Secret")
        DocumentAssignment.objects.create(document=secret, level="unit", unit=self.b1)
        general = self.doc("General")
        DocumentAssignment.objects.create(document=general, level="category", category=self.chillers)
        self.client.force_login(self.tech)
        self.assertEqual(self.client.get(reverse("unit_detail", args=[self.c1.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("unit_detail", args=[self.b1.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("document_file", args=[secret.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("document_file", args=[general.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("contract_list")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("search"), {"q": "Chiller B"}), "Chiller B</a>")

    def test_viewer_is_read_only(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("unit_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("unit_new")).status_code, 403)
        self.assertEqual(self.client.post(reverse("unit_delete", args=[self.c1.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("document_upload")).status_code, 403)

    def test_inline_only_for_safe_types(self):
        html = self.doc("Page", b"<script>alert(1)</script>", "html")
        self.client.force_login(self.admin)
        r = self.client.get(reverse("document_file", args=[html.pk]))
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_pdf_preview_is_not_sandboxed(self):
        # Chrome blocks its PDF viewer inside a sandboxed iframe.
        pdf = self.doc("Preview")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("document_detail", args=[pdf.pk]))
        self.assertContains(page, '<iframe class="preview"')
        self.assertNotContains(page, "sandbox=")
        self.assertContains(page, reverse("document_file", args=[pdf.pk]) + '" target="_blank"')  # phones open it full screen

    def test_qr_requires_login_then_opens_profile(self):
        url = reverse("unit_scan", args=[self.c1.qr_token])
        self.assertIn("/login/", self.client.get(url).url)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).url, self.c1.get_absolute_url())
        self.assertIn(b"<svg", self.client.get(reverse("unit_qr", args=[self.c1.pk])).content)

    def test_login_lockout(self):
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "lock", "password": "bad"})
        User.objects.create_user("lock", password="right-password-1")
        r = self.client.post(reverse("login"), {"username": "lock", "password": "right-password-1"})
        self.assertEqual(r.status_code, 200)
        from django.core.cache import cache
        cache.clear()

    def test_lockout_behind_proxy_uses_the_visitors_own_address(self):
        from django.core.cache import cache
        User.objects.create_user("proxied", password="right-password-1")
        with self.settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")):
            for _ in range(5):  # a stranger guessing from the internet, through the same proxy
                self.client.post(reverse("login"), {"username": "proxied", "password": "bad"}, HTTP_X_FORWARDED_FOR="203.0.113.9")
            stranger = self.client.post(reverse("login"), {"username": "proxied", "password": "right-password-1"}, HTTP_X_FORWARDED_FOR="203.0.113.9")
            owner = self.client.post(reverse("login"), {"username": "proxied", "password": "right-password-1"}, HTTP_X_FORWARDED_FOR="203.0.113.9, 198.51.100.4")
        cache.clear()
        self.assertEqual(stranger.status_code, 200)  # still locked for the guesser
        self.assertEqual(owner.status_code, 302)  # the real user, from another address, gets in

    def test_one_refused_address_does_not_stop_other_emails(self):
        bad = User.objects.create_user("mail1", password="x", email="bad@example.com")
        good = User.objects.create_user("mail2", password="x", email="good@example.com")
        for u in (bad, good):
            Notification.objects.create(user=u, kind="visit", dedupe_key=f"k{u.pk}", message_en="m", message_ar="m")

        def fake_send(subject, body, sender, to):
            if to == ["bad@example.com"]:
                raise smtplib.SMTPRecipientsRefused({to[0]: (550, b"no such user")})

        with mock.patch("core.services.send_mail", side_effect=fake_send):
            send_pending_emails()
        self.assertIsNotNone(Notification.objects.get(user=good).emailed_at)
        self.assertIsNone(Notification.objects.get(user=bad).emailed_at)


class Scheduling(Base):
    def general(self, c, months=3, **kw):
        return Schedule.objects.create(contract=c, scope="general", interval_value=months, start_date=c.start_date, **kw)

    def individual(self, c, unit, months=1, **kw):
        return Schedule.objects.create(contract=c, scope="unit", unit=unit, interval_value=months, start_date=c.start_date, **kw)

    def setup_contract(self):
        c = self.contract()
        set_contract_units(c, [self.c1, self.c2], self.admin)
        return c

    def test_general_schedule_covers_all_units(self):
        c = self.setup_contract()
        self.general(c)
        sync_contract_visits(c)
        self.assertEqual(c.visits.filter(unit=self.c1).count(), 8)  # every 3 months over 24 months
        self.assertEqual(c.visits.filter(unit=self.c2).count(), 8)
        self.assertEqual(c.visits.first().source, "general")

    def test_individual_replaces_general_without_duplicates(self):
        c = self.setup_contract()
        self.general(c)
        sync_contract_visits(c)
        self.individual(c, self.c1, 1)
        sync_contract_visits(c)
        c1_visits = c.visits.filter(unit=self.c1)
        self.assertEqual(c1_visits.count(), 24)
        self.assertEqual(set(c1_visits.values_list("source", flat=True)), {"unit"})
        self.assertEqual(c1_visits.values("scheduled_date").distinct().count(), 24)
        self.assertEqual(set(c.visits.filter(unit=self.c2).values_list("source", flat=True)), {"general"})

    def test_sync_is_idempotent(self):
        c = self.setup_contract()
        self.general(c)
        sync_contract_visits(c)
        n = c.visits.count()
        sync_contract_visits(c)
        sync_contract_visits(c)
        self.assertEqual(c.visits.count(), n)

    def test_new_unit_auto_included_only_when_enabled(self):
        c = self.setup_contract()
        self.general(c)
        set_contract_units(c, [self.c1, self.c2, self.a1], self.admin)
        self.assertTrue(c.visits.filter(unit=self.a1).exists())
        c2 = self.contract("MC-2", client=self.client_b)
        set_contract_units(c2, [self.b1], self.admin)
        s = self.general(c2, include_new_units=False)
        sync_contract_visits(c2)
        self.assertFalse(c2.visits.exists())  # snapshot empty: nothing covered
        s.covered_units.add(self.b1)
        sync_contract_visits(c2)
        self.assertTrue(c2.visits.exists())

    def test_completed_and_rescheduled_survive_schedule_changes(self):
        c = self.setup_contract()
        s = self.general(c)
        sync_contract_visits(c)
        first, second = list(c.visits.filter(unit=self.c1).order_by("scheduled_date"))[:2]
        Visit.objects.filter(pk=first.pk).update(status="completed", actual_date=self.today)
        moved = second.scheduled_date + timedelta(days=5)
        Visit.objects.filter(pk=second.pk).update(scheduled_date=moved, status="rescheduled")
        s.interval_value = 6
        s.save()
        sync_contract_visits(c)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.scheduled_date, moved)  # not moved back, and not regenerated on its planned date
        self.assertEqual(c.visits.filter(unit=self.c1, planned_date=second.planned_date).count(), 1)

    def test_no_generation_for_inactive_or_beyond_coverage(self):
        c = self.setup_contract()
        self.general(c, months=1)
        sync_contract_visits(c)
        self.assertFalse(c.visits.filter(scheduled_date__gt=c.end_date).exists())
        Contract.objects.filter(pk=c.pk).update(status="suspended")
        c.refresh_from_db()
        close_open_contract_visits(c, "suspended")
        self.assertFalse(c.visits.exclude(status="cancelled").exists())
        draft = self.contract("MC-D", client=self.client_b, status="draft")
        set_contract_units(draft, [self.b1], self.admin)
        self.general(draft)
        sync_contract_visits(draft)
        self.assertFalse(draft.visits.exists())

    def test_overlapping_contract_is_rejected_and_removal_keeps_history(self):
        from django.core.exceptions import ValidationError
        c = self.setup_contract()
        other = self.contract("MC-X")
        with self.assertRaises(ValidationError):
            set_contract_units(other, [self.c1], self.admin)
        set_contract_units(c, [self.c2], self.admin)
        self.assertTrue(c.equipment_links.filter(unit=self.c1, removed_at__isnull=False).exists())
        self.assertTrue(Unit.objects.filter(pk=self.c1.pk).exists())

    def test_month_end_does_not_drift(self):
        c = Contract.objects.create(number="MC-J", title="T", client=self.client_b, start_date=date(2027, 1, 31), end_date=date(2027, 5, 31), status="active")
        s = Schedule(contract=c, scope="general", interval_value=1, start_date=date(2027, 1, 31))
        self.assertEqual(s.planned_dates()[:3], [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)])

    def test_status_display_matches_filter(self):
        today = self.today
        cases = {"overdue": (today - timedelta(days=1), "scheduled"), "due": (today, "scheduled"), "upcoming": (today + timedelta(days=3), "scheduled"),
                 "scheduled": (today + timedelta(days=30), "scheduled"), "rescheduled": (today + timedelta(days=2), "rescheduled"),
                 "in_progress": (today, "in_progress"), "completed": (today, "completed"), "cancelled": (today, "cancelled")}
        # A visit sent to a phone and never reported on becomes overdue once its date has passed.
        for expected, (d, base) in [*cases.items(), ("overdue", (today - timedelta(days=4), "in_progress"))]:
            v = Visit.objects.create(unit=self.b1, client=self.client_b, scheduled_date=d, status=base)
            self.assertEqual(v.display_status, expected)
            self.assertTrue(Visit.objects.with_status(expected).filter(pk=v.pk).exists(), expected)
            self.assertFalse(base == "in_progress" and expected == "overdue" and Visit.objects.with_status("in_progress").filter(pk=v.pk).exists())
            v.delete()


class Reminders(Base):
    def test_visit_reminder_exactly_seven_days_with_spec_wording(self):
        v = Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=date(2026, 12, 20), technician=self.tech)
        self.assertEqual(run_reminders(date(2026, 12, 12)), 0)  # 8 days before: nothing
        self.assertGreater(run_reminders(date(2026, 12, 13)), 0)
        n = Notification.objects.get(user=self.tech, visit=v)
        self.assertEqual(n.message_en, "Reminder: Preventive Maintenance for Chiller 01 at Hikma is scheduled in 7 days, on 20 December 2026.")
        self.assertIn("الحكمة", n.message_ar)
        self.assertFalse(Notification.objects.filter(user=self.other_tech).exists())
        self.assertTrue(Notification.objects.filter(user=self.engineer, visit=v).exists())

    def test_no_duplicates_on_rerun_and_new_reminder_after_reschedule(self):
        v = Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=date(2026, 12, 20), technician=self.tech)
        run_reminders(date(2026, 12, 13))
        self.assertEqual(run_reminders(date(2026, 12, 13)), 0)
        count = Notification.objects.filter(user=self.tech).count()
        self.assertEqual(count, 1)
        run_reminders(date(2026, 12, 14))
        self.assertEqual(Notification.objects.filter(user=self.tech).count(), 1)
        Visit.objects.filter(pk=v.pk).update(scheduled_date=date(2026, 12, 25), status="rescheduled")
        run_reminders(date(2026, 12, 18))
        self.assertEqual(Notification.objects.filter(user=self.tech).count(), 2)

    def test_catches_up_after_downtime(self):
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=date(2026, 12, 20), technician=self.tech)
        run_reminders(date(2026, 12, 16))  # worker was down on the 13th
        self.assertIn("in 4 days", Notification.objects.get(user=self.tech).message_en)

    def test_overdue_visits_are_for_foremen_and_admins_only(self):
        foreman = User.objects.create_user("fore2", password="x", role=Role.objects.get(name_en="Foreman"))
        v = Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=date(2026, 12, 10), technician=self.tech)
        run_reminders(date(2026, 12, 13)), run_reminders(date(2026, 12, 14))  # twice: still one notification each
        told = Notification.objects.filter(visit=v, dedupe_key__startswith="overdue:")
        self.assertEqual(set(told.values_list("user__username", flat=True)), {"fore2", "admin", "eng"})  # not the technician or a viewer
        self.assertEqual(told.get(user=foreman).message_en, "Overdue: Preventive Maintenance for Chiller 01 at Hikma was due on 10 December 2026 (3 days ago) and has no report yet.")
        Visit.objects.filter(pk=v.pk).update(scheduled_date=self.today - timedelta(days=2))
        for user, sees in ((self.viewer, False), (foreman, True)):
            self.client.force_login(user)
            board = [key for key, *_ in self.client.get(reverse("visit_list")).context["columns"]]
            dash = self.client.get(reverse("dashboard")).context
            self.assertEqual(("overdue" in board, any(c[0] == "Overdue Visits" for c in dash["attention_cards"]), bool(dash["overdue"])), (sees, sees, sees))
        for user, code in ((self.viewer, 403), (self.admin, 200)):  # a viewer can open other reports, not this one; foremen have no Reports menu
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("report_view", args=["visits-overdue"])).status_code, code)

    def test_closed_visits_get_no_reminder(self):
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=date(2026, 12, 20), technician=self.tech, status="cancelled")
        self.assertEqual(run_reminders(date(2026, 12, 13)), 0)

    def test_contract_reminder_thirty_days(self):
        c = Contract.objects.create(number="MC-2026-001", title="T", client=self.client_a, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), status="active")
        with self.subTest("31 days before"):
            self.assertEqual(run_reminders(date(2026, 11, 30)), 0)
        run_reminders(date(2026, 12, 1))
        n = Notification.objects.get(user=self.admin, contract=c)
        self.assertEqual(n.message_en, "Attention: Maintenance Contract MC-2026-001 for Hikma will expire in 30 days. Please review and initiate the renewal process.")
        self.assertEqual(run_reminders(date(2026, 12, 1)), 0)

    def test_settings_are_respected(self):
        from .models import SystemSettings
        SystemSettings.objects.update(visit_reminder_days=3)
        Visit.objects.create(unit=self.c1, client=self.client_a, scheduled_date=date(2026, 12, 20), technician=self.tech)
        self.assertEqual(run_reminders(date(2026, 12, 13)), 0)
        self.assertGreater(run_reminders(date(2026, 12, 17)), 0)

    def test_db_constraint_blocks_duplicates(self):
        Notification.objects.create(user=self.tech, kind="visit", dedupe_key="k", message_en="a", message_ar="a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(user=self.tech, kind="visit", dedupe_key="k", message_en="a", message_ar="a")


class Specs(Base):
    def test_unit_value_overrides_model_value(self):
        f = FieldDefinition.objects.create(category=self.chillers, name_en="Capacity", field_type="measurement", units="kW, TR")
        TechnicalValue.objects.create(field=f, model=self.lxv, value={"value": "450", "unit": "kW"})
        self.assertEqual(spec_rows(unit=self.c1)[0]["value"], "450 kW")
        self.assertTrue(spec_rows(unit=self.c1)[0]["from_model"])
        self.client.force_login(self.admin)
        r = self.client.post(reverse("unit_specs", args=[self.c1.pk]), {f"f_{f.pk}": "1.50", f"f_{f.pk}_unit": "TR"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(spec_rows(unit=self.c1)[0]["value"], "1.5 TR")
        self.assertEqual(spec_rows(unit=self.c2)[0]["value"], "450 kW")

    def test_fields_differ_by_category_and_subtype(self):
        shared = FieldDefinition.objects.create(category=self.chillers, name_en="Shared")
        only_screw = FieldDefinition.objects.create(category=self.chillers, subtype=self.screw, name_en="Screw only")
        FieldDefinition.objects.create(category=self.ahu, name_en="Airflow")
        self.assertEqual(list(FieldDefinition.applying_to(self.chillers.pk, self.screw.pk)), [shared, only_screw])
        self.assertEqual(list(FieldDefinition.applying_to(self.chillers.pk, self.scroll.pk)), [shared])

    def test_every_field_type_saves_and_displays(self):
        kinds = {"text": "abc", "integer": "4", "decimal": "2.5", "date": "2026-05-01", "select": "A", "boolean": "1"}
        defs = {k: FieldDefinition.objects.create(category=self.ahu, name_en=k, field_type=k, options="A\nB") for k in kinds}
        multi = FieldDefinition.objects.create(category=self.ahu, name_en="multi", field_type="multiselect", options="A\nB")
        self.client.force_login(self.admin)
        data = {f"f_{defs[k].pk}": v for k, v in kinds.items()} | {f"f_{multi.pk}": ["A", "B"]}
        self.assertEqual(self.client.post(reverse("unit_specs", args=[self.a1.pk]), data).status_code, 302)
        rows = {r["label"]: r["value"] for r in spec_rows(unit=self.a1)}
        self.assertEqual(rows, {"text": "abc", "integer": "4", "decimal": "2.5", "date": "1 May 2026", "select": "A", "boolean": "Yes", "multi": "A, B"})

    def test_field_with_values_is_deactivated_not_deleted(self):
        f = FieldDefinition.objects.create(category=self.chillers, name_en="X")
        TechnicalValue.objects.create(field=f, unit=self.c1, value="1")
        self.client.force_login(self.admin)
        self.client.post(reverse("field_delete", args=[f.pk]))
        f.refresh_from_db()
        self.assertFalse(f.is_active)

    def test_suggested_templates_load_once(self):
        added = load_suggested()
        self.assertGreater(added, 50)
        self.assertEqual(load_suggested(), 0)


class CatalogDeletion(Base):
    def test_linked_subtype_needs_reassign_or_archive(self):
        self.client.force_login(self.admin)
        url = reverse("subtype_delete", args=[self.screw.pk])
        self.client.post(url, {"choice": "delete"})
        self.assertTrue(Subtype.objects.filter(pk=self.screw.pk).exists())
        doc = self.doc()
        DocumentAssignment.objects.create(document=doc, level="subtype", subtype=self.screw)
        self.client.post(url, {"choice": "reassign", "target": self.scroll.pk})
        self.assertFalse(Subtype.objects.filter(pk=self.screw.pk).exists())
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.subtype, self.scroll)
        self.assertEqual(doc.assignments.get().subtype, self.scroll)

    def test_category_reassign_and_archive(self):
        self.client.force_login(self.admin)
        url = reverse("category_delete", args=[self.ahu.pk])
        self.client.post(url, {"choice": "archive"})
        self.ahu.refresh_from_db()
        self.assertFalse(self.ahu.is_active)
        self.assertTrue(Unit.objects.filter(pk=self.a1.pk).exists())
        self.client.post(url, {"choice": "reassign", "target": self.chillers.pk})
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.category, self.chillers)
        self.assertFalse(Category.objects.filter(pk=self.ahu.pk).exists())

    def test_client_with_equipment_cannot_be_deleted(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("client_delete", args=[self.client_a.pk]))
        self.assertTrue(Client.objects.filter(pk=self.client_a.pk).exists())


class Pages(Base):
    def populate(self):
        c = self.contract()
        set_contract_units(c, [self.c1, self.c2], self.admin)
        Schedule.objects.create(contract=c, scope="general", interval_value=1, start_date=c.start_date)
        sync_contract_visits(c)
        doc = self.doc()
        DocumentAssignment.objects.create(document=doc, level="category", category=self.chillers)
        FieldDefinition.objects.create(category=self.chillers, name_en="Cap", field_type="measurement", units="kW")
        run_reminders()
        return c, doc, c.visits.first()

    def test_every_page_renders_in_both_languages(self):
        c, doc, v = self.populate()
        self.client.force_login(self.admin)
        f = FieldDefinition.objects.first()
        urls = [reverse(n) for n in ("dashboard", "unit_list", "unit_new", "catalog", "category_new", "client_list", "client_new", "manufacturer_list", "manufacturer_new",
                                     "model_new", "document_list", "document_upload", "contract_list", "contract_new", "calendar", "visit_list", "visit_new", "notifications",
                                     "report_list", "user_list", "user_new", "role_list", "role_new", "settings", "activity", "country_new", "doctype_new", "password_change")]
        urls += [reverse(n, args=[a]) for n, a in [
            ("unit_detail", self.c1.pk), ("unit_edit", self.c1.pk), ("unit_specs", self.c1.pk), ("category_detail", self.chillers.pk), ("category_edit", self.chillers.pk),
            ("category_delete", self.chillers.pk), ("subtype_edit", self.screw.pk), ("subtype_delete", self.screw.pk), ("client_detail", self.client_a.pk), ("client_edit", self.client_a.pk),
            ("manufacturer_detail", self.ciat.pk), ("manufacturer_edit", self.ciat.pk), ("model_detail", self.lxv.pk), ("model_edit", self.lxv.pk), ("model_specs", self.lxv.pk),
            ("document_detail", doc.pk), ("document_edit", doc.pk), ("contract_detail", c.pk), ("contract_edit", c.pk), ("contract_equipment", c.pk), ("contract_renew", c.pk),
            ("schedule_new", c.pk), ("visit_detail", v.pk), ("visit_edit", v.pk), ("visit_report", v.pk), ("user_edit", self.tech.pk), ("role_edit", self.tech.role_id)]]
        urls += [reverse("field_new", args=[self.chillers.pk]), reverse("field_edit", args=[self.chillers.pk, f.pk]), reverse("schedule_edit", args=[c.pk, c.schedules.first().pk]), reverse("subtype_new") + f"?category={self.chillers.pk}"]
        urls += [reverse("calendar") + f"?view={x}" for x in ("week", "day", "list")] + [reverse("calendar") + "?view=list&quick=week", reverse("visit_list") + "?quick=month&status=overdue", reverse("search") + "?q=SER"]
        from .views.reports import REPORTS
        urls += [reverse("report_view", args=[k]) for k in REPORTS]
        for lang, marker in (("en", 'dir="ltr"'), ("ar", 'dir="rtl"')):
            self.client.cookies["lang"] = lang
            for url in urls:
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, f"{lang} {url}")
                self.assertContains(r, marker)

    def test_arabic_is_actually_applied(self):
        self.client.force_login(self.admin)
        self.client.cookies["lang"] = "ar"
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "لوحة التحكم")
        self.assertNotContains(r, ">Dashboard<")

    def test_exports(self):
        self.populate()
        self.client.force_login(self.admin)
        from .views.reports import REPORTS
        for lang in ("en", "ar"):
            self.client.cookies["lang"] = lang
            for k in REPORTS:
                x = self.client.get(reverse("report_view", args=[k]), {"export": "xlsx"})
                self.assertEqual(x.content[:2], b"PK", k)
                p = self.client.get(reverse("report_view", args=[k]), {"export": "pdf"})
                self.assertEqual(p.content[:4], b"%PDF", k)

    def test_partial_serial_search_groups_results(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("search"), {"q": "3456"})
        self.assertContains(r, "Chiller 01")
        self.assertNotContains(r, "Chiller 02</a>")

    def test_visit_lifecycle(self):
        c, doc, v = self.populate()
        self.client.force_login(self.tech)
        Visit.objects.filter(pk=v.pk).update(technician=self.tech)
        # Sending a visit to a phone is the office's job: a technician cannot hand his visit to someone else.
        self.assertEqual(self.client.post(reverse("visit_start", args=[v.pk]), {"technician": self.tech.pk}).status_code, 403)
        self.client.force_login(self.engineer)
        self.client.post(reverse("visit_start", args=[v.pk]), {"technician": self.tech.pk})
        self.client.force_login(self.tech)
        v.refresh_from_db()
        self.assertEqual(v.status, "in_progress")
        self.assertEqual(self.client.post(reverse("visit_cancel", args=[v.pk]), {"reason": "x"}).status_code, 403)
        self.client.post(reverse("visit_complete", args=[v.pk]), {"actual_date": self.today.isoformat(), "notes": "done"})
        v.refresh_from_db()
        self.assertEqual(v.status, "completed")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("visit_edit", args=[v.pk])).status_code, 302)
        self.client.post(reverse("visit_cancel", args=[v.pk]), {"reason": "x"})
        v.refresh_from_db()
        self.assertEqual(v.status, "completed")

    def test_renewal_copies_units_and_schedule_as_draft(self):
        c, _, _ = self.populate()
        self.client.force_login(self.admin)
        start = c.end_date + timedelta(days=1)
        r = self.client.post(reverse("contract_renew", args=[c.pk]), {"number": "MC-1-R", "title": "T", "start_date": start.isoformat(), "end_date": add_months(start, 12).isoformat(), "currency": "JOD", "client": c.client_id})
        self.assertEqual(r.status_code, 302)
        new = Contract.objects.get(number="MC-1-R")
        self.assertEqual(new.status, "draft")
        self.assertEqual(new.active_units.count(), 2)
        self.assertEqual(new.schedules.count(), 1)
        self.assertFalse(new.visits.exists())


class ReviewFixes(Base):
    """One test per finding from the code review, so these bugs cannot quietly come back."""
    def contract_with_schedule(self, **schedule_kw):
        c = self.contract()
        set_contract_units(c, [self.c1], self.admin)
        s = Schedule.objects.create(contract=c, scope="general", interval_value=3, start_date=c.start_date, **schedule_kw)
        sync_contract_visits(c)
        return c, s

    def schedule_post(self, c, s, **changes):
        data = {"scope": "general", "frequency": "3", "start_date": c.start_date.isoformat(), "include_new_units": "on", "is_active": "on", **changes}
        return self.client.post(reverse("schedule_edit", args=[c.pk, s.pk]), data)

    def test_worker_survives_a_database_outage(self):
        down = OperationalError("database is down")
        with mock.patch.object(worker, "run_reminders", side_effect=down), mock.patch.object(worker.SystemSettings, "get", side_effect=down), \
                mock.patch.object(worker.SystemSettings.objects, "filter", side_effect=down), self.assertLogs("core.management.commands.worker", "ERROR"):
            worker.run_pass()  # must return, because nothing restarts the worker

    def test_field_type_is_locked_once_values_exist(self):
        f = FieldDefinition.objects.create(category=self.chillers, name_en="Refrigerant")
        data = {"name_en": "Refrigerant", "field_type": "date", "is_active": "on"}
        url = reverse("field_edit", args=[self.chillers.pk, f.pk])
        self.client.force_login(self.admin)
        TechnicalValue.objects.create(field=f, unit=self.c1, value="R-134a")
        self.assertEqual(self.client.post(url, data).status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.field_type, "text")
        self.assertEqual(self.client.get(reverse("unit_detail", args=[self.c1.pk])).status_code, 200)
        f.values.all().delete()
        self.assertEqual(self.client.post(url, data).status_code, 302)  # with no values the type is still free to change

    def test_fields_of_different_types_do_not_merge(self):
        text = FieldDefinition.objects.create(category=self.chillers, subtype=self.screw, name_en="Installed")
        FieldDefinition.objects.create(category=self.chillers, subtype=self.scroll, name_en="Installed", field_type="date")
        TechnicalValue.objects.create(field=text, unit=self.c1, value="last spring")
        reassign_subtype(self.screw, self.scroll)
        self.c1.refresh_from_db()
        self.assertIn("last spring", [r["value"] for r in spec_rows(unit=self.c1)])

    def test_changing_schedule_people_moves_future_visits_but_not_manual_ones(self):
        c, s = self.contract_with_schedule(technician=self.tech)
        visits = list(c.visits.order_by("scheduled_date"))
        Visit.objects.filter(pk=visits[1].pk).update(technician=self.viewer)  # reassigned by hand
        Visit.objects.filter(pk=visits[2].pk).update(status="completed", actual_date=self.today)
        self.client.force_login(self.admin)
        self.assertEqual(self.schedule_post(c, s, technician=self.other_tech.pk).status_code, 302)
        people = {v.pk: v.technician_id for v in c.visits.all()}
        self.assertEqual(people[visits[0].pk], self.other_tech.pk)
        self.assertEqual(people[visits[1].pk], self.viewer.pk)
        self.assertEqual(people[visits[2].pk], self.tech.pk)

    def test_individual_schedule_brings_its_people(self):
        c, s = self.contract_with_schedule(technician=self.tech)
        Schedule.objects.create(contract=c, scope="unit", unit=self.c1, interval_value=3, start_date=c.start_date, technician=self.other_tech)
        sync_contract_visits(c)
        self.assertEqual(set(c.visits.values_list("technician_id", flat=True)), {self.other_tech.pk})

    def test_excel_keeps_empty_cells_empty_and_quotes_formulas(self):
        Unit.objects.create(name_en="=1+1", client=self.client_a, category=self.chillers, model=self.lxv)  # no subtype, no serial
        self.client.force_login(self.admin)
        r = self.client.get(reverse("report_view", args=["equipment-client"]), {"export": "xlsx"})
        values = [c.value for row in load_workbook(io.BytesIO(r.content)).active.iter_rows(min_row=2) for c in row]
        self.assertNotIn("'", values)
        self.assertIn("'=1+1", values)

    def test_archiving_a_unit_stops_and_restoring_brings_back_its_visits(self):
        c, _ = self.contract_with_schedule(technician=self.tech)
        worked_on = c.visits.order_by("scheduled_date").first()
        Visit.objects.filter(pk=worked_on.pk).update(notes="parts ordered")
        count = c.visits.count()
        self.client.force_login(self.admin)
        self.client.post(reverse("unit_archive", args=[self.c1.pk]))
        self.assertEqual(list(c.visits.all()), [worked_on])  # worked-on visits are kept, the rest are cleared
        self.assertEqual(run_reminders(worked_on.scheduled_date), 0)
        self.client.post(reverse("unit_archive", args=[self.c1.pk]))
        self.assertEqual(c.visits.count(), count)

    def test_blocked_delete_leaves_no_log_entry(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("client_delete", args=[self.client_a.pk]))
        self.assertTrue(Client.objects.filter(pk=self.client_a.pk).exists())
        self.assertFalse(ActivityLog.objects.filter(action="deleted", target_type="Client").exists())

    def test_report_dates_filter_contracts_and_are_hidden_on_equipment(self):
        for number, end in (("OLD", date(2024, 6, 1)), ("NEW", date(2025, 6, 1))):
            Contract.objects.create(number=number, title="T", client=self.client_a, start_date=date(2024, 1, 1), end_date=end, status="active")
        self.client.force_login(self.admin)
        r = self.client.get(reverse("report_view", args=["contracts-expired"]), {"start": "2025-01-01"})
        self.assertContains(r, "NEW")
        self.assertNotContains(r, "OLD")
        self.assertNotContains(self.client.get(reverse("report_view", args=["equipment-client"])), 'name="start"')

    def test_no_visit_before_the_contract_starts(self):
        start = self.today + timedelta(days=60)
        c = Contract.objects.create(number="MC-S", title="T", client=self.client_a, start_date=start, end_date=start + timedelta(days=200), status="active")
        s = Schedule.objects.create(contract=c, scope="general", interval_value=1, start_date=self.today + timedelta(days=30))
        self.assertTrue(s.planned_dates())
        self.assertGreaterEqual(min(s.planned_dates()), start)

    def test_reschedule_rejects_past_dates_and_double_booking(self):
        c, _ = self.contract_with_schedule()
        first, second = list(c.visits.order_by("scheduled_date"))[:2]
        self.client.force_login(self.admin)
        url = reverse("visit_reschedule", args=[second.pk])
        for bad in (self.today - timedelta(days=1), first.scheduled_date):
            self.client.post(url, {"scheduled_date": bad.isoformat()})
            second.refresh_from_db()
            self.assertEqual(second.status, "scheduled", bad)
        good = second.scheduled_date + timedelta(days=1)
        self.client.post(url, {"scheduled_date": good.isoformat()})
        second.refresh_from_db()
        self.assertEqual((second.status, second.scheduled_date), ("rescheduled", good))

    def test_renewed_contract_stops_asking_for_renewal(self):
        old = Contract.objects.create(number="OLD", title="T", client=self.client_a, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), status="active")
        Contract.objects.create(number="NEW", title="T", client=self.client_a, start_date=date(2027, 1, 1), end_date=date(2027, 12, 31), status="draft", renewed_from=old)
        Contract.objects.filter(number="NEW").update(status="active")
        run_reminders(date(2026, 12, 1))
        self.assertFalse(Notification.objects.filter(contract=old).exists())

    def test_unit_cannot_change_client_on_a_live_contract(self):
        from .forms import UnitForm
        c = self.contract()
        set_contract_units(c, [self.c1], self.admin)
        self.assertTrue(UnitForm(instance=self.c1).fields["client"].disabled)
        self.assertFalse(UnitForm(instance=self.c2).fields["client"].disabled)
        Contract.objects.filter(pk=c.pk).update(status="terminated")
        self.assertFalse(UnitForm(instance=Unit.objects.get(pk=self.c1.pk)).fields["client"].disabled)

    def test_dashboard_counts_match_the_lists_they_open(self):
        SystemSettings.objects.update(contract_reminder_days=60)
        Contract.objects.create(number="SOON", title="T", client=self.client_a, start_date=self.today, end_date=self.today + timedelta(days=45), status="active")
        Contract.objects.create(number="LATER", title="T", client=self.client_b, start_date=self.today, end_date=self.today + timedelta(days=300), status="active")
        self.client.force_login(self.admin)
        cards = {row[0]: row[1] for row in self.client.get(reverse("dashboard")).context["maintenance_cards"]}
        self.assertEqual(cards["Contracts Expiring Soon"], 1)
        self.assertEqual(cards["Active Maintenance Contracts"], 1)

    def test_login_uses_saved_language_only_on_a_new_browser(self):
        User.objects.create_user("arabic", password="right-password-1", language="ar")
        data = {"username": "arabic", "password": "right-password-1"}
        self.assertEqual(self.client.post(reverse("login"), data).cookies["lang"].value, "ar")
        self.client.logout()
        self.client.cookies["lang"] = "en"
        self.assertNotIn("lang", self.client.post(reverse("login"), data).cookies)

    def test_deleting_a_unit_deletes_its_files(self):
        unit = Unit.objects.create(name_en="Temp", client=self.client_a, category=self.chillers, model=self.lxv)
        att = Attachment.objects.create(file=SimpleUploadedFile("p.png", b"x"), original_name="p.png", unit=unit)
        storage, name = att.file.storage, att.file.name
        self.assertTrue(storage.exists(name))
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("unit_delete", args=[unit.pk]))
        self.assertFalse(Unit.objects.filter(pk=unit.pk).exists())
        self.assertFalse(storage.exists(name))

    def test_login_redirect_keeps_the_whole_address(self):
        r = self.client.get(reverse("visit_list"), {"client": 1, "status": "overdue"})
        self.assertEqual(self.client.get(r.url).context["next"], "/visits/?client=1&status=overdue")

    def test_renewal_shifts_manual_dates(self):
        c = self.contract(months=12)
        set_contract_units(c, [self.c1], self.admin)
        d = c.start_date + timedelta(days=10)
        Schedule.objects.create(contract=c, scope="general", mode="manual", manual_dates=[d.isoformat()], start_date=d)
        from .services import copy_for_renewal
        start = c.end_date + timedelta(days=1)
        new = Contract.objects.create(number="MC-R", title="T", client=self.client_a, start_date=start, end_date=add_months(start, 12) - timedelta(days=1))
        copy_for_renewal(c, new, self.admin)
        self.assertEqual(new.schedules.get().manual_dates, [add_months(d, 12).isoformat()])


class Sorting(Base):
    def test_clients_page_ranks_by_units_and_hides_empty_clients(self):
        Client.objects.create(name_en="Empty Co", country=self.jordan)
        Client.objects.create(name_en="aaa small", country=self.jordan)
        Unit.objects.create(name_en="Small unit", client=Client.objects.get(name_en="aaa small"), category=self.chillers, model=self.lxv)
        self.client.force_login(self.admin)
        names = lambda r: [row[0] for row in r.context["table"]["rows"]]
        rows = names(self.client.get(reverse("client_list"), {"view": "list"}))
        self.assertEqual(len(rows), 3)
        self.assertIn("Hikma", rows[0])  # 3 units
        self.assertIn("aaa small", rows[1])  # 1 unit each: a before O, whatever the case
        self.assertIn("Other Co", rows[2])
        self.assertIn("Empty Co", names(self.client.get(reverse("client_list"), {"q": "Empty"}))[0])  # still reachable

    def test_a_new_category_shows_before_it_has_units(self):
        Category.objects.create(name_en="Ice Machines Test", name_ar="آلات الثلج")
        self.client.force_login(self.admin)
        cards = self.client.get(reverse("unit_list")).context["cards"]
        self.assertEqual(next(c["stats"][0][0] for c in cards if c["category"].name_en == "Ice Machines Test"), 0)

    def test_equipment_opens_on_types_then_subtypes_then_units(self):
        Unit.objects.create(name_en="Loose unit", client=self.client_a, category=self.chillers, model=self.lxv)
        self.client.force_login(self.admin)
        card = next(c for c in self.client.get(reverse("unit_list")).context["cards"] if c["category"] == self.chillers)
        page = self.client.get(reverse("unit_type", args=[self.chillers.pk])).context
        self.assertEqual(card["units"], page["unit_total"])
        self.assertEqual(card["units"], sum(i["n"] for i in page["items"]))
        self.assertEqual(page["items"][-1]["url"], reverse("unit_subtype", args=[self.chillers.pk, 0]))  # no unit is unreachable
        loose = self.client.get(page["items"][-1]["url"]).context["units"]
        self.assertEqual([r["unit"].name_en for r in loose if r["unit"].name_en == "Loose unit"], ["Loose unit"])
        self.assertEqual(self.client.get(reverse("unit_subtype", args=[self.ahu.pk, self.screw.pk])).status_code, 404)  # subtype of another type

    def test_clients_open_on_countries_then_sites(self):
        Client.objects.create(name_en="Empty Co", country=self.jordan)
        self.client.force_login(self.admin)
        r = self.client.get(reverse("client_list"))
        card = next(c for c in r.context["cards"] if c["country"] == self.jordan)
        sites = self.client.get(reverse("client_country", args=[self.jordan.pk])).context["sites"]
        self.assertEqual(card["sites"], len(sites))
        self.assertEqual(card["units"], sum(s.n_units for s in sites))
        self.assertNotIn("Empty Co", [s.name_en for s in sites])
        self.assertContains(self.client.get(reverse("client_list"), {"view": "list"}), f'class="back-link" href="{reverse("client_list")}"')
        self.assertContains(self.client.get(reverse("unit_list"), {"view": "list"}), f'class="back-link" href="{reverse("unit_list")}"')

    def test_lists_are_a_to_z_ignoring_case(self):
        Client.objects.create(name_en="zeta", country=self.jordan)
        Client.objects.create(name_en="Beta", country=self.jordan)
        Client.objects.create(name_en="alpha", country=self.jordan)
        self.assertEqual([c.name_en for c in Client.objects.filter(name_en__in=["zeta", "Beta", "alpha"])], ["alpha", "Beta", "zeta"])
        self.assertEqual([u.name_en for u in Unit.objects.all()], ["AHU 01", "Chiller 01", "Chiller 02", "Chiller B"])


class Backups(TransactionTestCase):
    """Commits for real, because pg_dump only sees committed rows. Uses only the test database and temp folders."""
    def setUp(self):
        self.assertTrue(settings.DATABASES["default"]["NAME"].startswith("test_"))
        self.backups = Path(tempfile.mkdtemp())
        self.files = Path(tempfile.mkdtemp())
        (self.files / "2026").mkdir()
        (self.files / "2026" / "manual.pdf").write_bytes(b"%PDF restored")
        overrides = override_settings(BACKUP_ROOT=self.backups, PRIVATE_ROOT=self.files)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def test_backup_can_actually_be_restored(self):
        Country.objects.create(name_en="Restoria", code="RS")
        archive = backup.run_backup()
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read("files/2026/manual.pdf"), b"%PDF restored")
            dump_name = next(n for n in z.namelist() if n.endswith(".dump"))
            z.extract(dump_name, self.backups)
        db = settings.DATABASES["default"]
        target = "hvac_restore_test"
        login = {"host": db["HOST"], "port": db["PORT"], "user": db["USER"], "password": db["PASSWORD"]}
        with psycopg.connect(dbname="postgres", autocommit=True, **login) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {target}")
            admin.execute(f"CREATE DATABASE {target}")
        try:
            pg_restore = Path(backup.find_pg_dump()).with_name("pg_restore.exe" if os.name == "nt" else "pg_restore")
            subprocess.run([str(pg_restore), "-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], "-d", target, str(self.backups / dump_name)],
                           check=True, env={**os.environ, "PGPASSWORD": db["PASSWORD"]}, capture_output=True, text=True)
            with psycopg.connect(dbname=target, **login) as restored:
                self.assertEqual(restored.execute("SELECT count(*) FROM core_country WHERE name_en = 'Restoria'").fetchone()[0], 1)
        finally:
            with psycopg.connect(dbname="postgres", autocommit=True, **login) as admin:
                admin.execute(f"DROP DATABASE IF EXISTS {target}")

    def test_many_backups_in_one_day_keep_the_older_days(self):
        for day in range(1, 21):
            (self.backups / f"backup-202601{day:02d}-020000.zip").write_bytes(b"daily")
        for hour in range(10, 30):  # twenty manual backups on the last day
            (self.backups / f"backup-20260120-{hour % 24:02d}{hour // 24:02d}00.zip").write_bytes(b"manual")
        backup.prune(self.backups)
        days = Counter(p.name[7:15] for p in self.backups.iterdir())
        self.assertEqual(sorted(days), [f"202601{d:02d}" for d in range(7, 21)])  # the newest 14 days
        self.assertEqual(days.pop("20260120"), 21)  # every backup of the latest day, so each change can still be undone
        self.assertEqual(set(days.values()), {1})  # one per earlier day

    def test_failed_backup_leaves_nothing_that_could_push_out_good_ones(self):
        good = self.backups / "backup-20260101-000000.zip"
        good.write_bytes(b"good")
        with mock.patch.object(zipfile.ZipFile, "write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            backup.run_backup()
        self.assertEqual([p.name for p in self.backups.iterdir()], [good.name])


class Translations(TestCase):
    def test_every_template_string_has_arabic(self):
        patterns = [r"""{%\s*t\s+"([^"]+)"\s*%}""", r"""{%\s*t\s+'([^']+)'\s*%}""", r"""{%\s*badge\s+\S+\s+"([^"]+)"\s*%}""", r"""heading="([^"]+)\""""]
        used = set()
        for f in (settings.BASE_DIR / "templates").rglob("*.html"):
            for p in patterns:
                used |= set(re.findall(p, f.read_text(encoding="utf-8")))
        self.assertEqual(sorted(used - set(AR)), [])

    def test_every_python_string_passed_to_t_has_arabic(self):
        used = set()
        for f in (settings.BASE_DIR / "core").rglob("*.py"):
            if "migrations" not in f.parts and f.name != "tests.py":
                used |= set(re.findall(r"""\bt\("([^"]+)"\)""", f.read_text(encoding="utf-8")))
        self.assertEqual(sorted(used - set(AR)), [])

    def test_model_labels_and_choices_have_arabic(self):
        from django.apps import apps
        missing = set()
        for model in apps.get_app_config("core").get_models():
            for field in model._meta.fields:
                if field.choices and field.name != "language":
                    missing |= {str(label) for _, label in field.choices} - set(AR)
        for label in {**Contract.STATUS_LABELS, **Visit.STATUS_LABELS}.values():
            if label not in AR:
                missing.add(label)
        self.assertEqual(sorted(missing), [])

    def test_permission_labels_have_arabic(self):
        from .perms import PERMISSION_LABELS, PERMISSIONS
        self.assertEqual(sorted((set(PERMISSION_LABELS.values()) | set(PERMISSIONS)) - set(AR)), [])

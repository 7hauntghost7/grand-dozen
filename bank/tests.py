"""Django tests:  python manage.py test"""
import json
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .image_storage import delete_question_image, upload_question_image
from .models import (
    Comment, ImageTarget, Option, Question, QuestionBank, QuestionImage,
    SavedQuestion, Subject, SubjectSection,
)

SAMPLE = """Q: Pick B
A. one
B. two
ANSWER: B
EXPLANATION: because

Q: Statements
TYPE: TF
A. yes
B. no
ANSWER: TF
"""
DESCRIPTIVE_SAMPLE = """Q: Explain the resting membrane potential.
TYPE: DESC
ANSWER: The voltage difference across a cell membrane at rest.
"""


def give(user, *codenames):
    for c in codenames:
        user.user_permissions.add(Permission.objects.get(codename=c))


class Base(TestCase):
    def setUp(self):
        self.subject = Subject.objects.create(name="Physiology")
        self.editor = User.objects.create_user("ed", password="pw")
        give(self.editor, "add_question", "change_question", "delete_question")
        self.student = User.objects.create_user("stu", password="pw")

    def import_sample(self, title="Haematology", semester=1):
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "semester": semester, "deck_title": title, "skip_dupes": "1"})
        self.client.logout()
        return QuestionBank.objects.get(title=title)

    def post_json(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")


class SubjectBackupTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("backup-admin", "admin@example.com", "secret")

    def test_backup_page_and_download_are_staff_only_and_include_answer_content(self):
        deck = self.import_sample()
        descriptive = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe resting potential.",
            answer="A voltage difference at rest.", explanation="Explanation text.", note="Source note.",
        )
        second_subject = Subject.objects.create(name="Anatomy")
        second_deck = QuestionBank.objects.create(
            subject=second_subject, semester=2, title="Anatomy basics",
        )
        Question.objects.create(
            bank=second_deck, kind=Question.DESC,
            stem="Name the body system.", answer="The skeletal system.",
        )
        anonymous = self.client.get(reverse("bank:subject_backup"))
        self.assertEqual(anonymous.status_code, 302)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("bank:subject_backup")).status_code, 302)

        self.client.force_login(self.admin)
        page = self.client.get(reverse("bank:subject_backup"))
        self.assertContains(page, "Download backup")
        self.assertContains(page, "never edits or deletes existing content")
        self.assertContains(page, "Backup")
        response = self.client.get(reverse("bank:export_subject_backup"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("grand-dozen-subject-backup.json", response["Content-Disposition"])
        payload = json.loads(response.content)
        self.assertEqual(
            {subject["name"] for subject in payload["subjects"]},
            {"Physiology", "Anatomy"},
        )
        subject_data = next(item for item in payload["subjects"] if item["name"] == "Physiology")
        deck_data = subject_data["decks"][0]
        self.assertEqual(subject_data["name"], self.subject.name)
        self.assertEqual(deck_data["title"], deck.title)
        self.assertIn(descriptive.answer, json.dumps(payload))
        self.assertEqual(
            {question["kind"] for question in deck_data["questions"]},
            {Question.MCQ, Question.TF, Question.DESC},
        )
        mcq = next(q for q in deck_data["questions"] if q["kind"] == Question.MCQ)
        self.assertEqual(len(mcq["options"]), 2)
        self.assertTrue(any(option["is_correct"] for option in mcq["options"]))

    def test_restore_adds_deleted_content_and_skips_matching_records(self):
        deck = self.import_sample()
        self.client.force_login(self.admin)
        backup = self.client.get(reverse("bank:export_subject_backup"))
        payload = json.loads(backup.content)
        original_questions = list(deck.questions.order_by("pk"))
        removed_question = original_questions[0]
        untouched_question = original_questions[1]
        original_max_pk = max(question.pk for question in original_questions)
        untouched_stem = untouched_question.stem
        removed_id = removed_question.pk
        removed_question.delete()

        upload = SimpleUploadedFile("backup.json", backup.content, content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        restored_question = Question.objects.get(pk=removed_id)
        self.assertEqual(restored_question.stem, payload["subjects"][0]["decks"][0]["questions"][0]["stem"])
        self.assertEqual(restored_question.options.count(), 2)
        new_question = Question.objects.create(bank=deck, stem="Created after restore")
        self.assertGreater(new_question.pk, original_max_pk)
        untouched_question.refresh_from_db()
        self.assertEqual(untouched_question.stem, untouched_stem)
        self.assertContains(response, "Added 0 subjects, 0 decks, 1 question, 2 options")
        self.assertContains(response, "skipped")

    def test_conflict_rejects_entire_import_without_overwriting_or_partial_restore(self):
        deck = self.import_sample()
        self.client.force_login(self.admin)
        backup = self.client.get(reverse("bank:export_subject_backup"))
        questions = list(deck.questions.order_by("pk"))
        missing_question_id = questions[1].pk
        questions[1].delete()
        questions[0].stem = "Changed after the backup"
        questions[0].save(update_fields=["stem"])

        upload = SimpleUploadedFile("backup.json", backup.content, content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertContains(response, "already exists with different content")
        self.assertFalse(Question.objects.filter(pk=missing_question_id).exists())
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Question.objects.get(pk=questions[0].pk).stem, "Changed after the backup")

    def test_invalid_backup_does_not_write_and_import_is_staff_only(self):
        self.client.force_login(self.student)
        denied = self.client.post(reverse("bank:import_subject_backup"), {})
        self.assertEqual(denied.status_code, 302)

        self.client.force_login(self.admin)
        upload = SimpleUploadedFile("backup.json", b"not JSON", content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertContains(response, "not valid UTF-8 JSON")
        self.assertEqual(Subject.objects.count(), 1)
        self.assertEqual(QuestionBank.objects.count(), 0)

    def test_upload_size_limit_is_enforced(self):
        self.client.force_login(self.admin)
        upload = SimpleUploadedFile("backup.json", b"{}", content_type="application/json")
        with patch("bank.views.MAX_BACKUP_BYTES", 1):
            response = self.client.post(
                reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
            )
        self.assertContains(response, "exceeds the 100 MB limit")
        self.assertEqual(Subject.objects.count(), 1)
        with patch("bank.subject_backup.MAX_BACKUP_BYTES", 1):
            response = self.client.get(reverse("bank:export_subject_backup"))
        self.assertEqual(response.status_code, 413)


class PwaTests(TestCase):
    def test_manifest_and_service_worker_are_public(self):
        manifest_response = self.client.get(reverse("bank:manifest"))
        self.assertEqual(manifest_response.status_code, 200)
        self.assertEqual(manifest_response["Content-Type"], "application/manifest+json")
        manifest = json.loads(b"".join(manifest_response.streaming_content))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})

        worker_response = self.client.get(reverse("bank:service_worker"))
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(worker_response["Service-Worker-Allowed"], "/")
        worker = b"".join(worker_response.streaming_content)
        self.assertIn(b"/static/", worker)
        self.assertNotIn(b"/api/", worker)

    def test_install_banner_only_renders_for_authenticated_users(self):
        login_page = self.client.get(reverse("bank:login"))
        self.assertNotContains(login_page, 'id="pwa-install"')
        self.assertContains(login_page, 'alt="Rawasi — Batch 101"')
        user = User.objects.create_user("pwa-user", password="pw")
        self.client.force_login(user)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, 'id="pwa-install"')
        self.assertContains(home, "Install Rawasi 101")
        self.assertContains(home, 'alt="Rawasi — Batch 101"')
        self.assertContains(home, 'class="theme-toggle"')
        bank = QuestionBank.objects.create(subject=Subject.objects.create(name="PWA"), title="Install test")
        deck = self.client.get(bank.get_absolute_url())
        self.assertNotContains(deck, 'id="pwa-install"')


class SubjectSectionSeedTests(TestCase):
    def test_seed_subjects_creates_default_anatomy_sections(self):
        call_command("seed_subjects", stdout=io.StringIO())
        anatomy = Subject.objects.get(slug="anatomy")
        self.assertEqual(
            list(anatomy.sections.values_list("name", flat=True)),
            ["Embryology", "Histology", "Gross Anatomy"],
        )


class ReviewWorkflowTests(Base):
    def make_staff_user(self):
        return User.objects.create_user("review-admin", password="review-test-password", is_staff=True)

    def test_review_mode_and_edit_endpoint_are_staff_only(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("bank:review_deck", args=[deck.slug])).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?review=1").status_code,
            403,
        )
        question = deck.questions.first()
        self.assertEqual(
            self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {}).status_code,
            403,
        )

    def test_deck_has_separate_review_actions_for_each_question_type(self):
        deck = self.import_sample()
        Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe a teacup.", answer="A very small bathtub."
        )
        self.client.force_login(self.make_staff_user())
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(
            page,
            f'href="{reverse("bank:quiz", args=[deck.slug])}?type=mcq_tf" '
            'aria-label="Start MCQs and True / False quiz"',
        )
        self.assertContains(
            page,
            f'href="{reverse("bank:quiz", args=[deck.slug])}?type=descriptive" '
            'aria-label="Start Descriptive quiz"',
        )
        self.assertContains(page, f'href="{reverse("bank:review_deck", args=[deck.slug])}?type=mcq_tf"')
        self.assertContains(page, f'href="{reverse("bank:review_deck", args=[deck.slug])}?type=descriptive"')

        mcq_review = self.client.get(
            reverse("bank:review_deck", args=[deck.slug]), {"type": "mcq_tf"})
        self.assertContains(mcq_review, "?review=1&amp;type=mcq_tf")
        mcq_ids = {
            question["kind"]
            for question in self.client.get(
                reverse("bank:api_questions", args=[deck.slug]), {"review": "1", "type": "mcq_tf"}
            ).json()["questions"]
        }
        self.assertEqual(mcq_ids, {Question.MCQ, Question.TF})

        descriptive_review = self.client.get(
            reverse("bank:review_deck", args=[deck.slug]), {"type": "descriptive"})
        self.assertContains(descriptive_review, "?review=1&amp;type=descriptive")
        descriptive = self.client.get(
            reverse("bank:api_questions", args=[deck.slug]), {"review": "1", "type": "descriptive"}
        ).json()["questions"]
        self.assertEqual(len(descriptive), 1)
        self.assertEqual(descriptive[0]["kind"], Question.DESC)

    def test_review_edit_saves_and_moves_to_bin_without_deleting_question(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        self.client.force_login(self.make_staff_user())
        review_url = reverse("bank:api_questions", args=[deck.slug]) + "?review=1"
        review_question = self.client.get(review_url).json()["questions"][0]
        self.assertIn("answer", review_question)
        self.assertIn("explanation", review_question)

        payload = {
            "kind": question.kind, "stem": "Corrected stem for review.", "answer": "",
            "explanation": "Updated explanation.", "note": "Checked by staff.",
            "is_in_sorting_bin": True,
            "options": [
                {"text": option.text, "is_correct": option.is_correct}
                for option in question.options.all()
            ],
        }
        response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), payload)
        self.assertEqual(response.status_code, 200, response.content)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Corrected stem for review.")
        self.assertTrue(question.is_in_sorting_bin)
        self.assertEqual(question.sorting_bin_at is not None, True)
        self.assertEqual(
            [q["id"] for q in self.client.get(
                reverse("bank:api_questions", args=[deck.slug]) + "?type=mcq_tf"
            ).json()["questions"]],
            [deck.questions.get(kind=Question.TF).pk],
        )
        self.assertEqual(
            self.client.get(reverse("bank:api_sorting_bin_questions")).json()["questions"][0]["id"],
            question.pk,
        )

    def test_staff_can_move_question_from_quiz_to_sorting_bin(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        endpoint = reverse("bank:api_question_sorting_bin", args=[question.pk])

        self.client.force_login(self.editor)
        self.assertEqual(self.post_json(endpoint, {}).status_code, 403)

        self.client.force_login(self.make_staff_user())
        quiz_page = self.client.get(reverse("bank:quiz", args=[deck.slug]))
        self.assertContains(quiz_page, 'data-can-sort="1"')
        response = self.post_json(endpoint, {})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["moved"])

        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)
        live_questions = self.client.get(
            reverse("bank:api_questions", args=[deck.slug]), {"type": "mcq_tf"}
        ).json()["questions"]
        self.assertNotIn(question.pk, [item["id"] for item in live_questions])
        bin_questions = self.client.get(reverse("bank:api_sorting_bin_questions")).json()["questions"]
        self.assertIn(question.pk, [item["id"] for item in bin_questions])

    def test_sorting_bin_page_offers_copyable_ai_reorganization_prompt(self):
        self.import_sample()
        self.client.force_login(self.make_staff_user())
        page = self.client.get(reverse("bank:sorting_bin"))

        self.assertContains(page, 'id="copy-sorting-prompt"')
        self.assertContains(page, "View the AI reorganization prompt")
        self.assertContains(page, "Reorganize these questions into the exact same plain-text format.")
        self.assertContains(
            page,
            "Available destination decks, grouped by SOURCE_SUBJECT.",
        )
        self.assertContains(
            page,
            "SOURCE_SUBJECT: Physiology",
        )
        self.assertContains(page, "TARGET_SEMESTER: 1 | TARGET_DECK: Haematology")
        self.assertContains(page, "Do not transfer questions between subjects")
        self.assertContains(page, "Never move a question to a different subject.")
        self.assertContains(page, "give it to your AI together with the exported sorting-bin text")

    def test_sorting_bin_questions_are_grouped_by_original_chapter(self):
        deck = self.import_sample()
        other_deck = QuestionBank.objects.create(
            subject=self.subject, semester=2, title="Cardiology"
        )
        first = deck.questions.first()
        first.is_in_sorting_bin = True
        first.save(update_fields=["is_in_sorting_bin"])
        second = Question.objects.create(
            bank=other_deck, kind=Question.DESC, stem="Describe a pulse.", answer="A beat."
        )
        second.is_in_sorting_bin = True
        second.save(update_fields=["is_in_sorting_bin"])

        self.client.force_login(self.make_staff_user())
        page = self.client.get(reverse("bank:sorting_bin"))
        content = page.content.decode()
        self.assertLess(content.index("Semester 1 › Haematology"), content.index("Semester 2 › Cardiology"))
        self.assertContains(page, "Semester 1 › Haematology")
        self.assertContains(page, "Semester 2 › Cardiology")

    def test_review_bin_tracks_questions_removed_from_the_active_deck(self):
        deck = self.import_sample()
        question = deck.questions.first()
        self.client.force_login(self.make_staff_user())

        response = self.client.post(reverse("bank:api_question_review_bin", args=[question.pk]))
        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertTrue(question.is_in_review_bin)
        self.assertFalse(question.is_in_sorting_bin)

        page = self.client.get(reverse("bank:review_bin"))
        self.assertContains(page, "Review bin")
        self.assertContains(page, "Q" + str(question.number or question.pk))
        self.assertNotIn(question.pk, [item["id"] for item in self.client.get(
            reverse("bank:api_questions", args=[deck.slug]), {"type": "mcq_tf"}
        ).json()["questions"]])

    def test_staff_can_delete_a_question_from_review_mode(self):
        deck = self.import_sample()
        question = deck.questions.first()
        self.client.force_login(self.make_staff_user())
        response = self.post_json(
            reverse("bank:api_question_edit", args=[question.pk]),
            {"delete_question": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())

    def test_true_false_review_allows_every_statement_to_be_false(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.TF)
        self.client.force_login(self.make_staff_user())
        response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {
            "kind": Question.TF, "stem": question.stem, "answer": "",
            "explanation": "", "note": "", "is_in_sorting_bin": False,
            "options": [
                {"text": option.text, "is_correct": False}
                for option in question.options.all()
            ],
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(any(option["is_correct"] for option in response.json()["question"]["options"]))

    def test_sorting_bin_export_preview_and_reimport_preserve_question_identity(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        question.is_in_sorting_bin = True
        question.save(update_fields=["is_in_sorting_bin"])
        self.client.force_login(self.make_staff_user())

        exported = self.client.get(reverse("bank:sorting_bin_export")).content.decode()
        self.assertIn(f"ID: {question.pk}", exported)
        reorganized = exported.replace("TARGET_SUBJECT:", "TARGET_SUBJECT: Physiology")
        reorganized = reorganized.replace("TARGET_SEMESTER:", "TARGET_SEMESTER: 2")
        reorganized = reorganized.replace("TARGET_DECK:", "TARGET_DECK: Corrected Chapter")
        reorganized = reorganized.replace("Q: Pick B", "Q: Rewritten question from the sorting bin")
        url = reverse("bank:sorting_bin_import")

        preview_response = self.client.post(url, {"action": "preview", "text": reorganized})
        self.assertRedirects(preview_response, url)
        preview = self.client.get(url)
        self.assertContains(preview, "Review destination chapters")
        self.assertContains(preview, "Ready</span>")
        self.assertContains(preview, "Semester 2 › Corrected Chapter")
        self.assertContains(preview, "Apply and refile 1 question")
        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)
        self.assertEqual(question.stem, "Pick B")
        self.assertFalse(QuestionBank.objects.filter(subject=self.subject, semester=2, title="Corrected Chapter").exists())

        response = self.client.post(url, {"action": "commit"})
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Rewritten question from the sorting bin")
        self.assertFalse(question.is_in_sorting_bin)
        self.assertEqual(question.bank.title, "Corrected Chapter")
        self.assertEqual(question.bank.semester, 2)
        self.assertEqual(question.bank.subject.name, "Physiology")

    def test_sorting_bin_preview_can_be_edited_or_discarded_without_applying(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        question.is_in_sorting_bin = True
        question.save(update_fields=["is_in_sorting_bin"])
        self.client.force_login(self.make_staff_user())
        url = reverse("bank:sorting_bin_import")
        text = self.client.get(reverse("bank:sorting_bin_export")).content.decode()

        self.assertRedirects(
            self.client.post(url, {"action": "preview", "text": text}), url)
        preview = self.client.get(url)
        self.assertContains(preview, "Edit imported text")
        self.assertContains(preview, "Discard preview")

        edit_response = self.client.post(url, {"action": "edit"})
        edit_url = url + "?edit=1"
        self.assertRedirects(edit_response, edit_url)
        self.assertContains(self.client.get(edit_url), f"ID: {question.pk}")

        self.assertRedirects(self.client.post(url, {"action": "cancel"}), url)
        self.assertNotIn("sorting_bin_pending_import", self.client.session)
        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)

    def test_sorting_bin_reimport_rejects_cross_subject_reassignment(self):
        deck = self.import_sample()
        other_subject, _ = Subject.objects.get_or_create(name="Anatomy", defaults={"order": 0})
        question = deck.questions.get(kind=Question.MCQ)
        question.is_in_sorting_bin = True
        question.save(update_fields=["is_in_sorting_bin"])
        self.client.force_login(self.make_staff_user())

        exported = self.client.get(reverse("bank:sorting_bin_export")).content.decode()
        reorganized = exported.replace("TARGET_SUBJECT:", "TARGET_SUBJECT: Anatomy")
        reorganized = reorganized.replace("TARGET_SEMESTER:", "TARGET_SEMESTER: 1")
        reorganized = reorganized.replace("TARGET_DECK:", "TARGET_DECK: Anatomy")
        preview_url = reverse("bank:sorting_bin_import")
        preview_response = self.client.post(
            preview_url, {"action": "preview", "text": reorganized})
        self.assertRedirects(preview_response, preview_url)
        preview = self.client.get(preview_url)
        self.assertContains(preview, "cannot be moved to another subject")

        response = self.client.post(preview_url, {"action": "commit"})
        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)
        self.assertEqual(question.bank.subject, self.subject)
        self.assertFalse(QuestionBank.objects.filter(subject=other_subject, title="Anatomy").exists())


class BrowseAndImportTests(Base):
    def anatomy_sections(self):
        anatomy, _ = Subject.objects.get_or_create(name="Anatomy", defaults={"order": 0})
        sections = []
        for order, name in enumerate(("Embryology", "Histology", "Gross Anatomy")):
            section, _ = SubjectSection.objects.get_or_create(
                subject=anatomy, name=name, defaults={"order": order},
            )
            sections.append(section)
        return anatomy, sections

    def test_import_permissions(self):
        url = reverse("bank:import")
        self.assertEqual(self.client.get(url).status_code, 302)          # anonymous -> login
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(url).status_code, 403)          # no permission

    def test_preview_commit_dupes_and_hierarchy(self):
        self.client.force_login(self.editor)
        url = reverse("bank:import")
        data = {"text": SAMPLE, "subject": self.subject.pk, "semester": 2,
                "deck_title": "Haematology", "skip_dupes": "1"}
        r = self.client.post(url, {**data, "action": "preview"})
        self.assertContains(r, "2</b> parsed")
        self.assertEqual(Question.objects.count(), 0)
        self.client.post(url, {**data, "action": "commit"})
        deck = QuestionBank.objects.get()
        self.assertEqual((deck.subject, deck.semester, deck.title), (self.subject, 2, "Haematology"))
        self.assertEqual(Question.objects.count(), 2)
        self.client.post(url, {**data, "action": "commit"})               # same deck again
        self.assertEqual(Question.objects.count(), 2)                      # duplicates skipped
        self.assertEqual(QuestionBank.objects.count(), 1)

    def test_anatomy_sections_are_ordered_and_show_their_decks(self):
        anatomy, sections = self.anatomy_sections()
        sections.sort(key=lambda section: section.order)
        self.assertEqual(
            [section.name for section in sections],
            ["Embryology", "Histology", "Gross Anatomy"],
        )
        deck = QuestionBank.objects.create(
            subject=anatomy, section=sections[2], semester=2, title="Thorax",
        )
        self.client.force_login(self.editor)
        page = self.client.get(anatomy.get_absolute_url())
        self.assertContains(page, "01")
        self.assertLess(page.content.index(b"Embryology"), page.content.index(b"Histology"))
        self.assertLess(page.content.index(b"Histology"), page.content.index(b"Gross Anatomy"))
        self.assertNotContains(page, "Thorax")
        section_page = self.client.get(sections[2].get_absolute_url())
        self.assertContains(section_page, "Thorax")
        self.assertContains(section_page, "Semester 2")
        self.assertEqual(deck.path, "Anatomy › Gross Anatomy › Semester 2 › Thorax")
        exported = self.client.get(reverse("bank:export", args=[deck.slug]))
        self.assertContains(exported, "SECTION: Gross Anatomy")

    def test_any_subject_can_have_its_own_sections(self):
        section = SubjectSection.objects.create(
            subject=self.subject, name="Neurophysiology", order=0,
        )
        self.client.force_login(self.editor)
        subject_page = self.client.get(self.subject.get_absolute_url())
        self.assertContains(subject_page, "Neurophysiology")
        section_page = self.client.get(section.get_absolute_url())
        self.assertContains(section_page, "No decks in Neurophysiology yet.")

    def test_import_can_assign_deck_to_anatomy_section(self):
        anatomy, sections = self.anatomy_sections()
        section = sections[1]
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": anatomy.pk,
            "section": section.pk, "semester": 3, "deck_title": "Epithelia",
            "skip_dupes": "1",
        })
        deck = QuestionBank.objects.get(title="Epithelia")
        self.assertEqual(deck.section, section)
        self.assertEqual(response.status_code, 302)

    def test_import_rejects_section_from_another_subject(self):
        _, sections = self.anatomy_sections()
        section = sections[2]
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "section": section.pk, "semester": 1, "deck_title": "Invalid category",
        })
        self.assertContains(response, "Choose a sub-subject belonging to the selected subject.")
        self.assertFalse(QuestionBank.objects.filter(title="Invalid category").exists())

    def test_commit_needs_subject_semester_title(self):
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {"action": "commit", "text": SAMPLE})
        self.assertEqual(QuestionBank.objects.count(), 0)

    def test_home_subject_and_deck_pages(self):
        deck = self.import_sample()
        self.subject.order = 1
        self.subject.save(update_fields=["order"])
        Subject.objects.get_or_create(name="Anatomy", defaults={"order": 0})
        Subject.objects.create(name="Biochemistry", order=2)
        self.client.force_login(self.editor)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, "Physiology")
        self.assertContains(home, "سُمُّوا رَوَاسِيَ بِالْجِبَالِ تَيَمُّناً")
        self.assertContains(home, "لَا كَالسَّفِينِ الرَّاسِيَاتِ رُكُودَا")
        self.assertNotContains(home, "Study with Rawasi")
        self.assertLess(home.content.index(b">Anatomy<"), home.content.index(b">Physiology<"))
        self.assertLess(home.content.index(b">Physiology<"), home.content.index(b">Biochemistry<"))
        self.assertContains(home, "Subjects")
        self.assertNotContains(home, "CHAPTER")
        self.assertNotContains(home, "Forecasts for clouds")
        self.assertNotContains(home, "deck ·")
        self.assertNotContains(home, "question ·")
        page = self.client.get(self.subject.get_absolute_url())
        self.assertContains(page, "Semester 1")
        self.assertContains(page, deck.title)

    def test_header_lines_prefill(self):
        self.client.force_login(self.editor)
        text = "SUBJECT: physiology\nSEMESTER: 3\nDECK: Renal\n\n" + SAMPLE
        self.client.post(reverse("bank:import"), {"action": "commit", "text": text, "skip_dupes": "1"})
        deck = QuestionBank.objects.get()
        self.assertEqual((deck.subject, deck.semester, deck.title), (self.subject, 3, "Renal"))

    def test_descriptive_import_and_export(self):
        deck = self.import_sample(title="Neurophysiology")
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": DESCRIPTIVE_SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": deck.title, "skip_dupes": "1"})
        question = Question.objects.get(kind=Question.DESC)
        self.assertEqual(question.answer, "The voltage difference across a cell membrane at rest.")
        self.assertEqual(question.number, "1")
        exported = self.client.get(reverse("bank:export", args=[question.bank.slug])).content.decode()
        self.assertIn("TYPE: DESC", exported)
        self.assertIn("ANSWER: " + question.answer, exported)

    def test_deck_page_shows_separate_quiz_modes_and_counts(self):
        deck = self.import_sample()
        Question.objects.create(bank=deck, kind=Question.DESC, stem="Describe a membrane.", answer="A lipid bilayer.")
        self.client.force_login(self.editor)
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(page, "Choose a question type")
        self.assertContains(page, "MCQs &amp; True / False")
        self.assertContains(page, "value=\"mcq_tf\"")
        self.assertContains(page, "<strong>2</strong> questions")
        self.assertContains(page, "Descriptive")
        self.assertContains(page, "value=\"descriptive\"")
        self.assertContains(page, "<strong>1</strong> questions")
        self.assertContains(page, "Shuffle question order")


class QuizApiTests(Base):
    def test_anonymous_users_are_redirected_to_login(self):
        deck = self.import_sample()
        self.client.logout()
        self.assertEqual(self.client.get(reverse("bank:home")).status_code, 302)
        self.assertEqual(self.client.get(reverse("bank:api_questions", args=[deck.slug])).status_code, 302)

    def test_stale_session_cache_does_not_lock_out_current_user(self):
        self.client.force_login(self.editor)
        from django.core.cache import cache
        cache.set(f"bank:active-session:{self.editor.pk}", "stale-session-key", None)
        self.assertEqual(self.client.get(reverse("bank:home")).status_code, 200)

    def test_api_hides_answers_and_checks(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        data = self.client.get(reverse("bank:api_questions", args=[deck.slug])).json()
        self.assertNotIn("is_correct", json.dumps(data))
        mcq, tf = data["questions"]
        wrong, right = mcq["options"][0]["id"], mcq["options"][1]["id"]
        chk = lambda pk, body: self.post_json(reverse("bank:api_check", args=[pk]), body).json()
        self.assertFalse(chk(mcq["id"], {"selected": wrong})["correct"])
        self.assertTrue(chk(mcq["id"], {"selected": right})["correct"])
        a, b = (o["id"] for o in tf["options"])
        self.assertTrue(chk(tf["id"], {"answers": {str(a): True, str(b): False}})["correct"])
        self.assertFalse(chk(tf["id"], {"answers": {str(a): True, str(b): True}})["correct"])

    def test_descriptive_api_filters_and_reveals_answer_on_request(self):
        deck = self.import_sample()
        question = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe resting potential.", answer="A voltage difference at rest.")
        self.client.force_login(self.editor)
        descriptive = self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?type=descriptive").json()
        self.assertEqual([q["id"] for q in descriptive["questions"]], [question.pk])
        self.assertNotIn(question.answer, json.dumps(descriptive))
        mcq_tf = self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?type=mcq_tf").json()
        self.assertEqual({q["kind"] for q in mcq_tf["questions"]}, {Question.MCQ, Question.TF})
        check_url = reverse("bank:api_check", args=[question.pk])
        self.assertEqual(self.post_json(check_url, {}).status_code, 400)
        revealed = self.post_json(check_url, {"reveal": True}).json()
        self.assertEqual(revealed["answer"], question.answer)


class SavedTests(Base):
    def test_save_toggle_and_saved_section(self):
        deck = self.import_sample()
        q = deck.questions.first()
        url = reverse("bank:api_save", args=[q.pk])
        self.assertEqual(self.post_json(url, {}).status_code, 401)        # anonymous
        self.client.force_login(self.student)
        self.assertTrue(self.post_json(url, {}).json()["saved"])
        self.assertEqual(SavedQuestion.objects.count(), 1)
        self.assertContains(self.client.get(reverse("bank:saved_home")), "1 saved question")
        self.assertContains(self.client.get(reverse("bank:saved_subject", args=[self.subject.slug])), "Semester 1")
        self.assertContains(self.client.get(reverse("bank:saved_deck", args=[deck.slug])), q.stem)
        api = self.client.get(reverse("bank:api_saved_questions", args=[deck.slug])).json()
        self.assertEqual([x["id"] for x in api["questions"]], [q.pk])
        self.assertFalse(self.post_json(url, {}).json()["saved"])          # toggles off
        self.assertEqual(SavedQuestion.objects.count(), 0)

    def test_saved_are_per_user(self):
        deck = self.import_sample()
        q = deck.questions.first()
        self.client.force_login(self.student)
        self.post_json(reverse("bank:api_save", args=[q.pk]), {})
        self.client.force_login(self.editor)
        api = self.client.get(reverse("bank:api_saved_questions", args=[deck.slug])).json()
        self.assertEqual(api["questions"], [])


class CommentAndEditTests(Base):
    def test_editing_from_quiz_returns_to_that_question_without_resetting_other_progress(self):
        deck = self.import_sample()
        question = deck.questions.filter(kind=Question.MCQ).first()
        self.client.force_login(self.editor)
        return_url = reverse("bank:quiz", args=[deck.slug]) + (
            f"?type=mcq_tf&resume_question={question.pk}"
        )
        edit_url = reverse("bank:question_edit", args=[question.pk])

        edit_page = self.client.get(edit_url, {"next": return_url})
        self.assertEqual(edit_page.status_code, 200)
        self.assertEqual(edit_page.context["next"], return_url)

        options = list(question.options.all())
        response = self.client.post(edit_url, {
            "next": return_url,
            "bank": deck.pk,
            "number": question.number,
            "kind": Question.MCQ,
            "stem": "Edited question from the quiz.",
            "answer": "",
            "explanation": question.explanation,
            "note": question.note,
            "options-TOTAL_FORMS": str(len(options) + 2),
            "options-INITIAL_FORMS": str(len(options)),
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            **{
                f"options-{index}-{field}": value
                for index, option in enumerate(options)
                for field, value in (
                    ("id", option.pk),
                    ("question", question.pk),
                    ("text", option.text),
                    ("is_correct", "on" if option.is_correct else ""),
                )
            },
        })

        self.assertRedirects(response, return_url, fetch_redirect_response=False)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Edited question from the quiz.")

    def test_add_descriptive_question_without_options(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:question_add", args=[deck.slug]), {
            "bank": deck.pk, "number": "", "kind": Question.DESC,
            "stem": "Describe a membrane.", "answer": "A lipid bilayer.",
            "explanation": "", "note": "",
            "options-TOTAL_FORMS": "4", "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 302)
        question = Question.objects.get(kind=Question.DESC)
        self.assertEqual(question.answer, "A lipid bilayer.")
        self.assertEqual(question.options.count(), 0)

    def test_comment_flow_and_editor_resolves(self):
        deck = self.import_sample()
        q = deck.questions.filter(kind="mcq").first()
        curl = reverse("bank:api_comment", args=[q.pk])
        self.assertEqual(self.post_json(curl, {"text": "typo"}).status_code, 401)
        self.client.force_login(self.student)
        self.assertEqual(self.post_json(curl, {"text": "  "}).status_code, 400)
        self.assertTrue(self.post_json(curl, {"text": "Option B looks wrong"}).json()["ok"])
        self.assertEqual(self.client.get(reverse("bank:comments")).status_code, 403)   # students can't read inbox

        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("bank:comments")), "Option B looks wrong")
        c = Comment.objects.get()
        opts = list(q.options.all())
        payload = {
            "bank": deck.pk, "number": "1", "kind": "mcq", "stem": "Edited stem",
            "explanation": "", "note": "", "resolve": [c.pk],
            "options-TOTAL_FORMS": "3", "options-INITIAL_FORMS": "2",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
            "options-0-id": opts[0].pk, "options-0-question": q.pk, "options-0-text": "one",
            "options-1-id": opts[1].pk, "options-1-question": q.pk, "options-1-text": "two",
            "options-1-is_correct": "on",
            "options-2-id": "", "options-2-question": q.pk, "options-2-text": "three",
        }
        r = self.client.post(reverse("bank:question_edit", args=[q.pk]), payload)
        self.assertEqual(r.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.stem, "Edited stem")
        self.assertEqual([o.text for o in q.options.all()], ["one", "two", "three"])
        c.refresh_from_db()
        self.assertTrue(c.resolved)
        self.assertEqual(c.resolved_by, self.editor)

    def test_edit_requires_permission(self):
        deck = self.import_sample()
        q = deck.questions.first()
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("bank:question_edit", args=[q.pk])).status_code, 403)

    def test_edit_rejects_mcq_without_correct_option(self):
        deck = self.import_sample()
        q = deck.questions.filter(kind="mcq").first()
        opts = list(q.options.all())
        self.client.force_login(self.editor)
        r = self.client.post(reverse("bank:question_edit", args=[q.pk]), {
            "bank": deck.pk, "number": "1", "kind": "mcq", "stem": "x",
            "explanation": "", "note": "",
            "options-TOTAL_FORMS": "2", "options-INITIAL_FORMS": "2",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
            "options-0-id": opts[0].pk, "options-0-question": q.pk, "options-0-text": "one",
            "options-1-id": opts[1].pk, "options-1-question": q.pk, "options-1-text": "two",
        })
        self.assertEqual(r.status_code, 200)                                # re-rendered with error
        self.assertContains(r, "correct option")

    def test_add_question_and_signup_disabled(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("bank:question_add", args=[deck.slug])).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)


class ImageQuestionTests(Base):
    def setUp(self):
        super().setUp()
        self.deck = QuestionBank.objects.create(
            subject=self.subject, semester=1, title="Image quiz",
        )

    @staticmethod
    def image_file():
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (16, 12), color="navy").save(output, format="PNG")
        return SimpleUploadedFile("diagram.png", output.getvalue(), content_type="image/png")

    def make_image(self):
        return QuestionImage.objects.create(
            bank=self.deck,
            public_id="supabase:diagram.png",
            secure_url="https://example.supabase.co/storage/v1/object/public/rawasi-question-images/diagram.png",
            original_name="diagram.png",
            width=1600,
            height=1200,
        )

    def make_identification(self, image, stem="Identify the marked structures."):
        question = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_asset=image, stem=stem,
        )
        target = ImageTarget.objects.create(
            question=question, label="A", answer="Test structure",
            x_percent=25, y_percent=60,
        )
        return question, target

    def test_deck_exposes_image_filters_authoring_and_staff_review_links(self):
        image = self.make_image()
        self.make_identification(image)
        mcq = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_asset=image,
            image_answer_mode=Question.IMAGE_MCQ, stem="Choose the structure.",
        )
        Option.objects.create(question=mcq, text="Correct", is_correct=True, order=0)
        Option.objects.create(question=mcq, text="Incorrect", is_correct=False, order=1)
        self.client.force_login(self.editor)
        page = self.client.get(self.deck.get_absolute_url())
        self.assertContains(page, "Images")
        self.assertContains(page, "2</strong> questions")
        self.assertContains(page, "?type=image&amp;image_mode=identify")
        self.assertContains(page, "?type=image&amp;image_mode=mcq")
        self.assertContains(page, reverse("bank:image_question_upload", args=[self.deck.slug]))

        reviewer = User.objects.create_user("image-card-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        staff_page = self.client.get(self.deck.get_absolute_url())
        self.assertContains(staff_page, "Review identification")
        self.assertContains(staff_page, "Review MCQs")

    def test_image_question_has_dedicated_editor_and_review_edit_support(self):
        image = self.make_image()
        question, target = self.make_identification(image)
        self.client.force_login(self.editor)
        editor = self.client.get(reverse("bank:question_edit", args=[question.pk]))
        self.assertEqual(editor.status_code, 200)
        self.assertContains(editor, "Identification targets")
        self.assertContains(editor, target.answer)
        self.assertContains(editor, "is-calibrated")
        self.assertContains(editor, "--image-aspect-ratio:1600/1200")
        self.assertContains(editor, "--image-max-width:104vh")

        reviewer = User.objects.create_user("image-edit-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {
            "kind": Question.IMAGE,
            "image_answer_mode": Question.IMAGE_IDENTIFY,
            "stem": "Identify the revised structure.",
            "targets": [{
                "label": "B", "answer": "Revised structure", "x_percent": 55, "y_percent": 70,
            }],
        })
        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Identify the revised structure.")
        self.assertEqual(question.image_asset, image)
        self.assertEqual(list(question.image_targets.values_list("label", "answer")), [("B", "Revised structure")])

    def test_upload_and_create_multiple_questions_from_one_image(self):
        self.client.force_login(self.editor)
        upload_url = reverse("bank:image_question_upload", args=[self.deck.slug])
        with patch("bank.views.upload_question_image", return_value={
            "public_id": "supabase:diagram.png",
            "secure_url": "https://example.supabase.co/storage/v1/object/public/rawasi-question-images/diagram.png",
            "width": 1600,
            "height": 1200,
        }) as upload:
            response = self.client.post(upload_url, {"image": self.image_file()})
        self.assertRedirects(
            response,
            reverse("bank:image_question_add", args=[self.deck.slug, QuestionImage.objects.get().pk]),
        )
        upload.assert_called_once()
        image = QuestionImage.objects.get()
        add_url = reverse("bank:image_question_add", args=[self.deck.slug, image.pk])
        self.assertEqual(self.client.get(add_url).status_code, 200)

        first = self.client.post(add_url, {
            "answer_mode": Question.IMAGE_IDENTIFY,
            "stem": "Identify structure A.",
            "targets_json": json.dumps([{
                "label": "A", "answer": "Structure A", "x_percent": 30, "y_percent": 45,
            }]),
            "options_json": "[]",
        })
        self.assertRedirects(first, self.deck.get_absolute_url())
        first_question = Question.objects.get()
        self.assertEqual(first_question.image_asset, image)
        first_target = first_question.image_targets.get()
        self.assertEqual(first_target.answer, "Structure A")
        self.assertEqual((float(first_target.x_percent), float(first_target.y_percent)), (30, 45))

        second = self.client.post(add_url, {
            "answer_mode": Question.IMAGE_MCQ,
            "stem": "Which structure is shown?",
            "targets_json": "[]",
            "options_json": json.dumps([
                {"text": "Structure A", "is_correct": True},
                {"text": "Structure B", "is_correct": False},
            ]),
            "add_another": "1",
        })
        self.assertRedirects(second, add_url)
        self.assertEqual(Question.objects.count(), 2)
        self.assertEqual(Question.objects.get(stem="Which structure is shown?").image_asset, image)
        self.assertEqual(image.questions.count(), 2)

    def test_debug_upload_saves_locally_and_removes_file_after_last_question(self):
        self.client.force_login(self.editor)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(DEBUG=True, SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="", MEDIA_ROOT=media_root):
                response = self.client.post(
                    reverse("bank:image_question_upload", args=[self.deck.slug]),
                    {"image": self.image_file()},
                )
                image = QuestionImage.objects.get()
                local_file = Path(media_root) / "question-images" / image.public_id[6:]
                self.assertTrue(local_file.is_file())
                self.assertEqual(image.public_id, f"local:{local_file.name}")
                self.assertTrue(image.secure_url.startswith("/media/question-images/"))
                self.assertEqual((image.width, image.height), (16, 12))
                question, _ = self.make_identification(image)

                deleted = self.client.post(reverse("bank:question_delete", args=[question.pk]))
                self.assertEqual(deleted.status_code, 302)
                self.assertFalse(local_file.exists())
                self.assertFalse(QuestionImage.objects.filter(pk=image.pk).exists())

    @override_settings(DEBUG=False, SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="")
    def test_upload_page_reports_missing_supabase_configuration(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse("bank:image_question_upload", args=[self.deck.slug]),
            {"image": self.image_file()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SUPABASE_URL")
        self.assertEqual(QuestionImage.objects.count(), 0)

    @override_settings(
        DEBUG=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-only-test-key",
        SUPABASE_STORAGE_BUCKET="rawasi-question-images",
    )
    def test_supabase_upload_uses_private_server_key_and_returns_public_url(self):
        with patch("bank.image_storage.urlopen", return_value=io.BytesIO(b"{}")) as open_url:
            result = upload_question_image(self.image_file())

        request = open_url.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.startswith(
            "https://example.supabase.co/storage/v1/object/rawasi-question-images/"
        ))
        self.assertEqual(request.get_header("Apikey"), "server-only-test-key")
        self.assertEqual(request.get_header("Authorization"), "Bearer server-only-test-key")
        self.assertEqual(request.get_header("Content-type"), "image/png")
        self.assertEqual(request.get_header("X-upsert"), "false")
        self.assertTrue(result["public_id"].startswith("supabase:"))
        self.assertTrue(result["secure_url"].startswith(
            "https://example.supabase.co/storage/v1/object/public/rawasi-question-images/"
        ))
        self.assertEqual((result["width"], result["height"]), (16, 12))

    @override_settings(
        DEBUG=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-only-test-key",
        SUPABASE_STORAGE_BUCKET="rawasi-question-images",
    )
    def test_supabase_delete_requests_only_the_referenced_object(self):
        with patch("bank.image_storage.urlopen", return_value=io.BytesIO(b"{}")) as open_url:
            delete_question_image("supabase:diagram.png")

        request = open_url.call_args.args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(json.loads(request.data), {"prefixes": ["diagram.png"]})
        self.assertEqual(request.get_header("Apikey"), "server-only-test-key")

    def test_filters_hide_identification_answers_and_review_reveals_them(self):
        image = self.make_image()
        identification, target = self.make_identification(image)
        mcq = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_asset=image,
            image_answer_mode=Question.IMAGE_MCQ, stem="Choose the correct structure.",
        )
        Option.objects.create(question=mcq, text="Correct", is_correct=True, order=0)
        Option.objects.create(question=mcq, text="Incorrect", is_correct=False, order=1)
        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[self.deck.slug])

        all_questions = self.client.get(api_url, {"type": "image"}).json()["questions"]
        self.assertEqual({question["id"] for question in all_questions}, {identification.pk, mcq.pk})
        self.assertNotIn(target.answer, json.dumps(all_questions))
        self.assertEqual(all_questions[0]["image_url"], image.secure_url)
        self.assertEqual(
            (all_questions[0]["image_width"], all_questions[0]["image_height"]),
            (image.width, image.height),
        )
        identify_questions = self.client.get(
            api_url, {"type": "image", "image_mode": Question.IMAGE_IDENTIFY},
        ).json()["questions"]
        self.assertEqual([question["id"] for question in identify_questions], [identification.pk])
        mcq_questions = self.client.get(
            api_url, {"type": "image", "image_mode": Question.IMAGE_MCQ},
        ).json()["questions"]
        self.assertEqual([question["id"] for question in mcq_questions], [mcq.pk])

        reveal_url = reverse("bank:api_image_target", args=[identification.pk, target.pk])
        revealed = self.post_json(reveal_url, {"action": "reveal"})
        self.assertEqual(revealed.json(), {"answer": target.answer, "target_id": target.pk})
        self.assertEqual(self.post_json(reveal_url, {"action": "grade", "grade": "known"}).status_code, 200)
        self.assertEqual(
            self.post_json(reverse("bank:api_check", args=[identification.pk]), {}).status_code, 400,
        )

        reviewer = User.objects.create_user("image-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        review = self.client.get(api_url, {"type": "image", "review": "1"}).json()["questions"]
        review_identification = next(question for question in review if question["id"] == identification.pk)
        self.assertEqual(review_identification["targets"][0]["answer"], target.answer)

        self.client.force_login(self.student)
        self.assertEqual(self.post_json(reveal_url, {"action": "invalid"}).status_code, 400)
        self.assertEqual(
            self.post_json(
                reverse("bank:api_check", args=[mcq.pk]),
                {"selected": mcq.options.get(is_correct=True).pk},
            ).json()["correct"],
            True,
        )

    def test_deck_and_bin_text_exports_exclude_image_questions(self):
        image = self.make_image()
        question, _ = self.make_identification(image, stem="Export must not flatten this image question.")
        question.is_in_review_bin = True
        question.save(update_fields=["is_in_review_bin"])
        self.client.force_login(self.editor)
        deck_export = self.client.get(reverse("bank:export", args=[self.deck.slug])).content.decode()
        self.assertNotIn(question.stem, deck_export)
        self.assertNotIn("TYPE: IMAGE", deck_export)

        reviewer = User.objects.create_user("image-exporter", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        for name in ("sorting_bin_export", "review_bin_export"):
            text = self.client.get(reverse(f"bank:{name}")).content.decode()
            self.assertNotIn(question.stem, text)
            self.assertIn("Image-dependent questions are not included", text)

    def test_shared_image_is_deleted_only_after_last_question(self):
        image = self.make_image()
        first, _ = self.make_identification(image, "First image question.")
        second, _ = self.make_identification(image, "Second image question.")
        self.client.force_login(self.editor)
        with patch("bank.views.delete_question_image") as delete:
            first_response = self.client.post(reverse("bank:question_delete", args=[first.pk]))
            self.assertRedirects(first_response, reverse("bank:manage_deck", args=[self.deck.slug]))
            delete.assert_not_called()
            self.assertTrue(QuestionImage.objects.filter(pk=image.pk).exists())

            second_response = self.client.post(reverse("bank:question_delete", args=[second.pk]))
            self.assertRedirects(second_response, reverse("bank:manage_deck", args=[self.deck.slug]))
            delete.assert_called_once_with(image.public_id)
        self.assertFalse(QuestionImage.objects.filter(pk=image.pk).exists())

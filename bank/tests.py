"""Django tests:  python manage.py test"""
import json
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import Comment, Option, Question, QuestionBank, SavedQuestion, Subject

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
        user = User.objects.create_user("pwa-user", password="pw")
        self.client.force_login(user)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, 'id="pwa-install"')
        self.assertContains(home, "Install app")
        self.assertContains(home, '<span class="mark">101</span>')
        bank = QuestionBank.objects.create(subject=Subject.objects.create(name="PWA"), title="Install test")
        deck = self.client.get(bank.get_absolute_url())
        self.assertNotContains(deck, 'id="pwa-install"')


class BrowseAndImportTests(Base):
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

    def test_commit_needs_subject_semester_title(self):
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {"action": "commit", "text": SAMPLE})
        self.assertEqual(QuestionBank.objects.count(), 0)

    def test_home_subject_and_deck_pages(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, "Physiology")
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
        self.assertContains(page, "MCQs &amp; True / False: 2")
        self.assertContains(page, "Descriptive questions: 1")
        self.assertContains(page, "value=\"descriptive\"")


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

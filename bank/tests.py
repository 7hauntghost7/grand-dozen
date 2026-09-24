"""Django tests:  python manage.py test"""
import json

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

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


class QuizApiTests(Base):
    def test_api_hides_answers_and_checks(self):
        deck = self.import_sample()
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

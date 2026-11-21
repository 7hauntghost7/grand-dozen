from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from .models import Question, QuestionBank, QuestionNote


class OmitNotesTests(TestCase):
    def setUp(self):
        self.bank = QuestionBank.objects.create(title="Lipids")
        self.other = QuestionBank.objects.create(title="Enzymes")
        self.q1 = Question.objects.create(bank=self.bank, stem="One?", note="Answer key as given in the source.")
        self.q2 = Question.objects.create(bank=self.bank, stem="Two?", note="No answer key was given.")
        self.q3 = Question.objects.create(bank=self.bank, stem="Three?")
        self.elsewhere = Question.objects.create(bank=self.other, stem="Four?", note="Keep me.")
        self.editor = User.objects.create_user("editor")
        self.editor.user_permissions.add(Permission.objects.get(codename="change_question"))
        self.student = User.objects.create_user("student")
        QuestionNote.objects.create(user=self.student, question=self.q1, text="my own note")

    def test_omit_notes_clears_only_this_chapter(self):
        self.client.force_login(self.editor)
        url = reverse("bank:manage_deck", args=[self.bank.slug])
        self.assertContains(self.client.get(url), "Omit notes (2)")
        self.client.post(url, {"action": "omit_notes"})
        self.assertEqual(list(Question.objects.filter(bank=self.bank).values_list("note", flat=True)), ["", "", ""])
        self.assertEqual(Question.objects.get(pk=self.elsewhere.pk).note, "Keep me.")
        self.assertTrue(QuestionNote.objects.filter(question=self.q1).exists())   # students' own notes stay
        self.assertNotContains(self.client.get(url), "Omit notes")

    def test_students_cannot(self):
        self.client.force_login(self.student)
        self.client.post(reverse("bank:manage_deck", args=[self.bank.slug]), {"action": "omit_notes"})
        self.assertEqual(Question.objects.get(pk=self.q1.pk).note, "Answer key as given in the source.")

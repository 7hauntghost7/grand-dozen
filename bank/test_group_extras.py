import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import groups
from .models import (ChallengeAttempt, ExamAttempt, GroupMember, GroupMute, GroupPost, GroupQuiz, PersonalQuiz,
                     PostReaction, QuestionBank, QuizAccess, SavedExam, StudyGroup)

QUESTIONS = [
    {"kind": "mcq", "stem": "GFR?", "options": ["125", "5"], "correct": [False, True], "answer": "", "explanation": ""},
    {"kind": "desc", "stem": "Define clearance.", "options": [], "correct": [], "answer": "Plasma cleared per min.",
     "explanation": ""},
]


class GroupExtrasTests(TestCase):
    def setUp(self):
        self.amna = User.objects.create_user("amna")
        self.sara = User.objects.create_user("sara")
        self.notify = patch.object(groups, "notify").start()
        self.addCleanup(patch.stopall)
        self.group = StudyGroup.objects.create(name="Renal squad", owner=self.amna, code="renal123")
        for u in (self.amna, self.sara):
            GroupMember.objects.create(group=self.group, user=u, status="active", joined_at=timezone.now())
        self.quiz = PersonalQuiz.objects.create(user=self.amna, title="Renal", questions=QUESTIONS)
        QuizAccess.objects.create(user=self.amna, quiz=self.quiz)

    def act(self, who, **data):
        self.client.force_login(who)
        return self.client.post(reverse("bank:group_action", args=[self.group.code]), data)

    def test_mute(self):
        self.act(self.sara, action="mute")
        self.assertTrue(GroupMute.objects.filter(group=self.group, user=self.sara).exists())
        self.act(self.amna, action="announce", text="Live at 9")
        ids = list(self.notify.call_args[0][0])
        self.assertNotIn(self.sara.pk, ids)
        self.assertContains(self.client.get(self.group.get_absolute_url()), 'value="mute"')   # amna isn't muted
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(self.group.get_absolute_url()), 'value="unmute"')
        self.act(self.sara, action="unmute")
        self.act(self.amna, action="announce", text="Again")
        self.assertIn(self.sara.pk, list(self.notify.call_args[0][0]))
        acad = StudyGroup.objects.get(is_everyone=True)                 # works in the Academic Secretary too
        self.client.force_login(self.sara)
        self.client.post(reverse("bank:group_action", args=[acad.code]), {"action": "mute"})
        self.assertTrue(GroupMute.objects.filter(group=acad, user=self.sara).exists())

    def test_reactions(self):
        post = GroupPost.objects.create(group=self.group, author=self.amna, kind="announce", text="Hi")
        self.act(self.sara, action="react", post=post.pk, emoji="👍")
        self.assertEqual(PostReaction.objects.get(post=post, user=self.sara).emoji, "👍")
        self.act(self.sara, action="react", post=post.pk, emoji="🤲")          # change it
        self.assertEqual(PostReaction.objects.get(post=post, user=self.sara).emoji, "🤲")
        self.act(self.sara, action="react", post=post.pk, emoji="😂")          # not one of the set
        self.assertEqual(PostReaction.objects.get(post=post, user=self.sara).emoji, "🤲")
        self.act(self.sara, action="react", post=post.pk, emoji="🤲")          # tap again: take it back
        self.assertFalse(PostReaction.objects.filter(post=post).exists())
        self.client.force_login(self.amna)
        r = self.client.post(reverse("bank:group_action", args=[self.group.code]),
                             {"action": "react", "post": post.pk, "emoji": "❤️"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertIn("❤️ <b>1</b>", r.json()["html"])
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(self.group.get_absolute_url()), "❤️ <b>1</b>")
        outsider = User.objects.create_user("omar")
        self.client.force_login(outsider)
        self.assertEqual(self.client.post(reverse("bank:group_action", args=[self.group.code]),
                                          {"action": "react", "post": post.pk, "emoji": "👍"}).status_code, 404)

    def test_add_group_quizzes_and_finished_challenges_to_my_space(self):
        groups.share_quiz(self.group, self.quiz, self.amna)
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(self.group.get_absolute_url()), "＋ My Space")
        self.act(self.sara, action="add_quiz", quiz=self.quiz.pk)
        self.assertTrue(QuizAccess.objects.filter(user=self.sara, quiz=self.quiz).exists())
        # A challenge: only after you've finished it (or it ended)
        quiz2 = PersonalQuiz.objects.create(user=self.amna, title="Exam style", questions=QUESTIONS)
        groups.share_quiz(self.group, quiz2, self.amna, {"reveal": "end", "days": "3", "allow_hiding": True})
        c = GroupQuiz.objects.get(quiz=quiz2)
        back = reverse("bank:challenge", args=[self.group.code, c.pk])
        self.act(self.sara, action="add_quiz", quiz=quiz2.pk, back=back)
        self.assertFalse(QuizAccess.objects.filter(user=self.sara, quiz=quiz2).exists())
        ChallengeAttempt.objects.create(challenge=c, user=self.sara, answers={"1": {"selected": 1}}, finished_at=timezone.now())
        r = self.act(self.sara, action="add_quiz", quiz=quiz2.pk, back=back)
        self.assertRedirects(r, back, fetch_redirect_response=False)
        self.assertTrue(QuizAccess.objects.filter(user=self.sara, quiz=quiz2).exists())

    def test_edit_a_quiz(self):
        url = reverse("bank:my_space_edit", args=[self.quiz.pk])
        self.client.force_login(self.sara)
        self.assertEqual(self.client.get(url).status_code, 404)                 # only the maker edits
        self.client.force_login(self.amna)
        self.assertContains(self.client.get(url), "quiz-questions")
        fixed = [dict(QUESTIONS[0], stem="Normal GFR?", correct=[True, False]), QUESTIONS[1],
                 {"kind": "mcq", "stem": "New one", "options": ["a", "b"], "correct": [True, False]}]
        save = reverse("bank:my_space_save", args=[self.quiz.pk])
        r = self.client.post(save, json.dumps({"title": "Renal v2", "questions": fixed}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.quiz.refresh_from_db()
        self.assertEqual((self.quiz.title, len(self.quiz.questions), self.quiz.questions[0]["stem"]), ("Renal v2", 3, "Normal GFR?"))
        # After a challenge was taken: the questions keep their places; tries are scored again
        groups.share_quiz(self.group, self.quiz, self.amna, {"reveal": "each", "days": "1", "allow_hiding": True})
        c = GroupQuiz.objects.get(quiz=self.quiz)
        attempt = ChallengeAttempt.objects.create(challenge=c, user=self.sara, answers={"1": {"selected": 1}},
                                                  score=0, total=2, finished_at=timezone.now())
        r = self.client.post(save, json.dumps({"questions": fixed[:2]}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        rekey = [dict(fixed[0], correct=[False, True]), fixed[1], fixed[2]]
        self.client.post(save, json.dumps({"questions": rekey}), content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 1)
        self.assertContains(self.client.get(reverse("bank:my_space")), "✏️ Edit")


class SavedExamTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("amna")
        self.client.force_login(self.user)
        self.bank = QuestionBank.objects.create(title="Renal")
        self.exam = ExamAttempt.objects.create(user=self.user, bank=self.bank, title="Renal mock", question_ids=[1, 2, 3],
                                               configuration={}, submitted_at=timezone.now())

    def test_save_and_retake(self):
        api = reverse("bank:api_exam_my_space", args=[self.exam.pk])
        self.assertTrue(self.client.post(api, "{}", content_type="application/json").json()["in_my_space"])
        row = SavedExam.objects.get(user=self.user)
        page = self.client.get(reverse("bank:my_space")).content.decode()
        self.assertIn("Renal mock", page)
        self.assertIn("Take it again", page)
        r = self.client.post(reverse("bank:my_space_exam", args=[row.pk]), {"action": "start"})
        fresh = ExamAttempt.objects.exclude(pk=self.exam.pk).get()
        self.assertRedirects(r, reverse("bank:exam_attempt", args=[fresh.pk]), fetch_redirect_response=False)
        self.assertEqual(fresh.question_ids, [1, 2, 3])
        self.assertIsNone(fresh.submitted_at)
        self.client.post(reverse("bank:my_space_exam", args=[row.pk]), {"action": "start"})   # continues, no 2nd copy
        self.assertEqual(ExamAttempt.objects.count(), 2)
        # Saving a retake doesn't keep the same exam twice
        fresh.submitted_at = timezone.now()
        fresh.save()
        self.client.post(reverse("bank:api_exam_my_space", args=[fresh.pk]), "{}", content_type="application/json")
        self.assertEqual(SavedExam.objects.count(), 1)
        self.client.post(api, json.dumps({"action": "remove"}), content_type="application/json")
        self.assertFalse(SavedExam.objects.exists())

    def test_only_finished_exams(self):
        self.exam.submitted_at = None
        self.exam.save()
        r = self.client.post(reverse("bank:api_exam_my_space", args=[self.exam.pk]), "{}", content_type="application/json")
        self.assertEqual(r.status_code, 409)
        other = User.objects.create_user("sara")
        self.client.force_login(other)
        r = self.client.post(reverse("bank:api_exam_my_space", args=[self.exam.pk]), "{}", content_type="application/json")
        self.assertEqual(r.status_code, 404)

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import groups
from .models import ChallengeAttempt, GroupMember, GroupPost, GroupQuiz, PersonalQuiz, QuizAccess, StudyGroup

QUESTIONS = [
    {"kind": "mcq", "stem": "GFR?", "options": ["125 ml/min", "5 ml/min", "1 l/min"], "correct": [True, False, False],
     "answer": "", "explanation": "Normal adult GFR."},
    {"kind": "tf", "stem": "The loop of Henle:", "options": ["Is in the medulla", "Secretes renin"],
     "correct": [True, False], "answer": "", "explanation": ""},
    {"kind": "desc", "stem": "Define clearance.", "options": [], "correct": [], "answer": "Volume of plasma cleared per minute.",
     "explanation": ""},
]


class ChallengeTests(TestCase):
    def setUp(self):
        self.amna = User.objects.create_user("amna")
        self.sara = User.objects.create_user("sara")
        self.omar = User.objects.create_user("omar")
        self.admin = User.objects.create_user("boss", is_staff=True)
        self.notify = patch.object(groups, "notify").start()
        self.addCleanup(patch.stopall)
        self.group = StudyGroup.objects.create(name="Renal squad", owner=self.amna, code="renal123")
        for u in (self.amna, self.sara, self.omar):
            GroupMember.objects.create(group=self.group, user=u, status="active", joined_at=timezone.now())
        self.quiz = PersonalQuiz.objects.create(user=self.amna, title="Renal challenge", questions=QUESTIONS)
        QuizAccess.objects.create(user=self.amna, quiz=self.quiz)

    def share(self, group=None, who=None, reveal="each", days="3", hiding="1"):
        self.client.force_login(who or self.amna)
        data = {"action": "share", "groups": (group or self.group).code, "challenge": "1", "reveal": reveal, "days": days}
        if hiding:
            data["allow_hiding"] = hiding
        self.client.post(reverse("bank:my_space_action", args=[self.quiz.pk]), data)
        return GroupQuiz.objects.get(group=group or self.group, quiz=self.quiz)

    def url(self, name, c):
        return reverse(name, args=[c.group.code, c.pk])

    def answer(self, c, who, pos, payload):
        self.client.force_login(who)
        return self.client.post(self.url("bank:challenge_answer", c), json.dumps({"pos": pos, "payload": payload}),
                                content_type="application/json")

    def take(self, c, who, picks, submit=True):
        self.client.force_login(who)
        self.client.get(self.url("bank:challenge_data", c) + "?start=1")
        for pos, payload in picks.items():
            self.answer(c, who, pos, payload)
        if submit:
            return self.client.post(self.url("bank:challenge_submit", c)).json()

    def test_sharing_as_a_challenge(self):
        c = self.share(reveal="end", days="7")
        self.assertTrue(c.leaderboard)
        self.assertEqual(c.reveal, "end")
        self.assertTrue(c.allow_hiding)
        self.assertAlmostEqual((c.ends_at - timezone.now()).total_seconds(), 7 * 86400, delta=60)
        self.assertTrue(GroupPost.objects.filter(group=self.group, kind="quiz", quiz=self.quiz).exists())
        self.notify.assert_called()
        self.client.force_login(self.sara)
        page = self.client.get(self.group.get_absolute_url()).content.decode()
        self.assertIn("started a challenge", page)
        self.assertIn(self.url("bank:challenge", c), page)

    def test_answers_stay_on_the_server_until_seen(self):
        c = self.share()
        self.client.force_login(self.sara)
        # The normal quiz page would hold the answers, so it sends you to the challenge instead.
        self.assertRedirects(self.client.get(reverse("bank:my_space_quiz", args=[self.quiz.pk])), self.url("bank:challenge", c))
        self.assertEqual(self.client.get(reverse("bank:my_space_questions", args=[self.quiz.pk])).status_code, 404)
        data = self.client.get(self.url("bank:challenge_data", c)).json()
        self.assertFalse(data["started"])
        self.assertNotIn("correct", json.dumps(data["questions"]))
        self.assertNotIn("Normal adult GFR", json.dumps(data))
        self.assertFalse(ChallengeAttempt.objects.exists())       # the clock starts on Start, not on opening
        self.client.get(self.url("bank:challenge_data", c) + "?start=1")
        self.assertTrue(ChallengeAttempt.objects.filter(user=self.sara).exists())
        # Can't add it to My Space while it runs
        self.client.post(reverse("bank:group_action", args=[self.group.code]), {"action": "add_quiz", "quiz": self.quiz.pk})
        self.assertFalse(QuizAccess.objects.filter(user=self.sara).exists())

    def test_after_each_question(self):
        c = self.share(reveal="each")
        self.client.force_login(self.sara)
        self.client.get(self.url("bank:challenge_data", c) + "?start=1")
        r = self.answer(c, self.sara, 1, {"selected": 1}).json()
        self.assertEqual(r["result"]["correct"], False)
        self.assertEqual(r["result"]["key"], [True, False, False])
        r = self.answer(c, self.sara, 1, {"selected": 0}).json()      # first answer is kept
        self.assertTrue(r["kept"])
        self.assertEqual(r["result"]["correct"], False)
        self.assertEqual(self.answer(c, self.sara, 2, {"answers": [True]}).status_code, 400)   # all statements needed
        self.assertTrue(self.answer(c, self.sara, 2, {"answers": [True, False]}).json()["result"]["correct"])
        self.assertIn("answer", self.answer(c, self.sara, 3, None).json()["result"])           # flashcard
        done = self.client.post(self.url("bank:challenge_submit", c)).json()
        self.assertEqual((done["score"], done["total"]), (1, 2))       # the written one isn't scored
        self.assertEqual(self.answer(c, self.sara, 1, {"selected": 0}).status_code, 409)
        # Finished: now the quiz opens normally
        self.assertEqual(self.client.get(reverse("bank:my_space_quiz", args=[self.quiz.pk])).status_code, 200)

    def test_after_submitting(self):
        c = self.share(reveal="end")
        self.client.force_login(self.sara)
        self.client.get(self.url("bank:challenge_data", c) + "?start=1")
        r = self.answer(c, self.sara, 1, {"selected": 1}).json()
        self.assertEqual(r, {"saved": True})                           # nothing given away
        self.answer(c, self.sara, 1, {"selected": 0})                  # changed their mind
        self.answer(c, self.sara, 2, {"answers": [True, True]})
        data = self.client.get(self.url("bank:challenge_data", c)).json()
        self.assertEqual(data["results"], {})
        done = self.client.post(self.url("bank:challenge_submit", c)).json()
        self.assertEqual(done["score"], 1)
        self.assertEqual(set(done["results"]), {"1", "2", "3"})
        self.assertEqual(done["results"]["2"]["key"], [True, False])

    def test_small_group_publishes_by_itself_and_ranks_ties_by_time(self):
        c = self.share()
        self.take(c, self.sara, {1: {"selected": 0}, 2: {"answers": [True, False]}})
        self.take(c, self.omar, {1: {"selected": 0}, 2: {"answers": [True, False]}})
        self.take(c, self.amna, {1: {"selected": 2}})
        ChallengeAttempt.objects.filter(user=self.omar).update(started_at=timezone.now() - timedelta(minutes=20))
        ChallengeAttempt.objects.filter(user=self.sara).update(started_at=timezone.now() - timedelta(minutes=5))
        self.client.force_login(self.omar)
        self.assertNotContains(self.client.get(self.group.get_absolute_url()), "took part")
        GroupQuiz.objects.filter(pk=c.pk).update(ends_at=timezone.now() - timedelta(minutes=1))
        page = self.client.get(self.group.get_absolute_url()).content.decode()
        c.refresh_from_db()
        self.assertIsNotNone(c.published_at)
        self.assertTrue(GroupPost.objects.filter(group=self.group, kind="board").exists())
        self.assertIn("3 took part", page)
        from .challenge import board
        rows, mine, count = board(c, self.omar)
        self.assertEqual([r["attempt"].user for r in rows], [self.sara, self.omar, self.amna])   # Sara was faster
        self.assertEqual(mine["rank"], 2)

    def test_academic_secretary_waits_for_admins(self):
        acad = StudyGroup.objects.get(is_everyone=True)
        self.quiz.user = self.admin
        self.quiz.save()
        QuizAccess.objects.create(user=self.admin, quiz=self.quiz)
        c = self.share(group=acad, who=self.admin)
        self.assertTrue(c.leaderboard)
        self.take(c, self.sara, {1: {"selected": 0}})
        self.take(c, self.omar, {1: {"selected": 0}}, submit=False)    # still answering when time runs out
        GroupQuiz.objects.filter(pk=c.pk).update(ends_at=timezone.now() - timedelta(minutes=1))
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(acad.get_absolute_url()), "the admins will publish the leaderboard")
        c.refresh_from_db()
        self.assertIsNone(c.published_at)
        self.assertIsNotNone(ChallengeAttempt.objects.get(user=self.omar).finished_at)   # handed in for them
        # Students can't see results or publish
        self.assertEqual(self.client.get(self.url("bank:challenge_results", c)).status_code, 404)
        self.client.post(self.url("bank:challenge_action", c), {"action": "publish"})
        c.refresh_from_db()
        self.assertIsNone(c.published_at)
        self.client.force_login(self.admin)
        page = self.client.get(self.url("bank:challenge_results", c)).content.decode()
        self.assertIn("sara", page)
        self.client.post(self.url("bank:challenge_action", c), {"action": "publish"})
        c.refresh_from_db()
        self.assertIsNotNone(c.published_at)
        self.client.post(self.url("bank:challenge_action", c), {"action": "pin"})
        c.refresh_from_db()
        self.assertIsNotNone(c.pinned_at)

    def test_hiding_names(self):
        self.sara.profile.hide_on_leaderboards = True
        self.sara.profile.save()
        c = self.share(hiding="")                                      # admin said names are always shown
        self.assertFalse(c.allow_hiding)
        self.take(c, self.sara, {1: {"selected": 0}})
        from .challenge import board, publish
        publish(c)
        rows, _, _ = board(c, self.omar)
        self.assertEqual(rows[0]["name"], "sara")
        c.allow_hiding = True
        c.save()
        rows, _, _ = board(c, self.omar)
        self.assertEqual(rows[0]["name"], "Anonymous")
        self.assertIsNone(rows[0]["profile"])
        self.assertEqual(board(c, self.sara)[0][0]["name"], "sara")    # you still see yourself
        self.assertEqual(board(c, self.omar, real_names=True)[0][0]["name"], "sara")   # managers' results page

    def test_late_tries_are_practice(self):
        c = self.share()
        GroupQuiz.objects.filter(pk=c.pk).update(ends_at=timezone.now() - timedelta(minutes=1))
        done = self.take(c, self.sara, {1: {"selected": 0}})
        self.assertFalse(done["ranked"])
        from .challenge import board
        c.refresh_from_db()
        self.assertEqual(board(c, self.sara)[2], 0)

    def test_home_card(self):
        acad = StudyGroup.objects.get(is_everyone=True)
        self.quiz.user = self.admin
        self.quiz.save()
        QuizAccess.objects.create(user=self.admin, quiz=self.quiz)
        c = self.share(group=acad, who=self.admin)
        self.client.force_login(self.sara)
        self.assertNotContains(self.client.get(reverse("bank:home")), "home-challenge-kicker")
        self.client.post(self.url("bank:challenge_action", c), {"action": "home", "for": "1"})   # not an admin
        c.refresh_from_db()
        self.assertIsNone(c.home_until)
        self.client.force_login(self.admin)
        self.client.post(self.url("bank:challenge_action", c), {"action": "home", "for": "1"})
        c.refresh_from_db()
        self.assertAlmostEqual((c.home_until - timezone.now()).total_seconds(), 86400, delta=60)
        self.client.force_login(self.sara)
        page = self.client.get(reverse("bank:home")).content.decode()
        self.assertIn("Renal challenge", page)
        self.assertIn("Start →", page)
        GroupQuiz.objects.filter(pk=c.pk).update(home_until=timezone.now() - timedelta(seconds=1))
        self.assertNotContains(self.client.get(reverse("bank:home")), "home-challenge-kicker")


class BuilderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("amna")
        self.client.force_login(self.user)

    def build(self, questions, title="Mine"):
        return self.client.post(reverse("bank:my_space_build"), json.dumps({"title": title, "questions": questions}),
                                content_type="application/json")

    def test_write_it_myself(self):
        r = self.build([
            {"kind": "mcq", "stem": "GFR?", "options": ["125", "", "5", ""], "correct": [False, False, True, False],
             "explanation": "Normal"},
            {"kind": "tf", "stem": "Kidney:", "options": ["Retroperitoneal", "Has 3 lobes"], "correct": [True, False]},
            {"kind": "desc", "stem": "Define GFR", "answer": "Filtered volume per minute"},
        ])
        self.assertEqual(r.status_code, 200)
        quiz = PersonalQuiz.objects.get(user=self.user)
        self.assertEqual(quiz.title, "Mine")
        self.assertEqual(quiz.questions[0]["options"], ["125", "5"])      # empty options dropped
        self.assertEqual(quiz.questions[0]["correct"], [False, True])     # the tick follows its option
        self.assertEqual(quiz.questions[1]["correct"], [True, False])
        self.assertTrue(QuizAccess.objects.filter(user=self.user, quiz=quiz).exists())

    def test_builder_problems(self):
        r = self.build([{"kind": "mcq", "stem": "GFR?", "options": ["a", "b"], "correct": [False, False]},
                        {"kind": "desc", "stem": "Define", "answer": ""},
                        {"kind": "mcq", "stem": "", "options": ["a", "b"], "correct": [True, False]}])
        self.assertEqual(r.status_code, 400)
        errors = r.json()["errors"]
        self.assertEqual(len(errors), 3)
        self.assertIn("correct option", errors[0])
        self.assertFalse(PersonalQuiz.objects.exists())

    def test_new_quiz_page_has_three_ways(self):
        page = self.client.get(reverse("bank:my_space_new")).content.decode()
        for text in ("With AI", "Write it myself", "Paste questions", "Convert them into the exact format"):
            self.assertIn(text, page)
        reply = "Q: GFR?\nA. 125\nB. 5\nANSWER: A\n"
        r = self.client.post(reverse("bank:my_space_new"), {"mode": "paste", "reply": reply, "action": "save", "title": "Pasted"})
        self.assertRedirects(r, reverse("bank:my_space"))
        self.assertEqual(PersonalQuiz.objects.get(title="Pasted").questions[0]["correct"], [True, False])

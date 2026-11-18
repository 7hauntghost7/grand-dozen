import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import friends, live, my_space
from .models import (LiveAnswer, LiveMember, LiveRoom, LiveSave, Option, PersonalQuiz, Question, QuestionBank,
                     QuestionNote, QuizAccess, SavedQuestion)


class LiveTests(TestCase):
    def setUp(self):
        self.lead = User.objects.create_user("amna")
        self.sara = User.objects.create_user("sara")
        self.omar = User.objects.create_user("omar")
        self.stranger = User.objects.create_user("carol")
        friends._make_friends(self.lead, self.sara)
        friends._make_friends(self.lead, self.omar)
        bank = QuestionBank.objects.create(title="Upper limb")
        self.qs = []
        for i in range(3):
            q = Question.objects.create(bank=bank, stem=f"Question {i}?", kind=Question.MCQ, explanation=f"Because {i}")
            for j, text in enumerate("ABCD"):
                Option.objects.create(question=q, text=f"{text}{i}", is_correct=(j == 1), order=j)
            self.qs.append(q)
        self.tf = Question.objects.create(bank=bank, stem="TF?", kind=Question.TF)
        self.tf_opts = [Option.objects.create(question=self.tf, text=t, is_correct=c, order=k)
                        for k, (t, c) in enumerate([("one", True), ("two", False)])]
        self.identify = Question.objects.create(bank=bank, stem="Label it", kind=Question.IMAGE,
                                                image_answer_mode=Question.IMAGE_IDENTIFY)
        self.notify = patch.object(friends, "notify").start()
        self.addCleanup(patch.stopall)

    # -------------------------------------------------------------- helpers
    def start(self, user=None, ids=None, mine=""):
        self.client.force_login(user or self.lead)
        ids = ids if ids is not None else [q.pk for q in self.qs] + [self.tf.pk, self.identify.pk]
        r = self.client.post(reverse("bank:live_start"), json.dumps({"ids": ids, "title": "Upper limb", "back": "/deck/x/quiz/", "mine": mine}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return LiveRoom.objects.latest("id")

    def act(self, room, user, action, **data):
        self.client.force_login(user)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse("bank:live_act", args=[room.pk]), json.dumps({"action": action, **data}),
                                 content_type="application/json")
        return r.json()

    def state(self, room, user):
        self.client.force_login(user)
        return self.client.get(reverse("bank:live_state", args=[room.pk])).json()

    def enter(self, room, user):
        self.client.force_login(user)
        return self.client.get(reverse("bank:live_room", args=[room.pk]))

    # -------------------------------------------------------------- tests
    def test_start_copies_the_questions_and_skips_labelling_ones(self):
        room = self.start()
        self.assertEqual(room.question_ids, [q.pk for q in self.qs] + [self.tf.pk])
        self.assertRegex(room.code, r"^\d{4}$")
        self.assertEqual((room.leader, room.host), (self.lead, self.lead))
        data = self.client.get(reverse("bank:live_questions", args=[room.pk])).json()["questions"]
        self.assertEqual(len(data), 4)
        self.assertNotIn("correct_ids", data[0])          # no answers before the reveal

    def test_everyone_answers_then_the_reveal_shows_who_picked_what(self):
        room = self.start()
        self.act(room, self.lead, "invite", friends=[self.sara.pk])
        self.enter(room, self.sara)
        opts = list(self.qs[0].options.all())
        self.act(room, self.sara, "answer", position=0, selected=opts[1].pk)
        self.act(room, self.lead, "answer", position=0, selected=opts[2].pk)
        s = self.state(room, self.sara)
        self.assertFalse(s["revealed"])
        self.assertNotIn("key", s)
        self.assertTrue(all(m["answered"] for m in s["members"]))
        self.assertIn("Only the leader", self.act(room, self.sara, "reveal")["error"])
        s = self.act(room, self.lead, "reveal")["state"]
        self.assertEqual(s["key"]["correct_ids"], [opts[1].pk])
        self.assertEqual(s["picks"], {str(opts[1].pk): [self.sara.pk], str(opts[2].pk): [self.lead.pk]})
        self.assertEqual((s["right"], s["wrong"]), ([self.sara.pk], [self.lead.pk]))
        self.assertEqual(s["faces"][str(self.sara.pk)]["name"], "sara")
        self.assertIn("already shown", self.act(room, self.sara, "answer", position=0, selected=opts[0].pk)["error"])
        # true / false
        self.act(room, self.lead, "go", position=3)
        self.act(room, self.sara, "answer", position=3, answers={str(self.tf_opts[0].pk): True, str(self.tf_opts[1].pk): False})
        self.assertTrue(LiveAnswer.objects.get(user=self.sara, position=3).correct)
        self.assertIn("moved on", self.act(room, self.sara, "answer", position=0, selected=opts[0].pk)["error"])

    def test_timer_reveals_by_itself(self):
        room = self.start()
        self.act(room, self.lead, "timer", seconds=30)
        room.refresh_from_db()
        self.assertIsNotNone(room.deadline)
        LiveRoom.objects.filter(pk=room.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        self.assertTrue(self.state(room, self.lead)["revealed"])
        self.assertIn("Pick a time", self.act(room, self.lead, "timer", seconds=7)["error"])

    def test_late_join_by_code_lands_on_the_current_question(self):
        room = self.start()
        self.act(room, self.lead, "go", position=2)
        self.client.force_login(self.stranger)
        self.client.post(reverse("bank:live_join"), {"code": room.code})
        self.assertFalse(room.members.filter(user=self.stranger).exists())       # not friends with anyone in it
        self.client.force_login(self.omar)
        r = self.client.post(reverse("bank:live_join"), {"code": room.code})
        self.assertRedirects(r, reverse("bank:live_room", args=[room.pk]))
        self.assertEqual(self.state(room, self.omar)["position"], 2)

    def test_room_holds_ten_people(self):
        room = self.start()
        for i in range(9):
            pal = User.objects.create_user(f"pal{i}")
            LiveMember.objects.create(room=room, user=pal)
        friends._make_friends(self.lead, self.stranger)
        self.client.force_login(self.stranger)
        self.client.post(reverse("bank:live_join"), {"code": room.code})
        self.assertFalse(room.members.filter(user=self.stranger).exists())
        self.assertIn("Only 0 more", self.act(room, self.lead, "invite", friends=[self.sara.pk])["error"])

    def test_leader_who_disappears_hands_over(self):
        room = self.start()
        self.act(room, self.lead, "invite", friends=[self.sara.pk])
        self.enter(room, self.sara)
        LiveMember.objects.filter(room=room, user=self.lead).update(last_seen=timezone.now() - timedelta(minutes=2))
        s = self.state(room, self.sara)
        self.assertEqual(s["leader"], self.sara.pk)
        self.assertTrue(s["me"]["leader"])

    def test_saves_reach_a_member_who_never_opened_it_and_can_be_undone(self):
        room = self.start()
        self.act(room, self.lead, "invite", friends=[self.sara.pk, self.omar.pk])
        self.assertEqual(self.notify.call_count, 2)                              # the invitations
        LiveMember.objects.filter(room=room, user=self.omar).update(follow_saves=False)
        self.act(room, self.lead, "save")                                         # question 0, for everyone
        self.assertTrue(SavedQuestion.objects.filter(user=self.sara, question=self.qs[0]).exists())  # phone not open
        self.assertFalse(SavedQuestion.objects.filter(user=self.omar, question=self.qs[0]).exists())  # not following
        self.assertTrue(SavedQuestion.objects.filter(user=self.lead, question=self.qs[0]).exists())
        # un-save takes it back, but never one they wrote a note on
        QuestionNote.objects.create(user=self.lead, question=self.qs[0], text="mine")
        self.act(room, self.lead, "save")
        self.assertFalse(SavedQuestion.objects.filter(user=self.sara, question=self.qs[0]).exists())
        self.assertTrue(SavedQuestion.objects.filter(user=self.lead, question=self.qs[0]).exists())
        self.act(room, self.lead, "save")
        self.act(room, self.lead, "go", position=1)
        self.act(room, self.lead, "save")
        # Omar starts following: he gets the saves so far
        self.act(room, self.omar, "follow", on=True)
        self.assertEqual(SavedQuestion.objects.filter(user=self.omar).count(), 2)

        self.notify.reset_mock()
        self.act(room, self.lead, "end")
        told = {c.args[0] for c in self.notify.call_args_list}
        self.assertEqual(told, {self.sara, self.omar})                           # not the leader
        self.assertIn("2 questions", self.notify.call_args_list[0].args[1])

        self.client.force_login(self.sara)
        page = self.client.get(reverse("bank:messages"))
        self.assertContains(page, "2 questions added to your Saved")
        member = LiveMember.objects.get(room=room, user=self.sara)
        self.client.post(reverse("bank:live_member_action", args=[member.pk]), {"action": "undo"})
        self.assertFalse(SavedQuestion.objects.filter(user=self.sara).exists())

    def test_invite_shows_in_messages_with_a_badge(self):
        room = self.start()
        self.act(room, self.lead, "invite", friends=[self.sara.pk])
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(reverse("bank:my_space")), 'aria-label="1 new"')
        page = self.client.get(reverse("bank:messages"))
        self.assertContains(page, "invited you to a live quiz")
        self.assertContains(page, reverse("bank:live_room", args=[room.pk]))
        self.assertNotContains(self.client.get(reverse("bank:my_space")), 'aria-label="1 new"')

    def test_summary_lists_hard_questions_and_saves_them(self):
        room = self.start()
        self.act(room, self.lead, "invite", friends=[self.sara.pk])
        self.enter(room, self.sara)
        for pos in range(3):
            opts = list(self.qs[pos].options.all())
            self.act(room, self.lead, "go", position=pos)
            self.act(room, self.sara, "answer", position=pos, selected=opts[1 if pos else 0].pk)
            self.act(room, self.lead, "answer", position=pos, selected=opts[1].pk)
            self.act(room, self.lead, "reveal")
        s = self.act(room, self.lead, "end")["state"]
        self.assertTrue(s["ended"])
        self.assertEqual(s["summary"]["covered"], 3)
        self.assertEqual([h["position"] for h in s["summary"]["hard"]], [0])
        self.assertEqual(s["summary"]["board"][0]["name"], "amna")
        self.assertEqual([(r["place"], r["right"]) for r in s["summary"]["board"]], [(1, 3), (2, 2)])
        self.assertIn("ended", self.act(room, self.lead, "go", position=1)["error"])
        s = self.act(room, self.lead, "save_hard")["state"]
        self.assertTrue(s["summary"]["hard_saved"])
        self.assertTrue(SavedQuestion.objects.filter(user=self.sara, question=self.qs[0]).exists())

    def test_strangers_cant_see_a_room(self):
        room = self.start()
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("bank:live_room", args=[room.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("bank:live_state", args=[room.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("bank:live_questions", args=[room.pk])).status_code, 404)

    def test_my_space_quiz_goes_live(self):
        good, _ = my_space.read_reply("Q: One?\nA. x\nB. y\nANSWER: B\nEXPLANATION: why\n\nQ: Two?\nTYPE: DESC\nANSWER: yes")
        quiz = PersonalQuiz.objects.create(user=self.lead, title="Mine", questions=good)
        QuizAccess.objects.create(user=self.lead, quiz=quiz)
        room = self.start(ids=[2, 1, 9], mine=str(quiz.pk))
        self.assertEqual((room.source, room.question_ids), ("mine", [2, 1]))
        data = self.client.get(reverse("bank:live_questions", args=[room.pk])).json()["questions"]
        self.assertEqual([q["stem"] for q in data], ["Two?", "One?"])
        self.assertNotIn("answer", data[0])
        self.act(room, self.lead, "go", position=1)
        self.act(room, self.lead, "answer", position=1, selected=data[1]["options"][1]["id"])
        s = self.act(room, self.lead, "reveal")["state"]
        self.assertTrue(s["me"]["correct"])
        self.assertFalse(s["can_save"])
        self.assertIn("can't be saved", self.act(room, self.lead, "save")["error"])

    def test_idle_rooms_end(self):
        room = self.start()
        LiveRoom.objects.filter(pk=room.pk).update(last_activity=timezone.now() - timedelta(hours=1))
        from django.core.cache import cache
        cache.delete("live-idle-check")
        live.close_idle_rooms()
        room.refresh_from_db()
        self.assertIsNotNone(room.ended_at)

    def test_quiz_page_offers_go_live(self):
        self.client.force_login(self.lead)
        page = self.client.get(reverse("bank:quiz", args=[self.qs[0].bank.slug]))
        self.assertContains(page, f'data-live-start="{reverse("bank:live_start")}"')


class LiveBlockTests(TestCase):
    def setUp(self):
        self.lead = User.objects.create_user("amna")
        self.sara = User.objects.create_user("sara")
        friends._make_friends(self.lead, self.sara)
        patch.object(friends, "notify").start()
        self.addCleanup(patch.stopall)
        bank = QuestionBank.objects.create(title="Long chapter")
        self.qs = []
        for i in range(55):
            q = Question.objects.create(bank=bank, stem=f"Q{i}?", kind=Question.MCQ)
            Option.objects.create(question=q, text="right", is_correct=True, order=0)
            Option.objects.create(question=q, text="wrong", is_correct=False, order=1)
            self.qs.append(q)
        self.client.force_login(self.lead)
        self.client.post(reverse("bank:live_start"), json.dumps({"ids": [q.pk for q in self.qs], "title": "Long"}),
                         content_type="application/json")
        self.room = LiveRoom.objects.get()
        LiveMember.objects.create(room=self.room, user=self.sara, joined_at=timezone.now(), last_seen=timezone.now())

    def act(self, user, action, **data):
        self.client.force_login(user)
        return self.client.post(reverse("bank:live_act", args=[self.room.pk]), json.dumps({"action": action, **data}),
                                content_type="application/json").json()

    def test_block_report_for_everyone_then_next_block(self):
        for pos in (0, 49):
            self.act(self.lead, "go", position=pos)
            wrong = self.qs[pos].options.get(is_correct=False).pk
            self.act(self.sara, "answer", position=pos, selected=wrong)
            self.act(self.lead, "reveal")
        self.assertEqual(self.act(self.lead, "finish_block")["state"]["results_block"], 0)
        self.client.force_login(self.sara)
        s = self.client.get(reverse("bank:live_state", args=[self.room.pk])).json()
        self.assertEqual(s["block_size"], 50)
        self.assertEqual(s["results_block"], 0)
        self.assertEqual(s["block_summary"]["hard_count"], 2)
        self.act(self.lead, "save_hard")
        self.assertEqual(SavedQuestion.objects.filter(user=self.sara).count(), 2)
        s = self.act(self.lead, "go", position=50)["state"]
        self.assertIsNone(s["results_block"])
        self.assertEqual(s["position"], 50)

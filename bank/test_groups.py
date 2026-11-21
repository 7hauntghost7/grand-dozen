import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from . import groups
from .models import (GroupMember, GroupPost, GroupQuiz, GroupSave, LiveMember, LiveRoom, Option, PersonalQuiz,
                     Question, QuestionBank, QuizAccess, StudyGroup)


class GroupTests(TestCase):
    def setUp(self):
        self.amna = User.objects.create_user("amna")
        self.sara = User.objects.create_user("sara")
        self.omar = User.objects.create_user("omar")
        self.admin = User.objects.create_user("boss", is_staff=True)
        self.notify = patch.object(groups, "notify").start()
        self.addCleanup(patch.stopall)
        bank = QuestionBank.objects.create(title="Renal")
        self.q = Question.objects.create(bank=bank, stem="GFR?", kind=Question.MCQ)
        Option.objects.create(question=self.q, text="125 ml/min", is_correct=True, order=0)
        Option.objects.create(question=self.q, text="5 ml/min", order=1)

    def make_group(self, user=None, name="Renal squad"):
        self.client.force_login(user or self.amna)
        self.client.post(reverse("bank:groups"), {"action": "create", "name": name, "emoji": "🫘"})
        return StudyGroup.objects.get(name=name)

    def act(self, group, who, **data):
        self.client.force_login(who)
        return self.client.post(reverse("bank:group_action", args=[group.code]), data)

    def test_create_join_request_and_accept(self):
        g = self.make_group()
        self.assertEqual(g.owner, self.amna)
        self.assertTrue(groups.is_member(g, self.amna))
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(g.get_absolute_url()), "Ask to join")
        self.act(g, self.sara, action="request")
        self.assertEqual(GroupMember.objects.get(group=g, user=self.sara).status, "pending")
        self.notify.assert_called()                                      # the owner hears about it
        self.assertIn("Request sent", self.client.get(g.get_absolute_url()).content.decode())
        self.act(g, self.omar, action="request")
        self.act(g, self.sara, action="accept", user=self.omar.pk)       # not the owner
        self.assertEqual(GroupMember.objects.get(group=g, user=self.omar).status, "pending")
        self.client.force_login(self.amna)
        self.assertContains(self.client.get(reverse("bank:groups")), "2 waiting")
        GroupMember.objects.filter(group=g, user=self.omar).update(status="active")
        self.assertContains(self.client.get(reverse("bank:groups")), "<b>2</b> members")
        GroupMember.objects.filter(group=g, user=self.omar).update(status="pending")
        self.act(g, self.amna, action="accept", user=self.sara.pk)
        self.assertTrue(groups.is_member(g, self.sara))
        self.assertTrue(GroupPost.objects.filter(group=g, kind="joined", author=self.sara).exists())
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(g.get_absolute_url()), "joined the group")

    def test_join_with_the_link_or_code(self):
        g = self.make_group()
        self.client.force_login(self.sara)
        r = self.client.post(reverse("bank:groups"), {"action": "code", "code": f"https://site/groups/{g.code}/"})
        self.assertRedirects(r, g.get_absolute_url(), fetch_redirect_response=False)

    def test_academic_secretary_holds_everyone_and_only_admins_post(self):
        acad = StudyGroup.objects.get(is_everyone=True)
        self.assertEqual(acad.name, "Academic Secretary")
        self.assertTrue(groups.is_member(acad, self.omar))
        self.assertIn(acad, groups.my_groups(self.omar))
        self.act(acad, self.omar, action="announce", text="hello all")
        self.assertFalse(acad.posts.exists())
        self.act(acad, self.admin, action="announce", text="Exam on Sunday")
        self.assertEqual(acad.posts.get().text, "Exam on Sunday")
        self.assertNotIn(acad, groups.postable_groups(self.omar))
        self.assertIn(acad, groups.postable_groups(self.admin))

    def test_save_question_to_group_and_practise(self):
        g = self.make_group()
        self.client.force_login(self.amna)
        r = self.client.post(reverse("bank:api_group_save", args=[self.q.pk]), json.dumps({"group": g.code}),
                             content_type="application/json")
        self.assertTrue(r.json()["saved"])
        self.assertTrue(GroupSave.objects.filter(group=g, question=self.q).exists())
        self.assertTrue(GroupPost.objects.filter(group=g, kind="saved").exists())
        data = self.client.get(reverse("bank:group_saved_questions", args=[g.code])).json()["questions"]
        self.assertEqual([q["id"] for q in data], [self.q.pk])
        self.assertIn(g.code, self.client.get(reverse("bank:api_groups_postable") + f"?question={self.q.pk}").content.decode())
        self.client.force_login(self.sara)                                # not a member
        r = self.client.post(reverse("bank:api_group_save", args=[self.q.pk]), json.dumps({"group": g.code}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(reverse("bank:group_saved_questions", args=[g.code])).status_code, 404)

    def test_share_a_my_space_quiz_with_the_group(self):
        g = self.make_group()
        GroupMember.objects.create(group=g, user=self.sara, status="active")
        quiz = PersonalQuiz.objects.create(user=self.amna, title="Kidney", questions=[])
        QuizAccess.objects.create(user=self.amna, quiz=quiz)
        self.client.force_login(self.amna)
        self.client.post(reverse("bank:my_space_action", args=[quiz.pk]), {"action": "share", "groups": [g.code]})
        self.assertTrue(GroupQuiz.objects.filter(group=g, quiz=quiz).exists())
        self.client.force_login(self.sara)
        self.assertEqual(self.client.get(reverse("bank:my_space_quiz", args=[quiz.pk])).status_code, 200)
        self.act(g, self.sara, action="add_quiz", quiz=quiz.pk)
        self.assertTrue(QuizAccess.objects.filter(user=self.sara, quiz=quiz).exists())
        self.client.force_login(self.omar)                                # not in the group
        self.assertEqual(self.client.get(reverse("bank:my_space_quiz", args=[quiz.pk])).status_code, 404)

    def test_live_quiz_posted_to_group_lets_members_join(self):
        g = self.make_group()
        GroupMember.objects.create(group=g, user=self.sara, status="active")   # not amna's friend
        self.client.force_login(self.amna)
        self.client.post(reverse("bank:live_start"), json.dumps({"ids": [self.q.pk], "title": "Renal"}),
                         content_type="application/json")
        room = LiveRoom.objects.get()
        r = self.client.post(reverse("bank:live_act", args=[room.pk]), json.dumps({"action": "group", "code": g.code}),
                             content_type="application/json")
        self.assertNotIn("error", r.json())
        self.assertTrue(GroupPost.objects.filter(group=g, kind="live", live_room=room).exists())
        self.client.force_login(self.sara)
        self.assertContains(self.client.get(g.get_absolute_url()), "Live now")
        r = self.client.post(reverse("bank:live_group_join", args=[room.pk]))
        self.assertRedirects(r, reverse("bank:live_room", args=[room.pk]), fetch_redirect_response=False)
        self.assertTrue(LiveMember.objects.filter(room=room, user=self.sara).exists())
        self.client.force_login(self.omar)                                # outsider
        self.assertEqual(self.client.post(reverse("bank:live_group_join", args=[room.pk])).status_code, 404)

    def test_owner_manages_and_members_leave(self):
        g = self.make_group()
        GroupMember.objects.create(group=g, user=self.sara, status="active")
        GroupMember.objects.create(group=g, user=self.omar, status="active")
        self.act(g, self.amna, action="remove", user=self.omar.pk)
        self.assertFalse(groups.is_member(g, self.omar))
        self.act(g, self.sara, action="leave")
        self.assertFalse(groups.is_member(g, self.sara))
        self.act(g, self.amna, action="leave")                            # the owner can't just leave
        self.assertTrue(groups.is_member(g, self.amna))
        old = g.code
        self.act(g, self.amna, action="new_link")
        g.refresh_from_db()
        self.assertNotEqual(g.code, old)
        self.act(g, self.amna, action="delete")
        self.assertFalse(StudyGroup.objects.filter(pk=g.pk).exists())

import json

from django.contrib.auth.models import Group, User
from django.contrib.sessions.models import Session
from django.test import TestCase
from django.urls import reverse

from . import control
from .models import (AuditEntry, ChapterDay, ControlAccess, DailyVisit, HourCount, Option, Question, QuestionBank,
                     QuestionStat, StudyGroup)


class ControlTests(TestCase):
    def setUp(self):
        control._last_mark.clear()
        self.owner = User.objects.create_superuser("owner", password="x")
        self.helper = User.objects.create_user("helper", is_staff=True)
        self.student = User.objects.create_user("sara", password="old-pass")
        Group.objects.get_or_create(name="Managers")

    def test_only_superusers_and_the_list_get_in(self):
        for name in ("control", "control_people", "control_add", "control_log"):
            self.client.force_login(self.student)
            self.assertEqual(self.client.get(reverse(f"bank:{name}")).status_code, 404)
            self.client.force_login(self.helper)                       # staff isn't enough
            self.assertEqual(self.client.get(reverse(f"bank:{name}")).status_code, 404)
            self.client.force_login(self.owner)
            self.assertEqual(self.client.get(reverse(f"bank:{name}")).status_code, 200)
        self.assertContains(self.client.get(reverse("bank:home")), "🛡 Control")
        # The owner adds the helper to the list
        self.client.post(reverse("bank:control_person", args=[self.helper.pk]), {"action": "grant"})
        self.assertTrue(ControlAccess.objects.filter(user=self.helper).exists())
        self.client.force_login(self.helper)
        self.assertEqual(self.client.get(reverse("bank:control")).status_code, 200)
        # ...who can't hand access to anyone else, or touch the owner
        self.client.post(reverse("bank:control_person", args=[self.student.pk]), {"action": "grant"})
        self.assertFalse(ControlAccess.objects.filter(user=self.student).exists())
        self.client.post(reverse("bank:control_person", args=[self.owner.pk]), {"action": "deactivate"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse("bank:home")), "🛡 Control")

    def test_roles(self):
        self.client.force_login(self.owner)
        url = reverse("bank:control_person", args=[self.student.pk])
        self.client.post(url, {"action": "role", "role": "manager"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_staff)
        self.assertTrue(self.student.groups.filter(name="Managers").exists())
        self.assertEqual(control.role_of(self.student), "manager")
        self.client.post(url, {"action": "role", "role": "team"})
        self.student.refresh_from_db()
        self.assertEqual(control.role_of(self.student), "team")
        self.client.post(url, {"action": "role", "role": "student"})
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_staff)
        self.assertTrue(AuditEntry.objects.filter(area="account", action="role", actor=self.owner).count() >= 3)
        self.client.post(reverse("bank:control_person", args=[self.owner.pk]), {"action": "role", "role": "student"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_staff)                              # not on yourself

    def test_password_sign_out_and_deactivate(self):
        other = self.client_class()
        other.post(reverse("bank:login"), {"username": "sara", "password": "old-pass"})
        self.assertTrue(other.get(reverse("bank:home")).status_code == 200)
        self.client.force_login(self.owner)
        url = reverse("bank:control_person", args=[self.student.pk])
        self.client.post(url, {"action": "password"})
        page = self.client.get(reverse("bank:control_people")).content.decode()
        self.assertIn("New password for", page)
        self.assertNotIn("New password for", self.client.get(reverse("bank:control_people")).content.decode())  # once
        self.student.refresh_from_db()
        self.assertFalse(self.student.check_password("old-pass"))
        self.assertEqual(other.get(reverse("bank:home")).status_code, 302)    # signed out
        self.client.post(url, {"action": "deactivate"})
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)
        self.client.post(url, {"action": "activate"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        actions = set(AuditEntry.objects.values_list("action", flat=True))
        self.assertTrue({"password", "deactivate", "activate"} <= actions)

    def test_bulk_accounts(self):
        self.client.force_login(self.owner)
        names = "Ahmed Ali Hassan\nSara Omer\nAhmed Ali\nMohamed Osman, m.osman\nأحمد علي\n\n"
        r = self.client.post(reverse("bank:control_add"), {"names": names, "university": "uofk", "year": "2"})
        created = r.context["created"]
        self.assertEqual([c["username"] for c in created], ["ahmed.ali", "sara.omer", "ahmed.ali2", "m.osman", "student"])
        u = User.objects.get(username="ahmed.ali")
        self.assertTrue(u.check_password(created[0]["password"]))
        self.assertEqual((u.profile.display_name, u.profile.year), ("Ahmed Ali Hassan", 2))
        self.assertContains(r, created[0]["password"])
        self.assertTrue(AuditEntry.objects.filter(action="bulk").exists())
        r = self.client.post(reverse("bank:control_add"), {"names": "Sara Omer"})
        self.assertEqual(r.context["created"][0]["username"], "sara.omer2")

    def test_audit_question_changes(self):
        bank = QuestionBank.objects.create(title="Renal", slug="renal")
        q = Question.objects.create(bank=bank, stem="GFR?", kind=Question.MCQ)
        a = Option.objects.create(question=q, text="125", is_correct=False, order=0)
        self.assertFalse(AuditEntry.objects.exists())                     # no person: not logged
        token = control._actor.set(self.owner)
        try:
            q.stem = "Normal GFR?"
            q.save()
            a.is_correct = True
            a.save()
            q.is_in_review_bin = True
            q.save()
            q.delete()
        finally:
            control._actor.reset(token)
        summaries = list(AuditEntry.objects.order_by("id").values_list("summary", flat=True))
        self.assertIn("Edited question", summaries[0])
        self.assertIn("answer key", summaries[1])
        self.assertIn("to the review bin", summaries[2])
        self.assertIn("Deleted question", summaries[3])
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("bank:control_log") + "?area=question"), "Normal GFR?")

    def test_group_deletion_is_logged(self):
        group = StudyGroup.objects.create(name="Old squad", owner=self.student, code="old12345")
        from .models import GroupMember
        GroupMember.objects.create(group=group, user=self.student, status="active")
        self.client.force_login(self.student)
        self.client.post(reverse("bank:group_action", args=[group.code]), {"action": "delete"})
        self.assertTrue(AuditEntry.objects.filter(area="group", summary__contains="Old squad").exists())

    def test_activity_is_site_wide(self):
        bank = QuestionBank.objects.create(title="Renal", slug="renal")
        q1 = Question.objects.create(bank=bank, stem="A?", kind=Question.MCQ)
        q2 = Question.objects.create(bank=bank, stem="B?", kind=Question.MCQ)
        self.client.force_login(self.student)
        self.client.get(reverse("bank:home"))
        self.assertTrue(DailyVisit.objects.filter(user=self.student).exists())
        self.assertIsNotNone(User.objects.get(pk=self.student.pk).profile.last_seen)
        body = json.dumps({"answers": [{"id": q1.pk, "ok": True}, {"id": q2.pk, "ok": False}, {"id": 999999, "ok": True},
                                       {"id": "x", "ok": True}]})
        r = self.client.post(reverse("bank:api_activity_answers"), body, content_type="text/plain")   # sendBeacon, no CSRF
        self.assertEqual(r.json()["counted"], 2)
        self.assertEqual(ChapterDay.objects.get(bank=bank).answers, 2)
        self.assertEqual(QuestionStat.objects.get(question=q2).missed, 1)
        self.assertEqual(sum(HourCount.objects.values_list("answers", flat=True)), 2)
        self.client.logout()
        self.assertNotEqual(self.client.post(reverse("bank:api_activity_answers"), body, content_type="text/plain").status_code, 200)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("bank:control")).content.decode()
        self.assertIn("Renal", page)
        self.assertNotIn("sara", page)                                     # no one's own numbers


class HiddenAccountTests(TestCase):
    def setUp(self):
        control._last_mark.clear()
        self.owner = User.objects.create_superuser("owner", password="x")
        self.helper = User.objects.create_user("helper", is_staff=True)
        ControlAccess.objects.create(user=self.helper)
        managers, _ = Group.objects.get_or_create(name="Managers")
        from django.contrib.auth.models import Permission
        managers.permissions.add(*Permission.objects.filter(codename__in=["view_user", "change_user", "view_profile"]))
        self.helper.groups.add(managers)
        self.secret = User.objects.create_user("secret.one")

    def test_only_the_owner_sees_hidden_accounts(self):
        self.client.force_login(self.helper)
        url = reverse("bank:control_person", args=[self.secret.pk])
        self.client.post(url, {"action": "hide"})                         # helpers can't hide
        from .models import HiddenAccount
        self.assertFalse(HiddenAccount.objects.exists())
        self.client.force_login(self.owner)
        self.client.post(url, {"action": "hide"})
        self.client.post(url, {"action": "password"})
        self.assertTrue(HiddenAccount.objects.filter(user=self.secret).exists())
        self.assertContains(self.client.get(reverse("bank:control_people")), "secret.one")
        self.assertContains(self.client.get(reverse("bank:control_log")), "secret.one")
        self.client.force_login(self.helper)
        self.assertNotContains(self.client.get(reverse("bank:control_people")), "secret.one")
        self.assertNotContains(self.client.get(reverse("bank:control_people") + "?q=secret"), "secret.one")
        self.assertNotContains(self.client.get(reverse("bank:control_log")), "secret.one")
        self.assertEqual(self.client.post(url, {"action": "deactivate"}).status_code, 404)
        self.assertNotContains(self.client.get("/admin/auth/user/"), "secret.one")
        self.assertNotEqual(self.client.get(f"/admin/auth/user/{self.secret.pk}/change/").status_code, 200)
        self.assertContains(self.client.get("/admin/auth/user/"), "helper")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/admin/auth/user/"), "secret.one")
        self.client.post(url, {"action": "unhide"})
        self.client.force_login(self.helper)
        self.assertContains(self.client.get(reverse("bank:control_people")), "secret.one")


class FirstPasswordTests(TestCase):
    def setUp(self):
        control._last_mark.clear()
        self.owner = User.objects.create_superuser("owner", password="x")
        self.client.force_login(self.owner)
        r = self.client.post(reverse("bank:control_add"), {"names": "Ahmed Ali"})
        self.cred = r.context["created"][0]
        self.client.logout()

    def login(self):
        self.client.post(reverse("bank:login"), {"username": self.cred["username"], "password": self.cred["password"]})

    def test_new_accounts_choose_their_own_password(self):
        self.login()
        r = self.client.get(reverse("bank:my_space"))
        self.assertRedirects(r, reverse("bank:welcome") + "?next=/my-space/", fetch_redirect_response=False)
        page = self.client.get(reverse("bank:welcome") + "?next=/my-space/")
        self.assertContains(page, "Welcome, Ahmed")
        self.assertEqual(self.client.post(reverse("bank:api_activity_answers"), "{}", content_type="text/plain").status_code, 200)
        r = self.client.post(reverse("bank:welcome"), {"password1": "short", "password2": "short"})
        self.assertContains(r, "too short")
        r = self.client.post(reverse("bank:welcome"), {"password1": "kidneys-2026", "password2": "kidneys-2027"})
        self.assertContains(r, "don’t match")
        r = self.client.post(reverse("bank:welcome"), {"password1": self.cred["password"], "password2": self.cred["password"]})
        self.assertContains(r, "not the password you were given")
        r = self.client.post(reverse("bank:welcome"), {"password1": "Kidneys-2026", "password2": "Kidneys-2026", "next": "/my-space/"})
        self.assertContains(r, "You’re all set, Ahmed!")
        self.assertEqual(self.client.get(reverse("bank:my_space")).status_code, 200)     # still signed in, page opens
        self.assertRedirects(self.client.get(reverse("bank:welcome")), "/", fetch_redirect_response=False)
        user = User.objects.get(username=self.cred["username"])
        self.assertTrue(user.check_password("Kidneys-2026"))
        self.assertFalse(user.profile.must_change_password)

    def test_a_reset_password_must_be_changed_too(self):
        user = User.objects.get(username=self.cred["username"])
        user.profile.must_change_password = False
        user.profile.save()
        self.client.force_login(self.owner)
        self.client.post(reverse("bank:control_person", args=[user.pk]), {"action": "password"})
        user.refresh_from_db()
        self.assertTrue(user.profile.must_change_password)
        self.assertFalse(User.objects.get(pk=self.owner.pk).profile.must_change_password)


class LibraryOrderTests(TestCase):
    def test_book_chapters_are_listed_by_number(self):
        from .models import Book, BookChapter
        book = Book.objects.create(title="Gray’s Review")
        for n in (3, 1, 10, 2):
            BookChapter.objects.create(book=book, number=n, title=f"Chapter title {n}")
        self.client.force_login(User.objects.create_user("reader"))
        page = self.client.get(book.get_absolute_url()).content.decode()
        spots = [page.index(f"Chapter title {n}") for n in (1, 2, 3, 10)]
        self.assertEqual(spots, sorted(spots))

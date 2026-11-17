from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from . import final_review
from .models import Option, Question, QuestionBank, ReviewChunk, ReviewFlag, Subject


class FinalReviewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("rev", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.subject = Subject.objects.create(name="Anatomy")
        self.thorax = QuestionBank.objects.create(subject=self.subject, semester=2, title="Thorax")
        self.limb = QuestionBank.objects.create(subject=self.subject, semester=3, title="Lower Limb")
        self.questions = []
        for i in range(5):
            q = Question.objects.create(bank=self.thorax, stem=f"Question {i}?")
            for j, text in enumerate(["Alpha", "Beta", "Gamma", "Delta"]):
                Option.objects.create(question=q, text=text, is_correct=(j == 0), order=j)
            self.questions.append(q)
        other = Subject.objects.create(name="Physiology")
        self.foreign = Question.objects.create(bank=QuestionBank.objects.create(subject=other, title="Cardio"), stem="x")

    def url(self, name, *args):
        return reverse(f"bank:{name}", args=args)

    def test_chunks_cover_every_question_and_copy_text_has_chapters_and_ids(self):
        from .models import Book, BookChapter
        chapter = BookChapter.objects.create(book=Book.objects.create(title="Gray"), number=1, title="Thorax")
        Question.objects.create(bank=self.thorax, stem="Library only", book_chapter=chapter)
        final_review.sync_chunks(self.subject, size=2)
        chunks = list(ReviewChunk.objects.filter(subject=self.subject))
        self.assertEqual([len(c.question_ids) for c in chunks], [2, 2, 1])
        response = self.client.post(self.url("final_review_chunk_copy", self.subject.slug, 1))
        text = response.json()["text"]
        self.assertIn("- Lower Limb", text)
        self.assertIn(f"#{self.questions[0].pk} [Thorax]", text)
        self.assertIn("Key: A", text)
        self.assertIn("CHUNK 1", text)
        self.assertEqual(ReviewChunk.objects.get(subject=self.subject, number=1).copied_by, self.staff)
        # New questions get their own chunk; existing ones are not shuffled.
        Question.objects.create(bank=self.limb, stem="Late arrival")
        self.assertEqual(final_review.sync_chunks(self.subject, size=2), 1)
        self.assertEqual(ReviewChunk.objects.filter(subject=self.subject).count(), 4)

    def test_paste_creates_flags_validates_and_ignores_repeats(self):
        q0, q1, q2, q3 = self.questions[:4]
        reply = f"""Here is my review:
```
CHUNK 1
{q0.pk} | MOVE | lower limb | about the femur
{q1.pk} | KEY | d | delta is right
- {q2.pk} | TF | A and B both correct
{q3.pk} | BROKEN | - | option C cut off
{q3.pk} | MOVE | Head and Neck | wrong
{self.foreign.pk} | WRONG | - | not ours
{q0.pk} | MOVE | Thorax | already there
{q0.pk} | KEY | A | key is fine
{q1.pk} | KEY | A | the key A is right
{q2.pk} | WRONG | - | no error; skip
{q3.pk} | TF | B,c | B and C are true
```"""
        final_review.sync_chunks(self.subject)
        response = self.client.post(self.url("final_review_subject", self.subject.slug), {"action": "paste", "text": reply})
        self.assertContains(response, "Added <b>5</b> flags")
        self.assertContains(response, "4 lines that only said a question is fine left out")
        self.assertContains(response, "no chapter called “Head and Neck”")
        self.assertContains(response, "is not in Anatomy")
        flags = {(f.question_id, f.kind): f for f in ReviewFlag.objects.all()}
        self.assertEqual(flags[q0.pk, "MOVE"].value, "Lower Limb")
        self.assertEqual(flags[q1.pk, "KEY"].value, "D")
        self.assertEqual((flags[q2.pk, "TF"].value, flags[q2.pk, "TF"].reason), ("A,B", "A and B both correct"))
        self.assertEqual(flags[q3.pk, "BROKEN"].reason, "option C cut off")
        self.assertEqual(flags[q3.pk, "TF"].value, "B,C")
        self.assertIsNotNone(ReviewChunk.objects.get(subject=self.subject, number=1).answered_at)
        self.client.post(self.url("final_review_subject", self.subject.slug), {"action": "paste", "text": reply})
        self.assertEqual(ReviewFlag.objects.count(), 5)

    def flag(self, question, kind, value=""):
        return ReviewFlag.objects.create(subject=self.subject, question=question, kind=kind, value=value)

    def act(self, flag, action):
        return self.client.post(self.url("final_review_flag", flag.pk), {"action": action})

    def test_one_click_fixes(self):
        q0, q1, q2, q3 = self.questions[:4]
        self.assertEqual(self.act(self.flag(q0, "MOVE", "Lower Limb"), "apply").json()["status"], "fixed")
        self.assertEqual(Question.objects.get(pk=q0.pk).bank, self.limb)

        self.act(self.flag(q1, "KEY", "C,D"), "apply")
        self.assertEqual([o.is_correct for o in q1.options.all()], [False, False, True, True])

        self.act(self.flag(q2, "TF"), "apply")
        self.assertEqual(Question.objects.get(pk=q2.pk).kind, Question.TF)
        # Letters in the flag (or named in an older flag's reason) become the true statements.
        q4 = self.questions[4]
        old = ReviewFlag.objects.create(subject=self.subject, question=q4, kind="TF", reason="A, B and D are all correct")
        self.act(old, "apply")
        self.assertEqual([o.is_correct for o in q4.options.all()], [True, True, False, True])
        self.act(self.flag(q2, "TF", "C"), "apply")
        self.assertEqual([o.is_correct for o in q2.options.all()], [False, False, True, False])

        broken, other = self.flag(q3, "BROKEN"), self.flag(q3, "WRONG")
        self.assertEqual(self.act(broken, "apply").status_code, 400)
        self.assertEqual(self.act(broken, "review").json()["status"], "reviewed")
        self.assertTrue(Question.objects.get(pk=q3.pk).is_in_review_bin)
        self.assertEqual(ReviewFlag.objects.get(pk=other.pk).status, "reviewed")
        self.assertEqual(self.act(broken, "reopen").json()["status"], "open")

    def test_restart_clears_open_flags_and_chunks_for_admins_only(self):
        final_review.sync_chunks(self.subject)
        ReviewChunk.objects.update(copied_by=self.staff, answered_at="2026-11-16T10:00Z", copied_at="2026-11-16T10:00Z")
        kept = ReviewFlag.objects.create(subject=self.subject, question=self.questions[0], kind="KEY", value="B", status="fixed")
        ReviewFlag.objects.create(subject=self.subject, question=self.questions[1], kind="WRONG")
        url = self.url("final_review_subject", self.subject.slug)
        self.assertEqual(self.client.post(url, {"action": "restart"}).status_code, 400)  # staff, not admin
        self.assertEqual(ReviewFlag.objects.count(), 2)
        self.staff.is_superuser = True
        self.staff.save()
        self.client.post(url, {"action": "restart"})
        self.assertEqual(list(ReviewFlag.objects.values_list("pk", flat=True)), [kept.pk])
        self.assertFalse(ReviewChunk.objects.filter(answered_at__isnull=False).exists())

    def test_merge_duplicate_moves_labels_and_bins_the_copy(self):
        keep, dup = self.questions[0], self.questions[1]
        Question.objects.filter(pk=keep.pk).update(label="U of K")
        Question.objects.filter(pk=dup.pk).update(label="Nile_Valley, Neelain")
        flag = ReviewFlag.objects.create(subject=self.subject, question=dup, kind="BROKEN",
                                         reason=f"duplicate of #{keep.pk}: add the Neelain label to #{keep.pk}")
        other = ReviewFlag.objects.create(subject=self.subject, question=dup, kind="KEY", value="B")
        self.assertEqual(self.act(flag, "merge").json()["status"], "reviewed")
        keep.refresh_from_db(); dup.refresh_from_db()
        self.assertEqual(keep.label, "U of K, Nile_Valley, Neelain")
        self.assertTrue(dup.is_in_review_bin)
        self.assertEqual(ReviewFlag.objects.get(pk=other.pk).status, "reviewed")
        plain = ReviewFlag.objects.create(subject=self.subject, question=self.questions[2], kind="BROKEN", reason="garbled stem")
        self.assertEqual(self.act(plain, "merge").status_code, 400)

    def test_merge_all_duplicates_follows_chains(self):
        a, b, c = self.questions[:3]
        Question.objects.filter(pk=a.pk).update(label="U of K")
        Question.objects.filter(pk=b.pk).update(label="Nile_Valley")
        Question.objects.filter(pk=c.pk).update(label="Neelain")
        # c names b, but b is itself a duplicate of a: c's label must end up on a.
        ReviewFlag.objects.create(subject=self.subject, question=c, kind="BROKEN", reason=f"duplicate of #{b.pk}")
        ReviewFlag.objects.create(subject=self.subject, question=b, kind="BROKEN", reason=f"damaged copy of #{a.pk}, which is clean")
        url = self.url("final_review_subject", self.subject.slug)
        self.assertEqual(self.client.post(url, {"action": "merge_duplicates"}).status_code, 400)  # staff, not admin
        self.staff.is_superuser = True
        self.staff.save()
        self.assertEqual(self.client.get(url).context["n_dups"], 2)
        self.client.post(url, {"action": "merge_duplicates"})
        a.refresh_from_db()
        self.assertEqual(set(a.label.split(", ")), {"U of K", "Nile_Valley", "Neelain"})
        self.assertEqual(Question.objects.filter(pk__in=[b.pk, c.pk], is_in_review_bin=True).count(), 2)
        self.assertFalse(ReviewFlag.objects.filter(status="open").exists())

    def test_written_questions_show_empty_answers_and_explanations(self):
        q = Question.objects.create(bank=self.thorax, kind=Question.DESC, stem="Name it.", explanation="The source said X.")
        chunk = ReviewChunk.objects.create(subject=self.subject, number=9, question_ids=[q.pk])
        text = final_review.chunk_text(chunk)
        self.assertIn("Answer: (empty)", text)
        self.assertIn("Explanation: The source said X.", text)

    def test_blocks_of_100_and_claiming(self):
        for i in range(101):
            ReviewFlag.objects.create(subject=self.subject, question=self.questions[i % 5], kind="WRONG", value=str(i))
        page = self.client.get(self.url("final_review_subject", self.subject.slug))
        self.assertEqual([b["total"] for b in page.context["blocks"]], [100, 1])
        block = self.client.get(self.url("final_review_block", self.subject.slug, 2))
        self.assertEqual(len(block.context["flags"]), 1)
        self.client.post(self.url("final_review_block", self.subject.slug, 1), {"action": "claim"})
        self.assertContains(self.client.get(self.url("final_review_subject", self.subject.slug)), "rev")
        self.assertEqual(self.client.get(self.url("final_review_block", self.subject.slug, 3)).status_code, 404)

    def test_students_cannot_open_it(self):
        self.client.force_login(User.objects.create_user("stu", password="pw"))
        self.assertEqual(self.client.get(self.url("final_review")).status_code, 403)


class LoginPageTests(TestCase):
    def test_signed_in_user_skips_the_login_page_and_it_never_shows_the_quiz_reminder(self):
        user = User.objects.create_user("stu2", password="pw")
        self.assertNotContains(self.client.get(reverse("bank:login")), "resume-activity")
        self.client.force_login(user)
        response = self.client.get(reverse("bank:login") + "?next=/saved/")
        self.assertRedirects(response, "/saved/", fetch_redirect_response=False)

    def test_pages_are_never_stored_by_the_browser_and_old_phone_is_signed_out(self):
        from django.test import Client
        User.objects.create_user("phone", password="pw")
        a, b = Client(), Client()
        a.post(reverse("bank:login"), {"username": "phone", "password": "pw"})
        home = a.get("/")
        self.assertIn("no-store", home["Cache-Control"])
        self.assertIn("no-store", a.get(reverse("bank:login") + "?x=1", follow=True)["Cache-Control"])
        b.post(reverse("bank:login"), {"username": "phone", "password": "pw"})
        response = a.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertNotContains(a.get(reverse("bank:login")), "resume-activity")

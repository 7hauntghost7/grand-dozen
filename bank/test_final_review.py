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
```"""
        final_review.sync_chunks(self.subject)
        response = self.client.post(self.url("final_review_subject", self.subject.slug), {"action": "paste", "text": reply})
        self.assertContains(response, "Added <b>4</b> flags")
        self.assertContains(response, "no chapter called “Head and Neck”")
        self.assertContains(response, "is not in Anatomy")
        flags = {f.question_id: f for f in ReviewFlag.objects.all()}
        self.assertEqual((flags[q0.pk].kind, flags[q0.pk].value), ("MOVE", "Lower Limb"))
        self.assertEqual(flags[q1.pk].value, "D")
        self.assertEqual(flags[q2.pk].reason, "A and B both correct")
        self.assertEqual(flags[q3.pk].reason, "option C cut off")
        self.assertIsNotNone(ReviewChunk.objects.get(subject=self.subject, number=1).answered_at)
        self.client.post(self.url("final_review_subject", self.subject.slug), {"action": "paste", "text": reply})
        self.assertEqual(ReviewFlag.objects.count(), 4)

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

        broken, other = self.flag(q3, "BROKEN"), self.flag(q3, "WRONG")
        self.assertEqual(self.act(broken, "apply").status_code, 400)
        self.assertEqual(self.act(broken, "review").json()["status"], "reviewed")
        self.assertTrue(Question.objects.get(pk=q3.pk).is_in_review_bin)
        self.assertEqual(ReviewFlag.objects.get(pk=other.pk).status, "reviewed")
        self.assertEqual(self.act(broken, "reopen").json()["status"], "open")

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

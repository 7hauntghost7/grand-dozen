from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from . import my_space
from unittest.mock import patch

from . import friends
from .models import PersonalQuiz, QuizAccess, QuizShare

REPLY = """```
Q: Which nerve supplies the deltoid?
A. Radial
B. Axillary
C. Ulnar
D. Median
E. Musculocutaneous
ANSWER: B
EXPLANATION: Axillary nerve. (Ch. 3, p. 112)

Q: Regarding the axillary nerve:
TYPE: TF
A. It arises from the posterior cord.
B. It supplies biceps.
ANSWER: TF
EXPLANATION: (Ch. 3, p. 113)

Q: Name the boundaries of the quadrangular space.
TYPE: DESC
ANSWER: Teres minor, teres major, long head of triceps, humerus.

Q: A broken question with no answer
A. one
B. two
```"""


class PromptTests(TestCase):
    def test_sizes_types_and_additional_requests(self):
        prompt = my_space.build_prompt("few", ["tf"], "hard", "only chapter 9")
        self.assertIn("Write 20 questions.", prompt)
        self.assertIn("All 20 questions are True/False.", prompt)
        self.assertIn("TYPE: TF", prompt)
        self.assertNotIn("TYPE: DESC", prompt)
        self.assertNotIn("BATCHES", prompt)
        self.assertTrue(prompt.rstrip().endswith("ADDITIONAL REQUESTS\nonly chapter 9"))

    def test_high_is_sent_in_batches(self):
        prompt = my_space.build_prompt("high", ["mcq", "desc"], "mixed")
        self.assertIn("Write 80 questions.", prompt)
        self.assertIn("4 batches of 20", prompt)
        self.assertIn("continue", prompt)
        self.assertTrue(prompt.rstrip().endswith("ADDITIONAL REQUESTS"))


class MySpaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("amna", password="pw")
        self.client.force_login(self.user)

    def test_check_then_save_keeps_good_questions_only(self):
        url = reverse("bank:my_space_new")
        r = self.client.post(url, {"reply": REPLY, "action": "check", "size": "few", "types": ["mcq"]})
        self.assertContains(r, "Save quiz · 3")
        self.assertContains(r, "with problems")
        self.assertFalse(PersonalQuiz.objects.exists())
        r = self.client.post(url, {"reply": REPLY + "\n\n" + REPLY, "action": "save", "title": "Upper limb"})
        self.assertRedirects(r, reverse("bank:my_space"))
        quiz = PersonalQuiz.objects.get()
        self.assertEqual((quiz.user, quiz.title, len(quiz.questions)), (self.user, "Upper limb", 3))  # repeats dropped
        self.assertTrue(QuizAccess.objects.filter(user=self.user, quiz=quiz).exists())

    def test_quiz_questions_are_private_and_carry_answers(self):
        good, _ = my_space.read_reply(REPLY)
        quiz = PersonalQuiz.objects.create(user=self.user, title="Mine", questions=good)
        QuizAccess.objects.create(user=self.user, quiz=quiz)
        data = self.client.get(reverse("bank:my_space_questions", args=[quiz.pk])).json()["questions"]
        self.assertEqual([q["kind"] for q in data], ["mcq", "tf", "desc"])
        self.assertEqual(data[0]["correct_ids"], [data[0]["options"][1]["id"]])
        self.assertEqual(list(data[1]["answer_key"].values()), [True, False])
        self.assertIn("Teres minor", data[2]["answer"])
        page = self.client.get(reverse("bank:my_space_quiz", args=[quiz.pk]))
        self.assertContains(page, 'data-personal="1"')

        other = User.objects.create_user("bakri")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("bank:my_space_questions", args=[quiz.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("bank:my_space_action", args=[quiz.pk]),
                                          {"action": "delete"}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("bank:my_space")), "Mine")

    def test_rename_and_delete(self):
        quiz = PersonalQuiz.objects.create(user=self.user, title="Old", questions=[])
        QuizAccess.objects.create(user=self.user, quiz=quiz)
        url = reverse("bank:my_space_action", args=[quiz.pk])
        self.client.post(url, {"action": "rename", "title": "New"})
        self.assertContains(self.client.get(reverse("bank:my_space")), "New")
        self.client.post(url, {"action": "delete"})
        self.assertFalse(PersonalQuiz.objects.exists())

    def test_chat_lines_and_fences_between_batches_are_ignored(self):
        reply = '''Here is batch 1:
```
Q: One?
A. x
B. y
ANSWER: A
EXPLANATION: (p. 1)
```

Great, here is **batch 2** (questions 21-40):

```text
**Q:** Two?
A. x
B. y
ANSWER: B
EXPLANATION: (p. 2)
```
Type continue for more.'''
        good, problems = my_space.read_reply(reply)
        self.assertEqual(problems, [])
        self.assertEqual([(q["stem"], q["explanation"]) for q in good], [("One?", "(p. 1)"), ("Two?", "(p. 2)")])

    def test_prompt_endpoint(self):
        r = self.client.get(reverse("bank:my_space_prompt"), {"size": "normal", "types": ["mcq", "tf"]})
        self.assertIn("Write 40 questions.", r.json()["prompt"])


class SharingTests(TestCase):
    def setUp(self):
        self.amna = User.objects.create_user("amna")
        self.bakri = User.objects.create_user("bakri")
        self.stranger = User.objects.create_user("carol")
        friends._make_friends(self.amna, self.bakri)
        good, _ = my_space.read_reply(REPLY)
        self.quiz = PersonalQuiz.objects.create(user=self.amna, title="Upper limb", questions=good)
        QuizAccess.objects.create(user=self.amna, quiz=self.quiz)
        self.client.force_login(self.amna)

    def send(self, *users):
        with patch.object(friends, "notify") as notify, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("bank:my_space_action", args=[self.quiz.pk]),
                             {"action": "share", "friends": [u.pk for u in users]})
        return notify

    def test_send_to_a_friend_notifies_and_shows_in_messages(self):
        notify = self.send(self.bakri, self.stranger)          # not friends with carol: skipped
        self.assertEqual(list(QuizShare.objects.values_list("recipient__username", flat=True)), ["bakri"])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], self.bakri)
        self.assertIn("Upper limb", notify.call_args.args[1])

        self.client.force_login(self.bakri)
        self.assertContains(self.client.get(reverse("bank:my_space")), 'aria-label="1 new"')
        inbox = self.client.get(reverse("bank:messages"))
        self.assertContains(inbox, "Upper limb")
        self.assertContains(inbox, "Add to My Space")
        self.assertNotContains(self.client.get(reverse("bank:my_space")), 'aria-label="1 new"')  # seen now
        # can try it before adding
        self.assertEqual(self.client.get(reverse("bank:my_space_questions", args=[self.quiz.pk])).status_code, 200)

    def test_adding_shares_the_same_stored_quiz(self):
        self.send(self.bakri)
        share = QuizShare.objects.get()
        self.client.force_login(self.bakri)
        self.client.post(reverse("bank:message_action", args=[share.pk]), {"action": "add"})
        self.assertEqual(PersonalQuiz.objects.count(), 1)       # no copy
        page = self.client.get(reverse("bank:my_space"))
        self.assertContains(page, "Upper limb")
        self.assertContains(page, "From ")
        # bakri can't rename amna's quiz
        self.client.post(reverse("bank:my_space_action", args=[self.quiz.pk]), {"action": "rename", "title": "X"})
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.title, "Upper limb")

        # amna removes it: bakri keeps it; when bakri removes it too, it's gone
        self.client.force_login(self.amna)
        self.client.post(reverse("bank:my_space_action", args=[self.quiz.pk]), {"action": "delete"})
        self.assertTrue(PersonalQuiz.objects.exists())
        self.client.force_login(self.bakri)
        self.client.post(reverse("bank:my_space_action", args=[self.quiz.pk]), {"action": "delete"})
        self.assertFalse(PersonalQuiz.objects.exists())

    def test_strangers_cannot_open_it(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("bank:my_space_quiz", args=[self.quiz.pk])).status_code, 404)

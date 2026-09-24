from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

SEMESTER_CHOICES = [(i, f"Semester {i}") for i in range(1, 13)]


def _unique_slug(model, base, instance):
    base = slugify(base)[:200] or "item"
    slug, i = base, 2
    while model.objects.filter(slug=slug).exclude(pk=instance.pk).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug


class Subject(models.Model):
    """Top-level classification: one big block on the home page."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.CharField(max_length=200, blank=True)
    order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers appear first.")

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Subject, self.name, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bank:subject_detail", args=[self.slug])


class QuestionBank(models.Model):
    """A deck = one lesson (e.g. 'Haematology') inside a subject and semester."""
    subject = models.ForeignKey(Subject, null=True, on_delete=models.PROTECT, related_name="decks")
    semester = models.PositiveSmallIntegerField(choices=SEMESTER_CHOICES, default=1)
    title = models.CharField("deck title", max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "deck"
        ordering = ["subject__order", "semester", "title"]
        constraints = [
            models.UniqueConstraint(fields=["subject", "semester", "title"], name="uniq_deck_per_semester"),
        ]

    def __str__(self):
        return f"{self.title} (S{self.semester})"

    @property
    def path(self):
        s = self.subject.name if self.subject_id else "Unfiled"
        return f"{s} › Semester {self.semester} › {self.title}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(QuestionBank, self.title, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bank:bank_detail", args=[self.slug])


class Question(models.Model):
    MCQ, TF = "mcq", "tf"
    KIND_CHOICES = [
        (MCQ, "Multiple choice (pick one; any option marked correct is accepted)"),
        (TF, "True / False (each option is a statement)"),
    ]

    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name="questions",
                             verbose_name="deck")
    number = models.CharField("source number", max_length=20, blank=True,
                              help_text="Label from the original paper, e.g. 17. Optional.")
    kind = models.CharField(max_length=3, choices=KIND_CHOICES, default=MCQ)
    stem = models.TextField("question")
    explanation = models.TextField(blank=True)
    note = models.TextField(blank=True, help_text="Warning shown to students (ambiguous / flawed source).")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["bank_id", "id"]

    def __str__(self):
        return f"{self.number or self.pk}. {self.stem[:70]}"


class Option(models.Model):
    """MCQ: a possible answer.  TF: a statement, is_correct = 'the statement is true'."""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="options")
    text = models.TextField()
    is_correct = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:60]


class SavedQuestion(models.Model):
    """A question a user bookmarked; shown under 'Saved' in the same subject/semester/deck layout."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_questions")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="saved_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "question"], name="uniq_saved_question")]
        ordering = ["-created_at"]


class Comment(models.Model):
    """Private message from a user to the admins about a question."""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                             related_name="question_comments")
    text = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["resolved", "-created_at"]

    def __str__(self):
        return f"Comment on Q{self.question_id}: {self.text[:40]}"

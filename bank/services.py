from django.db import transaction

from .models import Option, Question, QuestionBank
from .parser import question_key


def existing_keys(bank):
    """{fingerprint: label} for every question already stored in `bank`."""
    keys = {}
    for q in bank.questions.prefetch_related("options"):
        keys[question_key(q.kind, q.stem, [o.text for o in q.options.all()])] = q.number or f"#{q.pk}"
    return keys


def find_deck(subject, semester, title):
    return QuestionBank.objects.filter(
        subject=subject, semester=semester, title__iexact=title.strip()).first()


def get_or_create_deck(subject, semester, title):
    return find_deck(subject, semester, title) or QuestionBank.objects.create(
        subject=subject, semester=semester, title=title.strip())


@transaction.atomic
def save_questions(bank, parsed, skip_duplicates=True):
    """Persist questions with clean sequential deck numbering.

    Source NO/number labels are deliberately ignored. Existing questions are
    normalized to 1..N, then newly imported questions continue from N+1.
    """
    existing = list(bank.questions.order_by("id"))
    for i, q in enumerate(existing, start=1):
        label = str(i)
        if q.number != label:
            q.number = label
            q.save(update_fields=["number"])

    next_number = len(existing) + 1
    created = bad = dup = 0
    for pq in parsed:
        if not pq.ok:
            bad += 1
            continue
        if pq.duplicate_of and skip_duplicates:
            dup += 1
            continue
        q = Question.objects.create(
            bank=bank, number=str(next_number), kind=pq.kind, stem=pq.stem,
            explanation=pq.explanation, note=pq.note,
        )
        Option.objects.bulk_create([
            Option(question=q, text=t, is_correct=c, order=i)
            for i, (t, c) in enumerate(zip(pq.options, pq.correct))
        ])
        next_number += 1
        created += 1
    return created, bad, dup


def bank_as_dicts(bank):
    for q in bank.questions.prefetch_related("options"):
        yield {
            "number": q.number, "kind": q.kind, "stem": q.stem,
            "options": [(o.text, o.is_correct) for o in q.options.all()],
            "explanation": q.explanation, "note": q.note,
        }

from django.db import transaction

from .models import Option, Question, QuestionBank
from .parser import question_key


def existing_keys(bank):
    """{fingerprint: label} for every question already stored in `bank`."""
    keys = {}
    for q in bank.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False).prefetch_related("options"):
        keys[question_key(q.kind, q.stem, [o.text for o in q.options.all()], answer=q.answer)] = q.number or f"#{q.pk}"
    return keys


def find_deck(subject, semester, title):
    return QuestionBank.objects.filter(
        subject=subject, semester=semester, title__iexact=title.strip()).first()


def get_or_create_deck(subject, semester, title, section=None):
    deck = find_deck(subject, semester, title)
    if deck:
        if section and deck.section_id not in (None, section.pk):
            raise ValueError("This deck already belongs to a different sub-subject.")
        if section and deck.section_id is None:
            deck.section = section
            deck.full_clean()
            deck.save(update_fields=["section"])
        return deck
    deck = QuestionBank(subject=subject, section=section, semester=semester, title=title.strip())
    deck.full_clean()
    deck.save()
    return deck


def _number_group(kind):
    if kind == Question.IMAGE:
        return Question.IMAGE
    return Question.DESC if kind == Question.DESC else "mcq_tf"


@transaction.atomic
def save_questions(bank, parsed, skip_duplicates=True):
    """Persist questions with sequential MCQ/TF and descriptive numbering.

    Source NO/number labels are deliberately ignored. Existing questions are
    normalized within their quiz type, then imports continue that type's sequence.
    """
    existing = list(bank.questions.order_by("id"))
    next_numbers = {"mcq_tf": 0, Question.DESC: 0, Question.IMAGE: 0}
    for i, q in enumerate(existing, start=1):
        group = _number_group(q.kind)
        next_numbers[group] += 1
        label = str(next_numbers[group])
        if q.number != label:
            q.number = label
            q.save(update_fields=["number"])

    created = bad = dup = 0
    for pq in parsed:
        if not pq.ok:
            bad += 1
            continue
        if pq.duplicate_of and skip_duplicates:
            dup += 1
            continue
        group = _number_group(pq.kind)
        next_numbers[group] += 1
        q = Question.objects.create(
            bank=bank, number=str(next_numbers[group]), kind=pq.kind, stem=pq.stem,
            answer=pq.answer_raw if pq.kind == Question.DESC else "",
            explanation=pq.explanation, note=pq.note,
        )
        Option.objects.bulk_create([
            Option(question=q, text=t, is_correct=c, order=i)
            for i, (t, c) in enumerate(zip(pq.options, pq.correct))
        ])
        created += 1
    return created, bad, dup


def bank_as_dicts(bank):
    for q in bank.questions.filter(
        is_in_sorting_bin=False, is_in_review_bin=False,
    ).exclude(kind=Question.IMAGE).prefetch_related("options"):
        yield {
            "number": q.number, "kind": q.kind, "stem": q.stem,
            "options": [(o.text, o.is_correct) for o in q.options.all()],
            "answer": q.answer, "explanation": q.explanation, "note": q.note,
        }

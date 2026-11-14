"""FINAL REVIEW: teammates check every question with a free AI chat, paste its findings back,
then work through the flagged questions in blocks of 100 with one-click fixes.

1. Each subject's questions are cut into chunks (200 by default). "Copy for AI" copies a prompt,
   the subject's exact chapter names and one chunk.
2. The AI answers with one line per problem: ``ID | TYPE | VALUE | reason``. Any answer for any
   chunk is pasted into the subject's single box; each line becomes a ReviewFlag.
3. Open flags are split into blocks of 100 that teammates claim and resolve.
"""
import re

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Question, QuestionBank, ReviewBlock, ReviewChunk, ReviewFlag, Subject, TopicSearch
from .views import staff_required

CHUNK_SIZES = (50, 100, 150, 200, 300)
DEFAULT_CHUNK = 200
BLOCK = 100
LETTERS = "ABCDEFGHIJ"


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _live_questions(subject):
    return Question.objects.filter(bank__subject=subject, is_in_sorting_bin=False, is_in_review_bin=False)


def sync_chunks(subject, size=DEFAULT_CHUNK):
    """Put questions that are in no chunk yet (new subject, or newly added questions) into new chunks."""
    have = set()
    for ids in ReviewChunk.objects.filter(subject=subject).values_list("question_ids", flat=True):
        have.update(ids)
    new = [pk for pk in _live_questions(subject).filter(image_asset__isnull=True).order_by("bank__semester", "bank__title", "id")
           .values_list("pk", flat=True) if pk not in have]
    if not new:
        return 0
    start = (ReviewChunk.objects.filter(subject=subject).order_by("-number").values_list("number", flat=True).first() or 0) + 1
    ReviewChunk.objects.bulk_create([
        ReviewChunk(subject=subject, number=start + i, question_ids=new[j:j + size])
        for i, j in enumerate(range(0, len(new), size))
    ])
    return len(new)


def chapter_names(subject):
    return list(QuestionBank.objects.filter(subject=subject).order_by("semester", "title").values_list("title", flat=True))


PROMPT = """You are checking {subject} questions from a medical question bank before an exam. Read every question below carefully.

Reply ONLY with one line for each problem you find, in exactly this format:
ID | TYPE | VALUE | short reason

TYPE must be one of:
MOVE   | exact chapter name from the CHAPTERS list | the question clearly belongs to another chapter of this subject
KEY    | the correct letter(s), e.g. D or A,C | the marked answer is wrong
TF     | - | more than one option is correct, so it should become a True/False question
WRONG  | - | a factual error in the question or its options (not the answer key)
BROKEN | - | text is cut off, garbled, options missing or repeated, or it makes no sense

Rules:
- Flag only when you are confident; most questions are fine.
- For MOVE, copy the chapter name exactly as written in the list, without the part in brackets.
- A question may get more than one line. Never rewrite the questions.
- Skip questions that depend on an image you cannot see.
- Start your reply with the line CHUNK {number}. If nothing is wrong, reply CHUNK {number} then NONE.

Example reply:
CHUNK {number}
12345 | MOVE | {example_chapter} | it is about the {example_chapter_lower}
12346 | KEY | D | the ulnar nerve supplies adductor pollicis
12347 | TF | - | A and C are both correct

CHAPTERS:
{chapters}

QUESTIONS (CHUNK {number}):
"""


def _question_text(q):
    lines = [f"#{q.pk} [{q.bank.title}]" + (" (has an image you cannot see)" if q.image_asset_id else "")]
    lines.append("Q: " + " ".join(q.stem.split()))
    options = list(q.options.all())
    if q.kind == Question.DESC or not options:
        if q.answer:
            lines.append("Answer: " + " ".join(q.answer.split()))
        return "\n".join(lines)
    if q.kind == Question.TF:
        lines.append("(True/False: each statement is marked T or F)")
        for letter, o in zip(LETTERS, options):
            lines.append(f"{letter}. {' '.join(o.text.split())} ({'T' if o.is_correct else 'F'})")
        return "\n".join(lines)
    for letter, o in zip(LETTERS, options):
        lines.append(f"{letter}. {' '.join(o.text.split())}")
    lines.append("Key: " + (",".join(l for l, o in zip(LETTERS, options) if o.is_correct) or "none marked"))
    return "\n".join(lines)


def chunk_text(chunk):
    chapters = chapter_names(chunk.subject)
    example = chapters[0] if chapters else "Chapter name"
    head = PROMPT.format(subject=chunk.subject.name, number=chunk.number, chapters="\n".join(f"- {c}" for c in chapters),
                         example_chapter=example, example_chapter_lower=example.lower())
    qs = (Question.objects.filter(pk__in=chunk.question_ids).select_related("bank")
          .prefetch_related("options").order_by("bank__semester", "bank__title", "id"))
    return head + "\n\n".join(_question_text(q) for q in qs) + "\n"


LINE = re.compile(r"^#?(\d+)\s*\|\s*([A-Za-z]+)\s*(?:\|\s*([^|]*?)\s*)?(?:\|\s*(.*?)\s*)?$")


def parse_answer(subject, text):
    """Turn pasted AI replies into flag data. Returns (flags, errors, chunk_numbers)."""
    titles = {_norm(t): t for t in chapter_names(subject)}
    flags, errors, chunks, ids = [], [], set(), set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().strip("`*•-– ").strip()
        if not line:
            continue
        m = re.match(r"^chunk\s*#?\s*(\d+)\b", line, re.I)
        if m:
            chunks.add(int(m.group(1)))
            continue
        m = LINE.match(line)
        if not m:
            continue  # chatter, "NONE", headings
        pk, kind, value, reason = int(m.group(1)), m.group(2).upper(), (m.group(3) or "").strip(), (m.group(4) or "").strip()
        if kind not in dict(ReviewFlag.KIND_CHOICES):
            errors.append((n, raw, f"unknown type {kind}"))
            continue
        if kind in (ReviewFlag.TF, ReviewFlag.WRONG, ReviewFlag.BROKEN):
            if value and value != "-" and not reason:
                reason = value
            value = ""
        elif kind == ReviewFlag.KEY:
            letters = re.findall(r"\b([A-J])\b", value.upper())
            if not letters:
                errors.append((n, raw, "KEY needs the correct letter(s)"))
                continue
            value = ",".join(sorted(set(letters)))
        elif kind == ReviewFlag.MOVE:
            title = titles.get(_norm(value)) or titles.get(_norm(re.sub(r"\s*\([^)]*\)\s*$", "", value)))
            if not title:
                errors.append((n, raw, f"no chapter called “{value}” in {subject.name}"))
                continue
            value = title
        flags.append({"pk": pk, "kind": kind, "value": value, "reason": reason[:1000], "line": n, "raw": raw})
        ids.add(pk)
    known = dict(_live_questions(subject).filter(pk__in=ids).values_list("pk", "bank__title"))
    kept = []
    for f in flags:
        if f["pk"] not in known:
            errors.append((f["line"], f["raw"], f"question {f['pk']} is not in {subject.name}"))
        elif f["kind"] == ReviewFlag.MOVE and f["value"] == known[f["pk"]]:
            continue  # already in that chapter
        else:
            kept.append(f)
    return kept, errors, chunks


def add_flags(subject, flags, user):
    added = 0
    for f in flags:
        try:
            with transaction.atomic():
                ReviewFlag.objects.create(subject=subject, question_id=f["pk"], kind=f["kind"], value=f["value"],
                                          reason=f["reason"], created_by=user)
            added += 1
        except IntegrityError:
            pass  # the same flag was pasted before
    return added


def _blocks(subject):
    ids = list(ReviewFlag.objects.filter(subject=subject).order_by("id").values_list("id", "status"))
    claims = {b.number: b for b in ReviewBlock.objects.filter(subject=subject).select_related("claimed_by")}
    out = []
    for i in range(0, len(ids), BLOCK):
        part = ids[i:i + BLOCK]
        number = i // BLOCK + 1
        out.append({"number": number, "total": len(part), "done": sum(1 for _, s in part if s != ReviewFlag.OPEN),
                    "claim": claims.get(number)})
    return out


@staff_required
def home(request):
    subjects = Subject.objects.annotate(
        n_flags=Count("review_flags", distinct=True),
        n_open=Count("review_flags", filter=Q(review_flags__status=ReviewFlag.OPEN), distinct=True),
    ).order_by("order", "name")
    return render(request, "bank/final_review_home.html", {"subjects": subjects, "here": "final_review"})


@staff_required
def subject_page(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    report = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "paste":
            flags, errors, chunks = parse_answer(subject, request.POST.get("text", ""))
            added = add_flags(subject, flags, request.user)
            if chunks:
                ReviewChunk.objects.filter(subject=subject, number__in=chunks, answered_at__isnull=True).update(answered_at=timezone.now())
            report = {"added": added, "repeats": len(flags) - added, "errors": errors, "chunks": sorted(chunks)}
            if not flags and not errors and not chunks:
                messages.error(request, "No answer lines were found. Each line should look like: 12345 | KEY | D | reason")
        elif action == "resize":
            try:
                size = int(request.POST.get("size", ""))
            except ValueError:
                size = 0
            if size in CHUNK_SIZES:
                ReviewChunk.objects.filter(subject=subject, copied_at__isnull=True).delete()
                sync_chunks(subject, size)
                messages.success(request, f"Chunks that nobody has copied yet now hold {size} questions each.")
            return redirect("bank:final_review_subject", slug=subject.slug)
    sync_chunks(subject, _current_size(subject))
    chunks = list(ReviewChunk.objects.filter(subject=subject).select_related("copied_by"))
    flags = ReviewFlag.objects.filter(subject=subject)
    return render(request, "bank/final_review_subject.html", {
        "subject": subject, "chunks": chunks, "blocks": _blocks(subject), "report": report,
        "n_questions": sum(len(c.question_ids) for c in chunks),
        "n_copied": sum(1 for c in chunks if c.copied_at), "n_answered": sum(1 for c in chunks if c.answered_at),
        "n_flags": flags.count(), "n_open": flags.filter(status=ReviewFlag.OPEN).count(),
        "sizes": CHUNK_SIZES, "size": _current_size(subject), "here": "final_review",
    })


def _current_size(subject):
    first = ReviewChunk.objects.filter(subject=subject, copied_at__isnull=True).order_by("number").first()
    return len(first.question_ids) if first and len(first.question_ids) in CHUNK_SIZES else DEFAULT_CHUNK


@staff_required
@require_POST
def chunk_copy(request, slug, number):
    chunk = get_object_or_404(ReviewChunk.objects.select_related("subject"), subject__slug=slug, number=number)
    if not chunk.copied_at:
        chunk.copied_by, chunk.copied_at = request.user, timezone.now()
        chunk.save(update_fields=["copied_by", "copied_at"])
    return JsonResponse({"text": chunk_text(chunk), "by": str(chunk.copied_by or "")})


@staff_required
def block_page(request, slug, number):
    subject = get_object_or_404(Subject, slug=slug)
    ids = list(ReviewFlag.objects.filter(subject=subject).order_by("id").values_list("id", flat=True)[(number - 1) * BLOCK:number * BLOCK])
    if number < 1 or not ids:
        raise Http404
    block, _ = ReviewBlock.objects.get_or_create(subject=subject, number=number)
    if request.method == "POST" and request.POST.get("action") in ("claim", "release"):
        if request.POST["action"] == "claim":
            block.claimed_by, block.claimed_at = request.user, timezone.now()
        elif block.claimed_by_id == request.user.pk or request.user.is_superuser:
            block.claimed_by, block.claimed_at = None, None
        block.save()
        return redirect("bank:final_review_block", slug=slug, number=number)
    flags = list(ReviewFlag.objects.filter(pk__in=ids).select_related("question__bank", "resolved_by")
                 .prefetch_related("question__options").order_by("id"))
    for f in flags:
        f.options = [(LETTERS[i], o) for i, o in enumerate(f.question.options.all())]
    return render(request, "bank/final_review_block.html", {
        "subject": subject, "block": block, "number": number, "flags": flags,
        "done": sum(1 for f in flags if f.status != ReviewFlag.OPEN),
        "has_prev": number > 1, "has_next": ReviewFlag.objects.filter(subject=subject).count() > number * BLOCK,
        "here": "final_review",
    })


def _to_bin(question, which):
    now = timezone.now()
    question.is_in_sorting_bin = which == "sort"
    question.is_in_review_bin = which == "review"
    question.sorting_bin_at = now if which == "sort" else None
    question.review_bin_at = now if which == "review" else None
    question.save(update_fields=["is_in_sorting_bin", "is_in_review_bin", "sorting_bin_at", "review_bin_at", "updated_at"])


@staff_required
@require_POST
def flag_action(request, pk):
    flag = get_object_or_404(ReviewFlag.objects.select_related("question__bank"), pk=pk)
    action = request.POST.get("action")
    q = flag.question
    now = timezone.now()
    with transaction.atomic():
        if action == "apply":
            if flag.kind == ReviewFlag.MOVE:
                target = QuestionBank.objects.filter(subject=flag.subject, title=flag.value).first()
                if target is None:
                    return HttpResponseBadRequest("That chapter no longer exists.")
                old = q.bank_id
                Question.objects.filter(pk=q.pk).update(bank=target, updated_at=now)
                TopicSearch.objects.filter(bank__in=[old, target.pk]).delete()
            elif flag.kind == ReviewFlag.KEY:
                options = list(q.options.all())
                wanted = {LETTERS.index(l) for l in flag.value.split(",") if l in LETTERS}
                if not options or max(wanted, default=99) >= len(options):
                    return HttpResponseBadRequest("This question does not have those options.")
                for i, o in enumerate(options):
                    if o.is_correct != (i in wanted):
                        o.is_correct = i in wanted
                        o.save(update_fields=["is_correct"])
                Question.objects.filter(pk=q.pk).update(updated_at=now)
            elif flag.kind == ReviewFlag.TF:
                if q.kind != Question.MCQ:
                    return HttpResponseBadRequest("Only multiple-choice questions can be turned into True/False.")
                Question.objects.filter(pk=q.pk).update(kind=Question.TF, updated_at=now)
            else:
                return HttpResponseBadRequest("This kind of flag has no automatic fix; edit the question instead.")
            status = ReviewFlag.FIXED
        elif action == "ok":
            status = ReviewFlag.DISMISSED
        elif action == "done":
            status = ReviewFlag.FIXED
        elif action in ("sort", "review"):
            _to_bin(q, action)
            status = ReviewFlag.SORTED if action == "sort" else ReviewFlag.REVIEWED
            # The question has left the bank, so its other open flags are settled too.
            ReviewFlag.objects.filter(question=q, status=ReviewFlag.OPEN).exclude(pk=flag.pk).update(
                status=status, resolved_by=request.user, resolved_at=now)
        elif action == "reopen":
            status = ReviewFlag.OPEN
        else:
            return HttpResponseBadRequest("Unknown action.")
        flag.status = status
        flag.resolved_by = None if status == ReviewFlag.OPEN else request.user
        flag.resolved_at = None if status == ReviewFlag.OPEN else now
        flag.save(update_fields=["status", "resolved_by", "resolved_at"])
    return JsonResponse({"status": flag.status, "label": flag.get_status_display(), "by": str(request.user)})

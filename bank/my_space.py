"""MY SPACE: students make private quizzes from their own textbook with any AI chat.

1. The student picks a size and question types; the page writes a prompt in the site's question format.
2. They paste it into an AI chat together with their source and copy the reply back.
3. The reply is read with the normal import parser and saved as a PersonalQuiz only they can see.
"""
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import friends, parser
from .models import PersonalQuiz, QuizAccess, QuizShare

SIZES = (("few", "Few", 20), ("normal", "Normal", 40), ("high", "High", 80))
BATCH = 20                      # High is written in batches of this size: AI chats cut long replies short
TYPES = (("mcq", "MCQ (single best answer)"), ("tf", "True / False"), ("desc", "Descriptive (flashcards)"))
DIFFICULTIES = (("mixed", "Mixed"), ("easy", "Easy"), ("hard", "Hard"))
MAX_TEXT = 400_000
MAX_QUESTIONS = 200

FORMAT = {
    "mcq": """MCQ (single best answer):
Q: Which nerve supplies the deltoid muscle?
A. Radial nerve
B. Axillary nerve
C. Musculocutaneous nerve
D. Ulnar nerve
E. Median nerve
ANSWER: B
EXPLANATION: The axillary nerve (C5-C6) supplies deltoid and teres minor. (Ch. 3, p. 112)

- Exactly 5 options, A to E, each on its own line.
- Exactly ONE correct answer. No "all of the above" or "none of the above".""",
    "tf": """True / False:
Q: Regarding the axillary nerve:
TYPE: TF
A. It arises from the posterior cord.
B. It passes through the quadrangular space.
C. It supplies the biceps.
D. It carries fibres from C5 and C6.
E. It supplies the skin over the lower deltoid.
ANSWER: TTFTT
EXPLANATION: It supplies deltoid and teres minor, not biceps (musculocutaneous). (Ch. 3, p. 113)

- Write TYPE: TF under the question.
- Exactly 5 statements, A to E. ANSWER gives T or F for each statement, in order (5 letters).""",
    "desc": """Descriptive (short answer, used as a flashcard):
Q: List the boundaries of the quadrangular space.
TYPE: DESC
ANSWER: Teres minor above, teres major below, long head of triceps medially, surgical neck of humerus laterally.
EXPLANATION: The axillary nerve and posterior circumflex humeral vessels pass through it. (Ch. 3, p. 114)

- Write TYPE: DESC under the question. No options.
- ANSWER is the model answer, short and complete.""",
}

DIFFICULTY_TEXT = {
    "mixed": "a mix: about a third easy recall, a third moderate, a third hard (applied or clinical)",
    "easy": "easy: direct recall of key facts and definitions",
    "hard": "hard: applied and clinical reasoning, the level of a final exam",
}


def build_prompt(size, types, difficulty, extra=""):
    """The prompt the student pastes into an AI chat with their source."""
    count = {key: n for key, _name, n in SIZES}.get(size, 40)
    types = [t for t, _ in TYPES if t in types] or ["mcq"]
    names = {"mcq": "MCQ (single best answer)", "tf": "True/False", "desc": "Descriptive (short answer)"}
    if len(types) == 1:
        mix = f"All {count} questions are {names[types[0]]}."
    else:
        mix = ("Split them about evenly between these types: " + ", ".join(names[t] for t in types)
               + ". Mix the types through the list instead of grouping them.")
    batches = count // BATCH if count > BATCH * 2 else 1
    lines = [
        "I have attached my study source (a textbook or lecture). Write practice questions from it "
        "for a medical student.",
        "",
        "TASK",
        f"- Write {count} questions.",
        f"- {mix}",
        f"- Difficulty: {DIFFICULTY_TEXT.get(difficulty, DIFFICULTY_TEXT['mixed'])}.",
        "- Use ONLY the attached source. Every answer must be stated in it; no outside facts.",
        "- Cover the whole source evenly, the important points first. Don't repeat a fact.",
        "- Wrong options must be believable, from the same topic, and clearly wrong according to the source.",
        "- Every question needs an EXPLANATION that ends with where it is in the source in brackets, "
        "e.g. (Ch. 3, p. 112) or (Slide 14).",
        "- If the source is too short for this many good questions, write fewer and tell me.",
    ]
    if batches > 1:
        lines += [
            "",
            "BATCHES",
            f"- {count} questions is too long for one reply, so send them in {batches} batches of {BATCH}.",
            f"- Send batch 1 (questions 1-{BATCH}) now, then stop. Each time I write \"continue\", "
            "send the next batch. Never repeat a question from an earlier batch.",
            "- Every batch uses exactly the same format and goes in its own code block.",
        ]
    lines += [
        "",
        "FORMAT (follow it exactly: my website reads it automatically)",
        "- Put the questions inside ONE plain-text code block so I can copy them in one go.",
        "- No headings, bold, bullet points or question numbers. Every question starts with \"Q:\".",
        "- Leave one empty line between questions.",
        "",
    ]
    for t in types:
        lines += [FORMAT[t], ""]
    lines += ["ADDITIONAL REQUESTS", (extra or "").strip()]
    return "\n".join(lines).rstrip() + "\n"


def convert_prompt():
    """For questions the student already has (a past paper, a lecture's MCQs): turn them into the site format
    without changing them."""
    lines = [
        "Below are some questions I already have. Convert them into the exact format shown, so my website can "
        "read them. Don't write new questions.",
        "",
        "RULES",
        "- Keep every question. Don't skip, merge, shorten or reword any question, option, answer or explanation.",
        "- Fix only obvious typos.",
        "- Keep the correct answer that is given. If a question has no answer, work out the correct one.",
        "- If an explanation is given, copy it in full after EXPLANATION:. If there is none, leave the "
        "EXPLANATION line out.",
        "- MCQs with more than 5 options keep all of them (up to H).",
        "- A question that is a list of true/false statements is a True/False question.",
        "- A question with no options is Descriptive.",
        "",
        "FORMAT (follow it exactly)",
        "- Put all the questions inside ONE plain-text code block.",
        "- No headings, bold, bullet points or question numbers. Every question starts with \"Q:\".",
        "- Leave one empty line between questions.",
        "- If there are more than 40 questions, send 40, then stop. Each time I write \"continue\", send the next 40.",
        "",
    ]
    for t in ("mcq", "tf", "desc"):
        lines += [FORMAT[t].replace("- Exactly 5 options, A to E, each on its own line.",
                                    "- Each option on its own line, starting with its letter.")
                  .replace("- Exactly ONE correct answer. No \"all of the above\" or \"none of the above\".",
                           "- ANSWER is the letter of the correct option.")
                  .replace("- Exactly 5 statements, A to E. ANSWER gives T or F for each statement, in order (5 letters).",
                           "- Keep all its statements. ANSWER gives T or F for each statement, in order "
                           "(one letter per statement)."), ""]
    lines += ["MY QUESTIONS", "(paste them here)"]
    return "\n".join(lines) + "\n"


def check_built(items):
    """Questions written one by one on the New quiz page. Returns (stored questions, problems)."""
    good, problems = [], []
    if not isinstance(items, list):
        return good, ["No questions were sent."]
    for n, item in enumerate(items[:MAX_QUESTIONS + 1], 1):
        if not isinstance(item, dict):
            problems.append(f"Question {n} is empty.")
            continue
        kind = item.get("kind")
        stem = str(item.get("stem") or "").strip()[:4000]
        explanation = str(item.get("explanation") or "").strip()[:4000]
        options = [str(o).strip()[:1000] for o in item.get("options") or [] if str(o).strip()]
        correct = [c is True for c in (item.get("correct") or [])]
        if not stem:
            problems.append(f"Question {n} has no question text.")
            continue
        if kind == "desc":
            answer = str(item.get("answer") or "").strip()[:4000]
            if not answer:
                problems.append(f"Question {n} needs an answer.")
                continue
            good.append({"kind": "desc", "stem": stem, "options": [], "correct": [], "answer": answer,
                         "explanation": explanation})
        elif kind in ("mcq", "tf"):
            raw = [str(o).strip() for o in item.get("options") or []]
            correct = [c for o, c in zip(raw, correct + [False] * len(raw)) if o]
            if kind == "mcq" and len(options) < 2:
                problems.append(f"Question {n} needs at least 2 options.")
            elif kind == "tf" and not options:
                problems.append(f"Question {n} needs at least one statement.")
            elif len(options) > 8:
                problems.append(f"Question {n} has more than 8 options.")
            elif kind == "mcq" and not any(correct):
                problems.append(f"Question {n}: tap the correct option.")
            else:
                good.append({"kind": kind, "stem": stem, "options": options, "correct": correct, "answer": "",
                             "explanation": explanation})
        else:
            problems.append(f"Question {n} has no type.")
    if len(good) > MAX_QUESTIONS:
        problems.append(f"A quiz can hold up to {MAX_QUESTIONS} questions.")
    return good, problems


def _to_store(q):
    return {"kind": q.kind, "stem": q.stem, "options": list(q.options), "correct": list(q.correct),
            "answer": q.answer_raw.strip() if q.kind == parser.DESC else "", "explanation": q.explanation.strip()}


def clean_reply(text):
    """Drop what AI chats wrap around the questions: code fences, bold marks, and chat lines
    between batches ("Here is batch 2…") that would otherwise stick to the question before them."""
    out, after_blank = [], True
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            after_blank = True
            continue
        line = re.sub(r"^\s*\*\*([A-Za-z]+\s*:)\*\*", r"\1", line).replace("**", "")
        if not line.strip():
            out.append("")
            after_blank = True
            continue
        if after_blank and not parser.FIELD_RE.match(line) and not parser.OPT_RE.match(line):
            continue                     # a chat line, not part of a question
        out.append(line)
        after_blank = False
    return "\n".join(out)


def read_reply(text):
    """Parse the AI's reply. Returns (good questions as stored dicts, problems as (number, stem, errors))."""
    result = parser.parse(clean_reply(text))
    good, problems = [], []
    seen = set()
    for i, q in enumerate(result.questions, 1):
        if q.errors:
            problems.append((i, q.stem[:90], q.errors))
        elif q.key not in seen:          # an AI sometimes repeats a question in a later batch
            seen.add(q.key)
            good.append(_to_store(q))
    return good, problems


def serialize(quiz):
    """Questions in the shape the quiz runner (quiz.js) reads, with answers for local checking."""
    out = []
    for i, q in enumerate(quiz.questions, 1):
        opts = [{"id": i * 10 + j, "letter": chr(65 + j), "text": text} for j, text in enumerate(q.get("options", []))]
        correct = q.get("correct", [])
        item = {
            "id": i, "number": str(i), "label": "", "kind": q["kind"], "stem": q["stem"], "saved": False,
            "my_note": "", "is_lab": False, "image_answer_mode": None, "options": opts,
            "multi": q["kind"] == "mcq" and sum(correct) > 1,
            "answer": q.get("answer", ""), "explanation": q.get("explanation", ""), "note": "",
        }
        if q["kind"] == "tf":
            item["answer_key"] = {str(o["id"]): bool(c) for o, c in zip(opts, correct)}
        elif q["kind"] == "mcq":
            item["correct_ids"] = [o["id"] for o, c in zip(opts, correct) if c]
        out.append(item)
    return out


def _mix(quiz):
    return {kind: sum(q.get("kind") == kind for q in quiz.questions) for kind in ("mcq", "tf", "desc")}


def _reachable_quiz(user, pk):
    from .groups import my_groups
    return get_object_or_404(
        PersonalQuiz.objects.filter(Q(access__user=user) | Q(shares__recipient=user)
                                    | Q(group_shares__group__in=my_groups(user))).distinct(), pk=pk)


def _visible_quiz(user, pk):
    """A quiz in the user's My Space, one a friend sent them, or one shared with a group they're in.
    A running group challenge they haven't finished doesn't count: its answers would be on the page."""
    from .challenge import lock_for
    quiz = _reachable_quiz(user, pk)
    if lock_for(user, quiz):
        raise Http404
    return quiz


def _drop_if_unused(quiz):
    """The questions are stored once: delete them only when no one has the quiz or a message waiting about it."""
    if not quiz.access.exists() and not quiz.shares.filter(added_at__isnull=True).exists():
        quiz.delete()


@login_required
def home(request):
    rows = list(QuizAccess.objects.filter(user=request.user).select_related("quiz__user__profile"))
    quizzes = []
    for row in rows:
        quiz = row.quiz
        quiz.mix = _mix(quiz)
        quiz.mine = quiz.user_id == request.user.pk
        quizzes.append(quiz)
    from .groups import postable_groups
    from .challenge import DURATIONS
    from .models import SavedExam
    exams = list(SavedExam.objects.filter(user=request.user).select_related("exam__bank", "exam__subject"))
    for row in exams:
        tries = row.exam.__class__.objects.filter(user=request.user, question_ids=row.exam.question_ids,
                                                  submitted_at__isnull=False)
        row.tries = tries.count()
        row.last = tries.order_by("-submitted_at").first()
        row.open = row.exam.__class__.objects.filter(user=request.user, question_ids=row.exam.question_ids,
                                                     submitted_at__isnull=True).order_by("-started_at").first()
    return render(request, "bank/my_space.html", {"quizzes": quizzes, "friends": friends.friends_of(request.user),
                                                  "groups": postable_groups(request.user), "durations": DURATIONS,
                                                  "exams": exams})


@login_required
@require_POST
def exam_action(request, pk):
    """A mock exam kept in My Space: start a fresh try, or take it out of My Space."""
    from .models import SavedExam
    from .views import _create_shared_practice, _expire_exam_if_needed
    row = get_object_or_404(SavedExam.objects.select_related("exam"), pk=pk, user=request.user)
    if request.POST.get("action") == "remove":
        row.delete()
        messages.success(request, f"Removed “{row.exam.title}” from My Space.")
        return redirect("bank:my_space")
    unfinished = row.exam.__class__.objects.filter(user=request.user, question_ids=row.exam.question_ids,
                                                   submitted_at__isnull=True).order_by("-started_at").first()
    if unfinished:
        _expire_exam_if_needed(unfinished)
        unfinished.refresh_from_db()
        if unfinished.submitted_at is None:
            return redirect("bank:exam_attempt", attempt_id=unfinished.pk)
    attempt = _create_shared_practice(row.exam, request.user)
    return redirect("bank:exam_attempt", attempt_id=attempt.pk)


@login_required
def new_quiz(request):
    size = request.POST.get("size") or request.GET.get("size") or "normal"
    types = request.POST.getlist("types") or ["mcq"]
    title = (request.POST.get("title") or "").strip()[:120]
    reply = request.POST.get("reply", "")
    good, problems, checked = [], [], False
    if request.method == "POST":
        if len(reply) > MAX_TEXT:
            messages.error(request, "That reply is too long. Split it into two quizzes.")
        elif not reply.strip():
            messages.error(request, "Paste the AI's reply first.")
        else:
            good, problems = read_reply(reply)
            checked = True
            if len(good) > MAX_QUESTIONS:
                messages.error(request, f"A quiz can hold up to {MAX_QUESTIONS} questions; this reply has {len(good)}.")
            elif request.POST.get("action") == "save" and good:
                quiz = PersonalQuiz.objects.create(
                    user=request.user, questions=good,
                    title=title or f"My quiz · {timezone.localdate():%d %b %Y}")
                QuizAccess.objects.create(user=request.user, quiz=quiz)
                messages.success(request, f"Saved “{quiz.title}” with {len(good)} question{'s' if len(good) != 1 else ''}.")
                return redirect("bank:my_space")
    mode = request.POST.get("mode") or request.GET.get("mode") or "ai"
    return render(request, "bank/my_space_new.html", {
        "mode": mode if mode in ("ai", "write", "paste") else "ai", "convert_prompt": convert_prompt(),
        "sizes": SIZES, "types": TYPES, "difficulties": DIFFICULTIES, "size": size, "chosen_types": types,
        "difficulty": request.POST.get("difficulty", "mixed"), "extra": request.POST.get("extra", ""),
        "title": title, "reply": reply, "good": good, "problems": problems, "checked": checked, "batch": BATCH,
        "prompt": build_prompt(size, types, request.POST.get("difficulty", "mixed"), request.POST.get("extra", "")),
    })


@login_required
@require_POST
def build_quiz(request):
    """Save a quiz written question by question on the New quiz page (sent as JSON)."""
    from .views import json_body
    body = json_body(request) or {}
    good, problems = check_built(body.get("questions"))
    if problems or not good:
        return JsonResponse({"errors": problems or ["Add at least one question."]}, status=400)
    title = str(body.get("title") or "").strip()[:120] or f"My quiz · {timezone.localdate():%d %b %Y}"
    quiz = PersonalQuiz.objects.create(user=request.user, questions=good, title=title)
    QuizAccess.objects.create(user=request.user, quiz=quiz)
    messages.success(request, f"Saved “{quiz.title}” with {len(good)} question{'s' if len(good) != 1 else ''}.")
    return JsonResponse({"url": reverse("bank:my_space")})


@login_required
def edit_quiz(request, pk):
    """Fix a saved quiz question by question. Everyone who has it sees the fix (it's stored once)."""
    quiz = get_object_or_404(PersonalQuiz, pk=pk, user=request.user)
    from .models import ChallengeAttempt
    return render(request, "bank/my_space_edit.html", {
        "quiz": quiz, "questions": quiz.questions,
        "locked": ChallengeAttempt.objects.filter(challenge__quiz=quiz).exists(),
        "shared": quiz.access.exclude(user=request.user).count() + quiz.group_shares.count(),
    })


@login_required
@require_POST
def save_quiz(request, pk):
    """Save the edit page. Once people have taken it as a challenge, its questions keep their places and
    types (their answers point at them); finished tries are scored again with the corrected key."""
    from .challenge import score
    from .models import ChallengeAttempt
    from .views import json_body
    quiz = get_object_or_404(PersonalQuiz, pk=pk, user=request.user)
    body = json_body(request) or {}
    good, problems = check_built(body.get("questions"))
    if problems or not good:
        return JsonResponse({"errors": problems or ["A quiz needs at least one question."]}, status=400)
    attempts = list(ChallengeAttempt.objects.filter(challenge__quiz=quiz))
    if attempts and [q["kind"] for q in good] != [q.get("kind") for q in quiz.questions]:
        return JsonResponse({"errors": ["People have already taken this quiz as a challenge, so you can fix the "
                                        "wording, options and answers, but not add, remove, move or change the type "
                                        "of questions."]}, status=400)
    quiz.questions = good
    quiz.title = str(body.get("title") or "").strip()[:120] or quiz.title
    quiz.save(update_fields=["questions", "title", "updated_at"])
    for attempt in attempts:
        if attempt.finished_at:
            attempt.score, attempt.total = score(quiz, attempt.answers)
            attempt.save(update_fields=["score", "total"])
    messages.success(request, f"Saved your changes to “{quiz.title}”.")
    return JsonResponse({"url": reverse("bank:my_space")})


@login_required
def prompt_text(request):
    """The prompt for the current choices (the new-quiz page asks for it whenever a choice changes)."""
    return JsonResponse({"prompt": build_prompt(
        request.GET.get("size", "normal"), request.GET.getlist("types"),
        request.GET.get("difficulty", "mixed"), request.GET.get("extra", "")[:2000])})


@login_required
@ensure_csrf_cookie
def take_quiz(request, pk):
    from .challenge import lock_for
    lock = lock_for(request.user, _reachable_quiz(request.user, pk))
    if lock:
        return redirect("bank:challenge", lock.group.code, lock.pk)
    quiz = _visible_quiz(request.user, pk)
    in_space = quiz.access.filter(user=request.user).exists()
    return render(request, "bank/quiz.html", {
        "shuffle": "1" if request.GET.get("shuffle") else "0",
        "title": quiz.title, "key": f"mine:{quiz.pk}", "question_type": "mine", "personal": True, "live_mine": quiz.pk,
        "api": reverse("bank:my_space_questions", args=[quiz.pk]),
        "back": reverse("bank:my_space") if in_space else reverse("bank:messages"),
        "review": False, "edit_api": "", "can_sort": False, "sort_api": "",
    })


@login_required
def quiz_questions(request, pk):
    quiz = _visible_quiz(request.user, pk)
    return JsonResponse({"title": quiz.title, "questions": serialize(quiz)})


@login_required
@require_POST
def quiz_action(request, pk):
    access = get_object_or_404(QuizAccess.objects.select_related("quiz"), quiz_id=pk, user=request.user)
    quiz = access.quiz
    action = request.POST.get("action")
    if action == "delete":
        access.delete()
        _drop_if_unused(quiz)
        messages.success(request, f"Removed “{quiz.title}” from your space.")
    elif action == "rename" and quiz.user_id == request.user.pk:
        title = (request.POST.get("title") or "").strip()[:120]
        if title:
            quiz.title = title
            quiz.save(update_fields=["title", "updated_at"])
    elif action == "share":
        from .groups import share_quiz
        from .models import StudyGroup
        challenge = ({"reveal": request.POST.get("reveal"), "days": request.POST.get("days"),
                      "allow_hiding": request.POST.get("allow_hiding") == "1"}
                     if request.POST.get("challenge") == "1" else None)
        shared_to = [g.name for g in StudyGroup.objects.filter(code__in=request.POST.getlist("groups"))
                     if share_quiz(g, quiz, request.user, challenge)]
        if shared_to:
            messages.success(request, (f"Challenge “{quiz.title}” is live in {', '.join(shared_to)}." if challenge
                                       else f"Shared “{quiz.title}” with {', '.join(shared_to)}."))
        ids = {int(i) for i in request.POST.getlist("friends") if i.isdigit()}
        targets = [p.user for p in friends.friends_of(request.user) if p.user_id in ids]
        sent = 0
        name = friends._name(request.user)
        for friend in targets:
            if quiz.access.filter(user=friend).exists():
                continue                                    # they already have it
            share, created = QuizShare.objects.get_or_create(
                recipient=friend, quiz=quiz, defaults={"sender": request.user})
            if not created:                                 # sent before: bring the message back to the top
                share.sender, share.created_at, share.seen_at, share.added_at = request.user, timezone.now(), None, None
                share.save()
            friends.notify(friend, f"{name} sent you a quiz: {quiz.title}", reverse("bank:messages"))
            sent += 1
        if sent:
            messages.success(request, f"Sent “{quiz.title}” to {sent} friend{'s' if sent != 1 else ''}.")
        elif targets:
            messages.info(request, "They already have this quiz in their space.")
        elif not shared_to:
            messages.error(request, "Pick at least one friend or group.")
    return redirect("bank:my_space")


def unseen_count(user):
    from . import live
    return QuizShare.objects.filter(recipient=user, seen_at__isnull=True).count() + live.unseen_count(user)


@login_required
def inbox(request):
    shares = list(QuizShare.objects.filter(recipient=request.user)
                  .select_related("sender__profile", "quiz"))
    have = set(QuizAccess.objects.filter(user=request.user, quiz__in=[s.quiz_id for s in shares])
               .values_list("quiz_id", flat=True))
    for share in shares:
        share.quiz.mix = _mix(share.quiz)
        share.in_space = share.quiz_id in have
        share.is_new = share.seen_at is None
    QuizShare.objects.filter(recipient=request.user, seen_at__isnull=True).update(seen_at=timezone.now())
    from . import live
    live.close_idle_rooms()
    items = [{"type": "share", "time": s.created_at, "share": s, "is_new": s.is_new, "sender": s.sender}
             for s in shares] + live.inbox_items(request.user)
    items.sort(key=lambda item: item["time"], reverse=True)
    live.mark_seen(request.user)
    return render(request, "bank/messages.html", {"items": items})


@login_required
@require_POST
def inbox_action(request, pk):
    share = get_object_or_404(QuizShare.objects.select_related("quiz"), pk=pk, recipient=request.user)
    quiz = share.quiz
    if request.POST.get("action") == "add":
        QuizAccess.objects.get_or_create(user=request.user, quiz=quiz)
        share.added_at = share.added_at or timezone.now()
        share.save(update_fields=["added_at"])
        messages.success(request, f"Added “{quiz.title}” to My Space.")
    elif request.POST.get("action") == "remove":
        share.delete()
        _drop_if_unused(quiz)
    return redirect("bank:messages")

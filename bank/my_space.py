"""MY SPACE: students make private quizzes from their own textbook with any AI chat.

1. The student picks a size and question types; the page writes a prompt in the site's question format.
2. They paste it into an AI chat together with their source and copy the reply back.
3. The reply is read with the normal import parser and saved as a PersonalQuiz only they can see.
"""
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
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


def _visible_quiz(user, pk):
    """A quiz in the user's My Space, one a friend sent them, or one shared with a group they're in."""
    from .groups import my_groups
    return get_object_or_404(
        PersonalQuiz.objects.filter(Q(access__user=user) | Q(shares__recipient=user)
                                    | Q(group_shares__group__in=my_groups(user))).distinct(), pk=pk)


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
    return render(request, "bank/my_space.html", {"quizzes": quizzes, "friends": friends.friends_of(request.user),
                                                  "groups": postable_groups(request.user)})


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
    return render(request, "bank/my_space_new.html", {
        "sizes": SIZES, "types": TYPES, "difficulties": DIFFICULTIES, "size": size, "chosen_types": types,
        "difficulty": request.POST.get("difficulty", "mixed"), "extra": request.POST.get("extra", ""),
        "title": title, "reply": reply, "good": good, "problems": problems, "checked": checked, "batch": BATCH,
        "prompt": build_prompt(size, types, request.POST.get("difficulty", "mixed"), request.POST.get("extra", "")),
    })


@login_required
def prompt_text(request):
    """The prompt for the current choices (the new-quiz page asks for it whenever a choice changes)."""
    return JsonResponse({"prompt": build_prompt(
        request.GET.get("size", "normal"), request.GET.getlist("types"),
        request.GET.get("difficulty", "mixed"), request.GET.get("extra", "")[:2000])})


@login_required
@ensure_csrf_cookie
def take_quiz(request, pk):
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
        shared_to = [g.name for g in StudyGroup.objects.filter(code__in=request.POST.getlist("groups"))
                     if share_quiz(g, quiz, request.user)]
        if shared_to:
            messages.success(request, f"Shared “{quiz.title}” with {', '.join(shared_to)}.")
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

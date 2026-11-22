"""GROUP CHALLENGES: a My Space quiz shared with a group with its leaderboard switched on.

* Everyone's first try is scored. Answers are checked here, never sent ahead to the browser, so a challenge
  can't be read from the page. They show after each question or only after submitting (``reveal``).
* When it ends, small groups publish the leaderboard by themselves; in the Academic Secretary the admins see
  the results first and press Publish.
* Published boards live in the group's Leaderboard tab; one can be pinned. Admins can put an Academic
  Secretary challenge on everyone's Home for a while.
* Ties go to whoever finished faster. Descriptive questions are flashcards and are not scored.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .models import ChallengeAttempt, GroupPost, GroupQuiz, StudyGroup

DURATIONS = (("1", "1 day", 1), ("3", "3 days", 3), ("7", "1 week", 7), ("14", "2 weeks", 14))
HOME_DURATIONS = (("1", "1 day"), ("3", "3 days"), ("7", "1 week"), ("end", "Until it ends"))
BIG_BOARD = 10          # the Academic Secretary shows the top 10 and your own rank


# ---------------------------------------------------------------- rules
def can_manage(challenge, user):
    """See results before they're published, publish, pin, put on Home."""
    from .groups import can_announce
    group = challenge.group
    if group.is_everyone:
        return user.is_staff
    return can_announce(group, user) or challenge.shared_by_id == user.pk


def setup(challenge, reveal, days, allow_hiding):
    challenge.leaderboard = True
    challenge.reveal = GroupQuiz.END if reveal == GroupQuiz.END else GroupQuiz.EACH
    days = next((n for key, _l, n in DURATIONS if key == str(days)), 3)
    challenge.ends_at = timezone.now() + timedelta(days=days)
    challenge.allow_hiding = bool(allow_hiding)
    challenge.published_at = None


def judge(q, payload):
    """True/False for a scored question, None for a flashcard or no answer."""
    if not isinstance(payload, dict) or q.get("kind") == "desc":
        return None
    correct = q.get("correct", [])
    if q.get("kind") == "tf":
        answers = payload.get("answers")
        if not isinstance(answers, list) or len(answers) != len(correct) or any(a not in (True, False) for a in answers):
            return None
        return all(a == bool(c) for a, c in zip(answers, correct))
    selected = payload.get("selected")
    if not isinstance(selected, int) or not 0 <= selected < len(correct):
        return None
    return bool(correct[selected])


def score(quiz, answers):
    total = sum(q.get("kind") != "desc" for q in quiz.questions)
    right = sum(judge(q, answers.get(str(i))) is True for i, q in enumerate(quiz.questions, 1))
    return right, total


def result_for(q, payload):
    """What the page shows once a question's answer may be seen."""
    out = {"correct": judge(q, payload), "explanation": q.get("explanation", "")}
    if q.get("kind") == "desc":
        out["answer"] = q.get("answer", "")
    else:
        out["key"] = [bool(c) for c in q.get("correct", [])]
    return out


def finish(attempt, when=None):
    attempt.score, attempt.total = score(attempt.challenge.quiz, attempt.answers)
    attempt.finished_at = when or timezone.now()
    attempt.save(update_fields=["score", "total", "finished_at"])


def publish(challenge, user=None):
    """Close the challenge and put its leaderboard in the group."""
    from .groups import add_post
    now = timezone.now()
    end = min(now, challenge.ends_at) if challenge.ends_at else now
    for attempt in challenge.attempts.filter(finished_at__isnull=True, ranked=True).select_related("challenge__quiz"):
        if attempt.answers:
            finish(attempt, max(end, attempt.started_at))   # time ran out: what they answered counts
        else:
            attempt.delete()                                # opened it and never answered
    challenge.published_at = now
    if challenge.ends_at is None or challenge.ends_at > now:
        challenge.ends_at = now
    challenge.save(update_fields=["published_at", "ends_at"])
    group = challenge.group
    note = f"Leaderboard is out: {challenge.quiz.title}"
    add_post(group, user, GroupPost.BOARD, quiz=challenge.quiz,
             notify_text=f"{group.name}: {note}" if user else "")
    return note


def settle(group):
    """Challenges whose time is up: small groups publish by themselves."""
    due = GroupQuiz.objects.filter(group=group, leaderboard=True, published_at__isnull=True,
                                   ends_at__lte=timezone.now()).select_related("quiz", "group")
    for challenge in due:
        if group.is_everyone:
            # Admins publish; meanwhile, close the tries that ran out of time.
            for attempt in challenge.attempts.filter(finished_at__isnull=True, ranked=True).select_related("challenge__quiz"):
                if attempt.answers:
                    finish(attempt, max(challenge.ends_at, attempt.started_at))
                else:
                    attempt.delete()
        else:
            publish(challenge)


def board(challenge, viewer, limit=None, real_names=False):
    """(rows, my row) for ranked, finished tries: best score first, then fastest."""
    attempts = sorted(challenge.attempts.filter(ranked=True, finished_at__isnull=False).select_related("user__profile"),
                      key=lambda a: (-a.score, a.seconds, a.pk))
    rows, mine = [], None
    for rank, a in enumerate(attempts, 1):
        profile = getattr(a.user, "profile", None)
        hidden = challenge.allow_hiding and profile is not None and profile.hide_on_leaderboards
        row = {"rank": rank, "attempt": a, "me": a.user_id == viewer.pk, "hidden": hidden,
               "profile": None if (hidden and not real_names and a.user_id != viewer.pk) else profile,
               "name": (profile.name if profile else a.user.get_username())
               if (real_names or not hidden or a.user_id == viewer.pk) else "Anonymous",
               "percent": round(100 * a.score / a.total) if a.total else 0,
               "time": _clock(a.seconds)}
        rows.append(row)
        if row["me"]:
            mine = row
    return (rows[:limit] if limit else rows), mine, len(rows)


def _clock(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def leaderboard_context(group, user):
    """The group's Leaderboard tab: running challenges, then published boards (pinned first)."""
    settle(group)
    challenges = list(group.quizzes.filter(leaderboard=True).select_related("quiz", "shared_by__profile"))
    mine = {a.challenge_id: a for a in ChallengeAttempt.objects.filter(challenge__in=challenges, user=user)}
    running, waiting, boards = [], [], []
    for c in challenges:
        c.my = mine.get(c.pk)
        c.manage = can_manage(c, user)
        c.count = c.attempts.filter(finished_at__isnull=False, ranked=True).count()
        if c.published_at:
            c.rows, c.me, c.ranked = board(c, user, limit=BIG_BOARD if group.is_everyone else None)
            top = c.rows[:3]
            c.podium = [top[i] for i in (1, 0, 2) if i < len(top)]     # 2nd, 1st, 3rd like a real podium
            c.rest = c.rows[3:]
            c.me_outside = c.me and c.me not in c.rows
            boards.append(c)
        elif c.is_open:
            running.append(c)
        else:
            waiting.append(c)       # Academic Secretary: ended, the admins haven't published yet
    boards.sort(key=lambda c: (c.pinned_at is None, -(c.pinned_at or c.published_at).timestamp()))
    return {"running": running, "waiting": waiting, "boards": boards, "home_durations": HOME_DURATIONS}


def home_cards(user):
    """Academic Secretary challenges the admins put on Home."""
    now = timezone.now()
    cards = list(GroupQuiz.objects.filter(group__is_everyone=True, leaderboard=True, home_until__gt=now,
                                          published_at__isnull=True, ends_at__gt=now)
                 .select_related("quiz", "group"))
    mine = {a.challenge_id: a for a in ChallengeAttempt.objects.filter(challenge__in=cards, user=user)}
    for c in cards:
        c.my = mine.get(c.pk)
    return cards


def lock_for(user, quiz):
    """A challenge that's the only way this person reaches the quiz, still running, not yet done by them.
    While it runs, the quiz can't be opened as a normal quiz (that page has the answers in it)."""
    from .groups import my_groups
    if quiz.user_id == user.pk or quiz.access.filter(user=user).exists() or quiz.shares.filter(recipient=user).exists():
        return None
    shares = list(GroupQuiz.objects.filter(quiz=quiz, group__in=my_groups(user)).select_related("group"))
    for s in shares:
        if not s.is_open or s.attempts.filter(user=user, finished_at__isnull=False).exists():
            return None
    return shares[0] if shares else None


def _challenge_or_404(request, code, pk):
    from .groups import is_member
    group = get_object_or_404(StudyGroup, code=code)
    if not is_member(group, request.user):
        raise Http404
    return get_object_or_404(GroupQuiz.objects.select_related("quiz", "group"), pk=pk, group=group, leaderboard=True)


def _attempt(challenge, user, create=True):
    attempt = ChallengeAttempt.objects.filter(challenge=challenge, user=user).select_related("challenge__quiz").first()
    if attempt or not create:
        return attempt
    try:
        with transaction.atomic():
            return ChallengeAttempt.objects.create(challenge=challenge, user=user, ranked=challenge.is_open)
    except IntegrityError:
        return ChallengeAttempt.objects.get(challenge=challenge, user=user)


def _state(challenge, attempt):
    quiz = challenge.quiz
    finished = attempt is not None and attempt.finished_at is not None
    answers = attempt.answers if attempt else {}
    questions, results = [], {}
    for i, q in enumerate(quiz.questions, 1):
        questions.append({"n": i, "kind": q.get("kind"), "stem": q.get("stem", ""), "options": q.get("options", [])})
        payload = answers.get(str(i))
        shown = finished or (payload is not None and (challenge.reveal == GroupQuiz.EACH or q.get("kind") == "desc"))
        if shown:
            results[str(i)] = result_for(q, payload)
    data = {"title": quiz.title, "started": attempt is not None, "reveal": challenge.reveal, "open": challenge.is_open, "finished": finished,
            "ranked": bool(attempt and attempt.ranked), "questions": questions, "answers": answers, "results": results,
            "ends_at": challenge.ends_at.isoformat() if challenge.ends_at else None,
            "published": bool(challenge.published_at)}
    if finished:
        data.update(score=attempt.score, total=attempt.total, time=_clock(attempt.seconds))
    return data


# ---------------------------------------------------------------- pages
@login_required
@ensure_csrf_cookie
def page(request, code, pk):
    challenge = _challenge_or_404(request, code, pk)
    settle(challenge.group)
    challenge.refresh_from_db()
    return render(request, "bank/challenge.html", {
        "challenge": challenge, "group": challenge.group, "quiz": challenge.quiz,
        "manage": can_manage(challenge, request.user),
        "scored": sum(q.get("kind") != "desc" for q in challenge.quiz.questions),
        "attempt": _attempt(challenge, request.user, create=False),
    })


@login_required
def data(request, code, pk):
    challenge = _challenge_or_404(request, code, pk)
    settle(challenge.group)
    challenge.refresh_from_db()
    start = request.method == "POST" or request.GET.get("start") == "1"     # the clock starts on "Start"
    return JsonResponse(_state(challenge, _attempt(challenge, request.user, create=start)))


@login_required
@require_POST
def answer(request, code, pk):
    from .views import json_body
    challenge = _challenge_or_404(request, code, pk)
    settle(challenge.group)
    challenge.refresh_from_db()
    attempt = _attempt(challenge, request.user)
    if attempt.finished_at:
        return JsonResponse({"error": "You've already handed this in.", "state": _state(challenge, attempt)}, status=409)
    if attempt.ranked and not challenge.is_open:
        return JsonResponse({"error": "Time's up: this challenge has ended."}, status=409)
    body = json_body(request) or {}
    pos = body.get("pos")
    questions = challenge.quiz.questions
    if not isinstance(pos, int) or not 1 <= pos <= len(questions):
        return JsonResponse({"error": "No such question."}, status=400)
    q = questions[pos - 1]
    key = str(pos)
    payload = body.get("payload") if q.get("kind") != "desc" else {"seen": True}
    locked = challenge.reveal == GroupQuiz.EACH or q.get("kind") == "desc"
    if key in attempt.answers and locked:
        return JsonResponse({"result": result_for(q, attempt.answers[key]), "kept": True})
    if q.get("kind") != "desc" and judge(q, payload) is None:
        return JsonResponse({"error": "Pick an answer first."}, status=400)
    attempt.answers[key] = payload
    attempt.save(update_fields=["answers"])
    return JsonResponse({"result": result_for(q, payload)} if locked else {"saved": True})


@login_required
@require_POST
def submit(request, code, pk):
    challenge = _challenge_or_404(request, code, pk)
    attempt = _attempt(challenge, request.user)
    if not attempt.finished_at:
        if not attempt.answers:
            return JsonResponse({"error": "Answer at least one question first."}, status=400)
        finish(attempt)
    return JsonResponse(_state(challenge, attempt))


@login_required
def results(request, code, pk):
    """Managers: everyone's score with real names, before and after publishing."""
    challenge = _challenge_or_404(request, code, pk)
    if not can_manage(challenge, request.user):
        raise Http404
    settle(challenge.group)
    challenge.refresh_from_db()
    rows, _mine, count = board(challenge, request.user, real_names=True)
    unfinished = challenge.attempts.filter(finished_at__isnull=True, ranked=True).exclude(answers={}).count()
    late = challenge.attempts.filter(ranked=False, finished_at__isnull=False).count()
    return render(request, "bank/challenge_results.html", {
        "challenge": challenge, "group": challenge.group, "rows": rows, "count": count,
        "unfinished": unfinished, "late": late, "home_durations": HOME_DURATIONS,
    })


@login_required
@require_POST
def action(request, code, pk):
    from .groups import member_users, notify
    challenge = _challenge_or_404(request, code, pk)
    if not can_manage(challenge, request.user):
        raise Http404
    group = challenge.group
    act = request.POST.get("action")
    back = request.POST.get("back") or f"{group.get_absolute_url()}#leaderboard"
    if not back.startswith("/"):
        back = group.get_absolute_url()
    now = timezone.now()
    if act == "publish" and not challenge.published_at:
        publish(challenge, request.user)
        challenge.home_until = None
        challenge.save(update_fields=["home_until"])
        messages.success(request, "Leaderboard published.")
    elif act == "pin" and challenge.published_at:
        GroupQuiz.objects.filter(group=group, pinned_at__isnull=False).update(pinned_at=None)   # one pin per group
        challenge.pinned_at = now
        challenge.save(update_fields=["pinned_at"])
    elif act == "unpin":
        challenge.pinned_at = None
        challenge.save(update_fields=["pinned_at"])
    elif act == "home" and group.is_everyone and challenge.is_open:
        span = request.POST.get("for", "end")
        until = challenge.ends_at if span == "end" else now + timedelta(days=int(span) if span.isdigit() else 1)
        challenge.home_until = min(until, challenge.ends_at) if challenge.ends_at else until
        challenge.save(update_fields=["home_until"])
        notify(member_users(group).exclude(pk=request.user.pk).values_list("pk", flat=True),
               f"📣 Challenge: {challenge.quiz.title}. Take it before it ends!",
               reverse("bank:challenge", args=[group.code, challenge.pk]))
        messages.success(request, "It’s on everyone’s Home now.")
    elif act == "home_off":
        challenge.home_until = None
        challenge.save(update_fields=["home_until"])
        messages.success(request, "Taken off Home.")
    elif act == "extend" and not challenge.published_at:
        days = int(request.POST.get("days", "1")) if request.POST.get("days", "1").isdigit() else 1
        challenge.ends_at = max(challenge.ends_at or now, now) + timedelta(days=min(days, 14))
        challenge.save(update_fields=["ends_at"])
        messages.success(request, "Deadline moved.")
    return redirect(back)

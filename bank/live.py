"""LIVE QUIZ: friends take a quiz together.

* Anyone starts one from any quiz ("Go live"): the questions are copied into a LiveRoom in the order
  they had, and a 4-digit room code is made. Up to 10 people; only friends of someone in the room join.
* Everyone answers on their own phone; the leader reveals, and everyone sees who picked what.
  An optional timer reveals by itself. The leader moves the room; all screens follow by polling the
  state every second, so joining late or after a dropped connection lands on the current question.
* Saves: when the leader saves a question, it's written straight into the account of every member who
  follows saves, whether or not their phone is open. When the room ends, each of them gets one
  notification and a Messages entry with "Undo all".
* If the leader disappears, the lead passes to someone still connected. Rooms nobody touches for
  20 minutes are ended.
"""
import math
import random
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import friends, my_space
from .models import (Friendship, LiveAnswer, LiveMember, LiveRoom, LiveSave, PersonalQuiz, Question,
                     QuestionNote, SavedQuestion)
from .views import json_body, serialize

ONLINE = timedelta(seconds=20)        # polled within this long = connected
LEADER_AWAY = timedelta(seconds=45)   # then the lead passes to someone connected
IDLE = timedelta(minutes=20)          # untouched this long = ended
TIMERS = (0, 20, 30, 45, 60, 90)
BLOCK = 50                            # same blocks as the quiz screen
MAX_QUESTIONS = 600
KEY_FIELDS = ("kind", "correct_ids", "answer_key", "answer", "explanation", "note", "answer_image_url")


# ---------------------------------------------------------------- helpers
def _name(user):
    return friends._name(user)


def _new_code():
    used = set(LiveRoom.objects.filter(ended_at__isnull=True).values_list("code", flat=True))
    free = [c for c in (f"{random.randint(0, 9999):04d}" for _ in range(300)) if c not in used]
    if not free:
        raise RuntimeError("No free room code")
    return free[0]


def _active_members(room):
    return room.members.filter(left_at__isnull=True)


def _member_or_404(room, user):
    member = room.members.filter(user=user).first()
    if member is None:
        raise Http404("Not in this live quiz")
    return member


def _is_friend_of_room(room, user):
    ids = _active_members(room).values_list("user_id", flat=True)
    return Friendship.objects.filter(user=user, friend_id__in=ids).exists()


def _online(member, now):
    return member.last_seen is not None and now - member.last_seen <= ONLINE


def _questions(room, user):
    """The room's questions in order, without answers (each phone gets the key only after the reveal)."""
    if room.source == "mine":
        quiz = room.personal_quiz
        items = my_space.serialize(quiz) if quiz else []
        out = []
        for index in room.question_ids:
            item = dict(items[index - 1]) if 0 < index <= len(items) else {"missing": True}
            for key in ("answer", "explanation", "note", "answer_key", "correct_ids"):
                item.pop(key, None)
            out.append(item)
        return out
    qs = (Question.objects.filter(pk__in=room.question_ids)
          .select_related("image_asset", "answer_image_asset").prefetch_related("options", "image_targets"))
    by_id = {q["id"]: q for q in serialize(qs, user)}
    return [by_id.get(i, {"missing": True, "id": i}) for i in room.question_ids]


def _full(room, position):
    """One question with its answer key (cached: it's asked for by every phone after a reveal)."""
    key = f"live:{room.pk}:{position}"
    found = cache.get(key)
    if found is not None:
        return found
    found = {}
    if 0 <= position < len(room.question_ids):
        ident = room.question_ids[position]
        if room.source == "mine":
            items = my_space.serialize(room.personal_quiz) if room.personal_quiz else []
            if 0 < ident <= len(items):
                found = items[ident - 1]
        else:
            q = (Question.objects.filter(pk=ident).select_related("image_asset", "answer_image_asset")
                 .prefetch_related("options", "image_targets").first())
            if q:
                from django.contrib.auth.models import AnonymousUser
                found = serialize([q], AnonymousUser(), include_answers=True)[0]
    cache.set(key, found, 600)
    return found


def _grade(full, payload):
    kind = full.get("kind")
    if kind == "mcq":
        return payload.get("selected") in full.get("correct_ids", [])
    if kind == "tf":
        answers = payload.get("answers") or {}
        key = full.get("answer_key") or {}
        return bool(key) and all(answers.get(k) is v for k, v in key.items())
    if kind == "desc":
        grade = payload.get("grade")
        return None if grade not in ("known", "missed") else grade == "known"
    return None


def _touch(room, now):
    room.version += 1
    room.last_activity = now


# ---------------------------------------------------------------- saves
def _save_for(room, user, question_id):
    _, created = SavedQuestion.objects.get_or_create(user=user, question_id=question_id)
    if created:
        LiveSave.objects.get_or_create(room=room, user=user, question_id=question_id)


def _unsave_group(room, question_id):
    """Take back a group save: only from accounts where the room added it, and never if they wrote a note."""
    for row in LiveSave.objects.filter(room=room, question_id=question_id):
        if not QuestionNote.objects.filter(user_id=row.user_id, question_id=question_id).exists():
            SavedQuestion.objects.filter(user_id=row.user_id, question_id=question_id).delete()
        row.delete()


def _catch_up(room, member):
    """Someone who starts following (or is invited late) gets the saves made so far."""
    if room.source != "bank" or not member.follow_saves:
        return
    for question_id in room.group_saves:
        _save_for(room, member.user, question_id)


def _group_save(room, question_id):
    if not Question.objects.filter(pk=question_id).exists():      # deleted since the room started
        return
    room.group_saves = room.group_saves + [question_id]
    for member in _active_members(room).filter(follow_saves=True).select_related("user"):
        _save_for(room, member.user, question_id)


def _notify_saves(room):
    """One notification per member about the questions saved into their account (not the leader's own)."""
    now = timezone.now()
    leader_name = _name(room.leader)
    for member in room.members.select_related("user"):
        count = LiveSave.objects.filter(room=room, user=member.user).count()
        if count <= member.notified_saves or member.user_id == room.leader_id or member.undone_at:
            continue
        new = count - member.notified_saves
        member.notified_saves, member.summary_at, member.summary_seen_at = count, now, None
        member.save(update_fields=["notified_saves", "summary_at", "summary_seen_at"])
        friends.notify(member.user, f"{leader_name} saved {new} question{'s' if new != 1 else ''} to your account "
                                    f"in the live quiz “{room.title}”", reverse("bank:messages"))


def _end(room, now):
    room.ended_at = now
    room.deadline = None
    _touch(room, now)
    room.save()
    transaction.on_commit(lambda: _notify_saves(LiveRoom.objects.get(pk=room.pk)))


def close_idle_rooms():
    """End rooms nobody has used for a while (at most once a minute; called from page requests)."""
    if not cache.add("live-idle-check", 1, 60):
        return
    now = timezone.now()
    for room in LiveRoom.objects.filter(ended_at__isnull=True, last_activity__lt=now - IDLE):
        with transaction.atomic():
            _end(room, now)


# ---------------------------------------------------------------- state
def _housekeeping(room, now):
    """Timer reveals and leader handover; done by whichever phone polls first."""
    pos = room.position
    timer_due = room.deadline and now >= room.deadline and pos not in room.revealed
    leader = _active_members(room).filter(user_id=room.leader_id).first()
    leader_gone = leader is None or leader.last_seen is None or now - leader.last_seen > LEADER_AWAY
    if not (timer_due or leader_gone):
        return room
    with transaction.atomic():
        room = LiveRoom.objects.select_for_update().get(pk=room.pk)
        if room.ended_at:
            return room
        changed = False
        if room.deadline and now >= room.deadline and room.position not in room.revealed:
            room.revealed = room.revealed + [room.position]
            room.deadline = None
            changed = True
        if leader_gone:
            heir = (_active_members(room).filter(last_seen__gte=now - ONLINE).exclude(user_id=room.leader_id)
                    .order_by("joined_at", "id").first())
            if heir:
                room.leader_id = heir.user_id
                changed = True
        if changed:
            _touch(room, now)
            room.save()
    return room


def _person(member, room, now, answered):
    profile = getattr(member.user, "profile", None)
    return {
        "id": member.user_id, "name": _name(member.user), "initials": profile.initials if profile else "?",
        "color": profile.avatar_color if profile else "avatar-c0",
        "avatar": (profile.avatar_small_url or profile.avatar_url) if profile else "",
        "online": _online(member, now), "joined": member.joined_at is not None,
        "answered": member.user_id in answered, "leader": member.user_id == room.leader_id,
        "follow": member.follow_saves,
    }


def _block_positions(room, block):
    return range(block * BLOCK, min((block + 1) * BLOCK, len(room.question_ids)))


def _summary(room, positions=None):
    rows = LiveAnswer.objects.filter(room=room, correct__isnull=False)
    if positions is not None:
        rows = rows.filter(position__in=list(positions))
    rows = list(rows.values("user_id", "position", "correct"))
    users = {m.user_id: m.user for m in room.members.select_related("user__profile")}
    people, by_pos = {}, {}
    for row in rows:
        p = people.setdefault(row["user_id"], [0, 0])
        p[0] += row["correct"]; p[1] += 1
        q = by_pos.setdefault(row["position"], [0, 0])
        q[0] += not row["correct"]; q[1] += 1

    def entry(uid, right, total):
        user = users.get(uid)
        profile = getattr(user, "profile", None) if user else None
        return {"id": uid, "name": _name(user) if user else "?", "right": right, "total": total,
                "pct": round(100 * right / total), "initials": profile.initials if profile else "?",
                "color": profile.avatar_color if profile else "avatar-c0",
                "avatar": (profile.avatar_small_url or profile.avatar_url) if profile else ""}

    board = sorted((entry(uid, r, t) for uid, (r, t) in people.items()),
                   key=lambda x: (-x["right"], -x["pct"], x["name"]))
    # Places: the same number right shares a place (1, 1, 3 ...)
    for i, row in enumerate(board):
        row["place"] = board[i - 1]["place"] if i and row["right"] == board[i - 1]["right"] else i + 1
    stems = {}
    if room.source == "mine" and room.personal_quiz:
        items = room.personal_quiz.questions
        stems = {i: items[ident - 1]["stem"] for i, ident in enumerate(room.question_ids) if 0 < ident <= len(items)}
    elif room.source == "bank":
        found = dict(Question.objects.filter(pk__in=room.question_ids).values_list("pk", "stem"))
        stems = {i: found.get(ident, "") for i, ident in enumerate(room.question_ids)}
    hard = sorted(((pos, wrong, total) for pos, (wrong, total) in by_pos.items() if wrong),
                  key=lambda x: (-x[1] / x[2], -x[1], x[0]))
    hard_ids = [room.question_ids[pos] for pos, _w, _t in hard] if room.source == "bank" else []
    right = sum(r for r, _t in people.values())
    total = sum(t for _r, t in people.values())
    covered = [p for p in room.revealed if positions is None or p in positions]
    return {
        "covered": len(covered), "accuracy": round(100 * right / total) if total else None,
        "board": board,
        "hard": [{"position": pos, "stem": _short(stems.get(pos, "")), "wrong": w, "total": t}
                 for pos, w, t in hard[:15]],
        "hard_count": len(hard),
        "hard_saved": bool(hard_ids) and all(i in room.group_saves for i in hard_ids),
    }


def _short(text, limit=140):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _state(room, me, user, now):
    members = list(_active_members(room).select_related("user__profile"))
    pos = room.position
    answers = {a.user_id: a for a in LiveAnswer.objects.filter(room=room, position=pos)}
    revealed = pos in room.revealed
    mine = answers.get(user.pk)
    question_id = room.question_ids[pos] if pos < len(room.question_ids) else None
    out = {
        "v": room.version, "ended": room.ended_at is not None, "code": room.code, "title": room.title,
        "position": pos, "total": len(room.question_ids), "revealed": revealed,
        "timer": room.timer_seconds or 0,
        "seconds_left": (max(0, math.ceil((room.deadline - now).total_seconds()))
                         if room.deadline and not revealed else None),
        "leader": room.leader_id, "can_save": room.source == "bank",
        "group_saved": room.source == "bank" and question_id in room.group_saves,
        "me": {"id": user.pk, "leader": room.leader_id == user.pk, "follow": me.follow_saves,
               "answer": mine.payload if mine else None,
               "correct": mine.correct if (mine and revealed) else None},
        "members": [_person(m, room, now, answers) for m in members],
        "marks": {str(p): "ok" if c else "bad" for p, c in LiveAnswer.objects.filter(
            room=room, user=user, correct__isnull=False, position__in=room.revealed).values_list("position", "correct")},
        "full": len(members) >= LiveRoom.MAX_MEMBERS,
    }
    if revealed:
        full = _full(room, pos)
        out["key"] = {k: full[k] for k in KEY_FIELDS if k in full}
        # Everyone is sent as an id, with a face (name, initials, colour, picture) so phones can draw avatars,
        # including people who answered and then left.
        people = {m.user_id: m.user for m in members}
        missing = [uid for uid in answers if uid not in people]
        if missing:
            from django.contrib.auth import get_user_model
            people.update({u.pk: u for u in get_user_model().objects.filter(pk__in=missing).select_related("profile")})
        picks, right, wrong, faces = {}, [], [], {}
        for uid, answer in sorted(answers.items(), key=lambda item: _name(people[item[0]]) if item[0] in people else ""):
            person = people.get(uid)
            if person is None:
                continue
            profile = getattr(person, "profile", None)
            faces[str(uid)] = {"name": _name(person), "initials": profile.initials if profile else "?",
                               "color": profile.avatar_color if profile else "avatar-c0",
                               "avatar": (profile.avatar_small_url or profile.avatar_url) if profile else ""}
            payload = answer.payload or {}
            if full.get("kind") == "mcq" and "selected" in payload:
                picks.setdefault(str(payload["selected"]), []).append(uid)
            elif full.get("kind") == "tf":
                for option_id, value in (payload.get("answers") or {}).items():
                    picks.setdefault(f"{option_id}:{'T' if value else 'F'}", []).append(uid)
            elif full.get("kind") == "desc" and payload.get("grade"):
                picks.setdefault(payload["grade"], []).append(uid)
            if answer.correct is True:
                right.append(uid)
            elif answer.correct is False:
                wrong.append(uid)
        out.update(picks=picks, right=right, wrong=wrong, faces=faces)
    out["block_size"] = BLOCK
    out["results_block"] = room.results_block
    if room.results_block is not None and not room.ended_at:
        out["block_summary"] = _summary(room, _block_positions(room, room.results_block))
    if room.ended_at:
        out["summary"] = _summary(room)
    return out


# ---------------------------------------------------------------- views
@login_required
@require_POST
def start(request):
    data = json_body(request) or {}
    try:
        ids = list(dict.fromkeys(int(i) for i in data.get("ids") or []))[:MAX_QUESTIONS]
    except (TypeError, ValueError):
        return JsonResponse({"error": "Bad question list."}, status=400)
    mine = str(data.get("mine") or "")
    quiz = None
    if mine.isdigit():
        quiz = my_space._visible_quiz(request.user, int(mine))
        ids = [i for i in ids if 0 < i <= len(quiz.questions)]
    else:
        valid = set(Question.objects.filter(pk__in=ids, is_in_sorting_bin=False, is_in_review_bin=False)
                    .exclude(kind=Question.IMAGE, image_answer_mode=Question.IMAGE_IDENTIFY)
                    .values_list("pk", flat=True))
        ids = [i for i in ids if i in valid]
    if not ids:
        return JsonResponse({"error": "This quiz has no questions that can be played live "
                                      "(image-labelling questions can't)."}, status=400)
    back = str(data.get("back") or "")
    now = timezone.now()
    room = LiveRoom.objects.create(
        code=_new_code(), host=request.user, leader=request.user, title=str(data.get("title") or "Live quiz")[:160],
        back_url=back if back.startswith("/") and not back.startswith("//") else "",
        source="mine" if quiz else "bank", personal_quiz=quiz, question_ids=ids)
    LiveMember.objects.create(room=room, user=request.user, joined_at=now, last_seen=now)
    return JsonResponse({"url": reverse("bank:live_room", args=[room.pk])})


@login_required
@require_POST
def join(request):
    code = "".join(ch for ch in request.POST.get("code", "") if ch.isdigit())[:4]
    room = LiveRoom.objects.filter(code=code, ended_at__isnull=True).first()
    if room is None:
        messages.error(request, "No live quiz with that code is running.")
        return redirect("bank:messages")
    member = room.members.filter(user=request.user).first()
    if member is None or member.left_at:
        if not _is_friend_of_room(room, request.user):
            messages.error(request, "You can only join a live quiz where you're friends with someone in it.")
            return redirect("bank:messages")
        if _active_members(room).count() >= LiveRoom.MAX_MEMBERS:
            messages.error(request, f"That live quiz is full ({LiveRoom.MAX_MEMBERS} people).")
            return redirect("bank:messages")
    return redirect("bank:live_room", room.pk) if _enter(room, request.user, member) else redirect("bank:messages")


def _enter(room, user, member=None):
    """Open the room: become (or come back as) a connected member."""
    now = timezone.now()
    with transaction.atomic():
        room = LiveRoom.objects.select_for_update().get(pk=room.pk)
        member = room.members.filter(user=user).first()
        if member is None:
            try:
                member = LiveMember.objects.create(room=room, user=user, follow_saves=True)
            except IntegrityError:
                return False
            _catch_up(room, member)
        if member.left_at:
            member.left_at = None
            _catch_up(room, member)
        member.joined_at = member.joined_at or now
        member.last_seen = now
        member.invite_seen_at = member.invite_seen_at or now
        member.save()
        _touch(room, now)
        room.save()
    return True


@login_required
@ensure_csrf_cookie
def room_page(request, pk):
    room = get_object_or_404(LiveRoom, pk=pk)
    member = _member_or_404(room, request.user)
    if not room.ended_at:
        if member.left_at and _active_members(room).count() >= LiveRoom.MAX_MEMBERS:
            messages.error(request, "That live quiz is full now.")
            return redirect("bank:messages")
        _enter(room, request.user, member)
    in_room = set(room.members.filter(left_at__isnull=True).values_list("user_id", flat=True))
    pals = [{"id": p.user_id, "name": p.name, "initials": p.initials, "color": p.avatar_color,
             "avatar": p.avatar_small_url or p.avatar_url} for p in friends.friends_of(request.user)
            if p.user_id not in in_room]
    return render(request, "bank/live.html", {"room": room, "friends": pals})


@login_required
def room_questions(request, pk):
    room = get_object_or_404(LiveRoom, pk=pk)
    _member_or_404(room, request.user)
    return JsonResponse({"questions": _questions(room, request.user)})


@login_required
def room_state(request, pk):
    room = get_object_or_404(LiveRoom, pk=pk)
    member = room.members.filter(user=request.user, left_at__isnull=True).first()
    if member is None:
        return JsonResponse({"gone": True}, status=403)
    now = timezone.now()
    if not room.ended_at:
        updates = {}
        if member.last_seen is None or now - member.last_seen > timedelta(seconds=5):
            updates["last_seen"] = now
        if member.joined_at is None:
            updates["joined_at"] = now
        if updates:
            LiveMember.objects.filter(pk=member.pk).update(**updates)
            for key, value in updates.items():
                setattr(member, key, value)
        if now - room.last_activity > timedelta(seconds=60):
            LiveRoom.objects.filter(pk=room.pk).update(last_activity=now)
        room = _housekeeping(room, now)
        close_idle_rooms()
    return JsonResponse(_state(room, member, request.user, now))


@login_required
@require_POST
def room_act(request, pk):
    data = json_body(request)
    if data is None:
        return JsonResponse({"error": "Bad request."}, status=400)
    action = data.get("action")
    now = timezone.now()
    with transaction.atomic():
        room = get_object_or_404(LiveRoom.objects.select_for_update(), pk=pk)
        member = room.members.filter(user=request.user, left_at__isnull=True).first()
        if member is None:
            return JsonResponse({"error": "You're not in this live quiz."}, status=403)
        error = _do(room, member, request.user, action, data, now)
        if error:
            return JsonResponse({"error": error}, status=400)
        if action == "leave":
            return JsonResponse({"url": reverse("bank:messages")})
        member.refresh_from_db()
    room.refresh_from_db()
    return JsonResponse({"state": _state(room, member, request.user, now)})


def _do(room, member, user, action, data, now):
    """Apply one action inside the room lock. Returns an error message, or ""."""
    leader = room.leader_id == user.pk
    ended = room.ended_at is not None
    if ended and action not in ("save_hard", "leave"):
        return "This live quiz has ended."

    if action == "answer":
        pos = data.get("position")
        if pos != room.position:
            return "The leader has moved on."
        full = _full(room, pos)
        revealed = pos in room.revealed
        payload = {}
        if full.get("kind") == "desc":
            if not revealed:
                return "Wait for the answer, then mark yourself."
            payload = {"grade": data.get("grade")}
        elif revealed:
            return "The answer is already shown."
        elif full.get("kind") == "mcq":
            payload = {"selected": data.get("selected")}
        elif full.get("kind") == "tf":
            answers = data.get("answers") or {}
            payload = {"answers": {str(k): bool(v) for k, v in answers.items()}}
        else:
            return "This question can't be answered live."
        LiveAnswer.objects.update_or_create(room=room, user=user, position=pos,
                                            defaults={"payload": payload, "correct": _grade(full, payload)})
        if member.joined_at is None:
            member.joined_at = now
            member.save(update_fields=["joined_at"])

    elif action == "follow":
        member.follow_saves = bool(data.get("on"))
        member.save(update_fields=["follow_saves"])
        _catch_up(room, member)

    elif action == "leave":
        member.left_at = now
        member.save(update_fields=["left_at"])
        if leader:
            heir = (_active_members(room).filter(last_seen__gte=now - ONLINE).order_by("joined_at", "id").first()
                    or _active_members(room).filter(joined_at__isnull=False).order_by("joined_at", "id").first())
            if heir:
                room.leader_id = heir.user_id
            elif not _active_members(room).exists():
                _end(room, now)
                return ""

    elif action == "invite":
        ids = {int(i) for i in data.get("friends") or [] if str(i).isdigit()}
        pals = [p.user for p in friends.friends_of(user) if p.user_id in ids]
        space = LiveRoom.MAX_MEMBERS - _active_members(room).count()
        if not pals:
            return "Pick at least one friend."
        if len(pals) > space:
            return f"Only {space} more {'person fits' if space == 1 else 'people fit'} (the limit is {LiveRoom.MAX_MEMBERS})."
        for pal in pals:
            invited, created = LiveMember.objects.get_or_create(
                room=room, user=pal,
                defaults={"invited_by": user, "follow_saves": getattr(getattr(pal, "profile", None), "live_saves", True)})
            if not created:
                if not invited.left_at:
                    continue
                invited.left_at, invited.invite_seen_at, invited.invited_by = None, None, user
                invited.save(update_fields=["left_at", "invite_seen_at", "invited_by"])
            _catch_up(room, invited)
            friends.notify(pal, f"{_name(user)} invited you to a live quiz: {room.title}",
                           reverse("bank:live_room", args=[room.pk]))

    elif not leader:
        return "Only the leader can do that."

    elif action == "reveal":
        if room.position not in room.revealed:
            room.revealed = room.revealed + [room.position]
        room.deadline = None

    elif action == "go":
        pos = data.get("position")
        if not isinstance(pos, int) or not 0 <= pos < len(room.question_ids):
            return "No such question."
        room.position = pos
        room.results_block = None
        room.deadline = (now + timedelta(seconds=room.timer_seconds)
                         if room.timer_seconds and pos not in room.revealed else None)

    elif action == "finish_block":
        room.results_block = room.position // BLOCK
        room.deadline = None

    elif action == "block_back":
        room.results_block = None

    elif action == "timer":
        seconds = data.get("seconds")
        if seconds not in TIMERS:
            return "Pick a time from the list."
        room.timer_seconds = seconds or None
        room.deadline = (now + timedelta(seconds=seconds)
                         if seconds and room.position not in room.revealed else None)

    elif action == "save":
        if room.source != "bank":
            return "My Space questions can't be saved."
        question_id = room.question_ids[room.position]
        if question_id in room.group_saves:
            room.group_saves = [i for i in room.group_saves if i != question_id]
            _unsave_group(room, question_id)
        else:
            _group_save(room, question_id)

    elif action == "pass":
        heir = _active_members(room).filter(user_id=data.get("user"), joined_at__isnull=False).first()
        if heir is None:
            return "They need to be in the room to lead."
        room.leader_id = heir.user_id

    elif action == "end":
        _end(room, now)
        return ""

    elif action == "save_hard":
        if room.source != "bank":
            return "My Space questions can't be saved."
        missed = LiveAnswer.objects.filter(room=room, correct=False)
        if not ended and room.results_block is not None:          # just this block's report
            missed = missed.filter(position__in=list(_block_positions(room, room.results_block)))
        summary_ids = [room.question_ids[p] for p in sorted({a["position"] for a in missed.values("position")})]
        for question_id in summary_ids:
            if question_id not in room.group_saves:
                _group_save(room, question_id)
        if ended:
            transaction.on_commit(lambda: _notify_saves(LiveRoom.objects.get(pk=room.pk)))

    else:
        return "Unknown action."

    _touch(room, now)
    room.save()
    return ""


# ---------------------------------------------------------------- Messages
def inbox_items(user):
    """Live-quiz entries for the Messages page: invitations, rooms you're in, and saves made for you."""
    items = []
    for member in (LiveMember.objects.filter(user=user, left_at__isnull=True, room__ended_at__isnull=True)
                   .select_related("room__leader__profile", "invited_by__profile")):
        items.append({"type": "live", "time": member.created_at, "member": member, "room": member.room,
                      "is_new": member.invite_seen_at is None and member.joined_at is None,
                      "sender": member.invited_by or member.room.leader})
    for member in (LiveMember.objects.filter(user=user, summary_at__isnull=False, undone_at__isnull=True)
                   .select_related("room__leader__profile")):
        count = LiveSave.objects.filter(room=member.room, user=user).count()
        if count:
            items.append({"type": "saves", "time": member.summary_at, "member": member, "room": member.room,
                          "count": count, "is_new": member.summary_seen_at is None, "sender": member.room.leader})
    return items


def mark_seen(user):
    now = timezone.now()
    LiveMember.objects.filter(user=user, invite_seen_at__isnull=True).update(invite_seen_at=now)
    LiveMember.objects.filter(user=user, summary_at__isnull=False, summary_seen_at__isnull=True).update(summary_seen_at=now)


def unseen_count(user):
    return (LiveMember.objects.filter(user=user, invite_seen_at__isnull=True, joined_at__isnull=True,
                                      left_at__isnull=True, room__ended_at__isnull=True).count()
            + LiveMember.objects.filter(user=user, summary_at__isnull=False, summary_seen_at__isnull=True,
                                        undone_at__isnull=True).count())


@login_required
@require_POST
def member_action(request, pk):
    member = get_object_or_404(LiveMember.objects.select_related("room"), pk=pk, user=request.user)
    action = request.POST.get("action")
    if action == "decline":
        member.left_at = timezone.now()
        member.save(update_fields=["left_at"])
    elif action == "undo" and not member.undone_at:
        removed = 0
        for row in LiveSave.objects.filter(room=member.room, user=request.user):
            if not QuestionNote.objects.filter(user=request.user, question_id=row.question_id).exists():
                removed += SavedQuestion.objects.filter(user=request.user, question_id=row.question_id).delete()[0]
            row.delete()
        member.undone_at = timezone.now()
        member.save(update_fields=["undone_at"])
        messages.success(request, f"Removed {removed} question{'s' if removed != 1 else ''} from Saved.")
    return redirect("bank:messages")

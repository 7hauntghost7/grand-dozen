"""STUDY GROUPS.

* Any student creates a group and shares its link (or code). Others ask to join; the owner accepts.
* The Academic Secretary (``is_everyone``) holds every user with no membership rows; only admins post there.
* The feed is posts, not chat: a live quiz was started (with a Join button), a quiz was shared, a question
  was saved to the group, someone joined, or an announcement (from the owner, or admins in the
  Academic Secretary).
* Shared quizzes: My Space quizzes sent to the group, stored once (members add them to My Space).
* Group saved list: any member saves a question to the group; everyone can practise the list.
"""
import logging
import re
import secrets
import threading

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import push
from .models import (GroupMember, GroupPost, GroupQuiz, GroupSave, LiveRoom, PersonalQuiz, Question, QuizAccess,
                     StudyGroup)

log = logging.getLogger(__name__)
MAX_OWNED = 10
MAX_MEMBERS = 60


# ---------------------------------------------------------------- rules
def _name(user):
    profile = getattr(user, "profile", None)
    return profile.name if profile else user.get_username()


def my_groups(user):
    return StudyGroup.objects.filter(
        Q(is_everyone=True) | Q(members__user=user, members__status=GroupMember.ACTIVE)).distinct()


def is_member(group, user):
    return group.is_everyone or GroupMember.objects.filter(group=group, user=user, status=GroupMember.ACTIVE).exists()


def is_owner(group, user):
    return (group.owner_id == user.pk and not group.is_everyone) or (group.is_everyone and user.is_superuser)


def can_post(group, user):
    """Start live quizzes, share quizzes, save questions here."""
    return user.is_staff if group.is_everyone else is_member(group, user)


def can_announce(group, user):
    return user.is_staff if group.is_everyone else group.owner_id == user.pk


def can_delete_post(group, post, user):
    """The author, the group's owner, or admins in the Academic Secretary."""
    return post.author_id == user.pk or is_owner(group, user) or (group.is_everyone and user.is_staff)


def can_edit_post(post, user):
    return post.kind == GroupPost.ANNOUNCE and post.author_id == user.pk


def invites_for(user):
    return GroupMember.objects.filter(user=user, status=GroupMember.INVITED).select_related("group", "invited_by__profile")


def postable_groups(user):
    return [g for g in my_groups(user) if can_post(g, user)]


def member_users(group):
    users = get_user_model().objects.filter(is_active=True)
    return users if group.is_everyone else users.filter(group_memberships__group=group,
                                                       group_memberships__status=GroupMember.ACTIVE)


def new_code():
    while True:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].lower()
        if len(code) == 8 and not StudyGroup.objects.filter(code=code).exists():
            return code


def notify(user_ids, text, url):
    """Phone notifications to many people from one background job (a group can be the whole batch)."""
    if not push.configured() or not user_ids:
        return
    ids = list(user_ids)

    def run():
        close_old_connections()
        try:
            for user in get_user_model().objects.filter(pk__in=ids):
                try:
                    push.send_to_user(user, text, url=url)
                except Exception:
                    log.warning("Group notification to user %s failed", user.pk, exc_info=True)
        finally:
            close_old_connections()

    transaction.on_commit(lambda: threading.Thread(target=run, daemon=True).start())


def add_post(group, author, kind, text="", notify_text="", **links):
    post = GroupPost.objects.create(group=group, author=author, kind=kind, text=text, **links)
    if notify_text:
        people = member_users(group).exclude(pk=author.pk) if author else member_users(group)
        notify(people.values_list("pk", flat=True), notify_text, group.get_absolute_url())
    return post


def _group_or_404(code):
    return get_object_or_404(StudyGroup, code=code)


# ---------------------------------------------------------------- pages
@login_required
def home(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = " ".join(request.POST.get("name", "").split())[:60]
            if not name:
                messages.error(request, "Give the group a name.")
            elif StudyGroup.objects.filter(owner=request.user, is_everyone=False).count() >= MAX_OWNED:
                messages.error(request, f"You can own up to {MAX_OWNED} groups.")
            else:
                with transaction.atomic():
                    group = StudyGroup.objects.create(
                        name=name, emoji=(request.POST.get("emoji", "").strip() or "📚")[:8],
                        description=" ".join(request.POST.get("description", "").split())[:200],
                        owner=request.user, code=new_code())
                    GroupMember.objects.create(group=group, user=request.user, status=GroupMember.ACTIVE,
                                               joined_at=timezone.now())
                messages.success(request, f"Created “{group.name}”. Share its link so friends can ask to join.")
                return redirect(group)
        elif action == "code":
            text = request.POST.get("code", "").strip().rstrip("/")
            code = text.split("/")[-1].lower()
            if StudyGroup.objects.filter(code=code).exists():
                return redirect("bank:group", code)
            messages.error(request, "No group has that link or code.")
        return redirect("bank:groups")
    groups = list(my_groups(request.user))
    # Counted separately: my_groups() filters on the viewer's own membership row, which would count only them.
    counts = dict(GroupMember.objects.filter(group__in=groups, status=GroupMember.ACTIVE)
                  .values("group").annotate(n=Count("id")).values_list("group", "n"))
    users = get_user_model().objects.filter(is_active=True).count()
    pending = dict(GroupMember.objects.filter(group__owner=request.user, status=GroupMember.PENDING)
                   .values("group").annotate(n=Count("id")).values_list("group", "n"))
    for g in groups:
        g.count = users if g.is_everyone else counts.get(g.pk, 0)
        g.waiting = pending.get(g.pk, 0)
        g.last = g.posts.select_related("author__profile", "live_room", "quiz", "question").first()   # latest activity
    groups.sort(key=lambda g: (not g.is_everyone, -(g.last.created_at.timestamp() if g.last else 0)))
    asked = GroupMember.objects.filter(user=request.user, status=GroupMember.PENDING).select_related("group")
    return render(request, "bank/groups_home.html", {"groups": groups, "asked": asked, "invites": invites_for(request.user)})


@login_required
@ensure_csrf_cookie
def group_page(request, code):
    group = _group_or_404(code)
    user = request.user
    if not is_member(group, user):
        mine = GroupMember.objects.filter(group=group, user=user).select_related("invited_by__profile").first()
        count = GroupMember.objects.filter(group=group, status=GroupMember.ACTIVE).count()
        return render(request, "bank/group_join.html", {"group": group, "mine": mine, "count": count})
    from .challenge import settle
    settle(group)              # publish small groups' finished challenges before reading the feed
    posts = list(group.posts.select_related("author__profile", "live_room", "quiz", "question")[:60])
    shared = list(group.quizzes.select_related("quiz", "shared_by__profile"))
    have = set(QuizAccess.objects.filter(user=user, quiz__in=[s.quiz_id for s in shared]).values_list("quiz_id", flat=True))
    for s in shared:
        s.in_space = s.quiz_id in have
        s.mix = {k: sum(q.get("kind") == k for q in s.quiz.questions) for k in ("mcq", "tf", "desc")}
    saves = group.saves.select_related("question__bank", "saved_by__profile")
    members = (GroupMember.objects.filter(group=group, status=GroupMember.ACTIVE).select_related("user__profile")
               if not group.is_everyone else [])
    pending = (GroupMember.objects.filter(group=group, status=GroupMember.PENDING).select_related("user__profile")
               if is_owner(group, user) else [])
    count = get_user_model().objects.filter(is_active=True).count() if group.is_everyone else len(members)
    faces = [m.user.profile for m in list(members)[:6] if hasattr(m.user, "profile")]   # for the cover
    for p in posts:
        p.can_delete = can_delete_post(group, p, user)
        p.can_edit = can_edit_post(p, user)
    pals = []
    if not group.is_everyone:
        from .friends import friends_of
        taken = set(GroupMember.objects.filter(group=group).values_list("user_id", flat=True))
        pals = [p for p in friends_of(user) if p.user_id not in taken]
        invited = GroupMember.objects.filter(group=group, status=GroupMember.INVITED).select_related("user__profile")
    else:
        invited = []
    from . import challenge as ch
    board = ch.leaderboard_context(group, user)
    by_quiz = {c.quiz_id: c for c in board["running"] + board["waiting"] + board["boards"]}
    for p in posts:
        p.challenge = by_quiz.get(p.quiz_id) if p.quiz_id else None
    for s in shared:
        s.challenge = by_quiz.get(s.quiz_id)
    return render(request, "bank/group_page.html", {
        **board, "board_count": len(board["boards"]) + len(board["running"]) + len(board["waiting"]),
        "group": group, "posts": posts, "shared": shared, "saves": saves[:30], "saves_count": saves.count(),
        "members": members, "pending": pending, "count": count, "owner": is_owner(group, user),
        "can_post": can_post(group, user), "can_announce": can_announce(group, user),
        "link": request.build_absolute_uri(group.get_absolute_url()),
        "pals": pals, "invited": invited, "faces": faces,
    })


@login_required
@require_POST
def group_action(request, code):
    group = _group_or_404(code)
    user = request.user
    action = request.POST.get("action")
    member = GroupMember.objects.filter(group=group, user=user).first()
    owner = is_owner(group, user)
    target_id = request.POST.get("user")
    target = (GroupMember.objects.filter(group=group, user_id=target_id).select_related("user").first()
              if target_id and target_id.isdigit() else None)

    if action == "accept_invite" and member and member.status == GroupMember.INVITED:
        if member.invited_by_id and member.invited_by_id == group.owner_id:
            member.status, member.joined_at = GroupMember.ACTIVE, timezone.now()
            member.save(update_fields=["status", "joined_at"])
            add_post(group, user, GroupPost.JOINED)
            messages.success(request, f"Welcome to {group.name}!")
        else:   # a member invited them: the owner still decides
            member.status = GroupMember.PENDING
            member.save(update_fields=["status"])
            if group.owner_id:
                notify([group.owner_id], f"{_name(user)} wants to join {group.name}", group.get_absolute_url())
            messages.success(request, "Thanks! The owner will let you in.")
        return redirect(group)
    if action == "decline_invite" and member and member.status == GroupMember.INVITED:
        member.delete()
        return redirect("bank:groups")
    if action == "request" and not group.is_everyone and member and member.status == GroupMember.INVITED:
        return _accept_from_request(request, group, member)
    if action == "request" and not group.is_everyone:
        if member is None:
            if GroupMember.objects.filter(group=group, status=GroupMember.ACTIVE).count() >= MAX_MEMBERS:
                messages.error(request, "This group is full.")
            else:
                GroupMember.objects.create(group=group, user=user)
                if group.owner_id:
                    notify([group.owner_id], f"{_name(user)} asked to join {group.name}", group.get_absolute_url())
                messages.success(request, "Request sent. The owner will let you in.")
        return redirect(group)
    if action == "cancel" and member and member.status == GroupMember.PENDING:
        member.delete()
        return redirect("bank:groups")
    if action == "leave" and member and member.status == GroupMember.ACTIVE and group.owner_id != user.pk:
        member.delete()
        messages.success(request, f"You left {group.name}.")
        return redirect("bank:groups")
    if not is_member(group, user):
        raise Http404
    if action in ("accept", "decline", "remove", "make_owner", "edit", "new_link", "delete") and not owner:
        messages.error(request, "Only the group's owner can do that.")
        return redirect(group)

    if action == "accept" and target and target.status == GroupMember.PENDING:
        target.status, target.joined_at = GroupMember.ACTIVE, timezone.now()
        target.save(update_fields=["status", "joined_at"])
        add_post(group, target.user, GroupPost.JOINED)
        notify([target.user_id], f"You're in! {group.name} accepted you.", group.get_absolute_url())
    elif action in ("decline", "remove") and target and target.user_id != group.owner_id:
        target.delete()
    elif action == "make_owner" and target and target.status == GroupMember.ACTIVE:
        group.owner = target.user
        group.save(update_fields=["owner"])
        messages.success(request, f"{_name(target.user)} owns the group now.")
    elif action == "edit":
        group.name = " ".join(request.POST.get("name", "").split())[:60] or group.name
        group.emoji = (request.POST.get("emoji", "").strip() or group.emoji)[:8]
        group.description = " ".join(request.POST.get("description", "").split())[:200]
        group.save(update_fields=["name", "emoji", "description"])
    elif action == "new_link" and not group.is_everyone:
        group.code = new_code()
        group.save(update_fields=["code"])
        messages.success(request, "New link made. The old one no longer works.")
    elif action == "delete" and not group.is_everyone:
        name = group.name
        group.delete()
        messages.success(request, f"Deleted {name}.")
        return redirect("bank:groups")
    elif action == "invite" and not group.is_everyone:
        from .friends import friends_of
        ids = {int(i) for i in request.POST.getlist("friends") if i.isdigit()}
        pals = [p.user for p in friends_of(user) if p.user_id in ids]
        room = MAX_MEMBERS - GroupMember.objects.filter(group=group).exclude(status=GroupMember.PENDING).count()
        sent = 0
        for pal in pals[:max(room, 0)]:
            _, created = GroupMember.objects.get_or_create(
                group=group, user=pal, defaults={"status": GroupMember.INVITED, "invited_by": user})
            if created:
                sent += 1
                notify([pal.pk], f"{_name(user)} invited you to the group {group.name}", group.get_absolute_url())
        messages.success(request, f"Invited {sent} friend{'s' if sent != 1 else ''}." if sent else "Pick at least one friend.")
    elif action == "cancel_invite" and target and target.status == GroupMember.INVITED and (
            target.invited_by_id == user.pk or owner):
        target.delete()
    elif action in ("delete_post", "edit_post"):
        post = GroupPost.objects.filter(group=group, pk=request.POST.get("post")).first()
        if post is None:
            pass
        elif action == "delete_post" and can_delete_post(group, post, user):
            post.delete()
        elif action == "edit_post" and can_edit_post(post, user):
            text = request.POST.get("text", "").strip()[:1000]
            if text:
                post.text, post.edited_at = text, timezone.now()
                post.save(update_fields=["text", "edited_at"])
    elif action == "announce":
        if not can_announce(group, user):
            messages.error(request, "Only the owner can post announcements here." if not group.is_everyone
                           else "Only admins post in the Academic Secretary.")
        else:
            text = request.POST.get("text", "").strip()[:1000]
            if text:
                add_post(group, user, GroupPost.ANNOUNCE, text=text,
                         notify_text=f"{group.name}: {text[:90]}")
    elif action == "unsave":
        GroupSave.objects.filter(group=group, question_id=request.POST.get("question")).delete() if can_post(group, user) else None
    elif action == "unshare":
        share = GroupQuiz.objects.filter(group=group, quiz_id=request.POST.get("quiz")).first()
        if share and (share.shared_by_id == user.pk or owner):
            share.delete()
    elif action == "add_quiz":
        from .challenge import lock_for
        share = GroupQuiz.objects.filter(group=group, quiz_id=request.POST.get("quiz")).first()
        if share and lock_for(user, share.quiz):
            messages.error(request, "Finish the challenge first. You can add it to My Space after that.")
        elif share:
            QuizAccess.objects.get_or_create(user=user, quiz=share.quiz)
            messages.success(request, f"Added “{share.quiz.title}” to My Space.")
    return redirect(group)


def _accept_from_request(request, group, member):
    """Someone who was invited opened the link and pressed "Ask to join": treat it as accepting."""
    request.POST = request.POST.copy()
    request.POST["action"] = "accept_invite"
    return group_action(request, group.code)


@login_required
@ensure_csrf_cookie
def saved_quiz(request, code):
    group = _group_or_404(code)
    if not is_member(group, request.user):
        raise Http404
    return render(request, "bank/quiz.html", {
        "shuffle": "1" if request.GET.get("shuffle") else "0",
        "title": f"{group.name} · saved questions", "key": f"group:{group.pk}", "question_type": "saved",
        "api": reverse("bank:group_saved_questions", args=[group.code]), "back": group.get_absolute_url(),
        "review": False, "edit_api": "", "can_sort": False, "sort_api": "",
    })


@login_required
def saved_questions(request, code):
    from .views import serialize
    group = _group_or_404(code)
    if not is_member(group, request.user):
        raise Http404
    ids = list(group.saves.values_list("question_id", flat=True))
    qs = (Question.objects.filter(pk__in=ids, is_in_sorting_bin=False, is_in_review_bin=False)
          .select_related("image_asset", "answer_image_asset").prefetch_related("options", "image_targets"))
    by_id = {q["id"]: q for q in serialize(qs, request.user, include_answers=True)}
    return JsonResponse({"title": group.name, "questions": [by_id[i] for i in ids if i in by_id]})


# ---------------------------------------------------------------- used by other screens
@login_required
def api_postable(request):
    """The groups this person can save questions to and share quizzes with (for the pickers)."""
    question = request.GET.get("question")
    saved = set(GroupSave.objects.filter(question_id=question).values_list("group_id", flat=True)) if question else set()
    return JsonResponse({"groups": [{"code": g.code, "name": g.name, "emoji": g.emoji, "saved": g.pk in saved}
                                    for g in postable_groups(request.user)]})


@login_required
@require_POST
def api_group_save(request, pk):
    from .views import json_body
    question = get_object_or_404(Question, pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
    data = json_body(request) or {}
    group = StudyGroup.objects.filter(code=str(data.get("group", ""))).first()
    if group is None or not can_post(group, request.user):
        return JsonResponse({"error": "You can't save to that group."}, status=403)
    existing = GroupSave.objects.filter(group=group, question=question)
    if existing.exists():
        existing.delete()
        return JsonResponse({"saved": False})
    try:
        with transaction.atomic():
            GroupSave.objects.create(group=group, question=question, saved_by=request.user)
            add_post(group, request.user, GroupPost.SAVED, question=question)
    except IntegrityError:
        pass
    return JsonResponse({"saved": True})


def share_quiz(group, quiz, user, challenge=None):
    """Share a My Space quiz with a group (from My Space's Send panel).
    ``challenge`` = {"reveal", "days", "allow_hiding"} turns its leaderboard on."""
    from . import challenge as ch
    if not can_post(group, user):
        return False
    share, created = GroupQuiz.objects.get_or_create(group=group, quiz=quiz, defaults={"shared_by": user})
    if challenge and (created or not share.is_open):
        ch.setup(share, challenge.get("reveal"), challenge.get("days"), challenge.get("allow_hiding", True))
        share.shared_by = user
        share.save()
        ends = timezone.localtime(share.ends_at)
        add_post(group, user, GroupPost.QUIZ, quiz=quiz,
                 notify_text=f"🏆 New challenge in {group.name}: {quiz.title}. Ends {ends:%a %d %b, %H:%M}.")
        return True
    if created:
        add_post(group, user, GroupPost.QUIZ, quiz=quiz)
    return created


def post_live(group, room, user):
    """Post a live room to the group: members get a notification and a Join button in the feed."""
    if not can_post(group, user):
        return "You can't post in that group."
    room.group = group
    add_post(group, user, GroupPost.LIVE, live_room=room,
             notify_text=f"{_name(user)} started a live quiz in {group.name}: {room.title}")
    return ""


def pending_for_owner(user):
    """For the Groups tab badge: join requests waiting on this owner, plus invitations waiting on this user."""
    return (GroupMember.objects.filter(group__owner=user, group__is_everyone=False, status=GroupMember.PENDING).count()
            + GroupMember.objects.filter(user=user, status=GroupMember.INVITED).count())

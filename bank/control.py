"""CONTROL: the super admins' room. Only Django superusers and the people they pick (ControlAccess) see it.

* People: every account, search, last seen, role, reset password, sign out everywhere, deactivate.
* Add accounts: paste names, get usernames and passwords back (sign-up is closed).
* Overview: site-wide totals only (no one's scores): who's active, answers per day, busiest hours,
  most used subjects and chapters, the most-missed questions (often a wrong answer key).
* Log: who changed what and when (questions, accounts, groups, Control itself).
"""
import contextvars
import json
import re
import secrets
import time
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (AuditEntry, ChapterDay, ControlAccess, DailyVisit, HiddenAccount, HourCount, Profile, Question,
                     QuestionStat)

User = get_user_model()
MANAGERS = "Managers"
ROLES = (("student", "Student", "Uses the site. No team tools."),
         ("team", "Team member", "Final review, sorting and review bins, posts in the Academic Secretary."),
         ("manager", "Manager", "Everything a team member does, plus importing, editing and deleting questions."))
ONLINE = timedelta(minutes=5)
_actor = contextvars.ContextVar("control_actor", default=None)


# ---------------------------------------------------------------- who gets in
def can_control(user):
    return bool(user and user.is_authenticated and user.is_active
                and (user.is_superuser or ControlAccess.objects.filter(user=user).exists()))


def control_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not can_control(request.user):
            raise Http404
        return view(request, *args, **kwargs)
    return wrapped


def hidden_ids():
    return set(HiddenAccount.objects.values_list("user_id", flat=True))


def visible_users(viewer, qs=None):
    """Accounts this person may see in Control and Django Admin: the owner sees all, others don't see hidden ones."""
    qs = User.objects.all() if qs is None else qs
    return qs if viewer.is_superuser else qs.exclude(pk__in=HiddenAccount.objects.values("user_id"))


def role_of(user):
    if user.is_superuser:
        return "owner"
    if user.is_staff and user.groups.filter(name=MANAGERS).exists():
        return "manager"
    return "team" if user.is_staff else "student"


def set_role(user, role):
    managers, _ = Group.objects.get_or_create(name=MANAGERS)
    user.is_staff = role in ("team", "manager")
    user.save(update_fields=["is_staff"])
    if role == "manager":
        user.groups.add(managers)
    else:
        user.groups.remove(managers)


# ---------------------------------------------------------------- audit log
def audit(area, action, summary, url="", actor=None, about=None):
    actor = actor or _actor.get()
    if actor is None or not getattr(actor, "is_authenticated", False):
        return None                    # scripts and imports run without a person; they aren't logged
    return AuditEntry.objects.create(actor=actor, area=area, action=action, summary=summary[:300], target_url=url[:300],
                                     about=about)


QUESTION_FIELDS = {"stem": "question", "answer": "answer", "explanation": "explanation", "note": "note",
                   "kind": "type", "bank_id": "chapter", "is_in_sorting_bin": "sorting bin",
                   "is_in_review_bin": "review bin", "label": "label"}


def _question_pre_save(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk or _actor.get() is None:
        return
    old = Question.objects.filter(pk=instance.pk).values(*[f for f in QUESTION_FIELDS if hasattr(instance, f)]).first()
    if old:
        instance._audit_changed = [label for f, label in QUESTION_FIELDS.items()
                                   if f in old and old[f] != getattr(instance, f)]


def _question_post_save(sender, instance, created, raw=False, **kwargs):
    changed = getattr(instance, "_audit_changed", None)
    if raw or created or not changed:
        return
    instance._audit_changed = None
    if changed == ["review bin"] or changed == ["sorting bin"]:
        where = changed[0]
        moved = "to the" if getattr(instance, "is_in_review_bin" if where == "review bin" else "is_in_sorting_bin") else "out of the"
        text = f"Moved question #{instance.pk} {moved} {where}"
    else:
        text = f"Edited question #{instance.pk} ({', '.join(changed)})"
    audit(AuditEntry.QUESTION, "edit", f"{text}: {instance.stem[:120]}", reverse("bank:question_edit", args=[instance.pk]))


def _question_deleted(sender, instance, **kwargs):
    audit(AuditEntry.QUESTION, "delete", f"Deleted question #{instance.pk}: {instance.stem[:150]}")


def _option_pre_save(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk or _actor.get() is None:
        return
    old = sender.objects.filter(pk=instance.pk).values_list("is_correct", flat=True).first()
    if old is not None and old != instance.is_correct:
        audit(AuditEntry.QUESTION, "key", f"Changed the answer key of question #{instance.question_id}",
              reverse("bank:question_edit", args=[instance.question_id]))


def connect_signals():
    from django.db.models.signals import post_delete, post_save, pre_save
    from .models import Option
    pre_save.connect(_question_pre_save, sender=Question, dispatch_uid="control_q_pre")
    post_save.connect(_question_post_save, sender=Question, dispatch_uid="control_q_post")
    post_delete.connect(_question_deleted, sender=Question, dispatch_uid="control_q_del")
    pre_save.connect(_option_pre_save, sender=Option, dispatch_uid="control_opt_pre")


# ---------------------------------------------------------------- activity (site-wide only)
_last_mark = {}


class ActivityMiddleware:
    """Remembers who is using the site: last seen and one row per person per day. Each worker writes at
    most once every few minutes per person, so it costs next to nothing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        token = _actor.set(user if user is not None and user.is_authenticated else None)
        try:
            if user is not None and user.is_authenticated and not request.path.startswith(("/static/", "/push/")):
                now = time.monotonic()
                if now - _last_mark.get(user.pk, -1e9) > 180:
                    _last_mark[user.pk] = now
                    self.mark(user)
            return self.get_response(request)
        finally:
            _actor.reset(token)

    @staticmethod
    def mark(user):
        try:
            stamp = timezone.now()
            Profile.objects.filter(user=user).update(last_seen=stamp)
            day = timezone.localdate()
            if not DailyVisit.objects.filter(day=day, user=user).update(visits=F("visits") + 1):
                try:
                    with transaction.atomic():
                        DailyVisit.objects.create(day=day, user=user)
                except IntegrityError:
                    pass
        except Exception:      # counting must never break a page
            pass


def _bump(model, lookup, field, n):
    if not model.objects.filter(**lookup).update(**{field: F(field) + n}):
        try:
            with transaction.atomic():
                model.objects.create(**lookup, **{field: n})
        except IntegrityError:
            model.objects.filter(**lookup).update(**{field: F(field) + n})


@csrf_exempt          # sent with navigator.sendBeacon (no headers); it only adds to site-wide counters
@require_POST
def api_answers(request):
    """The quiz page reports answers in batches: [{"id": question id, "ok": true/false}]."""
    if not request.user.is_authenticated:
        return JsonResponse({}, status=401)
    try:
        items = json.loads(request.body or b"{}").get("answers", [])[:200]
    except (ValueError, AttributeError):
        return JsonResponse({}, status=400)
    picks = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and item.get("ok") in (True, False):
            picks[item["id"]] = item["ok"]             # one count per question per batch
    if not picks:
        return JsonResponse({"counted": 0})
    banks = dict(Question.objects.filter(pk__in=picks).values_list("pk", "bank_id"))
    now = timezone.localtime()
    per_bank = {}
    for qid, ok in picks.items():
        if qid not in banks:
            continue
        _bump(QuestionStat, {"question_id": qid}, "answered", 1)
        if not ok:
            QuestionStat.objects.filter(question_id=qid).update(missed=F("missed") + 1)
        per_bank[banks[qid]] = per_bank.get(banks[qid], 0) + 1
    for bank_id, n in per_bank.items():
        _bump(ChapterDay, {"day": now.date(), "bank_id": bank_id}, "answers", n)
    counted = sum(per_bank.values())
    if counted:
        _bump(HourCount, {"day": now.date(), "hour": now.hour}, "answers", counted)
    return JsonResponse({"counted": counted})


# ---------------------------------------------------------------- pages
def _tabs(request, here):
    return {"here_tab": here, "is_owner": request.user.is_superuser}


@control_required
def overview(request):
    today = timezone.localdate()
    now = timezone.now()
    users = visible_users(request.user, User.objects.filter(is_active=True))
    visits = DailyVisit.objects.all()

    def active(days):
        return visits.filter(day__gt=today - timedelta(days=days)).values("user").distinct().count()

    answers = ChapterDay.objects.all()
    def answered(days):
        return answers.filter(day__gt=today - timedelta(days=days)).aggregate(n=Sum("answers"))["n"] or 0

    # Last 30 days
    span = [today - timedelta(days=i) for i in range(29, -1, -1)]
    dau = dict(visits.filter(day__gte=span[0]).values("day").annotate(n=Count("user")).values_list("day", "n"))
    apd = dict(answers.filter(day__gte=span[0]).values("day").annotate(n=Sum("answers")).values_list("day", "n"))
    top_dau, top_apd = max(dau.values(), default=0) or 1, max(apd.values(), default=0) or 1
    days = [{"day": d, "users": dau.get(d, 0), "answers": apd.get(d, 0),
             "u": round(100 * dau.get(d, 0) / top_dau), "a": round(100 * apd.get(d, 0) / top_apd)} for d in span]
    hours_raw = dict(HourCount.objects.filter(day__gte=span[0]).values("hour").annotate(n=Sum("answers")).values_list("hour", "n"))
    top_h = max(hours_raw.values(), default=0) or 1
    hours = [{"hour": h, "n": hours_raw.get(h, 0), "h": round(100 * hours_raw.get(h, 0) / top_h)} for h in range(24)]
    week = answers.filter(day__gt=today - timedelta(days=7))
    subjects = list(week.values("bank__subject__name").annotate(n=Sum("answers")).order_by("-n")[:8])
    chapters = list(week.values("bank__title", "bank__slug", "bank__subject__name").annotate(n=Sum("answers")).order_by("-n")[:10])
    top_s = max((s["n"] for s in subjects), default=0) or 1
    for s in subjects:
        s["w"] = round(100 * s["n"] / top_s)
    missed = list(QuestionStat.objects.filter(answered__gte=8, question__is_in_review_bin=False)
                  .select_related("question__bank").order_by(F("missed") * 1000 / F("answered")).reverse()[:15])
    for m in missed:
        m.rate = round(100 * m.missed / m.answered)
    from .models import ChallengeAttempt, ExamAttempt, LiveRoom, PersonalQuiz, StudyGroup
    week_ago = now - timedelta(days=7)
    return render(request, "bank/control_overview.html", {
        **_tabs(request, "overview"),
        "accounts": users.count(),
        "never": users.filter(last_login__isnull=True).count(),
        "online": users.filter(profile__last_seen__gte=now - ONLINE).count(),
        "today": active(1), "week": active(7), "month": active(30),
        "answers_today": answered(1), "answers_week": answered(7),
        "days": days, "hours": hours, "subjects": subjects, "chapters": chapters, "missed": missed,
        "tracking_since": visits.order_by("day").values_list("day", flat=True).first(),
        "made": {"quizzes": PersonalQuiz.objects.filter(created_at__gte=week_ago).count(),
                 "exams": ExamAttempt.objects.filter(submitted_at__gte=week_ago).count(),
                 "live": LiveRoom.objects.filter(created_at__gte=week_ago).count(),
                 "challenges": ChallengeAttempt.objects.filter(finished_at__gte=week_ago).count(),
                 "groups": StudyGroup.objects.count(),
                 "questions": Question.objects.filter(is_in_sorting_bin=False, is_in_review_bin=False).count()},
    })


@control_required
def people(request):
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "")
    everyone = visible_users(request.user)
    rows = everyone.select_related("profile").prefetch_related("groups").order_by("-is_superuser", "-is_staff", "username")
    if q:
        rows = rows.filter(Q(username__icontains=q) | Q(profile__display_name__icontains=q) | Q(profile__handle__icontains=q))
    now = timezone.now()
    if show == "online":
        rows = rows.filter(profile__last_seen__gte=now - ONLINE)
    elif show == "never":
        rows = rows.filter(last_login__isnull=True, is_active=True)
    elif show == "off":
        rows = rows.filter(is_active=False)
    elif show == "team":
        rows = rows.filter(is_staff=True)
    elif show == "hidden" and request.user.is_superuser:
        rows = rows.filter(pk__in=HiddenAccount.objects.values("user_id"))
    page = Paginator(rows, 60).get_page(request.GET.get("page"))
    control_ids = set(ControlAccess.objects.values_list("user_id", flat=True))
    hidden = hidden_ids() if request.user.is_superuser else set()
    for u in page:
        u.hidden = u.pk in hidden
        u.role = role_of(u)
        u.control = u.is_superuser or u.pk in control_ids
        seen = getattr(getattr(u, "profile", None), "last_seen", None)
        u.online = bool(seen and seen >= now - ONLINE)
        u.seen = seen
    counts = {"all": everyone.count(), "online": everyone.filter(profile__last_seen__gte=now - ONLINE).count(),
              "never": everyone.filter(last_login__isnull=True, is_active=True).count(),
              "off": everyone.filter(is_active=False).count(), "team": everyone.filter(is_staff=True).count(),
              "hidden": len(hidden)}
    return render(request, "bank/control_people.html", {
        **_tabs(request, "people"), "page": page, "q": q, "show": show, "counts": counts, "roles": ROLES,
        "new_password": request.session.pop("control_new_password", None),
    })


WORDS = ("kidney", "aorta", "nephron", "atlas", "cortex", "sinus", "valve", "neuron", "retina", "femur", "radius",
         "ulna", "vagus", "pleura", "bile", "insulin", "plasma", "septum", "thymus", "tendon", "pulse", "lymph")


def make_password():
    return f"{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"


def sign_out_everywhere(user):
    for s in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        try:
            if str(s.get_decoded().get("_auth_user_id")) == str(user.pk):
                s.delete()
        except Exception:
            continue
    cache.delete(f"bank:active-session:{user.pk}")


@control_required
@require_POST
def person_action(request, pk):
    target = get_object_or_404(visible_users(request.user).select_related("profile"), pk=pk)
    me = request.user
    act = request.POST.get("action")
    name = getattr(getattr(target, "profile", None), "name", target.username)
    back = request.POST.get("back") or reverse("bank:control_people")
    if not back.startswith("/"):
        back = reverse("bank:control_people")
    if target.is_superuser and not me.is_superuser:
        messages.error(request, "Only the owner can change an owner's account.")
        return redirect(back)
    if target.pk == me.pk and act in ("role", "deactivate", "signout"):
        messages.error(request, "You can’t do that to your own account here.")
        return redirect(back)
    if act == "role":
        role = request.POST.get("role")
        if role in dict((k, v) for k, v, _ in ROLES) and not target.is_superuser:
            before = role_of(target)
            set_role(target, role)
            if before != role:
                audit(AuditEntry.ACCOUNT, "role", f"Changed {name}’s role: {before} → {role}", about=target)
                messages.success(request, f"{name} is now a {dict((k, v) for k, v, _ in ROLES)[role].lower()}.")
    elif act == "password":
        password = make_password()
        target.set_password(password)
        target.save(update_fields=["password"])
        Profile.objects.filter(user=target).update(must_change_password=True)     # they pick their own next time
        sign_out_everywhere(target)
        audit(AuditEntry.ACCOUNT, "password", f"Reset {name}’s password", about=target)
        request.session["control_new_password"] = {"name": name, "username": target.username, "password": password}
    elif act == "signout":
        sign_out_everywhere(target)
        audit(AuditEntry.ACCOUNT, "signout", f"Signed {name} out everywhere", about=target)
        messages.success(request, f"{name} is signed out on every phone and computer.")
    elif act == "deactivate" and target.is_active:
        target.is_active = False
        target.save(update_fields=["is_active"])
        sign_out_everywhere(target)
        audit(AuditEntry.ACCOUNT, "deactivate", f"Deactivated {name}", about=target)
        messages.success(request, f"{name} can’t sign in any more.")
    elif act == "activate" and not target.is_active:
        target.is_active = True
        target.save(update_fields=["is_active"])
        audit(AuditEntry.ACCOUNT, "activate", f"Reactivated {name}", about=target)
        messages.success(request, f"{name} can sign in again.")
    elif act in ("hide", "unhide"):
        if not me.is_superuser:
            messages.error(request, "Only the owner changes this.")
        elif act == "hide":
            HiddenAccount.objects.get_or_create(user=target)
            audit(AuditEntry.CONTROL, "hide", f"Archived {name}’s account", about=target)
            messages.success(request, f"Archived {name}’s account.")
        else:
            HiddenAccount.objects.filter(user=target).delete()
            audit(AuditEntry.CONTROL, "unhide", f"Unarchived {name}’s account", about=target)
    elif act in ("grant", "revoke"):
        if not me.is_superuser:
            messages.error(request, "Only the owner chooses who gets into Control.")
        elif act == "grant":
            ControlAccess.objects.get_or_create(user=target, defaults={"added_by": me})
            audit(AuditEntry.CONTROL, "grant", f"Gave {name} access to Control", about=target)
        else:
            ControlAccess.objects.filter(user=target).delete()
            audit(AuditEntry.CONTROL, "revoke", f"Took {name}’s access to Control away", about=target)
    return redirect(back)


def username_for(name, taken):
    """ahmed.ali from "Ahmed Ali Hassan"; Arabic or odd names fall back to student123."""
    words = [w for w in re.split(r"\s+", slugify(name, allow_unicode=False).replace("-", " ")) if w][:2]
    base = ".".join(words)[:24] or "student"
    candidate, n = base, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{base}{n}"
    taken.add(candidate.lower())
    return candidate


MAX_BULK = 300


@control_required
def add_accounts(request):
    from .sources import UNIVERSITIES
    created = []
    if request.method == "POST":
        lines = [l.strip() for l in request.POST.get("names", "").splitlines() if l.strip()]
        if not lines:
            messages.error(request, "Paste at least one name.")
        elif len(lines) > MAX_BULK:
            messages.error(request, f"Up to {MAX_BULK} names at a time.")
        else:
            university = request.POST.get("university", "")
            year = request.POST.get("year", "")
            taken = {u.lower() for u in User.objects.values_list("username", flat=True)}
            with transaction.atomic():
                for line in lines:
                    name, _, wanted = (part.strip() for part in line.partition(","))
                    name = " ".join(name.split())[:60]
                    wanted = re.sub(r"[^\w.@+-]", "", wanted)[:30]
                    if wanted and wanted.lower() not in taken:
                        username = wanted
                        taken.add(wanted.lower())
                    else:
                        username = username_for(name, taken)
                    password = make_password()
                    user = User.objects.create_user(username=username, password=password)
                    # Read the profile the sign-up signal just made straight from the database: on Django 6.1.3
                    # user.profile can still hold the "no profile yet" it cached before the row existed.
                    profile = Profile.objects.get(user=user)
                    profile.display_name = name
                    profile.must_change_password = True
                    if university in {k for k, _n, _ in UNIVERSITIES} | {"other"}:
                        profile.university = university
                    if year.isdigit() and 1 <= int(year) <= 6:
                        profile.year = int(year)
                    profile.save()
                    created.append({"name": name, "username": username, "password": password})
            audit(AuditEntry.ACCOUNT, "bulk", f"Added {len(created)} account{'s' if len(created) != 1 else ''}: "
                  + ", ".join(c["username"] for c in created[:12]) + ("…" if len(created) > 12 else ""))
    login = request.build_absolute_uri(reverse("bank:login"))
    sheet = "\n".join(f"{c['name']} · username: {c['username']} · password: {c['password']}" for c in created)
    return render(request, "bank/control_add.html", {
        **_tabs(request, "add"), "created": created, "login": login, "sheet": sheet,
        "universities": UNIVERSITIES, "max_bulk": MAX_BULK,
    })


@control_required
def log(request):
    area = request.GET.get("area", "")
    who = request.GET.get("who", "")
    q = request.GET.get("q", "").strip()
    rows = AuditEntry.objects.select_related("actor__profile")
    if not request.user.is_superuser:
        secret = HiddenAccount.objects.values("user_id")
        rows = rows.exclude(actor_id__in=secret).exclude(about_id__in=secret)
    if area in (AuditEntry.QUESTION, AuditEntry.ACCOUNT, AuditEntry.GROUP, AuditEntry.CONTROL):
        rows = rows.filter(area=area)
    if who.isdigit():
        rows = rows.filter(actor_id=int(who))
    if q:
        rows = rows.filter(summary__icontains=q)
    page = Paginator(rows, 80).get_page(request.GET.get("page"))
    actors = (visible_users(request.user).filter(pk__in=AuditEntry.objects.values("actor_id")).select_related("profile")
              .order_by("username"))
    return render(request, "bank/control_log.html", {
        **_tabs(request, "log"), "page": page, "area": area, "who": who, "q": q, "actors": actors,
    })

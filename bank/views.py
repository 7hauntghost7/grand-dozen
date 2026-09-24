import json
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, redirect_to_login
from django.db import transaction
from django.db.models import Count, Q
from django.core.cache import cache
from django.contrib.sessions.models import Session
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import parser, services
from .forms import QuestionForm, option_formset
from .models import Comment, Question, QuestionBank, SavedQuestion, Subject, SEMESTER_CHOICES


# ---------------------------------------------------------------- helpers
def perm_required(perm):
    """Anonymous -> login page; logged in without the permission -> 403."""
    def deco(view):
        @wraps(view)
        def wrapped(request, *a, **kw):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not request.user.has_perm(perm):
                return HttpResponseForbidden("You don't have permission to do this.")
            return view(request, *a, **kw)
        return wrapped
    return deco


def safe_next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
    return nxt if nxt and ok else default


def serialize(questions, user):
    qs = list(questions)
    saved = set()
    if user.is_authenticated and qs:
        saved = set(SavedQuestion.objects.filter(user=user, question_id__in=[q.id for q in qs])
                    .values_list("question_id", flat=True))
    data = []
    for q in qs:
        opts = list(q.options.all())
        data.append({
            "id": q.id, "number": q.number or str(len(data) + 1), "kind": q.kind,
            "stem": q.stem, "saved": q.id in saved,
            "multi": q.kind == Question.MCQ and sum(o.is_correct for o in opts) > 1,
            "options": [{"id": o.id, "letter": chr(65 + i), "text": o.text} for i, o in enumerate(opts)],
        })
    return data


def json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return None


# ---------------------------------------------------------------- browsing
def home(request):
    subjects = Subject.objects.annotate(
        n_decks=Count("decks", distinct=True), n_q=Count("decks__questions", distinct=True))
    unfiled = QuestionBank.objects.filter(subject__isnull=True).annotate(n=Count("questions"))
    return render(request, "bank/home.html", {"subjects": subjects, "unfiled": unfiled})


def subject_detail(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    decks = subject.decks.annotate(n=Count("questions")).order_by("semester", "title")
    return render(request, "bank/subject_detail.html", {"subject": subject, "decks": decks})


def bank_detail(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    return render(request, "bank/bank_detail.html", {"bank": bank, "count": bank.questions.count()})


def _quiz_context(request, **kw):
    ctx = {"shuffle": "1" if request.GET.get("shuffle") else "0"}
    ctx.update(kw)
    return ctx


@ensure_csrf_cookie
def quiz(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=bank.title, key=f"deck:{bank.slug}",
        api=reverse("bank:api_questions", args=[bank.slug]), back=bank.get_absolute_url()))


@login_required
@ensure_csrf_cookie
def saved_quiz(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=f"Saved · {bank.title}", key=f"saved:{request.user.pk}:{bank.slug}",
        api=reverse("bank:api_saved_questions", args=[bank.slug]),
        back=reverse("bank:saved_deck", args=[bank.slug])))


def export_bank(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    text = parser.to_text(bank.title, services.bank_as_dicts(bank),
                          subject=bank.subject.name if bank.subject else None, semester=bank.semester)
    resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{bank.slug}.txt"'
    return resp


# ---------------------------------------------------------------- JSON API
def api_questions(request, slug):
    """Questions WITHOUT answers; correctness is checked server-side per question."""
    bank = get_object_or_404(QuestionBank, slug=slug)
    qs = bank.questions.prefetch_related("options")
    return JsonResponse({"title": bank.title, "questions": serialize(qs, request.user)})


@login_required
def api_saved_questions(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    qs = (bank.questions.filter(saved_by__user=request.user)
          .prefetch_related("options").order_by("id"))
    return JsonResponse({"title": bank.title, "questions": serialize(qs, request.user)})


@require_POST
def api_check(request, pk):
    q = get_object_or_404(Question.objects.prefetch_related("options"), pk=pk)
    data = json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    opts = list(q.options.all())
    out = {"explanation": q.explanation, "note": q.note, "correct": None}
    reveal = bool(data.get("reveal"))
    if q.kind == Question.TF:
        key = {str(o.id): o.is_correct for o in opts}
        out["key"] = key
        if not reveal:
            answers = data.get("answers") or {}
            per = {k: answers.get(k) is v for k, v in key.items()}
            out["per"] = per
            out["correct"] = all(per.values())
    else:
        correct_ids = [o.id for o in opts if o.is_correct]
        out["correct_ids"] = correct_ids
        if not reveal:
            out["correct"] = data.get("selected") in correct_ids
    return JsonResponse(out)


@require_POST
def api_save(request, pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login"}, status=401)
    q = get_object_or_404(Question, pk=pk)
    data = json_body(request) or {}
    existing = SavedQuestion.objects.filter(user=request.user, question=q)
    want = data.get("saved")
    if want is None:
        want = not existing.exists()
    if want:
        SavedQuestion.objects.get_or_create(user=request.user, question=q)
    else:
        existing.delete()
    return JsonResponse({"saved": bool(want)})


@require_POST
def api_comment(request, pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login"}, status=401)
    q = get_object_or_404(Question, pk=pk)
    data = json_body(request) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return JsonResponse({"error": "Please write a comment first."}, status=400)
    if len(text) > 2000:
        return JsonResponse({"error": "Comment is too long (max 2000 characters)."}, status=400)
    Comment.objects.create(question=q, user=request.user, text=text)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------- saved section
def _saved_decks(user):
    return QuestionBank.objects.annotate(
        saved_n=Count("questions__saved_by", filter=Q(questions__saved_by__user=user), distinct=True)
    ).filter(saved_n__gt=0)


@login_required
def saved_home(request):
    subjects = Subject.objects.annotate(
        n_saved=Count("decks__questions__saved_by",
                      filter=Q(decks__questions__saved_by__user=request.user), distinct=True))
    return render(request, "bank/saved_home.html", {"subjects": subjects})


@login_required
def saved_subject(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    decks = _saved_decks(request.user).filter(subject=subject).order_by("semester", "title")
    return render(request, "bank/saved_subject.html", {"subject": subject, "decks": decks})


@login_required
def saved_deck(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    qs = (bank.questions.filter(saved_by__user=request.user)
          .prefetch_related("options").order_by("id"))
    return render(request, "bank/saved_deck.html", {"bank": bank, "questions": qs})


@login_required
@require_POST
def saved_remove(request, pk):
    SavedQuestion.objects.filter(user=request.user, question_id=pk).delete()
    return redirect(safe_next(request, reverse("bank:saved_home")))


# ---------------------------------------------------------------- editor pages
@perm_required("bank.change_question")
def manage_deck(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    qs = bank.questions.annotate(
        open_comments=Count("comments", filter=Q(comments__resolved=False), distinct=True))
    return render(request, "bank/manage_deck.html", {"bank": bank, "questions": qs})


def _edit_question(request, question, bank=None, is_new=False):
    Formset = option_formset(extra=4 if is_new else 2)
    form = QuestionForm(request.POST or None, instance=question)
    formset = Formset(request.POST or None, instance=question)
    if request.method == "POST":
        v1, v2 = form.is_valid(), formset.is_valid()
        if v1 and v2:
            with transaction.atomic():
                q = form.save()
                formset.instance = q
                formset.save()
                for i, o in enumerate(q.options.all()):   # keep options in a tidy 0..n-1 order
                    if o.order != i:
                        o.order = i
                        o.save(update_fields=["order"])
                ids = [int(x) for x in request.POST.getlist("resolve") if x.isdigit()]
                if ids and not is_new:
                    Comment.objects.filter(question=q, pk__in=ids, resolved=False).update(
                        resolved=True, resolved_by=request.user, resolved_at=timezone.now())
            messages.success(request, "Question saved.")
            return redirect(safe_next(request, reverse("bank:manage_deck", args=[q.bank.slug])))
    comments = question.comments.select_related("user") if question.pk else []
    return render(request, "bank/question_edit.html", {
        "form": form, "formset": formset, "question": question, "is_new": is_new,
        "comments": comments, "next": request.GET.get("next", ""),
    })


@perm_required("bank.change_question")
def question_edit(request, pk):
    q = get_object_or_404(Question.objects.select_related("bank__subject"), pk=pk)
    return _edit_question(request, q)


@perm_required("bank.add_question")
def question_add(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    return _edit_question(request, Question(bank=bank), is_new=True)


@perm_required("bank.delete_question")
@require_POST
def question_delete(request, pk):
    q = get_object_or_404(Question, pk=pk)
    slug = q.bank.slug
    q.delete()
    messages.success(request, "Question deleted.")
    return redirect("bank:manage_deck", slug=slug)


@perm_required("bank.change_question")
def comments_inbox(request):
    show = request.GET.get("show", "open")
    qs = Comment.objects.select_related("user", "question__bank__subject")
    qs = qs.filter(resolved=(show == "resolved"))
    return render(request, "bank/comments.html", {
        "comments": qs, "show": show,
        "n_open": Comment.objects.filter(resolved=False).count(),
        "n_resolved": Comment.objects.filter(resolved=True).count(),
    })


@perm_required("bank.change_question")
@require_POST
def comment_toggle(request, pk):
    c = get_object_or_404(Comment, pk=pk)
    c.resolved = not c.resolved
    c.resolved_by = request.user if c.resolved else None
    c.resolved_at = timezone.now() if c.resolved else None
    c.save()
    return redirect(safe_next(request, reverse("bank:comments")))


# ---------------------------------------------------------------- bulk import
@perm_required("bank.add_question")
def import_questions(request):
    subjects = Subject.objects.all()
    ctx = {
        "subjects": subjects, "semesters": [s for s, _ in SEMESTER_CHOICES], "prompt": parser.AI_PROMPT,
        "text": "", "subject_id": request.GET.get("subject", ""), "semester": request.GET.get("semester", ""),
        "deck_title": request.GET.get("deck_title", ""), "skip_dupes": True,
        "decks_json": [{"s": d.subject_id, "m": d.semester, "t": d.title}
                       for d in QuestionBank.objects.filter(subject__isnull=False)],
    }
    if request.method != "POST":
        return render(request, "bank/import.html", ctx)

    text = request.POST.get("text", "")
    upload = request.FILES.get("file")
    if upload:
        text = upload.read().decode("utf-8-sig", errors="replace")
    result = parser.parse(text)
    skip_dupes = bool(request.POST.get("skip_dupes"))

    # Form fields win; header lines (SUBJECT/SEMESTER/DECK) in the text fill any blanks.
    subject = None
    if request.POST.get("subject"):
        subject = Subject.objects.filter(pk=request.POST["subject"]).first()
    elif result.subject:
        subject = Subject.objects.filter(name__iexact=result.subject).first()
    try:
        semester = int(request.POST.get("semester") or result.semester or 0) or None
    except ValueError:
        semester = None
    title = request.POST.get("deck_title", "").strip() or result.bank_title

    deck = services.find_deck(subject, semester, title) if (subject and semester and title) else None
    if deck:
        parser.mark_duplicates(result.questions, services.existing_keys(deck))

    if request.POST.get("action") == "commit":
        missing = [n for n, v in (("subject", subject), ("semester", semester), ("deck title", title)) if not v]
        if missing:
            messages.error(request, "Please choose the " + ", ".join(missing) + ".")
        else:
            deck = deck or services.get_or_create_deck(subject, semester, title)
            created, bad, dup = services.save_questions(deck, result.questions, skip_dupes)
            messages.success(
                request, f"Imported {created} question(s) into “{deck.path}”. "
                         f"Skipped {bad} with errors and {dup} duplicate(s).")
            return redirect(deck)

    rows = []
    for q in result.questions:
        if q.kind == parser.TF:
            ans = "".join("T" if c else "F" for c in q.correct)
        else:
            ans = ", ".join(chr(65 + i) for i, c in enumerate(q.correct) if c)
        rows.append({"q": q, "answer": ans, "skipped": bool(q.duplicate_of) and skip_dupes})
    ctx.update({
        "text": text, "subject_id": str(subject.pk) if subject else "",
        "semester": str(semester or ""), "deck_title": title, "skip_dupes": skip_dupes,
        "deck_exists": deck, "rows": rows, "previewed": True,
        "importable": sum(1 for r in rows if r["q"].ok and not r["skipped"]),
        "n_errors": sum(1 for r in rows if not r["q"].ok),
        "n_dupes": sum(1 for r in rows if r["q"].duplicate_of),
        "stray": result.stray,
    })
    return render(request, "bank/import.html", ctx)


# ---------------------------------------------------------------- accounts
class SingleSessionLoginView(LoginView):
    template_name = "bank/login.html"

    def form_valid(self, form):
        user = form.get_user()
        cache_key = f"bank:active-session:{user.pk}"
        old_session_key = cache.get(cache_key)

        # Remove the previous browser session before creating the new one.
        # The old browser will therefore be logged out as soon as it makes
        # another request, while the new login becomes the only active session.
        if old_session_key and old_session_key != self.request.session.session_key:
            Session.objects.filter(session_key=old_session_key).delete()

        response = super().form_valid(form)
        if self.request.session.session_key:
            cache.set(cache_key, self.request.session.session_key, None)
        return response


def logout_view(request):
    if request.user.is_authenticated:
        cache.delete(f"bank:active-session:{request.user.pk}")
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)

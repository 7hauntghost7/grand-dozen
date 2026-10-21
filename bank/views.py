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
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import parser, services
from .forms import ImageQuestionForm, ImageUploadForm, QuestionForm, option_formset
from .image_storage import (
    ImageStorageError, delete_question_image, image_uploads_enabled, upload_question_image,
)
from .models import (
    Comment, ImageTarget, Option, Question, QuestionBank, QuestionImage,
    SavedQuestion, Subject, SubjectSection, SEMESTER_CHOICES,
)


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


def staff_required(view):
    """Require a staff account for curation workflows, independent of model perms."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not request.user.is_staff:
            return HttpResponseForbidden("Staff access is required.")
        return view(request, *args, **kwargs)
    return wrapped


def safe_next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
    return nxt if nxt and ok else default


def app_manifest(request):
    path = settings.BASE_DIR / "bank" / "static" / "bank" / "manifest.webmanifest"
    response = FileResponse(path.open("rb"), content_type="application/manifest+json")
    response["Cache-Control"] = "no-cache"
    return response


def service_worker(request):
    path = settings.BASE_DIR / "bank" / "static" / "bank" / "service-worker.js"
    response = FileResponse(path.open("rb"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def serialize(questions, user, review=False):
    qs = list(questions)
    saved = set()
    if user.is_authenticated and qs:
        saved = set(SavedQuestion.objects.filter(user=user, question_id__in=[q.id for q in qs])
                    .values_list("question_id", flat=True))
    data = []
    for q in qs:
        opts = list(q.options.all())
        question = {
            "id": q.id, "number": q.number or str(len(data) + 1), "kind": q.kind,
            "stem": q.stem, "saved": q.id in saved,
            "multi": q.kind == Question.MCQ and sum(o.is_correct for o in opts) > 1,
            "options": [{"id": o.id, "letter": chr(65 + i), "text": o.text} for i, o in enumerate(opts)],
        }
        if q.kind == Question.IMAGE:
            question.update({
                "image_url": q.image_asset.secure_url if q.image_asset_id else "",
                "image_width": q.image_asset.width if q.image_asset_id else 0,
                "image_height": q.image_asset.height if q.image_asset_id else 0,
                "image_answer_mode": q.image_answer_mode,
                "multi": q.image_answer_mode == Question.IMAGE_MCQ and sum(o.is_correct for o in opts) > 1,
                "targets": [
                    {
                        "id": target.pk,
                        "label": target.label,
                        "x_percent": float(target.x_percent) if target.x_percent is not None else None,
                        "y_percent": float(target.y_percent) if target.y_percent is not None else None,
                        **({"answer": target.answer} if review else {}),
                    }
                    for target in q.image_targets.all()
                ],
            })
        if review:
            question.update({
                "answer": q.answer,
                "explanation": q.explanation,
                "note": q.note,
                "is_in_sorting_bin": q.is_in_sorting_bin,
                "is_in_review_bin": q.is_in_review_bin,
                "source_chapter": q.bank.path if q.is_in_sorting_bin or q.is_in_review_bin else "",
                "options": [
                    {"id": o.id, "letter": chr(65 + i), "text": o.text, "is_correct": o.is_correct}
                    for i, o in enumerate(opts)
                ],
            })
        data.append(question)
    return data


def json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return None


# ---------------------------------------------------------------- browsing
@login_required
def home(request):
    subjects = Subject.objects.annotate(
        n_decks=Count("decks", distinct=True),
        n_q=Count(
            "decks__questions",
            filter=Q(decks__questions__is_in_sorting_bin=False, decks__questions__is_in_review_bin=False),
            distinct=True,
        ),
    ).order_by("order", "name")
    unfiled = QuestionBank.objects.filter(subject__isnull=True).annotate(
        n=Count("questions", filter=Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False))
    )
    return render(request, "bank/home.html", {"subjects": subjects, "unfiled": unfiled})


@login_required
def subject_detail(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    decks = subject.decks.annotate(
        n=Count("questions", filter=Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False))
    ).order_by("semester", "title")
    sections = subject.sections.annotate(
        n_decks=Count("decks", distinct=True),
        n_q=Count(
            "decks__questions",
            filter=Q(decks__questions__is_in_sorting_bin=False, decks__questions__is_in_review_bin=False),
            distinct=True,
        ),
    )
    return render(request, "bank/subject_detail.html", {
        "subject": subject, "decks": decks,
        "sections": sections, "unsectioned_decks": decks.filter(section__isnull=True),
    })


@login_required
def section_detail(request, slug, section_slug):
    subject = get_object_or_404(Subject, slug=slug)
    section = get_object_or_404(SubjectSection, subject=subject, slug=section_slug)
    decks = section.decks.annotate(
        n=Count("questions", filter=Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False))
    ).order_by("semester", "title")
    return render(request, "bank/section_detail.html", {
        "subject": subject, "section": section, "decks": decks,
    })


@login_required
def bank_detail(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    questions = bank.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False)
    return render(request, "bank/bank_detail.html", {
        "bank": bank,
        "count": questions.count(),
        "mcq_tf_count": questions.filter(kind__in=[Question.MCQ, Question.TF]).count(),
        "descriptive_count": questions.filter(kind=Question.DESC).count(),
        "image_count": questions.filter(kind=Question.IMAGE).count(),
        "image_identify_count": questions.filter(
            kind=Question.IMAGE, image_answer_mode=Question.IMAGE_IDENTIFY,
        ).count(),
        "image_mcq_count": questions.filter(
            kind=Question.IMAGE, image_answer_mode=Question.IMAGE_MCQ,
        ).count(),
    })


def _quiz_context(request, **kw):
    ctx = {"shuffle": "1" if request.GET.get("shuffle") else "0"}
    ctx.update(kw)
    return ctx


@login_required
@ensure_csrf_cookie
def quiz(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    quiz_type = request.GET.get("type", "mcq_tf")
    if quiz_type not in {"mcq_tf", "descriptive", "image"}:
        quiz_type = "mcq_tf"
    image_mode = request.GET.get("image_mode", "all")
    if image_mode not in {"all", Question.IMAGE_IDENTIFY, Question.IMAGE_MCQ}:
        image_mode = "all"
    query = f"type={quiz_type}"
    if quiz_type == "image":
        query += f"&image_mode={image_mode}"
    api = reverse("bank:api_questions", args=[bank.slug]) + "?" + query
    key = f"deck:{bank.slug}:{quiz_type}"
    if quiz_type == "image" and image_mode != "all":
        key += f":{image_mode}"
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=bank.title, key=key, question_type=quiz_type,
        api=api, back=bank.get_absolute_url(), review=False, edit_api="",
        start_question=request.GET.get("resume_question", ""),
        can_sort=request.user.is_staff,
        sort_api=reverse("bank:api_question_sorting_bin", args=[0]),
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@staff_required
@ensure_csrf_cookie
def review_deck(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    review_type = request.GET.get("type", "mcq_tf")
    if review_type not in {"mcq_tf", "descriptive", "image"}:
        review_type = "mcq_tf"
    image_mode = request.GET.get("image_mode", "all")
    if image_mode not in {"all", Question.IMAGE_IDENTIFY, Question.IMAGE_MCQ}:
        image_mode = "all"
    review_label = {
        "descriptive": "Descriptive",
        "image": "Images",
        "mcq_tf": "MCQs & True / False",
    }[review_type]
    query = f"review=1&type={review_type}"
    if review_type == "image":
        query += f"&image_mode={image_mode}"
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=f"Review {review_label} · {bank.title}",
        key=f"review:{bank.slug}:{review_type}:{image_mode}",
        question_type=review_type,
        api=reverse("bank:api_questions", args=[bank.slug]) + "?" + query,
        back=bank.get_absolute_url(), review=True,
        edit_api=reverse("bank:api_question_edit", args=[0]), can_sort=True, sort_api="",
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@login_required
@ensure_csrf_cookie
def saved_quiz(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=f"Saved · {bank.title}", key=f"saved:{request.user.pk}:{bank.slug}",
        api=reverse("bank:api_saved_questions", args=[bank.slug]),
        back=reverse("bank:saved_deck", args=[bank.slug]), review=False, edit_api="",
        can_sort=False, sort_api=""))


@login_required
def export_bank(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject", "section"), slug=slug)
    text = parser.to_text(bank.title, services.bank_as_dicts(bank),
                          subject=bank.subject.name if bank.subject else None, semester=bank.semester,
                          section=bank.section.name if bank.section else None)
    resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{bank.slug}.txt"'
    return resp


# ---------------------------------------------------------------- JSON API
@login_required
def api_questions(request, slug):
    """Questions WITHOUT answers; correctness is checked server-side per question."""
    bank = get_object_or_404(QuestionBank, slug=slug)
    review = request.GET.get("review") == "1"
    if review and not request.user.is_staff:
        return HttpResponseForbidden("Staff access is required.")
    qs = bank.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False)
    quiz_type = request.GET.get("type")
    if quiz_type == "descriptive":
        qs = qs.filter(kind=Question.DESC)
    elif quiz_type == "mcq_tf":
        qs = qs.filter(kind__in=[Question.MCQ, Question.TF])
    elif quiz_type == "image":
        qs = qs.filter(kind=Question.IMAGE)
        image_mode = request.GET.get("image_mode")
        if image_mode in {Question.IMAGE_IDENTIFY, Question.IMAGE_MCQ}:
            qs = qs.filter(image_answer_mode=image_mode)
    qs = qs.select_related("image_asset").prefetch_related("options", "image_targets")
    return JsonResponse({"title": bank.title, "questions": serialize(qs, request.user, review=review)})


@staff_required
def sorting_bin(request):
    questions = Question.objects.filter(is_in_sorting_bin=True).select_related(
        "bank__subject"
    ).order_by("bank__subject__name", "bank__semester", "bank__title", "id")
    groups = []
    for question in questions:
        path = question.bank.path
        if not groups or groups[-1]["path"] != path:
            groups.append({"path": path, "questions": []})
        groups[-1]["questions"].append(question)
    return render(request, "bank/sorting_bin.html", {
        "groups": groups,
        "count": len(questions),
        "prompt": _sorting_bin_prompt(),
    })


@staff_required
def review_bin(request):
    questions = Question.objects.filter(is_in_review_bin=True).select_related(
        "bank__subject"
    ).order_by("bank__subject__name", "bank__semester", "bank__title", "id")
    groups = []
    for question in questions:
        path = question.bank.path
        if not groups or groups[-1]["path"] != path:
            groups.append({"path": path, "questions": []})
        groups[-1]["questions"].append(question)
    return render(request, "bank/review_bin.html", {
        "groups": groups,
        "count": len(questions),
    })


@staff_required
def sorting_bin_export(request):
    questions = Question.objects.filter(is_in_sorting_bin=True).select_related(
        "bank__subject").prefetch_related("options")
    out = [
        "# Sorting-bin export. Preserve ID and SOURCE fields; fill in TARGET fields.",
        "# Use the companion AI prompt to classify and optionally correct these questions.",
        "# Image-dependent questions are not included; review them in the image question editor.",
        "",
    ]
    for question in questions.exclude(kind=Question.IMAGE):
        out += [
            f"Q: {question.stem}",
            f"ID: {question.pk}",
            f"SOURCE_SUBJECT: {question.bank.subject.name if question.bank.subject else ''}",
            f"SOURCE_SEMESTER: {question.bank.semester}",
            f"SOURCE_DECK: {question.bank.title}",
            "TARGET_SUBJECT:",
            "TARGET_SEMESTER:",
            "TARGET_DECK:",
        ]
        if question.number:
            out.append(f"NO: {question.number}")
        if question.kind == Question.DESC:
            out += ["TYPE: DESC", f"ANSWER: {question.answer}"]
        else:
            if question.kind == Question.TF:
                out.append("TYPE: TF")
            for index, option in enumerate(question.options.all()):
                out.append(f"{chr(65 + index)}. {option.text}")
            if question.kind == Question.TF:
                out.append("ANSWER: " + "".join("T" if option.is_correct else "F" for option in question.options.all()))
            else:
                out.append("ANSWER: " + ", ".join(
                    chr(65 + index) for index, option in enumerate(question.options.all()) if option.is_correct
                ))
        if question.explanation:
            out.append(f"EXPLANATION: {question.explanation}")
        if question.note:
            out.append(f"NOTE: {question.note}")
        out.append("")
    response = HttpResponse("\n".join(out), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="sorting-bin.txt"'
    return response


@staff_required
def review_bin_export(request):
    questions = Question.objects.filter(is_in_review_bin=True).select_related("bank__subject").prefetch_related("options")
    out = [
        "# Review-bin export. Preserve ID and source information for manual review or re-file.",
        "# Image-dependent questions are not included; review them in the image question editor.",
        "",
    ]
    for question in questions.exclude(kind=Question.IMAGE):
        out += [
            f"Q: {question.stem}",
            f"ID: {question.pk}",
            f"SOURCE_SUBJECT: {question.bank.subject.name if question.bank.subject else ''}",
            f"SOURCE_SEMESTER: {question.bank.semester}",
            f"SOURCE_DECK: {question.bank.title}",
        ]
        if question.number:
            out.append(f"NO: {question.number}")
        if question.kind == Question.DESC:
            out += ["TYPE: DESC", f"ANSWER: {question.answer}"]
        else:
            if question.kind == Question.TF:
                out.append("TYPE: TF")
            for index, option in enumerate(question.options.all()):
                out.append(f"{chr(65 + index)}. {option.text}")
            if question.kind == Question.TF:
                out.append("ANSWER: " + "".join("T" if option.is_correct else "F" for option in question.options.all()))
            else:
                out.append("ANSWER: " + ", ".join(
                    chr(65 + index) for index, option in enumerate(question.options.all()) if option.is_correct
                ))
        if question.explanation:
            out.append(f"EXPLANATION: {question.explanation}")
        if question.note:
            out.append(f"NOTE: {question.note}")
        out.append("")
    response = HttpResponse("\n".join(out), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="review-bin.txt"'
    return response


def _sorting_bin_prompt():
    destinations = QuestionBank.objects.select_related("subject").order_by(
        "subject__name", "semester", "title"
    )
    by_subject = {}
    for bank in destinations:
        subject_name = bank.subject.name if bank.subject else "Unfiled"
        by_subject.setdefault(subject_name, []).append(
            f"  - TARGET_SEMESTER: {bank.semester} | TARGET_DECK: {bank.title}"
        )
    destination_lines = []
    for subject_name, decks in by_subject.items():
        destination_lines.append(f"SOURCE_SUBJECT: {subject_name}")
        destination_lines.extend(decks)
    available = "\n".join(destination_lines) or "(none)"
    return (
        parser.SORTING_BIN_AI_PROMPT
        + "\nAvailable destination decks, grouped by SOURCE_SUBJECT. Each question may "
        "only use destinations under its own source subject:\n"
        + available
        + "\nChoose only a destination listed under that question's SOURCE_SUBJECT. "
        "Do not transfer questions between subjects or invent subject, semester, or deck names.\n"
    )


def _sorting_bin_import_rows(text):
    result = parser.parse(text)
    rows = []
    seen_ids = set()
    for parsed in result.questions:
        errors = list(parsed.errors)
        try:
            question_id = int(parsed.question_id)
        except (TypeError, ValueError):
            question_id = None
            errors.append("Missing or invalid stable ID.")
        if question_id in seen_ids:
            errors.append("Duplicate ID in this import.")
        if question_id is not None:
            seen_ids.add(question_id)
        question = Question.objects.filter(pk=question_id, is_in_sorting_bin=True).select_related(
            "bank__subject"
        ).first() if question_id is not None else None
        if question is None:
            errors.append("ID does not match a question currently in the sorting bin.")
        subject = Subject.objects.filter(name__iexact=parsed.target_subject.strip()).first()
        if not subject:
            errors.append("Choose an existing target subject.")
        elif question and question.bank.subject_id and subject.pk != question.bank.subject_id:
            errors.append(
                f"Question belongs to {question.bank.subject.name}; it cannot be moved to another subject."
            )
        try:
            semester = int(parsed.target_semester)
            if not 1 <= semester <= 12:
                raise ValueError
        except (TypeError, ValueError):
            semester = None
            errors.append("Target semester must be a number from 1 to 12.")
        deck_title = parsed.target_deck.strip()
        if not deck_title:
            errors.append("Target chapter/deck is required.")
        deck = services.find_deck(subject, semester, deck_title) if subject and semester and deck_title else None
        row = {
            "parsed": parsed, "question": question, "subject": subject, "semester": semester,
            "deck_title": deck_title, "deck": deck, "errors": errors,
            "source_path": question.bank.path if question else "Unknown source",
            "target_path": (
                f"{subject.name if subject else parsed.target_subject or 'Unknown subject'}"
                f" › Semester {semester if semester else parsed.target_semester or '?'}"
                f" › {deck_title or 'Unspecified chapter'}"
            ),
            "new_chapter": bool(subject and semester and deck_title and not deck),
            "answer": parsed.answer_raw if parsed.kind == Question.DESC else (
                "".join("T" if correct else "F" for correct in parsed.correct)
                if parsed.kind == Question.TF else ", ".join(
                    chr(65 + index) for index, correct in enumerate(parsed.correct) if correct
                )
            ),
        }
        rows.append(row)

    groups_by_target = {}
    for row in rows:
        target_path = row["target_path"]
        groups_by_target.setdefault(target_path, []).append(row)
    groups = [{"path": path, "rows": grouped_rows}
              for path, grouped_rows in groups_by_target.items()]
    return rows, groups


@staff_required
def sorting_bin_import(request):
    pending_key = "sorting_bin_pending_import"
    if request.method == "GET":
        text = request.session.get(pending_key)
        if text is not None and request.GET.get("edit") != "1":
            rows, groups = _sorting_bin_import_rows(text)
            return render(request, "bank/sorting_bin_import_preview.html", {
                "groups": groups,
                "row_count": len(rows),
                "valid_count": sum(not row["errors"] for row in rows),
                "error_count": sum(bool(row["errors"]) for row in rows),
            })
        return render(request, "bank/sorting_bin_import.html", {
            "text": text or "", "prompt": _sorting_bin_prompt(),
            "subjects": Subject.objects.order_by("name"),
        })

    action = request.POST.get("action")
    if action == "edit":
        return redirect(f"{reverse('bank:sorting_bin_import')}?edit=1")
    if action == "cancel":
        request.session.pop(pending_key, None)
        return redirect("bank:sorting_bin_import")

    if action == "preview":
        text = request.POST.get("text", "")
        upload = request.FILES.get("file")
        if upload:
            text = upload.read().decode("utf-8-sig", errors="replace")
        request.session[pending_key] = text
        request.session.modified = True
        return redirect("bank:sorting_bin_import")

    if action != "commit":
        messages.error(request, "Start by reviewing the reorganized text before applying it.")
        return redirect("bank:sorting_bin_import")

    text = request.session.get(pending_key)
    if text is None:
        messages.error(request, "There is no pending import to apply. Preview the reorganized text first.")
        return redirect("bank:sorting_bin_import")

    rows, groups = _sorting_bin_import_rows(text)
    if not rows:
        messages.error(request, "No questions were found to import.")
    elif any(row["errors"] for row in rows):
        messages.error(request, "Fix all preview errors before applying any changes.")
    else:
        with transaction.atomic():
            for row in rows:
                parsed = row["parsed"]
                question = row["question"]
                bank = row["deck"] or services.get_or_create_deck(
                    row["subject"], row["semester"], row["deck_title"])
                question.bank = bank
                question.stem = parsed.stem
                question.kind = parsed.kind
                question.answer = parsed.answer_raw if parsed.kind == Question.DESC else ""
                question.explanation = parsed.explanation
                question.note = parsed.note
                question.is_in_sorting_bin = False
                question.sorting_bin_at = None
                question.save()
                question.options.all().delete()
                Option.objects.bulk_create([
                    Option(question=question, text=option, is_correct=correct, order=index)
                    for index, (option, correct) in enumerate(zip(parsed.options, parsed.correct))
                ])
        request.session.pop(pending_key, None)
        messages.success(request, f"Updated and filed {len(rows)} sorting-bin question(s).")
        return redirect("bank:sorting_bin")

    return render(request, "bank/sorting_bin_import_preview.html", {
        "groups": groups,
        "row_count": len(rows),
        "valid_count": sum(not row["errors"] for row in rows),
        "error_count": sum(bool(row["errors"]) for row in rows),
    })


@staff_required
@ensure_csrf_cookie
def sorting_bin_review(request):
    return render(request, "bank/quiz.html", _quiz_context(
        request, title="Sorting bin review", key="sorting-bin-review",
        api=reverse("bank:api_sorting_bin_questions"),
        back=reverse("bank:sorting_bin"), review=True,
        start_question=request.GET.get("question", ""),
        edit_api=reverse("bank:api_question_edit", args=[0]), can_sort=False, sort_api="",
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@staff_required
@ensure_csrf_cookie
def review_bin_review(request):
    return render(request, "bank/quiz.html", _quiz_context(
        request, title="Review bin", key="review-bin-review",
        api=reverse("bank:api_review_bin_questions"),
        back=reverse("bank:review_bin"), review=True,
        start_question=request.GET.get("question", ""),
        edit_api=reverse("bank:api_question_edit", args=[0]), can_sort=False, sort_api="",
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@staff_required
@require_POST
def api_question_sorting_bin(request, pk):
    question = get_object_or_404(Question, pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
    question.is_in_sorting_bin = True
    question.is_in_review_bin = False
    question.sorting_bin_at = timezone.now()
    question.review_bin_at = None
    question.save(update_fields=["is_in_sorting_bin", "is_in_review_bin", "sorting_bin_at", "review_bin_at", "updated_at"])
    return JsonResponse({"moved": True, "id": question.pk, "bin": "sorting"})


@staff_required
@require_POST
def api_question_review_bin(request, pk):
    question = get_object_or_404(Question, pk=pk, is_in_review_bin=False, is_in_sorting_bin=False)
    question.is_in_review_bin = True
    question.is_in_sorting_bin = False
    question.review_bin_at = timezone.now()
    question.sorting_bin_at = None
    question.save(update_fields=["is_in_review_bin", "is_in_sorting_bin", "review_bin_at", "sorting_bin_at", "updated_at"])
    return JsonResponse({"moved": True, "id": question.pk, "bin": "review"})


@staff_required
def api_sorting_bin_questions(request):
    questions = Question.objects.filter(is_in_sorting_bin=True).select_related(
        "bank__subject", "image_asset").prefetch_related("options", "image_targets")
    return JsonResponse({"title": "Sorting bin", "questions": serialize(questions, request.user, review=True)})


@staff_required
def api_review_bin_questions(request):
    questions = Question.objects.filter(is_in_review_bin=True).select_related(
        "bank__subject", "image_asset").prefetch_related("options", "image_targets")
    return JsonResponse({"title": "Review bin", "questions": serialize(questions, request.user, review=True)})


@staff_required
@require_POST
def api_question_edit(request, pk):
    question = get_object_or_404(
        Question.objects.select_related("image_asset").prefetch_related("options", "image_targets"),
        pk=pk,
    )
    data = json_body(request)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    delete_question = data.get("delete_question", False)
    if not isinstance(delete_question, bool):
        return JsonResponse({"error": "Invalid delete action."}, status=400)
    if delete_question:
        image_id = question.image_asset_id
        question.delete()
        cleanup_warning = _delete_unused_question_image(image_id)
        return JsonResponse({"deleted": True, "warning": cleanup_warning})

    kind = data.get("kind")
    stem = data.get("stem")
    answer = data.get("answer", "")
    explanation = data.get("explanation", "")
    note = data.get("note", "")
    options = data.get("options", [])
    if kind not in {Question.MCQ, Question.TF, Question.DESC, Question.IMAGE}:
        return JsonResponse({"error": "Choose a valid question type."}, status=400)
    targets = data.get("targets", [])
    image_answer_mode = data.get("image_answer_mode", question.image_answer_mode)
    if question.kind == Question.IMAGE and kind != Question.IMAGE:
        return JsonResponse({"error": "Edit image questions using the image question editor."}, status=400)
    if kind == Question.IMAGE:
        if not question.image_asset_id:
            return JsonResponse({"error": "This image question has no associated image."}, status=400)
        if image_answer_mode not in {Question.IMAGE_IDENTIFY, Question.IMAGE_MCQ}:
            return JsonResponse({"error": "Choose an image answer style."}, status=400)
        if image_answer_mode == Question.IMAGE_IDENTIFY:
            if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
                return JsonResponse({"error": "Add between 1 and 50 identification targets."}, status=400)
            labels = set()
            for target in targets:
                if not isinstance(target, dict) or not isinstance(target.get("label"), str) or not target["label"].strip():
                    return JsonResponse({"error": "Every image target needs a label."}, status=400)
                if not isinstance(target.get("answer"), str) or not target["answer"].strip():
                    return JsonResponse({"error": "Every image target needs an answer."}, status=400)
                label = target["label"].strip().casefold()
                if label in labels:
                    return JsonResponse({"error": "Image target labels must be unique."}, status=400)
                labels.add(label)
                x, y = target.get("x_percent"), target.get("y_percent")
                if (x in (None, "")) != (y in (None, "")):
                    return JsonResponse({"error": "Each marker needs both coordinates or neither."}, status=400)
                if x not in (None, ""):
                    try:
                        coordinates = float(x), float(y)
                    except (TypeError, ValueError):
                        return JsonResponse({"error": "Image marker coordinates must be numbers."}, status=400)
                    if not all(0 <= coordinate <= 100 for coordinate in coordinates):
                        return JsonResponse({"error": "Image marker coordinates must be within the image."}, status=400)
            options = []
            targets = [
                {
                    "label": target["label"].strip(),
                    "answer": target["answer"].strip(),
                    "x_percent": target.get("x_percent") or None,
                    "y_percent": target.get("y_percent") or None,
                }
                for target in targets
            ]
        else:
            if not isinstance(options, list) or not 2 <= len(options) <= 8:
                return JsonResponse({"error": "Add between 2 and 8 image MCQ options."}, status=400)
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("text"), str)
                or not option["text"].strip()
                or not isinstance(option.get("is_correct"), bool)
                for option in options
            ):
                return JsonResponse({"error": "Every image MCQ option needs text and a correct/incorrect selection."}, status=400)
            if not any(option["is_correct"] for option in options):
                return JsonResponse({"error": "Select at least one correct image MCQ option."}, status=400)
            targets = []
    if not isinstance(stem, str) or not stem.strip():
        return JsonResponse({"error": "Question text is required."}, status=400)
    if not all(isinstance(value, str) for value in (answer, explanation, note)):
        return JsonResponse({"error": "Answer, explanation, and note must be text."}, status=400)
    if kind == Question.DESC:
        if not answer.strip():
            return JsonResponse({"error": "A descriptive question needs an answer."}, status=400)
        options = []
    elif kind == Question.IMAGE:
        pass
    else:
        minimum = 1 if kind == Question.TF else 2
        if not isinstance(options, list) or not minimum <= len(options) <= 8:
            return JsonResponse({"error": f"Add between {minimum} and 8 options."}, status=400)
        if any(not isinstance(option, dict) or not isinstance(option.get("text"), str)
               or not option["text"].strip() or not isinstance(option.get("is_correct"), bool)
               for option in options):
            return JsonResponse({"error": "Every option needs text and a correct/incorrect selection."}, status=400)
        if kind == Question.MCQ and not any(option["is_correct"] for option in options):
            return JsonResponse({"error": "Select at least one correct option."}, status=400)

    with transaction.atomic():
        question.kind = kind
        question.stem = stem.strip()
        question.answer = answer.strip() if kind == Question.DESC else ""
        if kind == Question.IMAGE:
            question.image_answer_mode = image_answer_mode
        question.explanation = explanation.strip()
        question.note = note.strip()
        move_to_bin = data.get("is_in_sorting_bin", question.is_in_sorting_bin)
        move_to_review_bin = data.get("is_in_review_bin", question.is_in_review_bin)
        if not isinstance(move_to_bin, bool):
            return JsonResponse({"error": "Invalid sorting-bin state."}, status=400)
        if not isinstance(move_to_review_bin, bool):
            return JsonResponse({"error": "Invalid review-bin state."}, status=400)
        question.is_in_sorting_bin = move_to_bin
        question.sorting_bin_at = timezone.now() if move_to_bin else None
        question.is_in_review_bin = move_to_review_bin
        question.review_bin_at = timezone.now() if move_to_review_bin else None
        if move_to_bin:
            question.is_in_review_bin = False
            question.review_bin_at = None
        if move_to_review_bin:
            question.is_in_sorting_bin = False
            question.sorting_bin_at = None
        question.save()

        existing_options = list(question.options.all())
        for index, option_data in enumerate(options):
            if index < len(existing_options):
                option = existing_options[index]
                option.text = option_data["text"].strip()
                option.is_correct = option_data["is_correct"]
                option.order = index
                option.save()
            else:
                Option.objects.create(
                    question=question, text=option_data["text"].strip(),
                    is_correct=option_data["is_correct"], order=index,
                )
        for option in existing_options[len(options):]:
            option.delete()
        if kind == Question.IMAGE:
            question.image_targets.all().delete()
            if image_answer_mode == Question.IMAGE_IDENTIFY:
                ImageTarget.objects.bulk_create([
                    ImageTarget(question=question, order=index, **target)
                    for index, target in enumerate(targets)
                ])

    updated = Question.objects.select_related("image_asset").prefetch_related(
        "options", "image_targets",
    ).get(pk=question.pk)
    return JsonResponse({"question": serialize([updated], request.user, review=True)[0]})


@login_required
@require_POST
def api_image_target(request, pk, target_pk):
    question = get_object_or_404(
        Question, pk=pk, kind=Question.IMAGE,
        image_answer_mode=Question.IMAGE_IDENTIFY,
        is_in_sorting_bin=False, is_in_review_bin=False,
    )
    target = get_object_or_404(ImageTarget, pk=target_pk, question=question)
    data = json_body(request)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    action = data.get("action")
    if action == "reveal":
        return JsonResponse({"answer": target.answer, "target_id": target.pk})
    if action == "grade" and data.get("grade") in {"known", "missed"}:
        return JsonResponse({"saved": True, "target_id": target.pk})
    return JsonResponse({"error": "Choose reveal or a valid self-assessment."}, status=400)


@login_required
def api_saved_questions(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    qs = (bank.questions.filter(saved_by__user=request.user, is_in_sorting_bin=False, is_in_review_bin=False)
          .prefetch_related("options").order_by("id"))
    return JsonResponse({"title": bank.title, "questions": serialize(qs, request.user)})


@login_required
@require_POST
def api_check(request, pk):
    q = get_object_or_404(
        Question.objects.prefetch_related("options"), pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
    data = json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    if q.kind == Question.IMAGE and q.image_answer_mode == Question.IMAGE_IDENTIFY:
        return JsonResponse({"error": "Identify targets individually to record self-assessment."}, status=400)
    opts = list(q.options.all())
    out = {"explanation": q.explanation, "note": q.note, "correct": None}
    reveal = bool(data.get("reveal"))
    if q.kind == Question.DESC:
        if not reveal:
            return JsonResponse({"error": "Descriptive answers can only be requested with reveal."}, status=400)
        out["answer"] = q.answer
    elif q.kind == Question.TF:
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
    q = get_object_or_404(Question, pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
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
        saved_n=Count(
            "questions__saved_by",
            filter=Q(questions__saved_by__user=user, questions__is_in_sorting_bin=False),
            distinct=True,
        )
    ).filter(saved_n__gt=0)


@login_required
def saved_home(request):
    subjects = Subject.objects.annotate(
        n_saved=Count("decks__questions__saved_by",
                      filter=Q(decks__questions__saved_by__user=request.user,
                               decks__questions__is_in_sorting_bin=False), distinct=True))
    return render(request, "bank/saved_home.html", {"subjects": subjects})


@login_required
def saved_subject(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    decks = _saved_decks(request.user).filter(subject=subject).order_by("semester", "title")
    return render(request, "bank/saved_subject.html", {"subject": subject, "decks": decks})


@login_required
def saved_deck(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    qs = (bank.questions.filter(saved_by__user=request.user, is_in_sorting_bin=False)
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
    qs = bank.questions.filter(is_in_sorting_bin=False).annotate(
        open_comments=Count("comments", filter=Q(comments__resolved=False), distinct=True))
    return render(request, "bank/manage_deck.html", {"bank": bank, "questions": qs})


def _edit_question(request, question, bank=None, is_new=False):
    Formset = option_formset(extra=4 if is_new else 2)
    form = QuestionForm(request.POST or None, instance=question)
    formset = Formset(request.POST or None, instance=question)
    if request.method == "POST":
        v1 = form.is_valid()
        if v1:
            formset.instance.kind = form.cleaned_data["kind"]
        v2 = formset.is_valid()
        if v1 and v2:
            with transaction.atomic():
                q = form.save()
                formset.instance = q
                if q.kind == Question.DESC:
                    q.options.all().delete()
                else:
                    formset.save()
                    ordered = []
                    for form in formset.forms:
                        if not form.cleaned_data:
                            continue
                        if form.cleaned_data.get("DELETE"):
                            continue
                        instance = form.instance
                        if instance.pk:
                            ordered.append(instance)
                    for index, option in enumerate(ordered):
                        if option.order != index:
                            option.order = index
                            option.save(update_fields=["order"])
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
    if q.kind == Question.IMAGE:
        return _image_question_form(request, q.bank, q.image_asset, question=q)
    return _edit_question(request, q)


@perm_required("bank.add_question")
def question_add(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    return _edit_question(request, Question(bank=bank), is_new=True)


def _image_question_form(request, bank, image, question=None):
    if image is None or image.bank_id != bank.pk:
        return HttpResponseForbidden("This image is not available in the selected chapter.")
    if question is not None and question.image_asset_id != image.pk:
        return HttpResponseForbidden("This question does not use the selected image.")
    initial = {}
    if question is not None:
        initial = {
            "answer_mode": question.image_answer_mode,
            "stem": question.stem,
            "explanation": question.explanation,
            "note": question.note,
            "targets_json": json.dumps([
                {"label": target.label, "answer": target.answer,
                 "x_percent": float(target.x_percent) if target.x_percent is not None else None,
                 "y_percent": float(target.y_percent) if target.y_percent is not None else None}
                for target in question.image_targets.all()
            ]),
            "options_json": json.dumps([
                {"text": option.text, "is_correct": option.is_correct}
                for option in question.options.all()
            ]),
        }
    form = ImageQuestionForm(request.POST or None, initial=initial or {
        "answer_mode": Question.IMAGE_IDENTIFY,
        "targets_json": "[]",
        "options_json": "[]",
    })
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if question is None:
                question = Question.objects.create(
                    bank=bank,
                    kind=Question.IMAGE,
                    number=str(bank.questions.filter(kind=Question.IMAGE).count() + 1),
                    image_asset=image,
                )
            question.kind = Question.IMAGE
            question.stem = form.cleaned_data["stem"]
            question.image_answer_mode = form.cleaned_data["answer_mode"]
            question.answer = ""
            question.explanation = form.cleaned_data["explanation"].strip()
            question.note = form.cleaned_data["note"].strip()
            question.save()
            question.options.all().delete()
            question.image_targets.all().delete()
            if question.image_answer_mode == Question.IMAGE_MCQ:
                Option.objects.bulk_create([
                    Option(
                        question=question, text=option["text"].strip(),
                        is_correct=option["is_correct"], order=index,
                    )
                    for index, option in enumerate(form.cleaned_data["options"])
                ])
            else:
                ImageTarget.objects.bulk_create([
                    ImageTarget(
                        question=question, label=target["label"].strip(),
                        answer=target["answer"].strip(),
                        x_percent=target.get("x_percent") or None,
                        y_percent=target.get("y_percent") or None,
                        order=index,
                    )
                    for index, target in enumerate(form.cleaned_data["targets"])
                ])
        if request.POST.get("add_another") == "1":
            messages.success(request, "Question saved. Add another separate question using the same image.")
            return redirect("bank:image_question_add", slug=bank.slug, image_id=image.pk)
        messages.success(request, "Image question saved.")
        return redirect(safe_next(request, bank.get_absolute_url()))
    return render(request, "bank/image_question_form.html", {
        "bank": bank, "image": image, "question": question,
        "form": form, "is_new": question is None,
        "initial_targets": form["targets_json"].value() or "[]",
        "initial_options": form["options_json"].value() or "[]",
        "next_url": request.POST.get("next") or request.GET.get("next", ""),
        "cancel_url": safe_next(request, bank.get_absolute_url()),
    })


@perm_required("bank.add_question")
def image_question_upload(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    form = ImageUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        uploaded = form.cleaned_data["image"]
        try:
            result = upload_question_image(uploaded)
        except (ImageStorageError, ValueError, OSError) as error:
            form.add_error("image", str(error))
        else:
            image = QuestionImage.objects.create(
                bank=bank,
                public_id=result["public_id"],
                secure_url=result["secure_url"],
                original_name=uploaded.name[:255],
                width=result["width"],
                height=result["height"],
            )
            return redirect("bank:image_question_add", slug=bank.slug, image_id=image.pk)
    return render(request, "bank/image_upload.html", {
        "bank": bank,
        "form": form,
        "image_uploads_enabled": image_uploads_enabled(),
        "local_image_uploads": settings.DEBUG,
    })


@perm_required("bank.add_question")
def image_question_add(request, slug, image_id):
    bank = get_object_or_404(QuestionBank, slug=slug)
    image = get_object_or_404(QuestionImage, pk=image_id, bank=bank)
    return _image_question_form(request, bank, image)


def _delete_unused_question_image(image_id):
    if image_id is None:
        return ""
    image = QuestionImage.objects.filter(pk=image_id).first()
    if image is None or image.questions.exists():
        return ""
    try:
        delete_question_image(image.public_id)
    except (ImageStorageError, ValueError, OSError) as error:
        return f"The question was deleted, but its unused image could not be removed from storage: {error}"
    image.delete()
    return ""


@perm_required("bank.delete_question")
@require_POST
def question_delete(request, pk):
    q = get_object_or_404(Question, pk=pk)
    slug = q.bank.slug
    image_id = q.image_asset_id
    q.delete()
    cleanup_warning = _delete_unused_question_image(image_id)
    if cleanup_warning:
        messages.warning(request, cleanup_warning)
    else:
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
        "section_id": request.GET.get("section", ""),
        "sections": SubjectSection.objects.select_related("subject").all(),
        "deck_title": request.GET.get("deck_title", ""), "skip_dupes": True,
        "decks_json": [{"s": d.subject_id, "m": d.semester, "c": d.section_id, "t": d.title}
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
    section_error = ""
    section = None
    section_value = request.POST.get("section", "").strip()
    if section_value:
        if subject:
            section = SubjectSection.objects.filter(pk=section_value, subject=subject).first()
        if not section:
            section_error = "Choose a sub-subject belonging to the selected subject."
    elif result.section and subject:
        section = SubjectSection.objects.filter(subject=subject, name__iexact=result.section).first()
        if not section:
            section_error = f"No sub-subject named “{result.section}” exists under {subject.name}."
    try:
        semester = int(request.POST.get("semester") or result.semester or 0) or None
    except ValueError:
        semester = None
    title = request.POST.get("deck_title", "").strip() or result.bank_title

    deck = services.find_deck(subject, semester, title) if (subject and semester and title) else None
    if deck and section and deck.section_id not in (None, section.pk):
        section_error = "This deck already belongs to a different sub-subject."
    if deck:
        parser.mark_duplicates(result.questions, services.existing_keys(deck))

    if request.POST.get("action") == "commit":
        missing = [n for n, v in (("subject", subject), ("semester", semester), ("deck title", title)) if not v]
        if section_error:
            messages.error(request, section_error)
        elif missing:
            messages.error(request, "Please choose the " + ", ".join(missing) + ".")
        else:
            deck = services.get_or_create_deck(subject, semester, title, section)
            created, bad, dup = services.save_questions(deck, result.questions, skip_dupes)
            messages.success(
                request, f"Imported {created} question(s) into “{deck.path}”. "
                         f"Skipped {bad} with errors and {dup} duplicate(s).")
            return redirect(deck)

    rows = []
    for q in result.questions:
        if q.kind == parser.DESC:
            ans = q.answer_raw
        elif q.kind == parser.TF:
            ans = "".join("T" if c else "F" for c in q.correct)
        else:
            ans = ", ".join(chr(65 + i) for i, c in enumerate(q.correct) if c)
        rows.append({"q": q, "answer": ans, "skipped": bool(q.duplicate_of) and skip_dupes})
    ctx.update({
        "text": text, "subject_id": str(subject.pk) if subject else "",
        "section_id": str(section.pk) if section else section_value,
        "selected_section": section,
        "section_error": section_error,
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

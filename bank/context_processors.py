from .models import Comment, Question

SITE_NAME = "Rawasi 101"


def site(request):
    ctx = {"SITE_NAME": SITE_NAME}
    u = getattr(request, "user", None)
    if u is not None and u.is_authenticated and u.has_perm("bank.change_question"):
        ctx["open_comments"] = Comment.objects.filter(resolved=False).count()
    if u is not None and u.is_authenticated and u.is_staff:
        ctx["sorting_bin_count"] = Question.objects.filter(is_in_sorting_bin=True).count()
        ctx["review_bin_count"] = Question.objects.filter(is_in_review_bin=True).count()
    return ctx

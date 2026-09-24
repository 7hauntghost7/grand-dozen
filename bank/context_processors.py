from .models import Comment

SITE_NAME = "The Grand Dozen"


def site(request):
    ctx = {"SITE_NAME": SITE_NAME}
    u = getattr(request, "user", None)
    if u is not None and u.is_authenticated and u.has_perm("bank.change_question"):
        ctx["open_comments"] = Comment.objects.filter(resolved=False).count()
    return ctx

"""First sign-in: accounts that were given a password by an admin (Control → Add accounts, or 🔑 New password)
pick their own before using the site."""
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse

from .models import Profile

ALLOWED = ("/welcome/", "/accounts/logout/", "/static/", "/media/", "/manifest.webmanifest", "/service-worker.js",
           "/push/tick/", "/api/activity/")


def needs_new_password(user):
    return bool(user.is_authenticated and Profile.objects.filter(user=user, must_change_password=True).exists())


class FirstPasswordMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and not request.path.startswith(ALLOWED)
                and needs_new_password(user)):
            if request.method != "GET" or request.headers.get("Accept", "").startswith("application/json"):
                from django.http import JsonResponse
                return JsonResponse({"error": "Choose your own password first."}, status=403)
            return redirect(f"{reverse('bank:welcome')}?next={request.get_full_path()}")
        return self.get_response(request)


@login_required
def welcome(request):
    user = request.user
    profile = user.profile
    nxt = request.GET.get("next") or request.POST.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/welcome/"):
        nxt = "/"
    if not profile.must_change_password and request.method == "GET":
        return redirect(nxt)
    errors, done = [], False
    if request.method == "POST":
        first, second = request.POST.get("password1", ""), request.POST.get("password2", "")
        if not first:
            errors.append("Type a new password.")
        elif first != second:
            errors.append("The two passwords don’t match.")
        elif user.check_password(first):
            errors.append("Pick a new one, not the password you were given.")
        else:
            try:
                validate_password(first, user)
            except ValidationError as e:
                errors.extend(e.messages)
        if not errors:
            user.set_password(first)
            user.save(update_fields=["password"])
            update_session_auth_hash(request, user)         # stay signed in on this phone
            profile.must_change_password = False
            profile.save(update_fields=["must_change_password"])
            done = True
    return render(request, "bank/welcome.html", {"profile": profile, "errors": errors, "done": done, "next": nxt})

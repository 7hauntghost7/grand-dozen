from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache


class SingleSessionMiddleware:
    """Require login for the site and allow only one active session per user."""

    CACHE_PREFIX = "bank:active-session:"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        login_path = settings.LOGIN_URL
        static_prefix = "/" + settings.STATIC_URL.lstrip("/")

        # The login page and static assets must remain reachable anonymously.
        if not request.user.is_authenticated and path != login_path and not path.startswith(static_prefix):
            return redirect_to_login(request.get_full_path(), login_path)

        if request.user.is_authenticated:
            active_key = cache.get(self.CACHE_PREFIX + str(request.user.pk))
            current_key = request.session.session_key

            if active_key and current_key and active_key != current_key:
                logout(request)
                return redirect_to_login(request.get_full_path(), login_path)

            # If the cache was restarted/cleared, adopt the current session.
            if current_key and not active_key:
                cache.set(self.CACHE_PREFIX + str(request.user.pk), current_key, None)

        return self.get_response(request)

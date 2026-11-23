"""Background jobs run in their own threads; each must close its database connection when it ends,
otherwise every notification leaves one idle connection behind until the database runs out."""
import threading
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import TransactionTestCase

from . import friends, groups, push


class BackgroundConnectionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("amna", password="x")

    def _closed_after(self, start_job):
        """Start a background job, wait for it, and say whether its thread closed its connection."""
        real_thread, real_close = threading.Thread, type(connections["default"]).close
        threads, closed_in = [], set()

        def thread(target, daemon=True):
            t = real_thread(target=target, daemon=daemon)
            threads.append(t)
            return t

        def close(conn):
            # Tests use in-memory SQLite, which Django never really closes, so record the call instead
            closed_in.add(threading.get_ident())
            return real_close(conn)

        # The live site keeps connections for 60 s; with the test default of 0 every cleanup closes them anyway
        with patch.dict(connections.settings["default"], {"CONN_MAX_AGE": 60}), \
                patch.object(threading, "Thread", side_effect=thread), \
                patch.object(type(connections["default"]), "close", close):
            start_job()
            self.assertEqual(len(threads), 1)
            threads[0].join(5)
        return threads[0].ident in closed_in

    def test_push_tick_closes_its_connection(self):
        push.cache.delete("push-tick")
        with patch.object(push, "configured", return_value=True), \
                patch.object(push, "send_due", side_effect=lambda: User.objects.count()):
            self.assertTrue(self._closed_after(push.maybe_tick))

    def test_friend_notification_closes_its_connection(self):
        with patch.object(friends.push, "configured", return_value=True), patch.object(friends.push, "send_to_user"):
            self.assertTrue(self._closed_after(lambda: friends.notify(self.user, "hi", "/")))

    def test_group_notification_closes_its_connection(self):
        with patch.object(groups.push, "configured", return_value=True), patch.object(groups.push, "send_to_user"):
            self.assertTrue(self._closed_after(lambda: groups.notify([self.user.pk], "hi", "/")))

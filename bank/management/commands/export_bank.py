from django.core.management.base import BaseCommand, CommandError

from bank import parser, services
from bank.models import QuestionBank


class Command(BaseCommand):
    help = "Print a deck in the plain-text import format (redirect to a file to back it up)."

    def add_arguments(self, ap):
        ap.add_argument("slug")

    def handle(self, *args, **o):
        try:
            bank = QuestionBank.objects.select_related("subject").get(slug=o["slug"])
        except QuestionBank.DoesNotExist:
            raise CommandError("No such deck slug.")
        self.stdout.write(parser.to_text(
            bank.title, services.bank_as_dicts(bank),
            subject=bank.subject.name if bank.subject else None, semester=bank.semester))

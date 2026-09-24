from django.core.management.base import BaseCommand

from bank.models import Subject

DEFAULTS = ["Physiology", "Anatomy", "Biochemistry", "Pathology"]


class Command(BaseCommand):
    help = "Create the subject blocks (default: 4 placeholder names — rename them in /admin/)."

    def add_arguments(self, ap):
        ap.add_argument("names", nargs="*", help="Subject names, in display order")

    def handle(self, *args, **o):
        for i, name in enumerate(o["names"] or DEFAULTS):
            s, created = Subject.objects.get_or_create(name=name, defaults={"order": i})
            self.stdout.write(f"{'created' if created else 'exists '} {s.name}")

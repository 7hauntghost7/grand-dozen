from django.db import migrations

ORDER = {"Gross Anatomy": 0, "Embryology": 1, "Histology": 2}


def gross_first(apps, schema_editor):
    """Anatomy's sections open with Gross Anatomy, then Embryology and Histology."""
    Section = apps.get_model("bank", "SubjectSection")
    for name, order in ORDER.items():
        Section.objects.filter(subject__name="Anatomy", name=name).update(order=order)


class Migration(migrations.Migration):
    dependencies = [("bank", "0033_fill_review_guides")]
    operations = [migrations.RunPython(gross_first, migrations.RunPython.noop)]

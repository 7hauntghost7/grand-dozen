from django.db import migrations
from django.db.models import Q


def categorize_image_mcqs(apps, schema_editor):
    Question = apps.get_model("bank", "Question")
    Question.objects.filter(
        Q(kind="image") & Q(image_answer_mode="mcq")
    ).update(kind="mcq", image_answer_mode="identify")


class Migration(migrations.Migration):

    dependencies = [
        ("bank", "0007_question_label"),
    ]

    operations = [
        migrations.RunPython(categorize_image_mcqs, migrations.RunPython.noop),
    ]

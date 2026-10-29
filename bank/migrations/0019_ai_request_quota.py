from django.db import migrations, models


def create_exam_marking_quota(apps, schema_editor):
    quota = apps.get_model("bank", "AIRequestQuota")
    quota.objects.using(schema_editor.connection.alias).get_or_create(
        name="gemini-exam-marking",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("bank", "0018_examtemplate_snapshot_examtemplate_source_attempt"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIRequestQuota",
            fields=[
                ("name", models.CharField(max_length=40, primary_key=True, serialize=False)),
                ("request_timestamps", models.JSONField(default=list)),
            ],
        ),
        migrations.RunPython(create_exam_marking_quota, migrations.RunPython.noop),
    ]

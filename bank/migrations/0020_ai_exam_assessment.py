from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("bank", "0019_ai_request_quota"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIExamAssessment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(
                    choices=[
                        ("queued", "Queued"),
                        ("processing", "Processing"),
                        ("completed", "Completed"),
                        ("failed", "Failed"),
                        ("cancelled", "Cancelled"),
                    ],
                    default="queued",
                    max_length=12,
                )),
                ("error_message", models.CharField(blank=True, max_length=240)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("attempt", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="ai_assessment",
                    to="bank.examattempt",
                )),
            ],
            options={"ordering": ["created_at", "pk"]},
        ),
    ]

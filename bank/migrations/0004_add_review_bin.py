from django.db import migrations, models


def add_review_bin_fields_if_missing(apps, schema_editor):
    question = apps.get_model('bank', 'Question')
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, question._meta.db_table
            )
        }

    fields = (
        ('is_in_review_bin', models.BooleanField(default=False)),
        ('review_bin_at', models.DateTimeField(blank=True, null=True)),
    )
    for name, field in fields:
        if name in columns:
            continue
        field.set_attributes_from_name(name)
        field.model = question
        schema_editor.add_field(question, field)


class Migration(migrations.Migration):

    dependencies = [
        ('bank', '0003_add_sorting_bin'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_review_bin_fields_if_missing,
                    reverse_code=migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='question',
                    name='is_in_review_bin',
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name='question',
                    name='review_bin_at',
                    field=models.DateTimeField(blank=True, null=True),
                ),
            ],
        ),
    ]

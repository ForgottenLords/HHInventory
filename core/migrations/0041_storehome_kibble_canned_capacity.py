from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0040_remove_special_diet_weight_digestive_joint"),
    ]

    operations = [
        migrations.AddField(
            model_name="storehome",
            name="kibble_capacity",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Practical limit or intended stock level for kibble, in units. Leave blank if unset.",
                null=True,
                verbose_name="Kibble Capacity",
            ),
        ),
        migrations.AddField(
            model_name="storehome",
            name="canned_capacity",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Practical limit or intended stock level for canned food, in units. Leave blank if unset.",
                null=True,
                verbose_name="Canned Capacity",
            ),
        ),
    ]

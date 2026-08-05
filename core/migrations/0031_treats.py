# Generated manually for Treats food subtype

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0030_storageitemintake_storageitemouttake"),
    ]

    operations = [
        migrations.CreateModel(
            name="Treats",
            fields=[
                (
                    "food_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="core.food",
                    ),
                ),
                (
                    "treat_size",
                    models.CharField(
                        blank=True,
                        choices=[("SM", "Small"), ("MD", "Medium"), ("LG", "Large")],
                        max_length=2,
                        verbose_name="Treat Size",
                    ),
                ),
            ],
            bases=("core.food",),
        ),
    ]

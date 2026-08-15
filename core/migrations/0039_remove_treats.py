# Generated manually to drop the Treats product subtype.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0038_treat_size_standard_unspecified"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Treats",
        ),
    ]

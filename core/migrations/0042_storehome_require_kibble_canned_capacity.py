import django.core.validators
from django.db import migrations, models
from django.db.models import Q


KIBBLE_CAPACITY_DEFAULT = 100
CANNED_CAPACITY_DEFAULT = 250


def fill_storehome_capacities(apps, schema_editor):
    Storehome = apps.get_model("core", "Storehome")
    Storehome.objects.filter(Q(kibble_capacity__isnull=True) | Q(kibble_capacity=0)).update(
        kibble_capacity=KIBBLE_CAPACITY_DEFAULT
    )
    Storehome.objects.filter(Q(canned_capacity__isnull=True) | Q(canned_capacity=0)).update(
        canned_capacity=CANNED_CAPACITY_DEFAULT
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_storehome_kibble_canned_capacity"),
    ]

    operations = [
        migrations.RunPython(fill_storehome_capacities, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="storehome",
            name="kibble_capacity",
            field=models.PositiveIntegerField(
                default=KIBBLE_CAPACITY_DEFAULT,
                help_text="Practical limit or intended stock level for kibble, in units.",
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="Kibble Capacity",
            ),
        ),
        migrations.AlterField(
            model_name="storehome",
            name="canned_capacity",
            field=models.PositiveIntegerField(
                default=CANNED_CAPACITY_DEFAULT,
                help_text="Practical limit or intended stock level for canned food, in units.",
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="Canned Capacity",
            ),
        ),
    ]

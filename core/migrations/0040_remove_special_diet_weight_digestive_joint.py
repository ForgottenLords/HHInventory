# Generated manually to drop Weight Management, Digestive Health, and Joint Health.

import multiselectfield.db.fields
from django.db import migrations
from django.db.models import Q

REMOVED_SPECIAL_DIETS = ("WGHT", "DIGE", "JONT")


def strip_removed_special_diets(apps, schema_editor):
    Food = apps.get_model("core", "Food")
    match = Q()
    for code in REMOVED_SPECIAL_DIETS:
        match |= Q(special_diet__contains=code)
    for food in Food.objects.exclude(special_diet="").filter(match).iterator():
        raw = food.special_diet
        if isinstance(raw, str):
            values = [part for part in raw.split(",") if part]
        else:
            values = [str(part) for part in (raw or []) if part]
        cleaned = [value for value in values if value not in REMOVED_SPECIAL_DIETS]
        if cleaned == values:
            continue
        food.special_diet = ",".join(cleaned)
        food.save(update_fields=["special_diet"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0039_remove_treats"),
    ]

    operations = [
        migrations.RunPython(strip_removed_special_diets, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="food",
            name="special_diet",
            field=multiselectfield.db.fields.MultiSelectField(
                blank=True,
                choices=[
                    ("SENS", "Sensitive Stomach"),
                    ("DENT", "Dental Health"),
                    ("SKIN", "Skin & Coat"),
                    ("LIMI", "Limited Ingredient"),
                    ("GRAI", "Grain-Free"),
                ],
                max_length=24,
                verbose_name="Special Diet",
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_storageitem_date_stored_datetime"),
    ]

    operations = [
        migrations.AddField(
            model_name="storehome",
            name="latitude",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                max_digits=9,
                null=True,
                verbose_name="Latitude",
            ),
        ),
        migrations.AddField(
            model_name="storehome",
            name="longitude",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                max_digits=10,
                null=True,
                verbose_name="Longitude",
            ),
        ),
    ]

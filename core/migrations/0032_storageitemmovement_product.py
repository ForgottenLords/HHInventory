# Generated manually for product-scoped movement reporting

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_treats"),
    ]

    operations = [
        migrations.AddField(
            model_name="storageitemintake",
            name="product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="storageitemintake_rows",
                to="core.product",
                verbose_name="Product",
            ),
        ),
        migrations.AddField(
            model_name="storageitemouttake",
            name="product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="storageitemouttake_rows",
                to="core.product",
                verbose_name="Product",
            ),
        ),
    ]

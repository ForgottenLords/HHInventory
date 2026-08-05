# Drop ContentType product_class; require product on intake/outtake rows.

import django.db.models.deletion
from django.db import migrations, models


def drop_rows_missing_product(apps, schema_editor):
    """Clear legacy movement rows that never got a product FK."""
    for model_name in ("StorageItemIntake", "StorageItemOuttake"):
        apps.get_model("core", model_name).objects.filter(product__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_storageitemmovement_product"),
    ]

    operations = [
        migrations.RunPython(drop_rows_missing_product, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="storageitemintake",
            name="product_class",
        ),
        migrations.RemoveField(
            model_name="storageitemouttake",
            name="product_class",
        ),
        migrations.AlterField(
            model_name="storageitemintake",
            name="product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="storageitemintake_rows",
                to="core.product",
                verbose_name="Product",
            ),
        ),
        migrations.AlterField(
            model_name="storageitemouttake",
            name="product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="storageitemouttake_rows",
                to="core.product",
                verbose_name="Product",
            ),
        ),
    ]

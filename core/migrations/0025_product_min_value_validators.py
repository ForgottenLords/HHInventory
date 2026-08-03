# Generated manually for MinValueValidator on estimated_price and weight

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_remove_product_missing_data_product_data_warnings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="estimated_price",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=8,
                null=True,
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="Estimated Price",
            ),
        ),
        migrations.AlterField(
            model_name="kibble",
            name="weight",
            field=models.FloatField(
                blank=True,
                null=True,
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="Bag Weight",
            ),
        ),
    ]

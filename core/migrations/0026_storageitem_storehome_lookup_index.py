from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_product_min_value_validators"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="storageitem",
            index=models.Index(
                fields=["storehome", "content_type", "object_id"],
                name="core_storag_storeho_803eb1_idx",
            ),
        ),
    ]

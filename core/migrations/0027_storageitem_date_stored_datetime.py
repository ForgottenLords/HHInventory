from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_storageitem_storehome_lookup_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="storageitem",
            name="date_stored",
            field=models.DateTimeField(auto_now_add=True, verbose_name="Date Stored"),
        ),
    ]

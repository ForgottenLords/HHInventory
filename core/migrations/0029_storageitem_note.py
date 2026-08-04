from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_storehome_latitude_longitude"),
    ]

    operations = [
        migrations.AddField(
            model_name="storageitem",
            name="note",
            field=models.TextField(blank=True, verbose_name="Note"),
        ),
    ]

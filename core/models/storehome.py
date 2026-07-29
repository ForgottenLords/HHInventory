from django.db import models


class Storehome(models.Model):
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    managers = models.ManyToManyField("core.UserProfile", verbose_name="Managers", help_text="Users who are permitted to manage inventory")
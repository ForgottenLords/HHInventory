from django.db import models  # noqa: F401

from django.contrib.auth.models import AbstractUser


class UserProfile(AbstractUser):
    pass
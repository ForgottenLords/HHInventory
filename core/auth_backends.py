from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from core.models import UserProfile


class EmailOrUsernameBackend(ModelBackend):
    """Authenticates against either username or email, case-insensitively."""

    def user_can_authenticate(self, user):
        if not super().user_can_authenticate(user):
            return False
        if user.is_superuser:
            return True
        return bool(getattr(user, "managed_storehome_id", None))

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        try:
            user = UserProfile.objects.get(Q(username__iexact=username) | Q(email__iexact=username))
        except UserProfile.DoesNotExist:
            UserProfile().set_password(password)
            return None
        except UserProfile.MultipleObjectsReturned:
            user = (
                UserProfile.objects.filter(Q(username__iexact=username) | Q(email__iexact=username))
                .order_by("id")
                .first()
            )

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

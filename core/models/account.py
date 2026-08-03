from django.db import models
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError
from django.contrib.auth.models import AbstractUser


class UserProfile(AbstractUser):
    email = models.EmailField("email address", unique=True, blank=True)
    managed_storehome = models.ForeignKey(
        "core.Storehome",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managers",
        verbose_name="Managed Storehome",
        help_text="The storehome this user is permitted to manage inventory for",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("username"), name="userprofile_username_ci_unique"),
            models.UniqueConstraint(Lower("email"), name="userprofile_email_ci_unique"),
        ]
        
    @classmethod
    def unique_conflict_check(cls, username=None, email=None, exclude_pk=None):
        qs = cls.objects.all()
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if username is not None and qs.filter(username__iexact=username).exists():
            raise ValidationError("That username is already taken.")
        if qs.filter(email__iexact=email).exists():
            raise ValidationError("That email is already in use.")
        return None

    @classmethod
    def can_view(cls, request):
        #User must have Permission
        if request.user.has_perm("core.view_userprofile"):
            return True, ""
        return False, "You do not have permission to view User Accounts"

    @classmethod
    def can_create(cls, request):
        #User must have Permission
        if request.user.has_perm("core.add_userprofile"):
            return True, ""
        return False, "You do not have permission to create User Accounts"

    def can_edit(self, request):
        #This permission check should be used to control access to basic User Profile Fields like:
        #  First and Last Name
        #  Email
        #  Username
        if self == request.user:
            #Users can edit their own accounts
            return True, ""
        elif not request.user.has_perm("core.change_userprofile"):
            #User must have Permission
            return False, "You do not have permission to edit User Information"
        elif not self.is_superuser:
            #Superusers can edit other NON Superuser accounts
            return True, ""
        return False, "You do not have permission to edit this User's Information"
        
    def can_edit_password(self, request):
        if self.is_superuser:
            #Superusers can edit all other user's passwords, even if they can't edit other superusers info
            return True, ""
        canEdit, canEditReason = self.can_edit(request)
        if not canEdit:
            #Users can't edit passwords for what they can't edit
            return False, canEditReason
        elif self == request.user:
            #Users can edit their own passwords
            return True, ""
        return False, "You do not have permission to edit this User's Password"
        
    def can_delete(self, request):
        canEdit, canEditReason = self.can_edit(request)
        if not canEdit:
            #Users can't delete what they can't edit
            return False, canEditReason
        elif not request.user.has_perm("core.delete_userprofile"):
            #User must have Permission
            return False, "You do not have permission to delete User Accounts"
        elif self==request.user:
            #Users cannot delete their own account
            return False, "You do not have permission to delete your own Account"
        return True, ""

    @classmethod
    def can_manage_incoming_inventory(cls, request):
        """Assigned storehome managers may receive inventory for their storehome."""
        user = request.user
        if not user.is_authenticated:
            return False, "Sign in to manage incoming inventory."
        if getattr(user, "managed_storehome_id", None):
            return True, ""
        return False, "You are not assigned to manage a storehome."

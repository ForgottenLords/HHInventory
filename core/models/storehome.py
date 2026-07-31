from django.db import models


class Storehome(models.Model):
    name = models.CharField(max_length=255, verbose_name="Name")
    address = models.CharField(max_length=255, verbose_name="Address")

    @classmethod
    def can_view(cls, request):
        #User must have Permission
        if request.user.has_perm("core.view_storehome"):
            return True, ""
        return False, "You do not have permission to view Storehomes"

    @classmethod
    def can_create(cls, request):
        #User must have Permission
        if request.user.has_perm("core.add_storehome"):
            return True, ""
        return False, "You do not have permission to create Storehomes"

    def can_edit(self, request):
        #User must have Permission
        if request.user.has_perm("core.change_storehome"):
            return True, ""
        return False, "You do not have permission to edit this Storehome"

    def can_delete(self, request):
        #User must have Permission
        if request.user.has_perm("core.delete_storehome"):
            return True, ""
        return False, "You do not have permission to delete this Storehome"
import graphene
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from graphene_django import DjangoObjectType
from graphql import GraphQLError

from core.models import Storehome, UserProfile

#Review: 2026-07-29
#Class well structured and comprehensible
class PermissionType(graphene.ObjectType):
    allowed = graphene.Boolean(required=True)
    reason = graphene.String()

#Review: 2026-07-29
#Class well structured and comprehensible
class UserType(DjangoObjectType):
    can_edit = graphene.Field(PermissionType)
    can_edit_password = graphene.Field(PermissionType)
    can_delete = graphene.Field(PermissionType)
    can_view_users = graphene.Field(PermissionType)
    can_create_user = graphene.Field(PermissionType)
    can_view_storehomes = graphene.Field(PermissionType)
    can_create_storehome = graphene.Field(PermissionType)

    class Meta:
        model = UserProfile
        fields = (
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "is_superuser",
            "is_active",
            "date_joined",
            "managed_storehome",
        )

    def resolve_can_edit(self, info):
        allowed, reason = self.can_edit(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_edit_password(self, info):
        allowed, reason = self.can_edit_password(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_delete(self, info):
        allowed, reason = self.can_delete(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_view_users(self, info):
        allowed, reason = UserProfile.can_view(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_create_user(self, info):
        allowed, reason = UserProfile.can_create(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_view_storehomes(self, info):
        allowed, reason = Storehome.can_view(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_create_storehome(self, info):
        allowed, reason = Storehome.can_create(info.context)
        return PermissionType(allowed=allowed, reason=reason)

#Review: 2026-07-29
#Class well structured and comprehensible
class StorehomeType(DjangoObjectType):
    can_edit = graphene.Field(PermissionType)
    can_delete = graphene.Field(PermissionType)

    class Meta:
        model = Storehome
        fields = ("id", "name", "address", "managers")

    def resolve_can_edit(self, info):
        allowed, reason = self.can_edit(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_delete(self, info):
        allowed, reason = self.can_delete(info.context)
        return PermissionType(allowed=allowed, reason=reason)

#Review: 2026-07-29
#Class well structured and comprehensible
class LoginMutation(graphene.Mutation):
    class Arguments:
        identifier = graphene.String(required=True)
        password = graphene.String(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    user = graphene.Field(UserType)

    def mutate(self, info, identifier, password):
        request = info.context

        user = authenticate(request, username=identifier, password=password)
        if user is None:
            return LoginMutation(ok=False, error="Invalid username/email or password.")

        login(request, user)
        return LoginMutation(ok=True, error=None, user=user)

#Review: 2026-07-29
#Class well structured and comprehensible
class LogoutMutation(graphene.Mutation):
    ok = graphene.Boolean()

    def mutate(self, info):
        logout(info.context)
        return LogoutMutation(ok=True)

#Review: 2026-07-29
#Class well structured and comprehensible
class CreateUserMutation(graphene.Mutation):
    class Arguments:
        username = graphene.String(required=True)
        email = graphene.String(required=True)
        password = graphene.String(required=True)
        first_name = graphene.String()
        last_name = graphene.String()
        managed_storehome_id = graphene.ID()

    ok = graphene.Boolean()
    error = graphene.String()
    user = graphene.Field(UserType)

    def mutate(self, info, username, email, password, first_name="", last_name="", managed_storehome_id=None):
        request = info.context
        
        allowed, reason = UserProfile.can_create(request)
        if not allowed:
            return CreateUserMutation(ok=False, error=reason)

        try:
            UserProfile.unique_conflict_check(username, email)
        except ValidationError as e:
            return CreateUserMutation(ok=False, error=str(e))

        managed_storehome = None
        if managed_storehome_id:
            try:
                managed_storehome = Storehome.objects.get(pk=managed_storehome_id)
            except Storehome.DoesNotExist:
                return CreateUserMutation(ok=False, error="Storehome not found.")

        user = UserProfile(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            managed_storehome=managed_storehome,
        )
        
        try:
            validate_password(password, user)
        except ValidationError as e:
            return CreateUserMutation(ok=False, error=" ".join(e.messages))

        user.set_password(password)
        user.save()

        return CreateUserMutation(ok=True, error=None, user=user)

#Review: 2026-07-29
#Class well structured and comprehensible
class UpdateUserMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        username = graphene.String()
        email = graphene.String()
        first_name = graphene.String()
        last_name = graphene.String()
        managed_storehome_id = graphene.ID()

    ok = graphene.Boolean()
    error = graphene.String()
    user = graphene.Field(UserType)

    def mutate(self, info, id, username=None, email=None, first_name=None, last_name=None, managed_storehome_id=None):
        request = info.context

        try:
            target = UserProfile.objects.get(pk=id)
        except UserProfile.DoesNotExist:
            return UpdateUserMutation(ok=False, error="User not found.")

        allowed, reason = target.can_edit(request)
        if not allowed:
            return UpdateUserMutation(ok=False, error=reason)

        if username is not None:
            target.username = username
        if email is not None:
            target.email = email
        if first_name is not None:
            target.first_name = first_name
        if last_name is not None:
            target.last_name = last_name
        if managed_storehome_id is not None:
            if managed_storehome_id == "":
                target.managed_storehome = None
            else:
                try:
                    target.managed_storehome = Storehome.objects.get(pk=managed_storehome_id)
                except Storehome.DoesNotExist:
                    return UpdateUserMutation(ok=False, error="Storehome not found.")
        
        try:
            UserProfile.unique_conflict_check(target.username, target.email, exclude_pk=target.pk)
        except ValidationError as e:
            return UpdateUserMutation(ok=False, error=str(e))

        target.save()

        return UpdateUserMutation(ok=True, error=None, user=target)

#Review: 2026-07-29
#Class well structured and comprehensible
class UpdateMyProfileMutation(graphene.Mutation):
    """Self-service profile editing. Deliberately excludes managed_storehome which remain admin-only via UpdateUserMutation."""

    class Arguments:
        username = graphene.String()
        email = graphene.String()
        first_name = graphene.String()
        last_name = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    user = graphene.Field(UserType)

    def mutate(self, info, username=None, email=None, first_name=None, last_name=None):
        request = info.context

        target = request.user
        
        allowed, reason = target.can_edit(request)
        if not allowed:
            return UpdateUserMutation(ok=False, error=reason)

        if username is not None:
            target.username = username
        if email is not None:
            target.email = email
        if first_name is not None:
            target.first_name = first_name
        if last_name is not None:
            target.last_name = last_name


        try:
            UserProfile.unique_conflict_check(target.username, target.email, exclude_pk=target.pk)
        except ValidationError as e:
            return UpdateMyProfileMutation(ok=False, error=str(e))

        target.save()
            
        return UpdateMyProfileMutation(ok=True, error=None, user=target)

#Review: 2026-07-29
#Class well structured and comprehensible
class ChangePasswordMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        password = graphene.String(required=True)
        current_password = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()

    def mutate(self, info, id, password, current_password=None):
        request = info.context
        
        try:
            target = UserProfile.objects.get(pk=id)
        except UserProfile.DoesNotExist:
            return ChangePasswordMutation(ok=False, error="User not found.")

        allowed, reason = target.can_edit_password(request)
        if not allowed:
            return ChangePasswordMutation(ok=False, error=reason)

        changing_own_password = request.user.pk == target.pk
        if changing_own_password and not target.check_password(current_password or ""):
            return ChangePasswordMutation(ok=False, error="Current password is incorrect.")

        if not password:
            return ChangePasswordMutation(ok=False, error="Password cannot be blank.")

        try:
            validate_password(password, target)
        except ValidationError as e:
            return ChangePasswordMutation(ok=False, error=" ".join(e.messages))

        target.set_password(password)
        target.save(update_fields=["password"])
        if changing_own_password:
            update_session_auth_hash(request, target)
            
        return ChangePasswordMutation(ok=True, error=None)

#Review: 2026-07-29
#Class well structured and comprehensible
class DeleteUserMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)

    ok = graphene.Boolean()
    error = graphene.String()

    def mutate(self, info, id):
        request = info.context
        try:
            target = UserProfile.objects.get(pk=id)
        except UserProfile.DoesNotExist:
            return DeleteUserMutation(ok=False, error="User not found.")

        allowed, reason = target.can_delete(request)
        if not allowed:
            return DeleteUserMutation(ok=False, error=reason)

        target.delete()
        
        return DeleteUserMutation(ok=True, error=None)

#Review: 2026-07-29
#Class well structured and comprehensible
class CreateStorehomeMutation(graphene.Mutation):
    class Arguments:
        name = graphene.String(required=True)
        address = graphene.String(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    storehome = graphene.Field(StorehomeType)

    def mutate(self, info, name, address):
        request = info.context
        allowed, reason = Storehome.can_create(request)
        if not allowed:
            return CreateStorehomeMutation(ok=False, error=reason)

        storehome = Storehome.objects.create(name=name, address=address)
        
        return CreateStorehomeMutation(ok=True, error=None, storehome=storehome)

#Review: 2026-07-29
#Class well structured and comprehensible
class UpdateStorehomeMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        name = graphene.String()
        address = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    storehome = graphene.Field(StorehomeType)

    def mutate(self, info, id, name=None, address=None):
        request = info.context
        
        try:
            storehome = Storehome.objects.get(pk=id)
        except Storehome.DoesNotExist:
            return UpdateStorehomeMutation(ok=False, error="Storehome not found.")

        allowed, reason = storehome.can_edit(request)
        if not allowed:
            return UpdateStorehomeMutation(ok=False, error=reason)

        if name is not None:
            storehome.name = name
        if address is not None:
            storehome.address = address
            
        storehome.save()
        
        return UpdateStorehomeMutation(ok=True, error=None, storehome=storehome)

#Review: 2026-07-29
#Class well structured and comprehensible
class DeleteStorehomeMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)

    ok = graphene.Boolean()
    error = graphene.String()

    def mutate(self, info, id):
        request = info.context
        
        try:
            storehome = Storehome.objects.get(pk=id)
        except Storehome.DoesNotExist:
            return DeleteStorehomeMutation(ok=False, error="Storehome not found.")

        allowed, reason = storehome.can_delete(request)
        if not allowed:
            return DeleteStorehomeMutation(ok=False, error=reason)

        storehome.delete()
        
        return DeleteStorehomeMutation(ok=True, error=None)

#Review: 2026-07-29
#Class well structured and comprehensible
class Query(graphene.ObjectType):
    me = graphene.Field(UserType)
    users = graphene.List(UserType)
    storehomes = graphene.List(StorehomeType)

    def resolve_me(self, info):
        user = info.context.user
        if not user.is_authenticated:
            return None
        return user

    def resolve_users(self, info):
        allowed, reason = UserProfile.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)
        return UserProfile.objects.all().order_by("username")

    def resolve_storehomes(self, info):
        allowed, reason = Storehome.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)
        return Storehome.objects.all().order_by("name")

#Review: 2026-07-29
#Class well structured and comprehensible
class Mutation(graphene.ObjectType):
    login = LoginMutation.Field()
    logout = LogoutMutation.Field()
    create_user = CreateUserMutation.Field()
    update_user = UpdateUserMutation.Field()
    update_my_profile = UpdateMyProfileMutation.Field()
    change_password = ChangePasswordMutation.Field()
    delete_user = DeleteUserMutation.Field()
    create_storehome = CreateStorehomeMutation.Field()
    update_storehome = UpdateStorehomeMutation.Field()
    delete_storehome = DeleteStorehomeMutation.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)

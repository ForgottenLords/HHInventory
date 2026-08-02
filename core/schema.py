import graphene
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils.text import capfirst
from graphene_django import DjangoObjectType
from graphql import GraphQLError

from core.models import (
    Canned,
    Food,
    Kibble,
    Product,
    StorageItem,
    Storehome,
    UserProfile,
    subclass_or_none,
)

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

class ChoiceType(graphene.ObjectType):
    value = graphene.String(required=True)
    label = graphene.String(required=True)

def enum_choices(choices):
    """Every option of a choices enum, in declaration order, for a client to offer as inputs."""
    return [ChoiceType(value=choice.value, label=str(choice.label)) for choice in choices]

def choice_entry(choices, value):
    """A stored choice paired with its human label, or nothing at all when the field is blank.

    Sending the value alongside the label lets one query both render a product and populate an
    edit form, without the client having to map labels back onto the values the model stores.
    """
    if not value:
        return None
    labels = dict(choices.choices)
    return ChoiceType(value=value, label=str(labels.get(value, value)))

def choice_entries(choices, values):
    """The multi-select counterpart of choice_entry, skipping any blank the field holds."""
    return [entry for entry in (choice_entry(choices, value) for value in values or []) if entry]

class KibbleDetailType(graphene.ObjectType):
    weight = graphene.Float()
    #The same number with its unit, so a client can render it without restating that it is pounds
    weight_display = graphene.String()
    kibble_size = graphene.Field(ChoiceType)

class CannedDetailType(graphene.ObjectType):
    texture = graphene.Field(ChoiceType)

class FoodDetailType(graphene.ObjectType):
    """The food-specific half of a product, with the kibble/canned rows nested beneath it."""

    life_stages = graphene.Field(ChoiceType)
    proteins = graphene.List(graphene.NonNull(ChoiceType), required=True)
    special_diet = graphene.List(graphene.NonNull(ChoiceType), required=True)
    kibble = graphene.Field(KibbleDetailType)
    canned = graphene.Field(CannedDetailType)

class ProductType(DjangoObjectType):
    product_type = graphene.String(required=True)
    photo_url = graphene.String()
    food = graphene.Field(FoodDetailType)
    data_warnings = graphene.List(graphene.NonNull(graphene.String), required=True)
    can_edit = graphene.Field(PermissionType)
    can_delete = graphene.Field(PermissionType)

    class Meta:
        model = Product
        fields = (
            "id",
            "name",
            "barcode",
            "brand",
            "country_of_origin",
            "estimated_price",
            "notes",
            "disallowed",
            "in_production",
            "last_updated",
        )

    def resolve_product_type(self, info):
        return self.product_type.label

    def resolve_photo_url(self, info):
        if not self.photo:
            return None
        return self.photo.url

    def resolve_data_warnings(self, info):
        return list(self.data_warnings or [])

    def resolve_food(self, info):
        food = subclass_or_none(self, "food")
        if food is None:
            return None

        kibble = subclass_or_none(food, "kibble")
        canned = subclass_or_none(food, "canned")
        return FoodDetailType(
            life_stages=choice_entry(Food.LifeStageChoices, food.life_stages),
            proteins=choice_entries(Food.ProteinChoices, food.proteins),
            special_diet=choice_entries(Food.SpecialDietChoices, food.special_diet),
            kibble=None if kibble is None else KibbleDetailType(
                weight=kibble.weight,
                weight_display=kibble.get_weight_display(),
                kibble_size=choice_entry(Kibble.KibbleSizeChoices, kibble.kibble_size),
            ),
            canned=None if canned is None else CannedDetailType(
                texture=choice_entry(Canned.TextureChoices, canned.texture),
            ),
        )

    def resolve_can_edit(self, info):
        allowed, reason = self.can_edit(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_delete(self, info):
        allowed, reason = self.can_delete(info.context)
        return PermissionType(allowed=allowed, reason=reason)

class ProductPageType(graphene.ObjectType):
    """One page of products plus the counters the library UI needs to render its pager."""

    items = graphene.List(graphene.NonNull(ProductType), required=True)
    total_count = graphene.Int(required=True)
    page = graphene.Int(required=True)
    page_size = graphene.Int(required=True)
    total_pages = graphene.Int(required=True)
    has_previous = graphene.Boolean(required=True)
    has_next = graphene.Boolean(required=True)

class ProductFilterOptionsType(graphene.ObjectType):
    product_types = graphene.List(graphene.NonNull(ChoiceType), required=True)
    proteins = graphene.List(graphene.NonNull(ChoiceType), required=True)
    life_stages = graphene.List(graphene.NonNull(ChoiceType), required=True)
    special_diets = graphene.List(graphene.NonNull(ChoiceType), required=True)

class ProductChoicesType(graphene.ObjectType):
    """Every choice list a product edit form needs to offer, whatever the product's type."""

    life_stages = graphene.List(graphene.NonNull(ChoiceType), required=True)
    proteins = graphene.List(graphene.NonNull(ChoiceType), required=True)
    special_diets = graphene.List(graphene.NonNull(ChoiceType), required=True)
    kibble_sizes = graphene.List(graphene.NonNull(ChoiceType), required=True)
    textures = graphene.List(graphene.NonNull(ChoiceType), required=True)

#Sort keys accepted from clients, mapped to the model field each one orders by. Anything
#outside this map is rejected rather than passed to order_by.
PRODUCT_SORT_FIELDS = {
    "name": "name",
    "brand": "brand",
    "estimatedPrice": "estimated_price",
}
PRODUCT_TEXT_SORT_KEYS = {"name", "brand"}
DEFAULT_PRODUCT_SORT = "name"
DEFAULT_PRODUCT_PAGE_SIZE = 20
MAX_PRODUCT_PAGE_SIZE = 100

def product_ordering(sort):
    """Turn a client sort key such as "-brand" into an ORDER BY expression."""
    descending = sort.startswith("-")
    key = sort[1:] if descending else sort
    if key not in PRODUCT_SORT_FIELDS:
        raise GraphQLError(f"Cannot sort products by '{key}'.")

    field = PRODUCT_SORT_FIELDS[key]
    #Text is compared case-insensitively; nulls_last keeps unpriced products off the front page
    expression = Lower(field) if key in PRODUCT_TEXT_SORT_KEYS else F(field)
    if descending:
        return expression.desc(nulls_last=True)
    return expression.asc(nulls_last=True)

def multiselect_has(field, value):
    """Match a MultiSelectField value stored as a comma-separated string."""
    return (
        Q(**{field: value})
        | Q(**{f"{field}__startswith": f"{value}," })
        | Q(**{f"{field}__endswith": f",{value}"})
        | Q(**{f"{field}__contains": f",{value}," })
    )

def filtered_products(search=None, product_type=None, has_data_warnings=None, protein=None, life_stage=None, special_diet=None, sort=DEFAULT_PRODUCT_SORT):
    queryset = Product.objects.select_related(*Product.TYPE_SELECT_RELATED)

    if search:
        #Every whitespace-separated term must match somewhere, so extra words narrow results
        for term in search.split():
            queryset = queryset.filter(
                Q(name__icontains=term)
                | Q(brand__icontains=term)
                | Q(barcode__icontains=term)
                | Q(notes__icontains=term)
            )
    if product_type:
        if product_type not in Product.TYPE_QUERY_FILTERS:
            raise GraphQLError(f"Unknown product type '{product_type}'.")
        queryset = queryset.filter(**Product.TYPE_QUERY_FILTERS[product_type])
    if has_data_warnings:
        # Null or [] means clean; anything else is one or more warning strings.
        queryset = queryset.exclude(Q(data_warnings__isnull=True) | Q(data_warnings=[]))
    if protein:
        if protein not in Food.ProteinChoices.values:
            raise GraphQLError(f"Unknown protein '{protein}'.")
        queryset = queryset.filter(multiselect_has("food__proteins", protein))
    if life_stage:
        if life_stage not in Food.LifeStageChoices.values:
            raise GraphQLError(f"Unknown life stage '{life_stage}'.")
        queryset = queryset.filter(food__life_stages=life_stage)
    if special_diet:
        if special_diet not in Food.SpecialDietChoices.values:
            raise GraphQLError(f"Unknown special diet '{special_diet}'.")
        queryset = queryset.filter(multiselect_has("food__special_diet", special_diet))

    #Tie-break on pk so rows with equal sort values keep a stable order across pages
    return queryset.order_by(product_ordering(sort), "pk")

#The model that declares each editable product field. A product's type is fixed once it exists,
#so a field may only be written when the product is an instance of the model that owns it.
PRODUCT_FIELD_OWNERS = {
    "name": Product,
    "barcode": Product,
    "brand": Product,
    "country_of_origin": Product,
    "estimated_price": Product,
    "notes": Product,
    "disallowed": Product,
    "in_production": Product,
    "life_stages": Food,
    "proteins": Food,
    "special_diet": Food,
    "weight": Kibble,
    "kibble_size": Kibble,
    "texture": Canned,
}

def field_label(model, name):
    return capfirst(model._meta.get_field(name).verbose_name)

def validation_message(product, error):
    """Flatten a full_clean failure into one sentence naming each field that was rejected."""
    messages = []
    for name, field_messages in error.message_dict.items():
        text = " ".join(field_messages)
        if name == NON_FIELD_ERRORS:
            messages.append(text)
        else:
            messages.append(f"{field_label(type(product), name)}: {text}")
    return " ".join(messages)

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

class UpdateProductMutation(graphene.Mutation):
    """Edits a product along with whichever subclass fields its type provides.

    Only the arguments the caller sends are written, so a client may submit just the fields its
    form showed. A product's type is fixed at creation, so any argument belonging to a subclass
    the product is not an instance of is refused rather than quietly dropped.
    """

    class Arguments:
        id = graphene.ID(required=True)
        name = graphene.String()
        barcode = graphene.String()
        brand = graphene.String()
        country_of_origin = graphene.String()
        estimated_price = graphene.Decimal()
        notes = graphene.String()
        disallowed = graphene.Boolean()
        in_production = graphene.Boolean()
        life_stages = graphene.String()
        proteins = graphene.List(graphene.NonNull(graphene.String))
        special_diet = graphene.List(graphene.NonNull(graphene.String))
        weight = graphene.Float()
        kibble_size = graphene.String()
        texture = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    product = graphene.Field(ProductType)

    def mutate(self, info, id, **fields):
        request = info.context

        product = (
            Product.objects.select_related(*Product.TYPE_SELECT_RELATED).filter(pk=id).first()
        )
        if product is None:
            return UpdateProductMutation(ok=False, error="Product not found.")

        allowed, reason = product.can_edit(request)
        if not allowed:
            return UpdateProductMutation(ok=False, error=reason)

        product_type = product.product_type.label
        target = product.specific

        for name, value in fields.items():
            owner = PRODUCT_FIELD_OWNERS[name]
            if not isinstance(target, owner):
                return UpdateProductMutation(
                    ok=False,
                    error=f"{field_label(owner, name)} does not apply to products of type '{product_type}'.",
                )
            setattr(target, name, value)

        try:
            target.full_clean()
        except ValidationError as e:
            return UpdateProductMutation(ok=False, error=validation_message(target, e))

        target.save()

        #Re-read so the caller sees the saved row, including the refreshed last_updated stamp
        return UpdateProductMutation(
            ok=True,
            error=None,
            product=Product.objects.select_related(*Product.TYPE_SELECT_RELATED).get(pk=target.pk),
        )

class DeleteProductMutation(graphene.Mutation):
    """Removes a product, the subclass rows beneath it, and every stored copy of it."""

    class Arguments:
        id = graphene.ID(required=True)

    ok = graphene.Boolean()
    error = graphene.String()

    def mutate(self, info, id):
        request = info.context

        try:
            product = Product.objects.get(pk=id)
        except Product.DoesNotExist:
            return DeleteProductMutation(ok=False, error="Product not found.")

        allowed, reason = product.can_delete(request)
        if not allowed:
            return DeleteProductMutation(ok=False, error=reason)

        #A storage item names whichever model in the chain its creator held, and a generic
        #relation has no database cascade, so those rows have to be cleared before the product
        #goes or they would point at an id that no longer exists.
        content_types = ContentType.objects.get_for_models(Product, Food, Kibble, Canned)
        StorageItem.objects.filter(
            content_type__in=content_types.values(), object_id=product.pk
        ).delete()

        #Deleting the base row cascades through the Food/Kibble/Canned tables that inherit it
        product.delete()

        return DeleteProductMutation(ok=True, error=None)

#Review: 2026-07-29
#Class well structured and comprehensible
class Query(graphene.ObjectType):
    me = graphene.Field(UserType)
    users = graphene.List(UserType)
    storehomes = graphene.List(StorehomeType)
    products = graphene.Field(
        ProductPageType,
        search=graphene.String(),
        product_type=graphene.String(),
        has_data_warnings=graphene.Boolean(),
        protein=graphene.String(),
        life_stage=graphene.String(),
        special_diet=graphene.String(),
        sort=graphene.String(),
        page=graphene.Int(),
        page_size=graphene.Int(),
    )
    product = graphene.Field(ProductType, id=graphene.ID(required=True))
    product_filter_options = graphene.Field(ProductFilterOptionsType)
    product_choices = graphene.Field(ProductChoicesType)

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

    def resolve_products(
        self,
        info,
        search=None,
        product_type=None,
        has_data_warnings=None,
        protein=None,
        life_stage=None,
        special_diet=None,
        sort=DEFAULT_PRODUCT_SORT,
        page=1,
        page_size=DEFAULT_PRODUCT_PAGE_SIZE,
    ):
        allowed, reason = Product.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)

        queryset = filtered_products(
            search=search,
            product_type=product_type,
            has_data_warnings=has_data_warnings,
            protein=protein,
            life_stage=life_stage,
            special_diet=special_diet,
            sort=sort,
        )

        if not page_size or page_size < 1:
            page_size = DEFAULT_PRODUCT_PAGE_SIZE
        paginator = Paginator(queryset, min(page_size, MAX_PRODUCT_PAGE_SIZE))

        #get_page sends anything past the end to the last page, so a stale page number degrades
        #instead of erroring. It also treats numbers below 1 as past the end, hence the max().
        current_page = paginator.get_page(max(page or 1, 1))

        return ProductPageType(
            items=list(current_page.object_list),
            total_count=paginator.count,
            page=current_page.number,
            page_size=paginator.per_page,
            total_pages=paginator.num_pages,
            has_previous=current_page.has_previous(),
            has_next=current_page.has_next(),
        )

    def resolve_product(self, info, id):
        allowed, reason = Product.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)

        #A missing product is a normal outcome for a stale link, so the page handles null itself
        return (
            Product.objects.select_related(*Product.TYPE_SELECT_RELATED)
            .filter(pk=id)
            .first()
        )

    def resolve_product_filter_options(self, info):
        allowed, reason = Product.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)

        return ProductFilterOptionsType(
            product_types=enum_choices(Product.TypeChoices),
            proteins=enum_choices(Food.ProteinChoices),
            life_stages=enum_choices(Food.LifeStageChoices),
            special_diets=enum_choices(Food.SpecialDietChoices),
        )

    def resolve_product_choices(self, info):
        allowed, reason = Product.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)

        return ProductChoicesType(
            life_stages=enum_choices(Food.LifeStageChoices),
            proteins=enum_choices(Food.ProteinChoices),
            special_diets=enum_choices(Food.SpecialDietChoices),
            kibble_sizes=enum_choices(Kibble.KibbleSizeChoices),
            textures=enum_choices(Canned.TextureChoices),
        )

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
    update_product = UpdateProductMutation.Field()
    delete_product = DeleteProductMutation.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)

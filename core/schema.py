from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import base64
import mimetypes
import re

import graphene
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import CharField, F, OuterRef, Q, Subquery
from django.db.models.functions import Lower
from django.utils import timezone
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
    Treats,
    UserProfile,
    subclass_or_none,
)

MAX_PRODUCT_PHOTO_UPLOAD_BYTES = 5 * 1024 * 1024


def parse_photo_base64(photo_base64):
    """Decode a data-URL or raw base64 image into (bytes, content_type)."""
    text = (photo_base64 or "").strip()
    content_type = "image/jpeg"
    raw = text
    match = re.match(r"^data:([^;,]+);base64,(.+)$", text, re.DOTALL)
    if match:
        content_type = match.group(1).strip().lower()
        raw = match.group(2)
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError("Invalid photo data.") from exc
    if not data:
        raise ValueError("Photo data is empty.")
    if len(data) > MAX_PRODUCT_PHOTO_UPLOAD_BYTES:
        raise ValueError("Photo is too large (max 5 MB).")
    if not content_type.startswith("image/"):
        raise ValueError("File must be an image.")
    return data, content_type


def photo_upload_filename(product, content_type):
    ext = mimetypes.guess_extension(content_type, strict=False) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    stem = re.sub(r"[^a-zA-Z0-9_-]", "", str(product.barcode or "product"))[:40] or "product"
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"{stem}_{stamp}{ext}"

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
    can_create_product = graphene.Field(PermissionType)
    can_manage_incoming_inventory = graphene.Field(PermissionType)

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

    def resolve_can_create_product(self, info):
        allowed, reason = Product.can_create(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_manage_incoming_inventory(self, info):
        allowed, reason = UserProfile.can_manage_storehome_inventory(info.context)
        return PermissionType(allowed=allowed, reason=reason)

#Review: 2026-07-29
#Class well structured and comprehensible
class StorehomeType(DjangoObjectType):
    can_edit = graphene.Field(PermissionType)
    can_delete = graphene.Field(PermissionType)

    class Meta:
        model = Storehome
        fields = ("id", "name", "address", "latitude", "longitude", "managers")

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

class TreatsDetailType(graphene.ObjectType):
    treat_size = graphene.Field(ChoiceType)

class FoodDetailType(graphene.ObjectType):
    """The food-specific half of a product, with the kibble/canned/treats rows nested beneath it."""

    life_stages = graphene.Field(ChoiceType)
    proteins = graphene.List(graphene.NonNull(ChoiceType), required=True)
    special_diet = graphene.List(graphene.NonNull(ChoiceType), required=True)
    kibble = graphene.Field(KibbleDetailType)
    canned = graphene.Field(CannedDetailType)
    treats = graphene.Field(TreatsDetailType)


class MovementHistogramBucketType(graphene.ObjectType):
    key = graphene.String(required=True)
    intake = graphene.Int(required=True)
    outtake = graphene.Int(required=True)
    net = graphene.Int(required=True)
    is_current = graphene.Boolean(required=True)
    label = graphene.String(required=True)
    show_label = graphene.Boolean(required=True)
    intake_height_pct = graphene.Int(required=True)
    outtake_height_pct = graphene.Int(required=True)


class ProductType(DjangoObjectType):
    product_type = graphene.String(required=True)
    photo_url = graphene.String()
    food = graphene.Field(FoodDetailType)
    data_warnings = graphene.List(graphene.NonNull(graphene.String), required=True)
    can_edit = graphene.Field(PermissionType)
    can_edit_disallowed = graphene.Field(PermissionType)
    can_mark_reviewed = graphene.Field(PermissionType)
    can_force_lookup_update = graphene.Field(PermissionType)
    can_delete = graphene.Field(PermissionType)
    is_reviewed = graphene.Boolean(required=True)
    reviewed_by = graphene.Field(UserType)
    total_quantity = graphene.Int(required=True)
    # Forward ref: StorageItemType is declared below.
    storage_items = graphene.List(
        graphene.NonNull(lambda: StorageItemType), required=True
    )
    movement_histogram = graphene.List(
        graphene.NonNull(MovementHistogramBucketType),
        required=True,
        months=graphene.Int(
            description="Number of recent calendar months to include (default 6)."
        ),
        description=(
            "Recorded intake and outtake units for this product across all "
            "Storehomes, bucketed by month."
        ),
    )

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
            "reviewed_at",
            "last_updated",
            "updater_class",
            "updater_last_updated",
        )

    def resolve_product_type(self, info):
        return self.product_type.label

    def resolve_photo_url(self, info):
        if not self.photo:
            return None
        return self.photo.url

    def resolve_data_warnings(self, info):
        return list(self.data_warnings or [])

    def resolve_is_reviewed(self, info):
        return self.is_reviewed

    def resolve_reviewed_by(self, info):
        return self.reviewed_by

    def resolve_food(self, info):
        food = subclass_or_none(self, "food")
        if food is None:
            return None

        kibble = subclass_or_none(food, "kibble")
        canned = subclass_or_none(food, "canned")
        treats = subclass_or_none(food, "treats")
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
            treats=None if treats is None else TreatsDetailType(
                treat_size=choice_entry(Treats.TreatSizeChoices, treats.treat_size),
            ),
        )

    def resolve_can_edit(self, info):
        allowed, reason = self.can_edit(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_edit_disallowed(self, info):
        allowed, reason = self.can_edit_disallowed(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_mark_reviewed(self, info):
        allowed, reason = self.can_mark_reviewed(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_force_lookup_update(self, info):
        allowed, reason = self.can_force_lookup_update(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_can_delete(self, info):
        allowed, reason = self.can_delete(info.context)
        return PermissionType(allowed=allowed, reason=reason)

    def resolve_total_quantity(self, info):
        # Prefer the list-query annotation; fall back for single-product fetches.
        annotated = getattr(self, "stock_quantity", None)
        if annotated is not None:
            return int(annotated)
        quantity = (
            StorageItem.annotate_product_stock_quantity(Product.objects.filter(pk=self.pk))
            .values_list("stock_quantity", flat=True)
            .first()
        )
        return int(quantity or 0)

    def resolve_storage_items(self, info):
        """Stock rows for this product across every storehome/warehouse."""
        return list(
            StorageItem.objects.select_related("storehome")
            .filter(
                content_type__in=StorageItem.product_content_types(),
                object_id=self.pk,
            )
            .order_by("storehome__name", "expiry_date", "pk")
        )

    def resolve_movement_histogram(self, info, months=None):
        kwargs = {"product": self}
        if months is not None:
            kwargs["months"] = months
        return [
            MovementHistogramBucketType(**bucket)
            for bucket in StorageItem.movement_histogram(**kwargs)
        ]

def storage_item_product(item, *, disallowed_only=False):
    """Product for a storage lot, preferring a prefetched attachment when present."""
    cached = getattr(item, "_prefetched_product", None)
    if cached is not None:
        return cached
    qs = Product.objects.filter(pk=item.object_id)
    if disallowed_only:
        return qs.only("disallowed").first()
    return qs.select_related(*Product.TYPE_SELECT_RELATED).first()


def storage_item_disposal_reasons(item):
    """Why a lot should be disposed: past keep date and/or disallowed product."""
    reasons = []
    if item.is_past_keep_date():
        reasons.append("Past Keep Date")
    product = storage_item_product(item, disallowed_only=True)
    if product is not None and product.disallowed:
        reasons.append("Disallowed Product")
    return reasons


class StorageItemType(DjangoObjectType):
    product = graphene.Field(ProductType)
    past_expiry = graphene.Boolean(required=True)
    past_keep_date = graphene.Boolean(required=True)
    keep_until_date = graphene.Date()
    needs_disposal = graphene.Boolean(required=True)
    disposal_reasons = graphene.List(graphene.NonNull(graphene.String), required=True)
    warnings_for = graphene.List(
        graphene.NonNull(graphene.String),
        required=True,
        intake_or_outtake=graphene.String(required=True),
        description='Policy messages for "intake" or "outtake" of this lot.',
    )

    class Meta:
        model = StorageItem
        fields = ("id", "quantity", "date_stored", "expiry_date", "note", "storehome")

    def resolve_product(self, info):
        # Rows may point at Product or a subclass ContentType; the shared pk is enough.
        return storage_item_product(self)

    def resolve_past_expiry(self, info):
        return self.is_past_expiry()

    def resolve_past_keep_date(self, info):
        return self.is_past_keep_date()

    def resolve_keep_until_date(self, info):
        return StorageItem.keep_until_date(self.expiry_date)

    def resolve_disposal_reasons(self, info):
        """Why this lot should be disposed: past keep date and/or disallowed product."""
        return storage_item_disposal_reasons(self)

    def resolve_needs_disposal(self, info):
        return bool(storage_item_disposal_reasons(self))

    def resolve_warnings_for(self, info, intake_or_outtake):
        product = storage_item_product(self, disallowed_only=True)
        if product is None:
            return []
        return StorageItem.warnings_for(product, self.expiry_date, intake_or_outtake)

class ProductPageType(graphene.ObjectType):
    """One page of products plus the counters the library UI needs to render its pager."""

    items = graphene.List(graphene.NonNull(ProductType), required=True)
    total_count = graphene.Int(required=True)
    page = graphene.Int(required=True)
    page_size = graphene.Int(required=True)
    total_pages = graphene.Int(required=True)
    has_previous = graphene.Boolean(required=True)
    has_next = graphene.Boolean(required=True)

class StorageItemPageType(graphene.ObjectType):
    """One page of storage lots plus the counters the inventory pager needs."""

    items = graphene.List(graphene.NonNull(StorageItemType), required=True)
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
    treat_sizes = graphene.List(graphene.NonNull(ChoiceType), required=True)

#Sort keys accepted from clients, mapped to the model field each one orders by. Anything
#outside this map is rejected rather than passed to order_by.
PRODUCT_SORT_FIELDS = {
    "name": "name",
    "brand": "brand",
    "estimatedPrice": "estimated_price",
}
# Fields used when ordering StorageItem lots (product annotations + lot fields).
STORAGE_ITEM_SORT_FIELDS = {
    "name": "product_name",
    "brand": "product_brand",
    "expiryDate": "expiry_date",
}
PRODUCT_TEXT_SORT_KEYS = {"name", "brand"}
DEFAULT_PRODUCT_SORT = "name"
DEFAULT_PRODUCT_PAGE_SIZE = 20
MAX_PRODUCT_PAGE_SIZE = 100

def product_ordering(sort, field_map=PRODUCT_SORT_FIELDS):
    """Turn a client sort key such as "-brand" into an ORDER BY expression."""
    descending = sort.startswith("-")
    key = sort[1:] if descending else sort
    if key not in field_map:
        raise GraphQLError(f"Cannot sort products by '{key}'.")

    field = field_map[key]
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

def apply_product_list_filters(
    queryset,
    search=None,
    product_type=None,
    has_data_warnings=None,
    protein=None,
    life_stage=None,
    special_diet=None,
):
    """Apply Product Library search/filters to a Product queryset."""
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
    return queryset

def filtered_products(
    search=None,
    product_type=None,
    has_data_warnings=None,
    protein=None,
    life_stage=None,
    special_diet=None,
    has_stock=None,
    not_yet_reviewed=None,
    sort=DEFAULT_PRODUCT_SORT,
    storehome=None,
):
    queryset = Product.objects.select_related(*Product.TYPE_SELECT_RELATED)
    # Annotate once so the library can show and filter on stock totals.
    # When storehome is set, totals are for that home only.
    queryset = StorageItem.annotate_product_stock_quantity(queryset, storehome=storehome)
    queryset = apply_product_list_filters(
        queryset,
        search=search,
        product_type=product_type,
        has_data_warnings=has_data_warnings,
        protein=protein,
        life_stage=life_stage,
        special_diet=special_diet,
    )
    if has_stock:
        queryset = queryset.filter(stock_quantity__gt=0)
    if not_yet_reviewed:
        queryset = queryset.filter(reviewed_at__isnull=True)

    #Tie-break on pk so rows with equal sort values keep a stable order across pages
    return queryset.order_by(product_ordering(sort), "pk")

def filtered_storage_items(
    storehome,
    search=None,
    product_type=None,
    needs_disposal=None,
    protein=None,
    life_stage=None,
    special_diet=None,
    sort=DEFAULT_PRODUCT_SORT,
):
    """Storage lots at a storehome, narrowed by product filters plus optional disposal flag.

    needs_disposal selects lots past their keep date or whose product is disallowed —
    stock that should be disposed of.

    Text search matches product name/brand/barcode/notes or the lot's own note.
    Every whitespace-separated term must match somewhere on the product or lot.
    """
    matching_products = apply_product_list_filters(
        Product.objects.all(),
        product_type=product_type,
        protein=protein,
        life_stage=life_stage,
        special_diet=special_diet,
    )
    content_type_ids = [ct.pk for ct in StorageItem.product_content_types()]
    product_name = Subquery(
        Product.objects.filter(pk=OuterRef("object_id")).values("name")[:1],
        output_field=CharField(),
    )
    product_brand = Subquery(
        Product.objects.filter(pk=OuterRef("object_id")).values("brand")[:1],
        output_field=CharField(),
    )
    queryset = StorageItem.objects.filter(
        storehome=storehome,
        content_type_id__in=content_type_ids,
        quantity__gt=0,
        object_id__in=matching_products.values("pk"),
    ).annotate(
        product_name=product_name,
        product_brand=product_brand,
    )
    if search:
        for term in search.split():
            product_term_ids = Product.objects.filter(
                Q(name__icontains=term)
                | Q(brand__icontains=term)
                | Q(barcode__icontains=term)
                | Q(notes__icontains=term)
            ).values("pk")
            queryset = queryset.filter(
                Q(object_id__in=product_term_ids) | Q(note__icontains=term)
            )
    if needs_disposal:
        disallowed_ids = Product.objects.filter(disallowed=True).values("pk")
        queryset = queryset.filter(
            StorageItem.past_keep_date_q() | Q(object_id__in=disallowed_ids)
        )
    # Primary sort from the client; expiry then pk keep same-product lots stable.
    return queryset.order_by(
        product_ordering(sort, STORAGE_ITEM_SORT_FIELDS),
        "expiry_date",
        "pk",
    )

def attach_products_to_storage_items(items):
    """Attach Product rows for GraphQL product resolution without N+1 queries."""
    product_ids = {item.object_id for item in items}
    products = {
        product.pk: product
        for product in Product.objects.select_related(*Product.TYPE_SELECT_RELATED).filter(
            pk__in=product_ids
        )
    }
    for item in items:
        item._prefetched_product = products.get(item.object_id)
    return items

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
    "treat_size": Treats,
}

def field_label(model, name):
    return capfirst(model._meta.get_field(name).verbose_name)

class FieldErrorType(graphene.ObjectType):
    """One model-field validation failure, keyed by the Django field name."""

    field = graphene.String(required=True)
    message = graphene.String(required=True)

def validation_field_errors(product, error):
    """Split a full_clean failure into per-field entries for form UIs.

    Non-field errors use an empty field name so the client can show them as a form banner.
    """
    errors = []
    for name, field_messages in error.message_dict.items():
        text = " ".join(field_messages)
        if name == NON_FIELD_ERRORS:
            errors.append(FieldErrorType(field="", message=text))
        else:
            errors.append(FieldErrorType(field=name, message=text))
    return errors

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
            return CreateUserMutation(ok=False, error=" ".join(e.messages))

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
        try:
            user.full_clean()
        except ValidationError as e:
            return CreateUserMutation(ok=False, error=validation_message(user, e))

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
            return UpdateUserMutation(ok=False, error=" ".join(e.messages))

        try:
            target.full_clean()
        except ValidationError as e:
            return UpdateUserMutation(ok=False, error=validation_message(target, e))

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
            return UpdateMyProfileMutation(ok=False, error=reason)

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
            return UpdateMyProfileMutation(ok=False, error=" ".join(e.messages))

        try:
            target.full_clean()
        except ValidationError as e:
            return UpdateMyProfileMutation(ok=False, error=validation_message(target, e))

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
def _parse_coordinate(value, field_name):
    """Parse a GraphQL coordinate string/number into Decimal, or None if blank."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError({field_name: f"Enter a valid {field_name}."}) from exc


class CreateStorehomeMutation(graphene.Mutation):
    class Arguments:
        name = graphene.String(required=True)
        address = graphene.String(required=True)
        latitude = graphene.String()
        longitude = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    storehome = graphene.Field(StorehomeType)

    def mutate(self, info, name, address, latitude=None, longitude=None):
        request = info.context
        allowed, reason = Storehome.can_create(request)
        if not allowed:
            return CreateStorehomeMutation(ok=False, error=reason)

        storehome = Storehome(name=name, address=address)
        try:
            lat = _parse_coordinate(latitude, "latitude")
            lng = _parse_coordinate(longitude, "longitude")
            if (lat is None) != (lng is None):
                raise ValidationError("Provide both latitude and longitude, or leave both blank.")
            storehome.latitude = lat
            storehome.longitude = lng
            storehome.full_clean()
        except ValidationError as e:
            return CreateStorehomeMutation(ok=False, error=validation_message(storehome, e))
        storehome.save()

        return CreateStorehomeMutation(ok=True, error=None, storehome=storehome)

#Review: 2026-07-29
#Class well structured and comprehensible
class UpdateStorehomeMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        name = graphene.String()
        address = graphene.String()
        latitude = graphene.String()
        longitude = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    storehome = graphene.Field(StorehomeType)

    def mutate(self, info, id, name=None, address=None, latitude=None, longitude=None):
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

        try:
            lat = _parse_coordinate(latitude, "latitude")
            lng = _parse_coordinate(longitude, "longitude")
            if (lat is None) != (lng is None):
                raise ValidationError("Provide both latitude and longitude, or leave both blank.")
            storehome.latitude = lat
            storehome.longitude = lng
            storehome.full_clean()
        except ValidationError as e:
            return UpdateStorehomeMutation(ok=False, error=validation_message(storehome, e))

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

class CreateProductMutation(graphene.Mutation):
    """Creates a product of the chosen type from a barcode, then fills fields via lookup APIs.

    Barcodes must be unique. Lookup failure does not block creation: the row is still saved so
    the user can fill details by hand. A non-null lookup_warning explains what went wrong.
    """

    class Arguments:
        barcode = graphene.String(required=True)
        product_type = graphene.String(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    lookup_warning = graphene.String()
    product = graphene.Field(ProductType)

    def mutate(self, info, barcode, product_type):
        request = info.context

        allowed, reason = Product.can_create(request)
        if not allowed:
            return CreateProductMutation(ok=False, error=reason)

        barcode = (barcode or "").strip()
        if not barcode:
            return CreateProductMutation(ok=False, error="Barcode is required.")

        if product_type not in Product.TypeChoices.values:
            return CreateProductMutation(
                ok=False, error=f"Unknown product type '{product_type}'."
            )

        if Product.objects.filter(barcode=barcode).exists():
            return CreateProductMutation(
                ok=False,
                error=f"A product with barcode '{barcode}' already exists.",
            )

        model = Product.model_for_type(product_type)
        # Blank name/brand let the lookup overwrite them; placeholders fill any gaps after.
        product = model(barcode=barcode, name="", brand="")

        lookup_warning = None
        try:
            product.update_from_lookup(save=False)
        except RuntimeError:
            lookup_warning = (
                "No product data was found for this barcode. "
                "Please fill in the details manually."
            )

        if Product._field_is_blank(product.name):
            product.name = "New Product"
        if Product._field_is_blank(product.brand):
            product.brand = "Unknown"

        try:
            product.full_clean()
        except ValidationError as e:
            return CreateProductMutation(ok=False, error=validation_message(product, e))

        try:
            product.save()
        except IntegrityError:
            return CreateProductMutation(
                ok=False,
                error=f"A product with barcode '{barcode}' already exists.",
            )

        return CreateProductMutation(
            ok=True,
            error=None,
            lookup_warning=lookup_warning,
            product=Product.objects.select_related(*Product.TYPE_SELECT_RELATED).get(
                pk=product.pk
            ),
        )


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
        mark_reviewed = graphene.Boolean(
            description=(
                "Superuser-only. When true, records the caller and timestamp as "
                "having reviewed the product definition; when false, clears that review."
            ),
        )
        life_stages = graphene.String()
        proteins = graphene.List(graphene.NonNull(graphene.String))
        special_diet = graphene.List(graphene.NonNull(graphene.String))
        weight = graphene.Float()
        kibble_size = graphene.String()
        texture = graphene.String()
        treat_size = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    field_errors = graphene.List(graphene.NonNull(FieldErrorType))
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

        if "disallowed" in fields:
            allowed, reason = product.can_edit_disallowed(request)
            if not allowed:
                return UpdateProductMutation(ok=False, error=reason)

        mark_reviewed = fields.pop("mark_reviewed", None)
        if mark_reviewed is not None:
            allowed, reason = product.can_mark_reviewed(request)
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

        if mark_reviewed is True:
            if not target.reviewed_at:
                target.reviewed_at = timezone.now()
                target.reviewed_by = request.user
        elif mark_reviewed is False:
            target.reviewed_at = None
            target.reviewed_by = None

        try:
            target.full_clean()
        except ValidationError as e:
            return UpdateProductMutation(
                ok=False,
                error=validation_message(target, e),
                field_errors=validation_field_errors(target, e),
            )

        target.save()

        #Re-read so the caller sees the saved row, including the refreshed last_updated stamp
        return UpdateProductMutation(
            ok=True,
            error=None,
            product=Product.objects.select_related(*Product.TYPE_SELECT_RELATED).get(pk=target.pk),
        )


class UpdateProductPhotoMutation(graphene.Mutation):
    """Replace a product's photo from a camera/base64 upload.

    Accepts a data URL (`data:image/jpeg;base64,...`) or raw base64. The previous
    stored file is removed before the new one is written.
    """

    class Arguments:
        id = graphene.ID(required=True)
        photo_base64 = graphene.String(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    product = graphene.Field(ProductType)

    def mutate(self, info, id, photo_base64):
        request = info.context

        product = (
            Product.objects.select_related(*Product.TYPE_SELECT_RELATED).filter(pk=id).first()
        )
        if product is None:
            return UpdateProductPhotoMutation(ok=False, error="Product not found.")

        allowed, reason = product.can_edit(request)
        if not allowed:
            return UpdateProductPhotoMutation(ok=False, error=reason)

        target = product.specific
        try:
            data, content_type = parse_photo_base64(photo_base64)
        except ValueError as exc:
            return UpdateProductPhotoMutation(ok=False, error=str(exc))

        if target.photo:
            target.photo.delete(save=False)

        filename = photo_upload_filename(target, content_type)
        target.photo.save(filename, ContentFile(data), save=True)

        return UpdateProductPhotoMutation(
            ok=True,
            error=None,
            product=Product.objects.select_related(*Product.TYPE_SELECT_RELATED).get(
                pk=target.pk
            ),
        )


class DeleteProductMutation(graphene.Mutation):
    """Removes a product and its subclass rows when no storehome still holds stock."""

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

        #Deleting the base row cascades through the Food/Kibble/Canned/Treats tables that inherit it
        product.delete()

        return DeleteProductMutation(ok=True, error=None)


class ForceProductLookupUpdateMutation(graphene.Mutation):
    """Superuser tool: optionally blank non-barcode fields and re-run ProductUpdater.

    Updater mapping only fills blank fields, so blank_fields=True is required for a
    full rescan overwrite. Fields are cleared only after an updater successfully
    returns data; a failed lookup keeps existing details. reset=True clears the
    once-per-day updater cooldown.
    """

    class Arguments:
        id = graphene.ID(required=True)
        blank_fields = graphene.Boolean(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    lookup_warning = graphene.String()
    product = graphene.Field(ProductType)

    def mutate(self, info, id, blank_fields):
        request = info.context

        product = (
            Product.objects.select_related(*Product.TYPE_SELECT_RELATED).filter(pk=id).first()
        )
        if product is None:
            return ForceProductLookupUpdateMutation(ok=False, error="Product not found.")

        allowed, reason = product.can_force_lookup_update(request)
        if not allowed:
            return ForceProductLookupUpdateMutation(ok=False, error=reason)

        try:
            lookup_warning = product.force_update_from_lookup(blank_fields=blank_fields)
        except ValidationError as e:
            return ForceProductLookupUpdateMutation(
                ok=False,
                error=validation_message(product.specific, e),
            )

        return ForceProductLookupUpdateMutation(
            ok=True,
            error=None,
            lookup_warning=lookup_warning,
            product=Product.objects.select_related(*Product.TYPE_SELECT_RELATED).get(
                pk=product.pk
            ),
        )


class CancelIncomingIntakeMutation(graphene.Mutation):
    """Signals that an intake wizard pass was abandoned before stock was received.

    The server decides whether to discard an unused product create (no stock, no
    prior movement history, recently written). Callers should treat cleanup as
    best-effort: cancel always returns ok unless the user cannot manage intake.
    """

    class Arguments:
        product_id = graphene.ID(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    discarded = graphene.Boolean()

    def mutate(self, info, product_id):
        _storehome, error = require_managed_storehome(info.context)
        if error:
            return CancelIncomingIntakeMutation(ok=False, error=error, discarded=False)

        product, error = load_product(product_id)
        if error:
            # Already gone — nothing to clean up.
            return CancelIncomingIntakeMutation(ok=True, error=None, discarded=False)

        discarded = product.discard_if_abandoned_intake(info.context)
        return CancelIncomingIntakeMutation(ok=True, error=None, discarded=discarded)


def parse_optional_date(value):
    """Accept YYYY-MM-DD strings from GraphQL clients; blank means no date."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Expiry date must be YYYY-MM-DD.") from exc


def require_managed_storehome(request, *, check_inventory_perm=True):
    """Return (storehome, None) or (None, error) for storehome-manager GraphQL ops."""
    allowed, reason = UserProfile.can_manage_storehome_inventory(request)
    if not allowed:
        return None, reason

    storehome = request.user.managed_storehome
    if storehome is None:
        return None, "You are not assigned to manage a storehome."

    if check_inventory_perm:
        allowed, reason = storehome.can_manage_inventory(request)
        if not allowed:
            return None, reason

    return storehome, None


def load_product(product_id):
    """Return (product, None) or (None, error) for a product primary key."""
    product = (
        Product.objects.select_related(*Product.TYPE_SELECT_RELATED)
        .filter(pk=product_id)
        .first()
    )
    if product is None:
        return None, "Product not found."
    return product, None


def paginate_queryset(queryset, page, page_size, page_type, *, transform_items=None):
    """Build a ProductPageType / StorageItemPageType-style page object."""
    if not page_size or page_size < 1:
        page_size = DEFAULT_PRODUCT_PAGE_SIZE
    paginator = Paginator(queryset, min(page_size, MAX_PRODUCT_PAGE_SIZE))
    # get_page sends anything past the end to the last page, so a stale page
    # number degrades instead of erroring. Numbers below 1 are treated as past
    # the end, hence the max().
    current_page = paginator.get_page(max(page or 1, 1))
    items = list(current_page.object_list)
    if transform_items is not None:
        items = transform_items(items)
    return page_type(
        items=items,
        total_count=paginator.count,
        page=current_page.number,
        page_size=paginator.per_page,
        total_pages=paginator.num_pages,
        has_previous=current_page.has_previous(),
        has_next=current_page.has_next(),
    )


class ReceiveInventoryMutation(graphene.Mutation):
    """Adds stock of a product to the caller's managed storehome.

    Lookup is by product id. The storehome always comes from the caller's
    managed_storehome assignment — managers cannot receive into other homes.
    """

    class Arguments:
        product_id = graphene.ID(required=True)
        quantity = graphene.Int(required=True)
        expiry_date = graphene.String(required=True)
        note = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    storage_item = graphene.Field(StorageItemType)

    def mutate(self, info, product_id, quantity, expiry_date, note=None):
        storehome, error = require_managed_storehome(info.context)
        if error:
            return ReceiveInventoryMutation(ok=False, error=error)

        if quantity is None or quantity < 1:
            return ReceiveInventoryMutation(ok=False, error="Quantity must be at least 1.")

        product, error = load_product(product_id)
        if error:
            return ReceiveInventoryMutation(ok=False, error=error)

        try:
            parsed_expiry = parse_optional_date(expiry_date)
        except ValueError as e:
            return ReceiveInventoryMutation(ok=False, error=str(e))

        if parsed_expiry is None:
            return ReceiveInventoryMutation(ok=False, error="Expiry date is required.")

        try:
            item = StorageItem.receive(
                storehome=storehome,
                product=product,
                quantity=quantity,
                expiry_date=parsed_expiry,
                note=note or "",
            )
        except ValueError as e:
            return ReceiveInventoryMutation(ok=False, error=str(e))

        return ReceiveInventoryMutation(ok=True, error=None, storage_item=item)


class UpdateStorageItemQuantityMutation(graphene.Mutation):
    """Update quantity, expiry date, and note on a stock lot at the caller's storehome.

    Quantity 0 deletes the lot. Managers may only adjust lots at their own storehome.
    """

    class Arguments:
        id = graphene.ID(required=True)
        quantity = graphene.Int(required=True)
        expiry_date = graphene.String()
        note = graphene.String()

    ok = graphene.Boolean()
    error = graphene.String()
    deleted = graphene.Boolean()
    storage_item = graphene.Field(StorageItemType)

    def mutate(self, info, id, quantity, expiry_date=None, note=None):
        storehome, error = require_managed_storehome(info.context)
        if error:
            return UpdateStorageItemQuantityMutation(
                ok=False, error=error, deleted=False
            )

        item = StorageItem.objects.filter(pk=id, storehome=storehome).first()
        if item is None:
            return UpdateStorageItemQuantityMutation(
                ok=False,
                error="Stock lot not found.",
                deleted=False,
            )

        if quantity is None or quantity < 0:
            return UpdateStorageItemQuantityMutation(
                ok=False,
                error="Quantity cannot be negative.",
                deleted=False,
            )

        if quantity == 0:
            try:
                item.set_quantity(0)
            except ValueError as e:
                return UpdateStorageItemQuantityMutation(ok=False, error=str(e), deleted=False)
            return UpdateStorageItemQuantityMutation(
                ok=True, error=None, deleted=True, storage_item=None
            )

        try:
            parsed_expiry = parse_optional_date(expiry_date)
        except ValueError as e:
            return UpdateStorageItemQuantityMutation(ok=False, error=str(e), deleted=False)

        if parsed_expiry is None:
            return UpdateStorageItemQuantityMutation(
                ok=False,
                error="Expiry date is required.",
                deleted=False,
            )

        try:
            updated = item.update_lot(
                quantity=quantity,
                expiry_date=parsed_expiry,
                note=note or "",
            )
        except ValueError as e:
            return UpdateStorageItemQuantityMutation(ok=False, error=str(e), deleted=False)

        return UpdateStorageItemQuantityMutation(
            ok=True, error=None, deleted=False, storage_item=updated
        )


class OuttakeInventoryMutation(graphene.Mutation):
    """Removes stock for a product expiry date at the caller's managed storehome.

    When multiple lots share the same expiry, units are taken from them in lot
    order until the requested quantity is removed. Empty lots are deleted.

    Past keep-by date and disallowed products produce warnings only — outtake
    is still allowed so disposal and other removals can proceed.
    """

    class Arguments:
        product_id = graphene.ID(required=True)
        expiry_date = graphene.String()
        quantity = graphene.Int(required=True)

    ok = graphene.Boolean()
    error = graphene.String()
    warnings = graphene.List(graphene.NonNull(graphene.String))
    quantity_removed = graphene.Int()
    remaining_quantity = graphene.Int()
    product = graphene.Field(ProductType)

    def mutate(self, info, product_id, quantity, expiry_date=None):
        def fail(error):
            return OuttakeInventoryMutation(
                ok=False,
                error=error,
                warnings=[],
                quantity_removed=0,
                remaining_quantity=0,
            )

        storehome, error = require_managed_storehome(info.context)
        if error:
            return fail(error)

        if quantity is None or quantity < 1:
            return fail("Quantity must be at least 1.")

        product, error = load_product(product_id)
        if error:
            return fail(error)

        try:
            parsed_expiry = parse_optional_date(expiry_date)
        except ValueError as e:
            return fail(str(e))

        warnings = StorageItem.warnings_for(product, parsed_expiry, "outtake")

        try:
            removed, remaining = StorageItem.outtake_by_expiry(
                storehome=storehome,
                product=product,
                expiry_date=parsed_expiry,
                quantity=quantity,
            )
        except ValueError as e:
            return fail(str(e))

        return OuttakeInventoryMutation(
            ok=True,
            error=None,
            warnings=warnings,
            quantity_removed=removed,
            remaining_quantity=remaining,
            product=product,
        )


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
        has_stock=graphene.Boolean(),
        not_yet_reviewed=graphene.Boolean(),
        sort=graphene.String(),
        page=graphene.Int(),
        page_size=graphene.Int(),
    )
    storehome_inventory = graphene.Field(
        StorageItemPageType,
        search=graphene.String(),
        product_type=graphene.String(),
        needs_disposal=graphene.Boolean(),
        protein=graphene.String(),
        life_stage=graphene.String(),
        special_diet=graphene.String(),
        sort=graphene.String(),
        page=graphene.Int(),
        page_size=graphene.Int(),
        description=(
            "Storage lots on hand in the caller's managed storehome. "
            "Search and filters apply to each lot's product; sort uses product columns. "
            "needsDisposal limits to lots past keep date or with a disallowed product."
        ),
    )
    product = graphene.Field(ProductType, id=graphene.ID(required=True))
    product_by_barcode = graphene.Field(
        ProductType, barcode=graphene.String(required=True)
    )
    product_filter_options = graphene.Field(ProductFilterOptionsType)
    product_choices = graphene.Field(ProductChoicesType)
    recent_incoming = graphene.List(
        graphene.NonNull(StorageItemType),
        required=True,
        description="Storage lots received into the caller's managed storehome within the last 24 hours.",
    )
    storehome_stock_by_barcode = graphene.List(
        graphene.NonNull(StorageItemType),
        barcode=graphene.String(required=True),
        required=True,
        description=(
            "On-hand lots for a barcode at the caller's managed storehome, "
            "sorted with the soonest expiry date first."
        ),
    )

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
        has_stock=None,
        not_yet_reviewed=None,
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
            has_stock=has_stock,
            not_yet_reviewed=not_yet_reviewed,
            sort=sort,
        )
        return paginate_queryset(queryset, page, page_size, ProductPageType)

    def resolve_storehome_inventory(
        self,
        info,
        search=None,
        product_type=None,
        needs_disposal=None,
        protein=None,
        life_stage=None,
        special_diet=None,
        sort=DEFAULT_PRODUCT_SORT,
        page=1,
        page_size=DEFAULT_PRODUCT_PAGE_SIZE,
    ):
        storehome, error = require_managed_storehome(
            info.context, check_inventory_perm=False
        )
        if error:
            raise GraphQLError(error)

        queryset = filtered_storage_items(
            storehome,
            search=search,
            product_type=product_type,
            needs_disposal=needs_disposal,
            protein=protein,
            life_stage=life_stage,
            special_diet=special_diet,
            sort=sort,
        )
        return paginate_queryset(
            queryset,
            page,
            page_size,
            StorageItemPageType,
            transform_items=attach_products_to_storage_items,
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

    def resolve_product_by_barcode(self, info, barcode):
        allowed, reason = Product.can_view(info.context)
        if not allowed:
            raise GraphQLError(reason)

        barcode = (barcode or "").strip()
        if not barcode:
            raise GraphQLError("Barcode is required.")

        # Null when the barcode is new — callers start a create flow rather than erroring.
        return (
            Product.objects.select_related(*Product.TYPE_SELECT_RELATED)
            .filter(barcode=barcode)
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
            treat_sizes=enum_choices(Treats.TreatSizeChoices),
        )

    def resolve_recent_incoming(self, info):
        storehome, error = require_managed_storehome(
            info.context, check_inventory_perm=False
        )
        if error:
            raise GraphQLError(error)
        if storehome is None:
            return []

        since = timezone.now() - timedelta(hours=24)
        return list(
            StorageItem.objects.filter(storehome=storehome, date_stored__gte=since)
            .select_related("storehome")
            .order_by("-date_stored", "-pk")
        )

    def resolve_storehome_stock_by_barcode(self, info, barcode):
        storehome, error = require_managed_storehome(
            info.context, check_inventory_perm=False
        )
        if error:
            raise GraphQLError(error)

        barcode = (barcode or "").strip()
        if not barcode:
            raise GraphQLError("Barcode is required.")

        product = (
            Product.objects.select_related(*Product.TYPE_SELECT_RELATED)
            .filter(barcode=barcode)
            .first()
        )
        if product is None:
            return []

        items = list(StorageItem.lots_for_product(storehome, product))
        return attach_products_to_storage_items(items)

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
    create_product = CreateProductMutation.Field()
    update_product = UpdateProductMutation.Field()
    update_product_photo = UpdateProductPhotoMutation.Field()
    delete_product = DeleteProductMutation.Field()
    force_product_lookup_update = ForceProductLookupUpdateMutation.Field()
    receive_inventory = ReceiveInventoryMutation.Field()
    cancel_incoming_intake = CancelIncomingIntakeMutation.Field()
    update_storage_item_quantity = UpdateStorageItemQuantityMutation.Field()
    outtake_inventory = OuttakeInventoryMutation.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)

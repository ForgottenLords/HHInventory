from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
import logging
import mimetypes
import re

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import (Count, DecimalField, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Sum, Value)
from django.db.models.functions import Coalesce, TruncMonth
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from multiselectfield import MultiSelectField

logger = logging.getLogger(__name__)


def add_calendar_months(value, months):
    """Shift a date by whole calendar months, clamping the day to the target month."""
    if value is None:
        return None
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def month_key(value):
    """YYYY-MM key for histogram bucketing."""
    if value is None:
        return None
    return f"{value.year:04d}-{value.month:02d}"


def parse_month_key(key):
    year, month = key.split("-")
    return date(int(year), int(month), 1)


class Product(models.Model):
    class TypeChoices(models.TextChoices):
        OTHER = "OTHER", _("Other")
        FOOD = "FOOD", _("Food (Unspecified)")
        KIBBLE = "KIBBLE", _("Kibble")
        CANNED = "CANNED", _("Canned")

    #Narrows a Product queryset to the rows whose most specific subclass is the keyed type
    TYPE_QUERY_FILTERS = {
        TypeChoices.OTHER.value: {"food__isnull": True},
        TypeChoices.FOOD.value: {
            "food__isnull": False,
            "food__kibble__isnull": True,
            "food__canned__isnull": True,
        },
        TypeChoices.KIBBLE.value: {"food__kibble__isnull": False},
        TypeChoices.CANNED.value: {"food__canned__isnull": False},
    }

    #Pulls the subclass rows in the same query, so product_type costs no extra queries per row
    TYPE_SELECT_RELATED = ("food__kibble", "food__canned")

    # Preferred JSON keys for updater payloads, best match first.
    UPDATER_NAME_KEYS = ("title", "name", "product_name", "productname", "item_name", "itemname")
    UPDATER_BRAND_KEYS = ("brand", "brand_name", "brandname", "manufacturer", "company", "make")
    UPDATER_PRICE_KEYS = (
        "lowest_recorded_price",
        "lowestrecordedprice",
        "price",
        "list_price",
        "listprice",
        "sale_price",
        "saleprice",
        "estimated_price",
        "estimatedprice",
    )
    UPDATER_NOTES_KEYS = (
        "description",
        "product_description",
        "productdescription",
        "long_description",
        "longdescription",
        "details",
        "summary",
        "about",
        "notes",
    )
    # Go-UPC uses imageUrl; UPCitemdb uses images (array of URLs).
    UPDATER_PHOTO_KEYS = (
        "imageUrl",
        "images",
        "image",
        "photo",
        "photoUrl",
        "thumbnail",
        "thumbnailUrl",
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    barcode = models.CharField(max_length=16, verbose_name="Barcode", unique=True)
    brand = models.CharField(max_length=100, verbose_name="Brand/Company")
    country_of_origin = models.CharField(max_length=100, blank=True, verbose_name="Country of Origin")
    photo = models.ImageField(upload_to="products/", blank=True, null=True, verbose_name="Photo")
    estimated_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
        verbose_name="Estimated Price",
    )
    notes = models.TextField(blank=True, verbose_name="Notes")
    disallowed = models.BooleanField(default=False, verbose_name="Disallowed", help_text="If set, Storehomes will not be able to intake this Product.")
    in_production = models.BooleanField(default=True, verbose_name="In Production")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_products", verbose_name="Reviewed By")
    reviewed_at = models.DateTimeField(blank=True, null=True, verbose_name="Reviewed At", help_text="Marks that a superuser has reviewed the entire product description for accuracy. Once reviewed, non-superusers cannot edit the product definition")
    last_updated = models.DateTimeField(auto_now=True, verbose_name="Last Updated")
    data_warnings = models.JSONField(default=list, blank=True, null=True, verbose_name="Data Warnings")
    updater_class = models.CharField(max_length=200, blank=True, null=True, verbose_name="Updater Class")
    updater_data = models.JSONField(blank=True, null=True, verbose_name="Updater Data")
    updater_last_updated = models.DateTimeField(blank=True, null=True, verbose_name="Updater Last Updated")

    def subclass_or_none(self, accessor):
        """
        Return a subclass of a product if the accessor exists, otherwise return None.
        Works for all levels of the inheritance hierarchy.
        """
        try:
            return getattr(self, accessor)
        except ObjectDoesNotExist:
            return None

    @property
    def is_reviewed(self):
        """True when a superuser has recorded a product-definition review."""
        return self.reviewed_at is not None

    @property
    def specific(self):
        """The most derived instance behind this row, which owns every field the product has.

        Saving it writes each inherited table at once, so an edit cannot leave the Product row
        and its Food/Kibble/Canned rows disagreeing. Works for rows loaded as Product and for
        instances already constructed as Food/Kibble/Canned (including unsaved drafts).
        """
        if isinstance(self, (Kibble, Canned)):
            return self
        if isinstance(self, Food):
            return (
                self.subclass_or_none("kibble")
                or self.subclass_or_none("canned")
                or self
            )
        food = self.subclass_or_none("food")
        if food is None:
            return self
        return (
            food.subclass_or_none("kibble")
            or food.subclass_or_none("canned")
            or food
        )

    @property
    def product_type(self):
        specific = self.specific
        if isinstance(specific, Kibble):
            return self.TypeChoices.KIBBLE
        if isinstance(specific, Canned):
            return self.TypeChoices.CANNED
        if isinstance(specific, Food):
            return self.TypeChoices.FOOD
        return self.TypeChoices.OTHER

    @classmethod
    def model_for_type(cls, product_type):
        """The concrete model that should own a new row of the given TypeChoices value."""
        try:
            return PRODUCT_TYPE_MODELS[product_type]
        except KeyError as exc:
            raise ValueError(f"Unknown product type '{product_type}'.") from exc

    @classmethod
    def _is_storehome_manager(cls, request):
        return bool(getattr(request.user, "managed_storehome_id", None))

    @classmethod
    def can_view(cls, request):
        if request.user.has_perm("core.view_product"):
            return True, ""
        # Storehome managers need library lookups while receiving inventory.
        if cls._is_storehome_manager(request):
            return True, ""
        return False, "You do not have permission to view Products"

    @classmethod
    def can_view_library(cls, request):
        """Access to Product Library / detail pages (stricter than can_view)."""
        if request.user.has_perm("core.view_product"):
            return True, ""
        return False, "You do not have permission to view Products"

    @classmethod
    def can_create(cls, request):
        if request.user.has_perm("core.add_product"):
            return True, ""
        # Managers may add missing products when receiving inventory.
        if cls._is_storehome_manager(request):
            return True, ""
        return False, "You do not have permission to create Products"

    def can_edit(self, request):
        if self.is_reviewed and not request.user.is_superuser:
            return False, "This product has been reviewed and can only be edited by a superuser."
        if request.user.has_perm("core.change_product"):
            return True, ""
        if self._is_storehome_manager(request):
            return True, ""
        return False, "You do not have permission to edit this Product"

    def can_edit_disallowed(self, request):
        if request.user.is_superuser:
            return True, ""
        return False, "Only superusers may change whether a product is disallowed."

    def can_mark_reviewed(self, request):
        if request.user.is_superuser:
            return True, ""
        return False, "Only superusers may mark a product as reviewed."

    def can_force_lookup_update(self, request):
        if not request.user.is_superuser:
            return False, "Only superusers may force a Product Updater rescan."
        if self.is_reviewed:
            return False, (
                "Reviewed products cannot be rescanned. "
                "Clear the Product Reviewed flag before forcing a rescan."
            )
        return self.can_edit(request)

    def has_storehome_stock(self):
        """True when any storehome still holds a StorageItem lot for this product."""
        return StorageItem.objects.filter(
            content_type__in=StorageItem.product_content_types(),
            object_id=self.pk,
        ).exists()

    def can_delete(self, request):
        if self.has_storehome_stock():
            return False, "This product still has stock in one or more storehomes."
        if request.user.has_perm("core.delete_product"):
            return True, ""
        # Managers may discard empty products (e.g. abandoned intake creates).
        if self._is_storehome_manager(request) and self.last_updated > timezone.now() - timedelta(hours=1):
            return True, ""
        return False, "You do not have permission to delete this Product"

    def discard_if_abandoned_intake(self, request):
        """Delete this product when a cancelled intake left behind an unused create.

        Returns True when the row was removed. Safe no-op when the product has
        stock, prior intake/outtake history, was not written recently, or the
        caller may not delete it.
        """
        if self.has_storehome_stock():
            return False
        if StorageItemIntake.objects.filter(product_id=self.pk).exists():
            return False
        if StorageItemOuttake.objects.filter(product_id=self.pk).exists():
            return False
        allowed, _reason = self.can_delete(request)
        if not allowed:
            return False
        self.delete()
        return True

    def photo_upload_filename(self, content_type):
        """Name a stored photo from barcode, timestamp, and MIME type."""
        if self.barcode in ["", None]:
            raise ValueError("Unexpected Error MISSING_BARCODE")
        mime = (content_type or "").split(";", 1)[0].strip()
        ext = mimetypes.guess_extension(mime, strict=False) or ".jpg"
        if ext == ".jpe":
            ext = ".jpg"
        stem = re.sub(r"[^a-zA-Z0-9_-]", "", str(self.barcode))[:40]
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        return f"{stem}_{stamp}{ext}"

    def update_from_lookup(self, reset=False, save=True, blank_before_apply=False):
        """Try each ProductUpdater in order until one succeeds, or raise if all fail.

        On success the winning updater has already written updater_data / updater_class
        onto this product. Returns that updater instance. Pass save=False to fill an
        unsaved draft without writing to the database.

        When blank_before_apply is True, fillable fields are cleared only after an
        updater successfully returns data (see ProductUpdater.update_product).
        """
        # Imported here so inventory and product_lookup do not import each other at load time.
        from .product_lookup import PRODUCT_UPDATERS

        if reset:
            self.updater_data = None
            self.updater_class = None
            self.updater_last_updated = None
            if save:
                self.save()

        errors = []
        for updater_cls in PRODUCT_UPDATERS:
            try:
                updater = updater_cls(self)
                updater.update_product(save=save, blank_before_apply=blank_before_apply)
                return updater
            except RuntimeError as exc:
                errors.append(f"{updater_cls.__name__}: {exc}")

        raise RuntimeError(
            f"All product updaters failed for barcode {self.barcode}: "
            + "; ".join(errors)
        )

    def blank_for_lookup_rescan(self):
        """Clear fillable fields owned by this model layer so a lookup can rewrite them.

        Keeps barcode, operational flags (disallowed, in_production), and review
        metadata (reviewed_by, reviewed_at). Call on the leaf instance so
        Food/Kibble/Canned fields are cleared too.
        """
        self.name = ""
        self.brand = ""
        self.country_of_origin = ""
        self.estimated_price = None
        self.notes = ""
        if self.photo:
            self.photo.delete(save=False)

    def force_update_from_lookup(self, blank_fields=True):
        """Reset updater state and re-run ProductUpdater.

        When blank_fields is True, fillable fields are cleared only after an updater
        successfully returns data, then rewritten from that payload. A failed lookup
        leaves existing product details intact.

        Returns a lookup_warning string when every updater fails, otherwise None.
        Name/brand placeholders match CreateProductMutation so the row stays valid.
        """
        leaf = self.specific

        lookup_warning = None
        try:
            leaf.update_from_lookup(
                reset=True, save=False, blank_before_apply=blank_fields
            )
        except RuntimeError:
            lookup_warning = (
                "No product data was found for this barcode. "
                "Please fill in the details manually."
            )

        if Product._field_is_blank(leaf.name):
            leaf.name = "New Product"
        if Product._field_is_blank(leaf.brand):
            leaf.brand = "Unknown"

        leaf.full_clean()
        leaf.save()
        return lookup_warning

    def _apply_updater_fields(self, data, applied, updater):
        """Map name / brand / estimated_price / notes / photo from preferred JSON keys onto blank fields."""

        def _coerce_price(raw):
            if raw is None or isinstance(raw, bool):
                return None
            if isinstance(raw, Decimal):
                return raw
            if isinstance(raw, (int, float)):
                return Decimal(str(raw))
            text = str(raw).strip()
            if not text:
                return None
            # Pull the first currency-looking number out of free text.
            match = re.search(r"[-+]?\d[\d,]*\.?\d*", text.replace(",", ""))
            if not match:
                return None
            try:
                return Decimal(match.group(0))
            except InvalidOperation:
                return None

        name = updater._find_by_preferred_keys(data, self.UPDATER_NAME_KEYS)
        if name is not None and updater._is_blank(self.name):
            self.name = str(name).strip()[:200]
            applied["name"] = self.name

        brand = updater._find_by_preferred_keys(data, self.UPDATER_BRAND_KEYS)
        if brand is not None and updater._is_blank(self.brand):
            self.brand = str(brand).strip()[:100]
            applied["brand"] = self.brand

        raw_price, price_currency = updater._find_price_and_currency(
            data, self.UPDATER_PRICE_KEYS
        )
        price = _coerce_price(raw_price)
        if price is not None and self.estimated_price is None:
            try:
                price = updater.convert_price_to_cad(price, price_currency)
            except Exception as exc:
                # Skip foreign-currency prices we cannot convert rather than
                # storing a non-CAD amount in estimated_price.
                logger.warning(
                    "Skipping updater price %s (%s) for barcode %s: %s",
                    raw_price,
                    price_currency or "USD",
                    self.barcode,
                    exc,
                )
                price = None
            if price is not None:
                self.estimated_price = price
                applied["estimated_price"] = self.estimated_price

        notes = updater._find_by_preferred_keys(data, self.UPDATER_NOTES_KEYS)
        if notes is not None and updater._is_blank(self.notes):
            # Descriptions from lookup APIs often include markup; keep plain text only.
            self.notes = unescape(strip_tags(str(notes))).strip()
            applied["notes"] = self.notes

        # Photo download is best-effort: a bad URL must not undo the other mapped fields.
        photo_url = updater._find_photo_url(data, self.UPDATER_PHOTO_KEYS)
        if photo_url and updater._is_blank(self.photo):
            try:
                photo_file = updater.download_photo(photo_url)
            except Exception as exc:
                photo_file = None
            if photo_file is not None:
                self.photo.save(photo_file.name, photo_file, save=False)
                applied["photo"] = self.photo.name

    @staticmethod
    def _field_is_blank(value):
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        if isinstance(value, (list, tuple, set)) and len(value) == 0:
            return True
        return False

    def identify_data_warnings(self):
        """Reset data_warnings and record issues for fields owned by this model layer."""
        self.data_warnings = []
        if self._field_is_blank(self.name):
            self.data_warnings.append("Missing Product Name")
        if self._field_is_blank(self.brand):
            self.data_warnings.append("Missing Brand Info")
        if self.estimated_price is None:
            self.data_warnings.append("Missing Price Information")
        if self.disallowed:
            self.data_warnings.append("Disallowed")

    @classmethod
    def library_quality_stats(cls, queryset=None):
        """Counts of Product Library rows with data-quality issues.

        Uses the same field rules as identify_data_warnings().
        """
        qs = cls.objects.all() if queryset is None else queryset
        blank_text = Q(name="") | Q(name__isnull=True)
        blank_brand = Q(brand="") | Q(brand__isnull=True)
        missing_protein_q = Q(proteins__isnull=True) | Q(proteins="")
        # Food/Kibble pk matches Product.pk under multi-table inheritance.
        return {
            "product_count": qs.count(),
            "missing_price_products": qs.filter(estimated_price__isnull=True).count(),
            "missing_name_products": qs.filter(blank_text).count(),
            "missing_brand_products": qs.filter(blank_brand).count(),
            "missing_protein_products": Food.objects.filter(
                pk__in=qs.values("pk")
            )
            .filter(missing_protein_q)
            .count(),
            "missing_bag_weight_products": Kibble.objects.filter(
                pk__in=qs.values("pk"), weight__isnull=True
            ).count(),
            "disallowed_products": qs.filter(disallowed=True).count(),
        }

    def save(self, *args, **kwargs):
        # Run on the leaf so Food/Kibble checks fire even when save() was called on Product.
        leaf = self.specific
        leaf.identify_data_warnings()
        if leaf is not self:
            self.data_warnings = leaf.data_warnings
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = list(set(update_fields) | {"data_warnings"})
        super().save(*args, **kwargs)


@receiver(pre_delete, sender=Product)
def delete_product_photo_file(sender, instance, **kwargs):
    """Remove the stored image; Django does not delete ImageField files on its own."""
    if instance.photo:
        instance.photo.delete(save=False)


class Food(Product):
    class ProteinChoices(models.TextChoices):
        CHICKEN = "CHKN", _("Chicken")
        BEEF = "BEEF", _("Beef")
        BISON = "BSON", _("Bison")
        DUCK = "DUCK", _("Duck")
        FISH = "FISH", _("Fish (Unspecified)")
        HAKE = "HAKE", _("Hake")
        KANGAROO = "KANG", _("Kangaroo")
        LAMB = "LAMB", _("Lamb")
        PORK = "PORK", _("Pork")
        RABBIT = "RBBT", _("Rabbit")
        SALMON = "SALM", _("Salmon")
        TROUT = "TROT", _("Trout")
        TURKEY = "TURK", _("Turkey")
        VENISON = "VENI", _("Venison")
        WHITEFISH = "WTFS", _("Whitefish")
        OTHER = "OTHR", _("Other")

    class LifeStageChoices(models.TextChoices):
        UNKNOWN = "UNK", _("Unknown")
        PUPPY = "PUP", _("Puppy")
        ADULT = "ADL", _("Adult")
        PUPPY_TO_ADULT = "PTA", _("Puppy To Adult")
        ALL_STAGES = "ALL", _("All-Stages")
        SENIOR = "SNR", _("Senior")

    class SpecialDietChoices(models.TextChoices):
        SENSITIVE_STOMACH = "SENS", _("Sensitive Stomach")
        DENTAL_HEALTH = "DENT", _("Dental Health")
        SKIN_AND_COAT = "SKIN", _("Skin & Coat")
        LIMITED_INGREDIENT = "LIMI", _("Limited Ingredient")
        GRAIN_FREE = "GRAI", _("Grain-Free")

    # Extra phrases scanned in updater JSON beyond each choice's official label.
    PROTEIN_UPDATER_ALIASES = {
        ProteinChoices.FISH: ("Fish", "Seafood"),
        ProteinChoices.WHITEFISH: ("White Fish", "White-Fish"),
        ProteinChoices.CHICKEN: ("Poultry",),
        ProteinChoices.SALMON: ("Atlantic Salmon",),
        ProteinChoices.HAKE: ("Cape Hake", "Pacific Hake", "Silver Hake"),
        ProteinChoices.TROUT: ("Rainbow Trout", "Ocean Trout", "Steelhead Trout", "Steelhead"),
        ProteinChoices.KANGAROO: ("Roo",),
    }
    SPECIAL_DIET_UPDATER_ALIASES = {
        SpecialDietChoices.GRAIN_FREE: ("Grain Free", "Grainfree", "No Grain"),
        SpecialDietChoices.SKIN_AND_COAT: ("Skin and Coat", "Skin Coat"),
        SpecialDietChoices.LIMITED_INGREDIENT: ("Limited Ingredients", "LID"),
        SpecialDietChoices.SENSITIVE_STOMACH: ("Sensitive Digestion", "Easy Digestion"),
        SpecialDietChoices.DENTAL_HEALTH: ("Dental Care", "Oral Care"),
    }
    LIFE_STAGE_UPDATER_ALIASES = {
        # Longer compound phrases win over bare Puppy/Adult via longest-match.
        LifeStageChoices.PUPPY_TO_ADULT: (
            "Puppy to Adult",
            "From Puppy to Adult",
            "Puppy & Adult",
            "Puppy and Adult",
            "Puppies and Adults",
            "Puppies & Adults",
            "Growth and Maintenance",
            "Growth & Maintenance",
        ),
        LifeStageChoices.PUPPY: ("Puppies", "Growth", "For Puppies", "Puppy Formula"),
        LifeStageChoices.ADULT: ("Adults", "Maintenance", "For Adults", "Adult Formula"),
        LifeStageChoices.ALL_STAGES: (
            "All Stages",
            "All Life Stages",
            "All Life Stage",
            "All Ages",
            "Every Life Stage",
        ),
        LifeStageChoices.SENIOR: ("Seniors", "Mature", "For Seniors", "7+", "Elder", "Senior Formula"),
    }

    life_stages = models.CharField(max_length=3, choices=LifeStageChoices.choices, default=LifeStageChoices.UNKNOWN, verbose_name="Life Stage")
    proteins = MultiSelectField(choices=ProteinChoices.choices, blank=True, verbose_name="Proteins")
    special_diet = MultiSelectField(choices=SpecialDietChoices.choices, blank=True, verbose_name="Special Diet")

    def identify_data_warnings(self):
        super().identify_data_warnings()
        if self._field_is_blank(self.proteins):
            self.data_warnings.append("Missing Protein Info")

    def _apply_updater_fields(self, data, applied, updater):
        super()._apply_updater_fields(data, applied, updater)

        # API titles/names often embed these attributes; always include the
        # resolved product name even when it was already on the row.
        scan_data = data
        if not updater._is_blank(self.name):
            scan_data = {"payload": data, "name": self.name}

        # Skip OTHER — "Other" is too common in prose and is manual-only.
        protein_choices = [
            choice
            for choice in self.ProteinChoices.choices
            if choice[0] != self.ProteinChoices.OTHER
        ]
        proteins = updater._find_choice_labels_in_data(
            scan_data, protein_choices, self.PROTEIN_UPDATER_ALIASES
        )
        if proteins and updater._is_blank(self.proteins):
            self.proteins = proteins
            applied["proteins"] = list(self.proteins)

        diets = updater._find_choice_labels_in_data(
            scan_data, self.SpecialDietChoices.choices, self.SPECIAL_DIET_UPDATER_ALIASES
        )
        if diets and updater._is_blank(self.special_diet):
            self.special_diet = diets
            applied["special_diet"] = list(self.special_diet)

        # Skip UNKNOWN — matching "Unknown" in text is useless, and UNK is the fillable default.
        life_stage_choices = [
            choice
            for choice in self.LifeStageChoices.choices
            if choice[0] != self.LifeStageChoices.UNKNOWN
        ]
        matched_stages = updater._find_choice_labels_in_data(
            scan_data, life_stage_choices, self.LIFE_STAGE_UPDATER_ALIASES
        )
        life_stage = None
        if matched_stages:
            matched_set = set(matched_stages)
            # Explicit compound hit, or Puppy + Adult as separate phrases → Puppy To Adult.
            if self.LifeStageChoices.PUPPY_TO_ADULT in matched_set or (
                self.LifeStageChoices.PUPPY in matched_set
                and self.LifeStageChoices.ADULT in matched_set
            ):
                life_stage = self.LifeStageChoices.PUPPY_TO_ADULT
            else:
                life_stage = matched_stages[0]
        if life_stage and (
            updater._is_blank(self.life_stages)
            or self.life_stages == self.LifeStageChoices.UNKNOWN
        ):
            self.life_stages = life_stage
            applied["life_stages"] = self.life_stages

    def blank_for_lookup_rescan(self):
        super().blank_for_lookup_rescan()
        self.life_stages = self.LifeStageChoices.UNKNOWN
        self.proteins = []
        self.special_diet = []


class Kibble(Food):
    class KibbleSizeChoices(models.TextChoices):
        SMALL = "SM", _("Small")
        MEDIUM = "MD", _("Standard/Unspecified")
        LARGE = "LG", _("Large")

    #The unit weight is stored in. Named here rather than in each template, so a client showing
    #the number and a client showing get_weight_display() cannot disagree about what it means.
    WEIGHT_UNIT = "lbs"

    UPDATER_WEIGHT_KEYS = (
        "weight",
        "net_weight",
        "netweight",
        "package_weight",
        "packageweight",
        "item_weight",
        "itemweight",
    )

    # Bite/kibble and breed-size phrases both map onto Breed/Kibble Size.
    KIBBLE_SIZE_UPDATER_ALIASES = {
        KibbleSizeChoices.SMALL: (
            "Small Bite",
            "Mini Kibble",
            "Tiny Bite",
            "Small Kibble",
            "Small Breed",
            "Toy Breed",
            "Mini Breed",
        ),
        # Keep "Medium" as an alias so product text still maps after the display label change.
        KibbleSizeChoices.MEDIUM: ("Medium", "Medium Bite", "Medium Kibble", "Medium Breed"),
        KibbleSizeChoices.LARGE: (
            "Large Bite",
            "Large Kibble",
            "Big Bite",
            "Big Kibble",
            "Large Breed",
            "Giant Breed",
        ),
    }
    # Bare Small/Medium/Large only count when nearer to kibble or breed cues.
    KIBBLE_SIZE_REQUIRE_NEAR = (
        "kibble",
        "kibbles",
        "bite",
        "bites",
        "piece",
        "pieces",
        "nibble",
        "breed",
        "breeds",
    )

    weight = models.FloatField(verbose_name="Bag Weight", blank=True, null=True, validators=[MinValueValidator(0)])
    kibble_size = models.CharField(max_length=2, choices=KibbleSizeChoices.choices, blank=True, verbose_name="Breed/Kibble Size")

    def get_weight_display(self):
        """The bag weight with its unit, trimmed of the decimals a whole number does not need."""
        if self.weight is None:
            return ""
        #The fixed two places round the float's noise away before the zeroes are stripped back off
        number = f"{self.weight:.2f}".rstrip("0").rstrip(".")
        return f"{number} {self.WEIGHT_UNIT}"

    def identify_data_warnings(self):
        super().identify_data_warnings()
        if self.weight is None:
            self.data_warnings.append("Missing Bag Weight")

    def _apply_updater_fields(self, data, applied, updater):
        super()._apply_updater_fields(data, applied, updater)

        def _coerce_weight_lbs(raw):
            """Parse a weight into pounds. Requires an explicit unit; bare numbers are ignored."""
            if raw is None or isinstance(raw, bool):
                return None
            # Numeric JSON with no unit cannot be trusted (often grams/oz shipping weight).
            if isinstance(raw, (int, float)):
                return None

            text = str(raw).strip().lower()
            if not text:
                return None

            match = re.search(
                r"([-+]?\d*\.?\d+)\s*(lbs?|pounds?|oz|ounces?|kg|kilograms?|g|grams?)\b",
                text,
            )
            if not match:
                return None

            amount = float(match.group(1))
            unit = match.group(2).lower()
            factor = {
                "lb": 1.0,
                "lbs": 1.0,
                "pound": 1.0,
                "pounds": 1.0,
                "oz": 1.0 / 16.0,
                "ounce": 1.0 / 16.0,
                "ounces": 1.0 / 16.0,
                "g": 1.0 / 453.59237,
                "gram": 1.0 / 453.59237,
                "grams": 1.0 / 453.59237,
                "kg": 2.2046226218,
                "kilogram": 2.2046226218,
                "kilograms": 2.2046226218,
            }.get(unit)
            if factor is None:
                return None
            return amount * factor


        # Prefer bag weight embedded in the product name ("… 12 lbs") over dedicated
        # weight keys, which often carry shipping/gross weight instead of net size.
        weight = _coerce_weight_lbs(
            updater._find_by_preferred_keys(data, Product.UPDATER_NAME_KEYS)
        )
        if weight is None:
            weight = _coerce_weight_lbs(self.name)
        if weight is None:
            weight = _coerce_weight_lbs(
                updater._find_by_preferred_keys(data, self.UPDATER_WEIGHT_KEYS)
            )
        if weight is not None and self.weight is None:
            self.weight = weight
            applied["weight"] = self.weight

        # Include resolved name so breed/kibble size cues in titles are visible.
        scan_data = data
        if not updater._is_blank(self.name):
            scan_data = {"payload": data, "name": self.name}
        kibble_size = updater._find_best_choice_label_in_data(
            scan_data,
            self.KibbleSizeChoices.choices,
            self.KIBBLE_SIZE_UPDATER_ALIASES,
            require_near=self.KIBBLE_SIZE_REQUIRE_NEAR,
        )
        if kibble_size and updater._is_blank(self.kibble_size):
            self.kibble_size = kibble_size
            applied["kibble_size"] = self.kibble_size

    def blank_for_lookup_rescan(self):
        super().blank_for_lookup_rescan()
        self.weight = None
        self.kibble_size = ""


class Canned(Food):
    class TextureChoices(models.TextChoices):
        PATE = "PATE", _("Paté")
        CHUNKS_GRAVY = "CHGR", _("Chunks in Gravy")
        STEW = "STEW", _("Stew")
        SHREDS = "SHRD", _("Shreds")

    # Extra phrases scanned in updater JSON beyond each texture label.
    TEXTURE_UPDATER_ALIASES = {
        TextureChoices.PATE: ("Pate", "Pâté", "Terrine"),
        TextureChoices.CHUNKS_GRAVY: ("Chunks", "In Gravy", "Chunky"),
        TextureChoices.STEW: ("Stewed", "Hearty Stew"),
        TextureChoices.SHREDS: ("Shredded", "Minced", "Flaked"),
    }

    texture = models.CharField(max_length=4, choices=TextureChoices.choices, blank=True, verbose_name="Texture")

    def _apply_updater_fields(self, data, applied, updater):
        super()._apply_updater_fields(data, applied, updater)

        texture = updater._find_best_choice_label_in_data(
            data, self.TextureChoices.choices, self.TEXTURE_UPDATER_ALIASES
        )
        if texture and updater._is_blank(self.texture):
            self.texture = texture
            applied["texture"] = self.texture

    def blank_for_lookup_rescan(self):
        super().blank_for_lookup_rescan()
        self.texture = ""


# Concrete model for each TypeChoices value. Declared after the subclasses exist.
PRODUCT_TYPE_MODELS = {
    Product.TypeChoices.OTHER.value: Product,
    Product.TypeChoices.FOOD.value: Food,
    Product.TypeChoices.KIBBLE.value: Kibble,
    Product.TypeChoices.CANNED.value: Canned,
}

# Storehome field that holds a practical stock limit for a product type, when one exists.
PRODUCT_TYPE_CAPACITY_FIELDS = {
    Product.TypeChoices.KIBBLE.value: "kibble_capacity",
    Product.TypeChoices.CANNED.value: "canned_capacity",
}

# Capacity-backed types first, then the remaining TypeChoices in declaration order.
STOCK_LEVEL_TYPE_ORDER = (
    Product.TypeChoices.FOOD,
    Product.TypeChoices.KIBBLE,
    Product.TypeChoices.CANNED,
    Product.TypeChoices.OTHER,
)


class StorageItem(models.Model):
    storehome = models.ForeignKey("core.Storehome", on_delete=models.CASCADE, related_name="storage_items", verbose_name="Storehome")

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, verbose_name="Product Type")
    object_id = models.PositiveIntegerField(verbose_name="Product ID")
    product = GenericForeignKey("content_type", "object_id")

    quantity = models.PositiveIntegerField(default=1, verbose_name="Quantity")
    date_stored = models.DateTimeField(auto_now_add=True, verbose_name="Date Stored")
    expiry_date = models.DateField(null=True, blank=True, verbose_name="Expiry Date")
    note = models.TextField(blank=True, verbose_name="Note")

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["storehome", "content_type", "object_id"]),
        ]

    @classmethod
    def product_content_type(cls):
        """Always store against the Product base row so subclass receives share the same product id."""
        return ContentType.objects.get_for_model(Product)

    @classmethod
    def product_content_types(cls):
        """Every ContentType a StorageItem might use for a product in the inheritance chain."""
        types = list(ContentType.objects.get_for_models(Product, Food, Kibble, Canned).values())
        leftover = ContentType.objects.filter(app_label="core", model="treats").first()
        if leftover is not None:
            types.append(leftover)
        return types

    @classmethod
    def annotate_product_stock_quantity(
        cls, queryset, annotation_name="stock_quantity", storehome=None
    ):
        """Sum StorageItem quantities onto each Product row.

        Matches any ContentType in the product inheritance chain so rows written
        against Product, Food, Kibble, Canned, or a former Treats row all count toward the same total.
        When storehome is set, only lots at that storehome are counted.
        Products with no stock annotate as 0 rather than null.
        """
        content_type_ids = [ct.pk for ct in cls.product_content_types()]
        filters = {
            "object_id": OuterRef("pk"),
            "content_type_id__in": content_type_ids,
        }
        if storehome is not None:
            filters["storehome"] = storehome
        totals = (
            cls.objects.filter(**filters)
            .values("object_id")
            .annotate(total=Sum("quantity"))
            .values("total")[:1]
        )
        return queryset.annotate(
            **{
                annotation_name: Coalesce(
                    Subquery(totals, output_field=IntegerField()),
                    Value(0),
                )
            }
        )

    @classmethod
    def post_expiry_keep_months(cls):
        return max(0, int(getattr(settings, "INVENTORY_POST_EXPIRY_KEEP_MONTHS", 6)))

    @classmethod
    def keep_until_date(cls, expiry_date):
        """Last calendar day a lot may be kept after its printed expiry date."""
        if expiry_date is None:
            return None
        return add_calendar_months(expiry_date, cls.post_expiry_keep_months())

    @classmethod
    def past_keep_date_cutoff(cls, today=None):
        """Expiry dates strictly before this cutoff are past their keep window."""
        today = today or timezone.localdate()
        return add_calendar_months(today, -cls.post_expiry_keep_months())

    @classmethod
    def past_keep_date_q(cls, today=None):
        cutoff = cls.past_keep_date_cutoff(today=today)
        return Q(expiry_date__isnull=False, expiry_date__lt=cutoff)

    def is_past_expiry(self, today=None):
        """True when the printed expiry date has already passed."""
        today = today or timezone.localdate()
        if self.expiry_date is None:
            return False
        return today > self.expiry_date

    def is_past_keep_date(self, today=None):
        today = today or timezone.localdate()
        keep_until = self.keep_until_date(self.expiry_date)
        if keep_until is None:
            return False
        return today > keep_until

    @classmethod
    def expiry_histogram(cls, queryset=None, today=None):
        """Month buckets of on-hand units by expiry date.

        Range runs from the earlier of (current month, earliest stocked expiry)
        through the later of (current month, latest stocked expiry), with empty
        months filled as zero. Bars past the keep window are flagged for disposal.
        """
        today = today or timezone.localdate()
        qs = cls.objects.all() if queryset is None else queryset
        past_keep = cls.past_keep_date_q(today=today)
        rows = (
            qs.filter(expiry_date__isnull=False)
            .annotate(month=TruncMonth("expiry_date"))
            .values("month")
            .annotate(
                # Don't name this "quantity" — a second Sum("quantity") in the
                # same annotate() would then try to aggregate the first Sum.
                total_units=Coalesce(Sum("quantity"), Value(0)),
                past_keep_units=Coalesce(
                    Sum("quantity", filter=past_keep), Value(0)
                ),
            )
            .order_by("month")
        )

        by_month = {}
        for row in rows:
            month = row["month"]
            if month is None:
                continue
            # TruncMonth may return datetime; normalize to a date month start.
            if hasattr(month, "date"):
                month = month.date()
            key = month_key(month)
            by_month[key] = {
                "quantity": int(row["total_units"] or 0),
                "needs_disposal": int(row["past_keep_units"] or 0) > 0,
            }
        if not by_month:
            return []

        current = month_key(today)
        month_keys = sorted(by_month.keys())
        start = month_keys[0] if month_keys[0] < current else current
        end = month_keys[-1] if month_keys[-1] > current else current

        buckets = []
        cursor = parse_month_key(start)
        end_date = parse_month_key(end)
        while cursor <= end_date:
            key = month_key(cursor)
            existing = by_month.get(key)
            quantity = existing["quantity"] if existing else 0
            buckets.append(
                {
                    "key": key,
                    "quantity": quantity,
                    "needs_disposal": bool(existing and existing["needs_disposal"]),
                    "is_current": key == current,
                    "label": cursor.strftime("%b %Y"),
                    "show_label": quantity > 0 or key == current,
                    "is_empty": quantity == 0 and key != current,
                }
            )
            cursor = add_calendar_months(cursor, 1)
            if len(buckets) > 240:
                break

        max_quantity = max((bucket["quantity"] for bucket in buckets), default=0)
        for bucket in buckets:
            quantity = bucket["quantity"]
            if quantity <= 0 or max_quantity <= 0:
                bucket["height_pct"] = 0
            else:
                bucket["height_pct"] = max(2, round(quantity / max_quantity * 100))
        return buckets

    @classmethod
    def movement_histogram(cls, storehome=None, product=None, months=6, today=None):
        """Recent months of recorded intake vs outtake units.

        Always returns ``months`` buckets ending at the current month so quiet
        periods stay visible. Heights share one scale (max of either series).
        When ``product`` is set, only movements for that product are included
        (across all Storehomes unless ``storehome`` is also set).
        """
        today = today or timezone.localdate()
        months = max(1, int(months))
        end_month = date(today.year, today.month, 1)
        start_month = add_calendar_months(end_month, -(months - 1))
        end_exclusive = add_calendar_months(end_month, 1)

        intake_by_month = StorageItemIntake.monthly_counts(
            storehome=storehome,
            product=product,
            start=start_month,
            end_exclusive=end_exclusive,
        )
        outtake_by_month = StorageItemOuttake.monthly_counts(
            storehome=storehome,
            product=product,
            start=start_month,
            end_exclusive=end_exclusive,
        )

        current = month_key(today)
        buckets = []
        cursor = start_month
        while cursor < end_exclusive:
            key = month_key(cursor)
            intake = int(intake_by_month.get(key, 0))
            outtake = int(outtake_by_month.get(key, 0))
            net = intake - outtake
            buckets.append(
                {
                    "key": key,
                    "intake": intake,
                    "outtake": outtake,
                    "net": net,
                    "is_current": key == current,
                    "label": cursor.strftime("%b %Y"),
                    "show_label": True,
                }
            )
            cursor = add_calendar_months(cursor, 1)

        max_quantity = max(
            (max(bucket["intake"], bucket["outtake"]) for bucket in buckets),
            default=0,
        )
        for bucket in buckets:
            for series in ("intake", "outtake"):
                quantity = bucket[series]
                if quantity <= 0 or max_quantity <= 0:
                    bucket[f"{series}_height_pct"] = 0
                else:
                    bucket[f"{series}_height_pct"] = max(
                        2, round(quantity / max_quantity * 100)
                    )
        return buckets

    @classmethod
    def _quantity_by_type_aggregates(cls):
        """Sum(quantity) per TypeChoices value, for use in aggregate() or annotate()."""
        agg_kwargs = {}
        for type_value, filters in Product.TYPE_QUERY_FILTERS.items():
            product_ids = Product.objects.filter(**filters).values("pk")
            agg_kwargs[type_value] = Coalesce(
                Sum("quantity", filter=Q(object_id__in=product_ids)),
                Value(0),
            )
        return agg_kwargs

    @classmethod
    def _stock_level_entry(cls, choice, units, storehome=None):
        """One product-type stock cell: units, plus capacity fill for types that have a limit."""
        units = int(units or 0)
        capacity = None
        if storehome is not None:
            field_name = PRODUCT_TYPE_CAPACITY_FIELDS.get(choice.value)
            if field_name:
                raw = getattr(storehome, field_name)
                if raw is not None:
                    capacity = int(raw)
        capacity_pct = None
        over_capacity = False
        fill_pct = 0
        if capacity:
            capacity_pct = round(units * 100 / capacity)
            over_capacity = units > capacity
            if units > 0:
                fill_pct = min(100, max(2, capacity_pct))
        return {
            "type": choice.value,
            "label": choice.label,
            "units": units,
            "capacity": capacity,
            "capacity_pct": capacity_pct,
            "over_capacity": over_capacity,
            "fill_pct": fill_pct,
        }

    @classmethod
    def stock_levels(cls, queryset=None, storehome=None):
        """Units on hand by product type, with percent of capacity for kibble and canned.

        Kibble and canned always include that Storehome's capacity. Food and Other
        have no capacity and appear as unit counts. The capacity bar is units ÷
        capacity, capped at 100% fill when stock is over the limit.
        """
        qs = cls.objects.all() if queryset is None else queryset
        agg_kwargs = cls._quantity_by_type_aggregates()
        totals = qs.aggregate(**agg_kwargs) if agg_kwargs else {}
        return [
            cls._stock_level_entry(choice, totals.get(choice.value), storehome=storehome)
            for choice in STOCK_LEVEL_TYPE_ORDER
        ]

    @classmethod
    def stock_levels_by_storehome(cls, storehomes=None):
        """Per-Storehome stock by product type, for the system overview matrix.

        Kibble and canned cells are percent of that Storehome's capacity. Food and
        Other are unit counts. Storehomes with no lots still appear as zeros.
        """
        from core.models.storehome import Storehome

        if storehomes is None:
            storehomes = list(Storehome.objects.order_by("name"))
        else:
            storehomes = list(storehomes)
        columns = [{"type": choice.value, "label": choice.label} for choice in STOCK_LEVEL_TYPE_ORDER]
        if not storehomes:
            return {"columns": columns, "rows": []}

        agg_kwargs = cls._quantity_by_type_aggregates()
        grouped = (
            cls.objects.filter(storehome_id__in=[storehome.pk for storehome in storehomes])
            .values("storehome_id")
            .annotate(**agg_kwargs)
        )
        totals_by_id = {row["storehome_id"]: row for row in grouped}
        rows = []
        for storehome in storehomes:
            totals = totals_by_id.get(storehome.pk, {})
            rows.append(
                {
                    "id": storehome.pk,
                    "name": storehome.name,
                    "cells": [
                        cls._stock_level_entry(choice, totals.get(choice.value), storehome=storehome)
                        for choice in STOCK_LEVEL_TYPE_ORDER
                    ],
                }
            )
        return {"columns": columns, "rows": rows}

    @classmethod
    def inventory_stats(cls, queryset=None, storehome=None):
        """Stock totals for dashboards.

        Dollar totals use each product's estimated_price × lot quantity. Lots whose
        product has no estimated price contribute $0.
        When storehome is set, movement_histogram is scoped to that storehome;
        otherwise it covers all Storehomes.
        """
        money = DecimalField(max_digits=14, decimal_places=2)
        zero = Value(Decimal("0.00"), output_field=money)
        qs = cls.objects.all() if queryset is None else queryset
        past_keep = cls.past_keep_date_q()
        # object_id is the Product pk for every ContentType in the inheritance chain.
        disallowed_stock = Q(
            object_id__in=Product.objects.filter(disallowed=True).values("pk")
        )

        unit_price = Coalesce(
            Subquery(
                Product.objects.filter(pk=OuterRef("object_id")).values("estimated_price")[:1],
                output_field=DecimalField(max_digits=8, decimal_places=2),
            ),
            zero,
        )
        priced = qs.annotate(unit_price=unit_price).annotate(
            line_value=ExpressionWrapper(
                F("quantity") * F("unit_price"),
                output_field=money,
            )
        )
        agg = priced.aggregate(
            total_units=Coalesce(Sum("quantity"), Value(0)),
            product_count=Count("object_id", distinct=True),
            estimated_value=Coalesce(Sum("line_value"), zero),
            past_keep_date_units=Coalesce(Sum("quantity", filter=past_keep), Value(0)),
            disallowed_in_stock_products=Count(
                "object_id", filter=disallowed_stock, distinct=True
            ),
            disallowed_in_stock_units=Coalesce(
                Sum("quantity", filter=disallowed_stock), Value(0)
            ),
        )
        expiry_histogram = cls.expiry_histogram(queryset=qs)
        return {
            "total_units": int(agg["total_units"] or 0),
            "product_count": int(agg["product_count"] or 0),
            "estimated_value": (agg["estimated_value"] or Decimal("0.00")).quantize(Decimal("0.01")),
            "past_keep_date_units": int(agg["past_keep_date_units"] or 0),
            "disallowed_in_stock_products": int(agg["disallowed_in_stock_products"] or 0),
            "disallowed_in_stock_units": int(agg["disallowed_in_stock_units"] or 0),
            "post_expiry_keep_months": cls.post_expiry_keep_months(),
            "stock_levels": cls.stock_levels(queryset=qs, storehome=storehome),
            "expiry_histogram": expiry_histogram,
            "movement_histogram": cls.movement_histogram(storehome=storehome),
        }

    @classmethod
    def warnings_for(cls, product, expiry_date, prefix=None):
        """Policy messages for a product lot.

        Returns "Disallowed Product" and/or "Past Keep Date"; empty means the
        lot is clean. "intake" and "outtake" map to "Do Not Intake" and
        "Do Not Distribute"; any other prefix is used as-is. When prefix is
        set, each reason is formatted as "{prefix}: {reason}". Intake callers
        treat results as hard errors; outtake callers treat them as soft
        warnings. Omit prefix for unprefixed disposal reasons.
        """
        if prefix == "intake":
            prefix = "Do Not Intake"
        elif prefix == "outtake":
            prefix = "Do Not Distribute"

        warnings = []
        if product is not None and product.disallowed:
            warnings.append("Disallowed Product")
        keep_until = cls.keep_until_date(expiry_date)
        if keep_until is not None and timezone.localdate() > keep_until:
            warnings.append("Past Keep Date")
        if prefix:
            return [f"{prefix}: {reason}" for reason in warnings]
        return warnings

    @classmethod
    def receive(cls, storehome, product, quantity, expiry_date, note=""):
        """Add a new stock lot for a product at a storehome.

        Each receive creates its own row so quantity, expiry date, date stored,
        and note stay tied to that intake rather than merging into an existing lot.
        Also records a StorageItemIntake row for product / Storehome reporting.
        """
        if quantity < 1:
            raise ValueError("Quantity must be at least 1.")
        if expiry_date is None:
            raise ValueError("Expiry date is required.")
        warnings = cls.warnings_for(product, expiry_date, "intake")
        if warnings:
            raise ValueError(warnings[0])

        specific = product.specific
        with transaction.atomic():
            # Multi-table inheritance keeps one pk across Product/Food/Kibble/Canned rows.
            item = cls.objects.create(
                storehome=storehome,
                content_type=cls.product_content_type(),
                object_id=product.pk,
                quantity=quantity,
                expiry_date=expiry_date,
                note=(note or "").strip(),
            )
            StorageItemIntake.record(storehome, specific, quantity)
            return item

    def set_quantity(self, quantity):
        """Set this lot's quantity, or delete the lot when quantity reaches 0.

        Returns the updated instance, or None when the lot was removed.
        """
        if quantity is None or quantity < 0:
            raise ValueError("Quantity cannot be negative.")
        if quantity == 0:
            self.delete()
            return None
        self.quantity = quantity
        self.save(update_fields=["quantity"])
        return self

    def update_lot(self, quantity, expiry_date, note=""):
        """Update this lot's quantity, expiry date, and note.

        Quantity 0 deletes the lot (expiry/note are ignored). Returns the
        updated instance, or None when the lot was removed.
        """
        if quantity == 0:
            return self.set_quantity(0)
        if quantity is None or quantity < 0:
            raise ValueError("Quantity cannot be negative.")
        if expiry_date is None:
            raise ValueError("Expiry date is required.")
        self.quantity = quantity
        self.expiry_date = expiry_date
        self.note = (note or "").strip()
        self.save(update_fields=["quantity", "expiry_date", "note"])
        return self

    def outtake(self, quantity):
        """Remove quantity units from this lot, deleting the lot when it reaches 0.

        Returns the updated instance, or None when the lot was removed.
        """
        if quantity is None or quantity < 1:
            raise ValueError("Quantity must be at least 1.")
        if quantity > self.quantity:
            raise ValueError(
                f"Only {self.quantity} unit(s) available for this expiry date."
            )
        return self.set_quantity(self.quantity - quantity)

    @classmethod
    def lots_for_product(cls, storehome, product):
        """On-hand lots for a product at a storehome, soonest expiry first."""
        content_type_ids = [ct.pk for ct in cls.product_content_types()]
        return (
            cls.objects.filter(
                storehome=storehome,
                content_type_id__in=content_type_ids,
                object_id=product.pk,
                quantity__gt=0,
            )
            .order_by(F("expiry_date").asc(nulls_last=True), "pk")
        )

    @classmethod
    def outtake_by_expiry(cls, storehome, product, expiry_date, quantity):
        """Remove quantity from lots matching product + expiry at a storehome.

        Lots with the same expiry are drained in pk order. Returns
        (quantity_removed, remaining_on_that_expiry).

        Records a StorageItemOuttake row for product / Storehome reporting only
        when the outtake has no soft warnings (disallowed product / past keep date).
        """
        if quantity is None or quantity < 1:
            raise ValueError("Quantity must be at least 1.")

        warnings = cls.warnings_for(product, expiry_date, "outtake")
        specific = product.specific
        with transaction.atomic():
            lots = list(cls.lots_for_product(storehome, product).select_for_update())
            if expiry_date is None:
                matching = [lot for lot in lots if lot.expiry_date is None]
            else:
                matching = [lot for lot in lots if lot.expiry_date == expiry_date]

            available = sum(lot.quantity for lot in matching)
            if available < 1:
                raise ValueError("No stock found for that expiry date.")
            if quantity > available:
                raise ValueError(
                    f"Only {available} unit(s) available for this expiry date."
                )

            remaining_to_remove = quantity
            for lot in matching:
                if remaining_to_remove < 1:
                    break
                take = min(lot.quantity, remaining_to_remove)
                lot.outtake(take)
                remaining_to_remove -= take

            if not warnings:
                StorageItemOuttake.record(storehome, specific, quantity)

            return quantity, available - quantity


class StorageItemMovementBase(models.Model):
    """Shared reporting fields for intake / outtake movement rows."""

    # Concrete subclasses set this to their DateTimeField name (intake_date / outtake_date).
    DATE_FIELD = None

    product = models.ForeignKey(
        "core.Product",
        on_delete=models.CASCADE,
        related_name="%(class)s_rows",
        verbose_name="Product",
    )
    count = models.PositiveIntegerField(default=1, verbose_name="Count")

    class Meta:
        abstract = True

    @classmethod
    def record(cls, storehome, product, count):
        """Create a movement row for a product (or product subclass) at a storehome."""
        # Multi-table inheritance shares one pk across Product / Food / leaf rows.
        return cls.objects.create(
            storehome=storehome,
            product_id=product.pk,
            count=count,
        )

    @classmethod
    def monthly_counts(cls, storehome=None, product=None, start=None, end_exclusive=None):
        """Sum movement counts by calendar month.

        ``start`` and ``end_exclusive`` are dates (month starts). Returns
        ``{YYYY-MM: total_count}`` for rows with a recorded timestamp in
        ``[start, end_exclusive)``.
        """
        if not cls.DATE_FIELD:
            raise ValueError(f"{cls.__name__} must define DATE_FIELD.")

        qs = cls.objects.all()
        if storehome is not None:
            qs = qs.filter(storehome=storehome)
        if product is not None:
            qs = qs.filter(product_id=product.pk)

        date_field = cls.DATE_FIELD
        tz = timezone.get_current_timezone()
        if start is not None:
            start_dt = timezone.make_aware(datetime.combine(start, time.min), tz)
            qs = qs.filter(**{f"{date_field}__gte": start_dt})
        if end_exclusive is not None:
            end_dt = timezone.make_aware(datetime.combine(end_exclusive, time.min), tz)
            qs = qs.filter(**{f"{date_field}__lt": end_dt})

        rows = (
            qs.annotate(month=TruncMonth(date_field))
            .values("month")
            .annotate(total=Coalesce(Sum("count"), Value(0)))
            .order_by("month")
        )

        by_month = {}
        for row in rows:
            month = row["month"]
            if month is None:
                continue
            if hasattr(month, "date"):
                month = month.date()
            by_month[month_key(month)] = int(row["total"] or 0)
        return by_month


class StorageItemIntake(StorageItemMovementBase):
    DATE_FIELD = "intake_date"

    storehome = models.ForeignKey(
        "core.Storehome",
        on_delete=models.CASCADE,
        related_name="storage_item_intakes",
        verbose_name="Storehome",
    )
    intake_date = models.DateTimeField(auto_now_add=True, verbose_name="Intake Date")


class StorageItemOuttake(StorageItemMovementBase):
    DATE_FIELD = "outtake_date"

    storehome = models.ForeignKey(
        "core.Storehome",
        on_delete=models.CASCADE,
        related_name="storage_item_outtakes",
        verbose_name="Storehome",
    )
    outtake_date = models.DateTimeField(auto_now_add=True, verbose_name="Outtake Date")

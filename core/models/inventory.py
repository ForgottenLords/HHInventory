from decimal import Decimal, InvalidOperation
import re

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from multiselectfield import MultiSelectField


def subclass_or_none(instance, accessor):
    try:
        return getattr(instance, accessor)
    except ObjectDoesNotExist:
        return None


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
    disallowed = models.BooleanField(default=False, verbose_name="Disallowed")
    in_production = models.BooleanField(default=True, verbose_name="In Production")
    last_updated = models.DateTimeField(auto_now=True, verbose_name="Last Updated")
    data_warnings = models.JSONField(default=list, blank=True, null=True, verbose_name="Data Warnings")
    updater_class = models.CharField(max_length=200, blank=True, null=True, verbose_name="Updater Class")
    updater_data = models.JSONField(blank=True, null=True, verbose_name="Updater Data")
    updater_last_updated = models.DateTimeField(blank=True, null=True, verbose_name="Updater Last Updated")

    @property
    def specific(self):
        """The most derived instance behind this row, which owns every field the product has.

        Saving it writes each inherited table at once, so an edit cannot leave the Product row
        and its Food/Kibble/Canned rows disagreeing. Works for rows loaded as Product and for
        instances already constructed as Food/Kibble/Canned (including unsaved drafts).
        """
        if isinstance(self, Kibble) or isinstance(self, Canned):
            return self
        if isinstance(self, Food):
            return (
                subclass_or_none(self, "kibble")
                or subclass_or_none(self, "canned")
                or self
            )
        food = subclass_or_none(self, "food")
        if food is None:
            return self
        return subclass_or_none(food, "kibble") or subclass_or_none(food, "canned") or food

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
    def can_view(cls, request):
        if request.user.has_perm("core.view_product"):
            return True, ""
        return False, "You do not have permission to view Products"

    @classmethod
    def can_create(cls, request):
        if request.user.has_perm("core.add_product"):
            return True, ""
        return False, "You do not have permission to create Products"

    def can_edit(self, request):
        if request.user.has_perm("core.change_product"):
            return True, ""
        return False, "You do not have permission to edit this Product"

    def can_delete(self, request):
        if request.user.has_perm("core.delete_product"):
            return True, ""
        #TODO prevent deletion if the product is in use
        return False, "You do not have permission to delete this Product"

    def update_from_lookup(self, reset=False, save=True):
        """Try each ProductUpdater in order until one succeeds, or raise if all fail.

        On success the winning updater has already written updater_data / updater_class
        onto this product. Returns that updater instance. Pass save=False to fill an
        unsaved draft without writing to the database.
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
                updater.update_product(save=save)
                return updater
            except RuntimeError as exc:
                errors.append(f"{updater_cls.__name__}: {exc}")

        raise RuntimeError(
            f"All product updaters failed for barcode {self.barcode}: "
            + "; ".join(errors)
        )

    def _apply_updater_fields(self, data, applied, updater):
        """Map name / brand / estimated_price / notes from preferred JSON keys onto blank fields."""

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

        price = _coerce_price(updater._find_by_preferred_keys(data, self.UPDATER_PRICE_KEYS))
        if price is not None and self.estimated_price is None:
            self.estimated_price = price
            applied["estimated_price"] = self.estimated_price

        notes = updater._find_by_preferred_keys(data, self.UPDATER_NOTES_KEYS)
        if notes is not None and updater._is_blank(self.notes):
            self.notes = str(notes).strip()
            applied["notes"] = self.notes

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
        if self.disallowed:
            self.data_warnings.append("Disallowed")

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



class Food(Product):
    class ProteinChoices(models.TextChoices):
        CHICKEN = "CHKN", _("Chicken")
        BEEF = "BEEF", _("Beef")
        BISON = "BSON", _("Bison")
        DUCK = "DUCK", _("Duck")
        FISH = "FISH", _("Fish (Unspecified)")
        LAMB = "LAMB", _("Lamb")
        PORK = "PORK", _("Pork")
        RABBIT = "RBBT", _("Rabbit")
        SALMON = "SALM", _("Salmon")
        TURKEY = "TURK", _("Turkey")
        VENISON = "VENI", _("Venison")
        WHITEFISH = "WTFS", _("Whitefish")
        OTHER = "OTHR", _("Other")

    class LifeStageChoices(models.TextChoices):
        UNKNOWN = "UNK", _("Unknown")
        PUPPY = "PUP", _("Puppy")
        ADULT = "ADL", _("Adult")
        ALL_STAGES = "ALL", _("All-Stages")
        SENIOR = "SNR", _("Senior")

    class SpecialDietChoices(models.TextChoices):
        SENSITIVE_STOMACH = "SENS", _("Sensitive Stomach")
        WEIGHT_MANAGEMENT = "WGHT", _("Weight Management")
        DENTAL_HEALTH = "DENT", _("Dental Health")
        DIGESTIVE_HEALTH = "DIGE", _("Digestive Health")
        JOINT_HEALTH = "JONT", _("Joint Health")
        SKIN_AND_COAT = "SKIN", _("Skin & Coat")
        LIMITED_INGREDIENT = "LIMI", _("Limited Ingredient")
        GRAIN_FREE = "GRAI", _("Grain-Free")

    # Extra phrases scanned in updater JSON beyond each choice's official label.
    PROTEIN_UPDATER_ALIASES = {
        ProteinChoices.FISH: ("Fish", "Seafood"),
        ProteinChoices.WHITEFISH: ("White Fish", "White-Fish"),
        ProteinChoices.CHICKEN: ("Poultry",),
        ProteinChoices.SALMON: ("Atlantic Salmon",),
    }
    SPECIAL_DIET_UPDATER_ALIASES = {
        SpecialDietChoices.GRAIN_FREE: ("Grain Free", "Grainfree", "No Grain"),
        SpecialDietChoices.SKIN_AND_COAT: ("Skin and Coat", "Skin Coat"),
        SpecialDietChoices.LIMITED_INGREDIENT: ("Limited Ingredients", "LID"),
        SpecialDietChoices.WEIGHT_MANAGEMENT: ("Weight Control", "Weight Loss"),
        SpecialDietChoices.SENSITIVE_STOMACH: ("Sensitive Digestion", "Easy Digestion"),
        SpecialDietChoices.JOINT_HEALTH: ("Joint Support", "Hip and Joint"),
        SpecialDietChoices.DENTAL_HEALTH: ("Dental Care", "Oral Care"),
        SpecialDietChoices.DIGESTIVE_HEALTH: ("Digestive Care", "Gut Health"),
    }
    LIFE_STAGE_UPDATER_ALIASES = {
        LifeStageChoices.PUPPY: ("Puppies", "Growth", "For Puppies"),
        LifeStageChoices.ADULT: ("Adults", "Maintenance", "For Adults"),
        LifeStageChoices.ALL_STAGES: ("All Stages", "All Life Stages", "All Ages"),
        LifeStageChoices.SENIOR: ("Seniors", "Mature", "For Seniors", "7+", "Elder"),
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

        # Skip OTHER — "Other" is too common in prose and is manual-only.
        protein_choices = [
            choice
            for choice in self.ProteinChoices.choices
            if choice[0] != self.ProteinChoices.OTHER
        ]
        proteins = updater._find_choice_labels_in_data(
            data, protein_choices, self.PROTEIN_UPDATER_ALIASES
        )
        if proteins and updater._is_blank(self.proteins):
            self.proteins = proteins
            applied["proteins"] = list(self.proteins)

        diets = updater._find_choice_labels_in_data(
            data, self.SpecialDietChoices.choices, self.SPECIAL_DIET_UPDATER_ALIASES
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
        life_stage = updater._find_best_choice_label_in_data(
            data, life_stage_choices, self.LIFE_STAGE_UPDATER_ALIASES
        )
        if life_stage and (
            updater._is_blank(self.life_stages)
            or self.life_stages == self.LifeStageChoices.UNKNOWN
        ):
            self.life_stages = life_stage
            applied["life_stages"] = self.life_stages


class Kibble(Food):
    class KibbleSizeChoices(models.TextChoices):
        SMALL = "SM", _("Small")
        MEDIUM = "MD", _("Medium")
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

    # Prefer bite/kibble phrases. "Breed" aliases are dog size, not kibble size.
    KIBBLE_SIZE_UPDATER_ALIASES = {
        KibbleSizeChoices.SMALL: ("Small Bite", "Mini Kibble", "Tiny Bite", "Small Kibble"),
        KibbleSizeChoices.MEDIUM: ("Medium Bite", "Medium Kibble"),
        KibbleSizeChoices.LARGE: ("Large Bite", "Large Kibble", "Big Bite", "Big Kibble"),
    }
    # Light context: size words count as kibble size only when nearer to these than to dog-size words.
    KIBBLE_SIZE_REQUIRE_NEAR = ("kibble", "kibbles", "bite", "bites", "piece", "pieces", "nibble")
    KIBBLE_SIZE_REJECT_NEAR = ("breed", "breeds")

    weight = models.FloatField(verbose_name="Bag Weight", blank=True, null=True, validators=[MinValueValidator(0)])
    kibble_size = models.CharField(max_length=2, choices=KibbleSizeChoices.choices, blank=True, verbose_name="Kibble Size")

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
            """Parse a weight into pounds. Bare numbers are treated as already in lbs."""
            if raw is None or isinstance(raw, bool):
                return None
            if isinstance(raw, (int, float)):
                return float(raw)

            text = str(raw).strip().lower()
            if not text:
                return None

            match = re.search(
                r"([-+]?\d*\.?\d+)\s*(lbs?|pounds?|oz|ounces?|kg|kilograms?|g|grams?)?",
                text,
            )
            if not match:
                return None

            amount = float(match.group(1))
            unit = (match.group(2) or "lbs").lower()
            factor =  {
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


        weight = _coerce_weight_lbs(updater._find_by_preferred_keys(data, self.UPDATER_WEIGHT_KEYS))
        if weight is not None and self.weight is None:
            self.weight = weight
            applied["weight"] = self.weight

        kibble_size = updater._find_best_choice_label_in_data(
            data,
            self.KibbleSizeChoices.choices,
            self.KIBBLE_SIZE_UPDATER_ALIASES,
            require_near=self.KIBBLE_SIZE_REQUIRE_NEAR,
            reject_near=self.KIBBLE_SIZE_REJECT_NEAR,
        )
        if kibble_size and updater._is_blank(self.kibble_size):
            self.kibble_size = kibble_size
            applied["kibble_size"] = self.kibble_size


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


# Concrete model for each TypeChoices value. Declared after the subclasses exist.
PRODUCT_TYPE_MODELS = {
    Product.TypeChoices.OTHER.value: Product,
    Product.TypeChoices.FOOD.value: Food,
    Product.TypeChoices.KIBBLE.value: Kibble,
    Product.TypeChoices.CANNED.value: Canned,
}


class StorageItem(models.Model):
    storehome = models.ForeignKey("core.Storehome", on_delete=models.CASCADE, related_name="storage_items", verbose_name="Storehome")

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, verbose_name="Product Type")
    object_id = models.PositiveIntegerField(verbose_name="Product ID")
    product = GenericForeignKey("content_type", "object_id")

    quantity = models.PositiveIntegerField(default=1, verbose_name="Quantity")
    date_stored = models.DateField(auto_now_add=True, verbose_name="Date Stored")
    expiry_date = models.DateField(null=True, blank=True, verbose_name="Expiry Date")

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

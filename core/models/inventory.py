from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
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

    name = models.CharField(max_length=200, verbose_name="Name")
    barcode = models.CharField(max_length=16, verbose_name="Barcode", unique=True)
    brand = models.CharField(max_length=100, verbose_name="Brand/Company")
    country_of_origin = models.CharField(max_length=100, blank=True, verbose_name="Country of Origin")
    photo = models.ImageField(upload_to="products/", blank=True, null=True, verbose_name="Photo")
    estimated_price = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True, verbose_name="Estimated Price")
    notes = models.TextField(blank=True, verbose_name="Notes")
    disallowed = models.BooleanField(default=False, verbose_name="Disallowed")
    in_production = models.BooleanField(default=True, verbose_name="In Production")
    last_updated = models.DateTimeField(auto_now=True, verbose_name="Last Updated")

    @property
    def specific(self):
        """The most derived instance behind this row, which owns every field the product has.

        Saving it writes each inherited table at once, so an edit cannot leave the Product row
        and its Food/Kibble/Canned rows disagreeing.
        """
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

class Food(Product):
    class ProteinChoices(models.TextChoices):
        CHICKEN = "CHKN", _("Chicken")
        BEEF = "BEEF", _("Beef")
        LAMB = "LAMB", _("Lamb")
        TURKEY = "TURK", _("Turkey")
        DUCK = "DUCK", _("Duck")
        SALMON = "SALM", _("Salmon")
        FISH = "FISH", _("Fish (Unspecified)")
        WHITEFISH = "WTFS", _("Whitefish")
        PORK = "PORK", _("Pork")
        VENISON = "VENI", _("Venison")
        BISON = "BSON", _("Bison")
        RABBIT = "RBBT", _("Rabbit")
        
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

    life_stages = models.CharField(max_length=3, choices=LifeStageChoices.choices, default=LifeStageChoices.UNKNOWN, verbose_name="Life Stage")
    proteins = MultiSelectField(choices=ProteinChoices.choices, blank=True, verbose_name="Proteins")
    special_diet = MultiSelectField(choices=SpecialDietChoices.choices, blank=True, verbose_name="Special Diet")

class Kibble(Food):
    class KibbleSizeChoices(models.TextChoices):
        SMALL = "SM", _("Small")
        MEDIUM = "MD", _("Medium")
        LARGE = "LG", _("Large")

    #The unit weight is stored in. Named here rather than in each template, so a client showing
    #the number and a client showing get_weight_display() cannot disagree about what it means.
    WEIGHT_UNIT = "lbs"

    weight = models.FloatField(verbose_name="Bag Weight")
    kibble_size = models.CharField(max_length=2, choices=KibbleSizeChoices.choices, blank=True, verbose_name="Kibble Size")

    def get_weight_display(self):
        """The bag weight with its unit, trimmed of the decimals a whole number does not need."""
        if self.weight is None:
            return ""
        #The fixed two places round the float's noise away before the zeroes are stripped back off
        number = f"{self.weight:.2f}".rstrip("0").rstrip(".")
        return f"{number} {self.WEIGHT_UNIT}"


class Canned(Food):
    class TextureChoices(models.TextChoices):
        PATE = "PATE", _("Paté")
        CHUNKS_GRAVY = "CHGR", _("Chunks in Gravy")
        STEW = "STEW", _("Stew")
        SHREDS = "SHRD", _("Shreds")

    texture = models.CharField(max_length=4, choices=TextureChoices.choices, blank=True, verbose_name="Texture")

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

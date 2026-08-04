from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.db import models


class Storehome(models.Model):
    name = models.CharField(max_length=255, verbose_name="Name")
    address = models.CharField(max_length=255, verbose_name="Address")
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        verbose_name="Latitude",
    )
    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        blank=True,
        null=True,
        verbose_name="Longitude",
    )

    @staticmethod
    def _truncate_coordinate(value, max_digits, decimal_places):
        """Truncate a coordinate to the field's digit budget instead of rejecting it."""
        if value is None or value == "":
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return value

        quant = Decimal("1").scaleb(-decimal_places)
        number = number.quantize(quant, rounding=ROUND_DOWN)

        # Keep at most max_digits total digits (sign excluded), truncating the integer side if needed.
        sign = Decimal("-1") if number < 0 else Decimal("1")
        absolute = abs(number)
        as_text = f"{absolute:f}"
        digit_count = len(as_text.replace(".", ""))
        if digit_count > max_digits:
            int_digits = max_digits - decimal_places
            int_part, _, frac_part = as_text.partition(".")
            int_part = int_part[-int_digits:] if int_digits > 0 else "0"
            frac_part = (frac_part + "0" * decimal_places)[:decimal_places]
            absolute = Decimal(f"{int_part}.{frac_part}" if decimal_places else int_part)
            number = sign * absolute
        return number

    def _truncate_coordinates(self):
        lat_field = self._meta.get_field("latitude")
        lng_field = self._meta.get_field("longitude")
        if self.latitude is not None:
            self.latitude = self._truncate_coordinate(
                self.latitude, lat_field.max_digits, lat_field.decimal_places
            )
        if self.longitude is not None:
            self.longitude = self._truncate_coordinate(
                self.longitude, lng_field.max_digits, lng_field.decimal_places
            )

    def clean_fields(self, exclude=None):
        # Truncate before DecimalField max_digits validation runs.
        self._truncate_coordinates()
        super().clean_fields(exclude=exclude)

    def save(self, *args, **kwargs):
        self._truncate_coordinates()
        super().save(*args, **kwargs)

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

    def has_inventory(self):
        """True when this storehome still holds any StorageItem lots."""
        return self.storage_items.exists()

    def can_delete(self, request):
        if not request.user.has_perm("core.delete_storehome"):
            return False, "You do not have permission to delete this Storehome"
        if self.has_inventory():
            return False, "This storehome still has inventory."
        return True, ""

    def can_manage_inventory(self, request):
        """True when the user is assigned as a manager of this storehome."""
        user = request.user
        if not user.is_authenticated:
            return False, "Sign in to manage inventory."
        if getattr(user, "managed_storehome_id", None) == self.pk:
            return True, ""
        return False, "You are not a manager of this storehome."

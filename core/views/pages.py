from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin, UserPassesTestMixin
from django.db.models.fields import NOT_PROVIDED
from django.shortcuts import redirect
from django.views.generic import TemplateView

from core.models import Canned, Food, Kibble, Product, StorageItem, Storehome, UserProfile


def field_copy(model, *names, extra_labels=None):
    """Labels, help texts, and defaults from model fields, so templates never restate a model's wording."""
    labels = {}
    help_texts = {}
    defaults = {}
    for name in names:
        field = model._meta.get_field(name)
        labels[name] = field.verbose_name
        if field.help_text:
            help_texts[name] = str(field.help_text)
        default = field.default
        if default is not NOT_PROVIDED:
            defaults[name] = default() if callable(default) else default
    if extra_labels:
        labels.update(extra_labels)
    return {"labels": labels, "help_texts": help_texts, "defaults": defaults}


def _merge_field_copy(*copies):
    merged = {"labels": {}, "help_texts": {}, "defaults": {}}
    for copy in copies:
        for key, values in merged.items():
            values.update(copy[key])
    return merged


def product_field_copy(*extras):
    """Product-family copy for shared create/edit/delete dialogs, plus any extra field copies."""
    return _merge_field_copy(
        field_copy(
            Product,
            "name",
            "barcode",
            "brand",
            "country_of_origin",
            "estimated_price",
            "notes",
            "disallowed",
            "reviewed_by",
            "reviewed_at",
            "last_updated",
            "updater_class",
            "updater_last_updated",
            extra_labels={"product_reviewed": "Product Reviewed"},
        ),
        field_copy(Food, "life_stages", "proteins", "special_diet"),
        field_copy(Kibble, "weight", "kibble_size", extra_labels={"weight_unit": Kibble.WEIGHT_UNIT}),
        field_copy(Canned, "texture"),
        *extras,
    )


class LandingLoginRequiredMixin(LoginRequiredMixin):
    """Send anonymous users to the landing page; authenticated denials go to the dashboard."""

    login_url = "landing"

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            return redirect("dashboard")
        return super().handle_no_permission()


class RequestUserContextMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        return context


class LandingView(TemplateView):
    template_name = "login.html"

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if request.user.is_superuser:
                return redirect("system-overview")
            if getattr(request.user, "managed_storehome_id", None):
                return redirect("dashboard")
            logout(request)
        return super().get(request, *args, **kwargs)


class DashboardView(LandingLoginRequiredMixin, RequestUserContextMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        managed_storehome = getattr(self.request.user, "managed_storehome", None)
        storehome_stats = None
        if managed_storehome is not None:
            storehome_stats = StorageItem.inventory_stats(
                StorageItem.objects.filter(storehome=managed_storehome),
                storehome=managed_storehome,
            )
        context["managed_storehome"] = managed_storehome
        context["storehome_stats"] = storehome_stats
        return context


class SystemOverviewView(LandingLoginRequiredMixin, UserPassesTestMixin, RequestUserContextMixin, TemplateView):
    template_name = "system_overview.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["system_stats"] = {**StorageItem.inventory_stats()}
        context["library_quality_stats"] = Product.library_quality_stats()
        context["storehome_stock_levels"] = StorageItem.stock_levels_by_storehome()
        return context


class ProductLibraryView(LandingLoginRequiredMixin, PermissionRequiredMixin, RequestUserContextMixin, TemplateView):
    template_name = "product_library.html"
    permission_required = "core.view_product"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(product_field_copy(field_copy(StorageItem, "quantity")))
        return context


class ProductView(LandingLoginRequiredMixin, PermissionRequiredMixin, RequestUserContextMixin, TemplateView):
    template_name = "product_view.html"
    permission_required = "core.view_product"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product_id"] = self.kwargs["product_id"]
        context.update(
            product_field_copy(
                field_copy(StorageItem, "quantity", "date_stored", "expiry_date"),
                field_copy(Storehome, "name", "address", extra_labels={"storehome": "Storehome"}),
            )
        )
        return context


class ManageUsersView(LandingLoginRequiredMixin, PermissionRequiredMixin, RequestUserContextMixin, TemplateView):
    template_name = "manage_users.html"
    permission_required = "core.view_userprofile"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            field_copy(
                UserProfile,
                "username",
                "email",
                "first_name",
                "last_name",
                "managed_storehome",
                "password",
                extra_labels={"name": "Name"},
            )
        )
        return context


class ManageStorehomesView(LandingLoginRequiredMixin, PermissionRequiredMixin, RequestUserContextMixin, TemplateView):
    template_name = "manage_storehomes.html"
    permission_required = "core.view_storehome"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            field_copy(
                Storehome,
                "name",
                "address",
                "latitude",
                "longitude",
                "kibble_capacity",
                "canned_capacity",
                extra_labels={"managers": "Managers"},
            )
        )
        return context


class ManagedInventoryView(LandingLoginRequiredMixin, UserPassesTestMixin, RequestUserContextMixin, TemplateView):
    def test_func(self):
        allowed, _reason = UserProfile.can_manage_storehome_inventory(self.request)
        return allowed

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["storehome"] = self.request.user.managed_storehome
        context.update(product_field_copy(field_copy(StorageItem, "quantity", "expiry_date", "note")))
        return context


class IncomingInventoryView(ManagedInventoryView):
    template_name = "incoming_inventory.html"


class OutgoingInventoryView(ManagedInventoryView):
    template_name = "outgoing_inventory.html"


class StorehomeInventoryView(ManagedInventoryView):
    template_name = "storehome_inventory.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_view_product_detail, _reason = Product.can_view_library(self.request)
        context["can_view_product_detail"] = can_view_product_detail
        return context

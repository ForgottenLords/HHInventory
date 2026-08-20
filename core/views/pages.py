from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models.fields import NOT_PROVIDED
from django.shortcuts import redirect, render
from django.utils.text import capfirst

from core.models import Canned, Food, Kibble, Product, StorageItem, Storehome, UserProfile


def field_copy(model, *names, extra_labels=None):
    """Labels, help texts, and defaults from model fields, so templates never restate a model's wording."""
    labels = {}
    help_texts = {}
    defaults = {}
    for name in names:
        field = model._meta.get_field(name)
        labels[name] = capfirst(field.verbose_name)
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


def landing(request):
    if request.user.is_authenticated:
        user = request.user
        if user.is_superuser:
            return redirect("system-overview")
        if getattr(user, "managed_storehome_id", None):
            return redirect("dashboard")
        logout(request)
    return render(request, "login.html")


@login_required(login_url="landing")
def dashboard(request):
    user = request.user
    managed_storehome = getattr(user, "managed_storehome", None)
    storehome_stats = None
    if managed_storehome is not None:
        storehome_stats = StorageItem.inventory_stats(
            StorageItem.objects.filter(storehome=managed_storehome),
            storehome=managed_storehome,
        )

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "managed_storehome": managed_storehome,
            "storehome_stats": storehome_stats,
        },
    )


@login_required(login_url="landing")
def system_overview(request):
    user = request.user
    if not user.is_superuser:
        return redirect("dashboard")

    system_stats = {
        **StorageItem.inventory_stats(),
    }
    library_quality_stats = Product.library_quality_stats()
    storehome_stock_levels = StorageItem.stock_levels_by_storehome()

    return render(
        request,
        "system_overview.html",
        {
            "user": user,
            "system_stats": system_stats,
            "library_quality_stats": library_quality_stats,
            "storehome_stock_levels": storehome_stock_levels,
        },
    )


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_library(request):
    return render(
        request,
        "product_library.html",
        {"user": request.user, **product_field_copy(field_copy(StorageItem, "quantity"))},
    )


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_view(request, product_id):
    return render(
        request,
        "product_view.html",
        {
            "user": request.user,
            "product_id": product_id,
            **product_field_copy(
                field_copy(StorageItem, "quantity", "date_stored", "expiry_date"),
                field_copy(Storehome, "name", "address", extra_labels={"storehome": "Storehome"}),
            ),
        },
    )


@login_required(login_url="landing")
@permission_required("core.view_userprofile", login_url="dashboard")
def manage_users(request):
    return render(
        request,
        "manage_users.html",
        {
            "user": request.user,
            **field_copy(
                UserProfile,
                "username",
                "email",
                "first_name",
                "last_name",
                "managed_storehome",
                "password",
                extra_labels={"name": "Name"},
            ),
        },
    )


@login_required(login_url="landing")
@permission_required("core.view_storehome", login_url="dashboard")
def manage_storehomes(request):
    return render(
        request,
        "manage_storehomes.html",
        {
            "user": request.user,
            **field_copy(
                Storehome,
                "name",
                "address",
                "latitude",
                "longitude",
                "kibble_capacity",
                "canned_capacity",
                extra_labels={"managers": "Managers"},
            ),
        },
    )


def _managed_inventory_page(request, template_name, **extra_context):
    allowed, _reason = UserProfile.can_manage_storehome_inventory(request)
    if not allowed:
        return redirect("dashboard")

    return render(
        request,
        template_name,
        {
            "user": request.user,
            "storehome": request.user.managed_storehome,
            **product_field_copy(field_copy(StorageItem, "quantity", "expiry_date", "note")),
            **extra_context,
        },
    )


@login_required(login_url="landing")
def incoming_inventory(request):
    return _managed_inventory_page(request, "incoming_inventory.html")


@login_required(login_url="landing")
def outgoing_inventory(request):
    return _managed_inventory_page(request, "outgoing_inventory.html")


@login_required(login_url="landing")
def storehome_inventory(request):
    can_view_product_detail, _reason = Product.can_view_library(request)
    return _managed_inventory_page(request, "storehome_inventory.html", can_view_product_detail=can_view_product_detail)

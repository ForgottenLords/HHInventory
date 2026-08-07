from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.utils.text import capfirst

from core.models import Canned, Food, Kibble, Product, StorageItem, Storehome, Treats, UserProfile


def field_labels(model, *names, **kwargs):
    """Field verbose names keyed by field name, so templates never restate a model's wording."""
    labelsDict={name: capfirst(model._meta.get_field(name).verbose_name) for name in names}
    labelsDict.update(kwargs.get("extra_labels", {}))
    return labelsDict


def field_help_texts(model, *names):
    """Non-empty field help_text keyed by field name, so templates never restate a model's wording."""
    help_texts = {}
    for name in names:
        text = model._meta.get_field(name).help_text
        if text:
            help_texts[name] = str(text)
    return help_texts


def product_labels():
    """Every product label the shared edit and delete dialogs name, plus the page's own fields."""
    return {
        **field_labels(
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
        **field_labels(Food, "life_stages", "proteins", "special_diet"),
        **field_labels(
            Kibble,
            "weight",
            "kibble_size",
            extra_labels={"weight_unit": Kibble.WEIGHT_UNIT},
        ),
        **field_labels(Canned, "texture"),
        **field_labels(Treats, "treat_size"),
    }


def product_help_texts():
    """Product-family help_text for shared create/edit forms."""
    return {
        **field_help_texts(
            Product,
            "name",
            "barcode",
            "brand",
            "country_of_origin",
            "estimated_price",
            "notes",
            "disallowed",
            "reviewed_at",
        ),
        **field_help_texts(Food, "life_stages", "proteins", "special_diet"),
        **field_help_texts(Kibble, "weight", "kibble_size"),
        **field_help_texts(Canned, "texture"),
        **field_help_texts(Treats, "treat_size"),
    }


def landing(request):
    if request.user.is_authenticated:
        if getattr(user, "managed_storehome_id", None):
            goToPage = "dashboard"
        if user.is_superuser:
            goToPage = "system-overview"
        else:
            raise Exception("User is not a superuser and is not assigned to a storehome")
        return redirect(goToPage)
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

    return render(
        request,
        "system_overview.html",
        {
            "user": user,
            "system_stats": system_stats,
            "library_quality_stats": library_quality_stats,
        },
    )


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_library(request):
    labels = {
        **product_labels(),
        **field_labels(StorageItem, "quantity"),
    }
    help_texts = {
        **product_help_texts(),
        **field_help_texts(StorageItem, "quantity"),
    }
    return render(
        request,
        "product_library.html",
        {"user": request.user, "labels": labels, "help_texts": help_texts},
    )


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_view(request, product_id):
    labels = {
        **product_labels(),
        **field_labels(StorageItem, "quantity", "date_stored", "expiry_date"),
        **field_labels(Storehome, "name", "address", extra_labels={"storehome": "Storehome"}),
    }
    help_texts = {
        **product_help_texts(),
        **field_help_texts(StorageItem, "quantity", "date_stored", "expiry_date"),
        **field_help_texts(Storehome, "name", "address"),
    }
    return render(
        request,
        "product_view.html",
        {
            "user": request.user,
            "product_id": product_id,
            "labels": labels,
            "help_texts": help_texts,
        },
    )


@login_required(login_url="landing")
@permission_required("core.view_userprofile", login_url="dashboard")
def manage_users(request):
    field_names = (
        "username",
        "email",
        "first_name",
        "last_name",
        "managed_storehome",
        "password",
    )
    labels = field_labels(
        UserProfile,
        *field_names,
        extra_labels={"name": "Name"},
    )
    help_texts = field_help_texts(UserProfile, *field_names)
    return render(
        request,
        "manage_users.html",
        {"user": request.user, "labels": labels, "help_texts": help_texts},
    )


@login_required(login_url="landing")
@permission_required("core.view_storehome", login_url="dashboard")
def manage_storehomes(request):
    field_names = ("name", "address", "latitude", "longitude")
    labels = field_labels(
        Storehome,
        *field_names,
        extra_labels={"managers": "Managers"},
    )
    help_texts = field_help_texts(Storehome, *field_names)
    return render(
        request,
        "manage_storehomes.html",
        {"user": request.user, "labels": labels, "help_texts": help_texts},
    )


def _managed_inventory_page(request, template_name, **extra_context):
    allowed, _reason = UserProfile.can_manage_storehome_inventory(request)
    if not allowed:
        return redirect("dashboard")

    storage_fields = ("quantity", "expiry_date", "note")
    labels = {
        **product_labels(),
        **field_labels(StorageItem, *storage_fields),
    }
    help_texts = {
        **product_help_texts(),
        **field_help_texts(StorageItem, *storage_fields),
    }
    return render(
        request,
        template_name,
        {
            "user": request.user,
            "storehome": request.user.managed_storehome,
            "labels": labels,
            "help_texts": help_texts,
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
    return _managed_inventory_page(
        request,
        "storehome_inventory.html",
        can_view_product_detail=request.user.has_perm("core.view_product"),
    )

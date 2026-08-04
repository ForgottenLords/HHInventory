from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.utils.text import capfirst

from core.models import Canned, Food, Kibble, Product, StorageItem, Storehome, UserProfile


def field_labels(model, *names, **kwargs):
    """Field verbose names keyed by field name, so templates never restate a model's wording."""
    labelsDict={name: capfirst(model._meta.get_field(name).verbose_name) for name in names}
    labelsDict.update(kwargs.get("extra_labels", {}))
    return labelsDict

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
            "last_updated",
        ),
        **field_labels(Food, "life_stages", "proteins", "special_diet"),
        **field_labels(
            Kibble,
            "weight",
            "kibble_size",
            extra_labels={"weight_unit": Kibble.WEIGHT_UNIT},
        ),
        **field_labels(Canned, "texture"),
    }


def landing(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "login.html")


@login_required(login_url="landing")
def dashboard(request):
    user = request.user
    managed_storehome = getattr(user, "managed_storehome", None)
    storehome_stats = None
    if managed_storehome is not None:
        storehome_stats = StorageItem.inventory_stats(
            StorageItem.objects.filter(storehome=managed_storehome)
        )

    system_stats = None
    library_quality_stats = None
    if user.is_superuser:
        system_stats = {
            **StorageItem.inventory_stats(),
        }
        library_quality_stats = Product.library_quality_stats()

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "managed_storehome": managed_storehome,
            "storehome_stats": storehome_stats,
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
    return render(
        request,
        "product_library.html",
        {"user": request.user, "labels": labels},
    )


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_view(request, product_id):
    labels = {
        **product_labels(),
        **field_labels(StorageItem, "quantity", "date_stored", "expiry_date"),
        **field_labels(Storehome, "name", "address", extra_labels={"storehome": "Storehome"}),
    }
    return render(
        request,
        "product_view.html",
        {"user": request.user, "product_id": product_id, "labels": labels},
    )


@login_required(login_url="landing")
@permission_required("core.view_userprofile", login_url="dashboard")
def manage_users(request):
    labels = field_labels(
        UserProfile,
        "username",
        "email",
        "first_name",
        "last_name",
        "managed_storehome",
        "password",
        extra_labels={"name": "Name"},
    )
    return render(request, "manage_users.html", {"user": request.user, "labels": labels})


@login_required(login_url="landing")
@permission_required("core.view_storehome", login_url="dashboard")
def manage_storehomes(request):
    labels = field_labels(
        Storehome,
        "name",
        "address",
        "latitude",
        "longitude",
        extra_labels={"managers": "Managers"},
    )
    return render(request, "manage_storehomes.html", {"user": request.user, "labels": labels})


@login_required(login_url="landing")
def incoming_inventory(request):
    allowed, _reason = UserProfile.can_manage_incoming_inventory(request)
    if not allowed:
        return redirect("dashboard")

    storehome = request.user.managed_storehome
    labels = {
        **product_labels(),
        **field_labels(StorageItem, "quantity", "expiry_date", "note"),
    }
    return render(
        request,
        "incoming_inventory.html",
        {
            "user": request.user,
            "storehome": storehome,
            "labels": labels,
        },
    )


@login_required(login_url="landing")
def outgoing_inventory(request):
    allowed, _reason = UserProfile.can_manage_incoming_inventory(request)
    if not allowed:
        return redirect("dashboard")

    storehome = request.user.managed_storehome
    labels = {
        **product_labels(),
        **field_labels(StorageItem, "quantity", "expiry_date", "note"),
    }
    return render(
        request,
        "outgoing_inventory.html",
        {
            "user": request.user,
            "storehome": storehome,
            "labels": labels,
        },
    )


@login_required(login_url="landing")
def storehome_inventory(request):
    allowed, _reason = UserProfile.can_manage_incoming_inventory(request)
    if not allowed:
        return redirect("dashboard")

    storehome = request.user.managed_storehome
    labels = {
        **product_labels(),
        **field_labels(StorageItem, "quantity", "expiry_date", "note"),
    }
    return render(
        request,
        "storehome_inventory.html",
        {
            "user": request.user,
            "storehome": storehome,
            "labels": labels,
            "can_view_product_detail": request.user.has_perm("core.view_product"),
        },
    )

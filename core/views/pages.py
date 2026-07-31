from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.utils.text import capfirst

from core.models import Canned, Food, Kibble, Product, Storehome, UserProfile


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
    return render(request, "dashboard.html", {"user": request.user})


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_library(request):
    return render(
        request,
        "product_library.html",
        {"user": request.user, "labels": product_labels()},
    )


@login_required(login_url="landing")
@permission_required("core.view_product", login_url="dashboard")
def product_view(request, product_id):
    return render(
        request,
        "product_view.html",
        {"user": request.user, "product_id": product_id, "labels": product_labels()},
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
    labels = field_labels(Storehome, "name", "address", extra_labels={"managers": "Managers"})
    return render(request, "manage_storehomes.html", {"user": request.user, "labels": labels})

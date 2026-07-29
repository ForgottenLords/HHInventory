from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render


def landing(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "login.html")


@login_required(login_url="landing")
def dashboard(request):
    return render(request, "dashboard.html", {"user": request.user})


@login_required(login_url="landing")
@permission_required("core.view_userprofile", login_url="dashboard")
def manage_users(request):
    return render(request, "manage_users.html", {"user": request.user})


@login_required(login_url="landing")
@permission_required("core.view_storehome", login_url="dashboard")
def manage_storehomes(request):
    return render(request, "manage_storehomes.html", {"user": request.user})

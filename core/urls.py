from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health_check, name="health-check"),
    path("", views.landing, name="landing"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("manage/users/", views.manage_users, name="manage-users"),
    path("manage/storehomes/", views.manage_storehomes, name="manage-storehomes"),
]

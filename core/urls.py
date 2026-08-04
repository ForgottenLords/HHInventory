from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health_check, name="health-check"),
    path("", views.landing, name="landing"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("products/", views.product_library, name="product-library"),
    path("products/<int:product_id>/", views.product_view, name="product-view"),
    path("manage/users/", views.manage_users, name="manage-users"),
    path("manage/storehomes/", views.manage_storehomes, name="manage-storehomes"),
    path("inventory/incoming/", views.incoming_inventory, name="incoming-inventory"),
    path("inventory/outgoing/", views.outgoing_inventory, name="outgoing-inventory"),
    path("inventory/", views.storehome_inventory, name="storehome-inventory"),
]

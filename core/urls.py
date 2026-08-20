from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.HealthCheckView.as_view(), name="health-check"),
    path("", views.LandingView.as_view(), name="landing"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("system-overview/", views.SystemOverviewView.as_view(), name="system-overview"),
    path("products/", views.ProductLibraryView.as_view(), name="product-library"),
    path("products/<int:product_id>/", views.ProductView.as_view(), name="product-view"),
    path("manage/users/", views.ManageUsersView.as_view(), name="manage-users"),
    path("manage/storehomes/", views.ManageStorehomesView.as_view(), name="manage-storehomes"),
    path("inventory/incoming/", views.IncomingInventoryView.as_view(), name="incoming-inventory"),
    path("inventory/outgoing/", views.OutgoingInventoryView.as_view(), name="outgoing-inventory"),
    path("inventory/", views.StorehomeInventoryView.as_view(), name="storehome-inventory"),
]

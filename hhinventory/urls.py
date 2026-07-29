from django.contrib import admin
from django.urls import include, path
from graphene_django.views import GraphQLView

from hhinventory.settings import DEBUG

from core.schema import schema

urlpatterns = [
    path("admin/", admin.site.urls),
    path("graphql/", GraphQLView.as_view(graphiql=DEBUG, schema=schema)),
    path("", include("core.urls")),
]

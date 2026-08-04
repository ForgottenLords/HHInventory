from django.conf import settings
from django.conf.urls.static import static
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

# Local FileSystemStorage media only; S3-backed deploys serve MEDIA_URL from the bucket.
if not settings.USE_S3:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

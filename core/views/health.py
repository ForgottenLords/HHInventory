from django.http import JsonResponse
from django.views import View


class HealthCheckView(View):
    """Unauthenticated liveness probe for EB, uptime monitors, and deploy checks.

    Always returns 200 without hitting the database or login flow so infrastructure
    health stays decoupled from the rest of the app.
    """

    def get(self, request, *args, **kwargs):
        return JsonResponse({"status": "ok"})

    head = get

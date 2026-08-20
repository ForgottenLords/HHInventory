from django.http import JsonResponse


def health_check(request):
    """Unauthenticated liveness probe for EB, uptime monitors, and deploy checks.

    Always returns 200 without hitting the database or login flow so infrastructure
    health stays decoupled from the rest of the app.
    """
    return JsonResponse({"status": "ok"})

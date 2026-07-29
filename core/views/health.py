from django.http import JsonResponse


def health_check(request):
    """Used by the EB load balancer / target group health check."""
    return JsonResponse({"status": "ok"})

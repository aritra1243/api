"""URL configuration for the optimizer app."""
from django.urls import path
from optimizer.views import HealthCheckView, RouteOptimizerView

app_name = "optimizer"

urlpatterns = [
    path("route/", RouteOptimizerView.as_view(), name="route-optimizer"),
    path("route/health/", HealthCheckView.as_view(), name="health-check"),
]

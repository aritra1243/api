"""
Optimizer app configuration.
Auto-loads fuel station data into memory on startup for fast access.
"""
from django.apps import AppConfig


class OptimizerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "optimizer"
    verbose_name = "Fuel Route Optimizer"

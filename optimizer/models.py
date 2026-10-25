"""
Django models for the optimizer app.
FuelStation is loaded from CSV into SQLite for fast spatial lookup.
"""
from django.db import models


class FuelStation(models.Model):
    """Represents a fuel station with location and price data."""
    station_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    latitude = models.FloatField(db_index=True)
    longitude = models.FloatField(db_index=True)
    retail_price = models.FloatField()  # price per gallon in USD

    class Meta:
        indexes = [
            models.Index(fields=['latitude', 'longitude']),
            models.Index(fields=['state']),
        ]

    def __str__(self):
        return f"{self.name} - {self.city}, {self.state} (${self.retail_price:.3f}/gal)"

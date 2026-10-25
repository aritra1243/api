"""
Serializers for the optimizer API.
"""
from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """Input validation for the route planning request."""
    start = serializers.CharField(
        max_length=200,
        help_text="Starting location within the USA (e.g., 'New York, NY')"
    )
    finish = serializers.CharField(
        max_length=200,
        help_text="Destination location within the USA (e.g., 'Los Angeles, CA')"
    )

    def validate_start(self, value):
        return value.strip()

    def validate_finish(self, value):
        return value.strip()

    def validate(self, data):
        if data["start"].lower() == data["finish"].lower():
            raise serializers.ValidationError("Start and finish locations must be different.")
        return data


class FuelStopSerializer(serializers.Serializer):
    """Represents a single fuel stop."""
    stop_number = serializers.IntegerField()
    station_name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    distance_from_start_miles = serializers.FloatField()
    miles_to_this_stop = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost_at_stop = serializers.FloatField()

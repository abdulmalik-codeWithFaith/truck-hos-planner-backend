from rest_framework import serializers

MAX_CYCLE_HOURS = 70


class TripRequestSerializer(serializers.Serializer):
    current_location = serializers.CharField(max_length=255, allow_blank=False, trim_whitespace=True)
    pickup_location = serializers.CharField(max_length=255, allow_blank=False, trim_whitespace=True)
    dropoff_location = serializers.CharField(max_length=255, allow_blank=False, trim_whitespace=True)
    current_cycle_used = serializers.FloatField(min_value=0, max_value=MAX_CYCLE_HOURS)

    def validate(self, data):
        pickup = data["pickup_location"].strip().lower()
        dropoff = data["dropoff_location"].strip().lower()
        if pickup == dropoff:
            raise serializers.ValidationError(
                {"dropoff_location": "Pickup and dropoff locations can't be the same."}
            )
        return data
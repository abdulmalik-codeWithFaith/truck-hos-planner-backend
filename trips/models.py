from django.db import models


class Trip(models.Model):
    """
    Optional record of a planned trip. The trip calculation itself is
    stateless (nothing downstream depends on this row existing) — kept
    minimal per the assessment's guidance not to spend time on database
    features beyond what's asked for.
    """

    current_location = models.CharField(max_length=255)
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)
    current_cycle_used = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.current_location} -> {self.pickup_location} -> {self.dropoff_location}"
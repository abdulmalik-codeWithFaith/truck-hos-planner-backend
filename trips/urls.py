from django.urls import path

from .views import plan_trip

urlpatterns = [
    path("trips/plan/", plan_trip, name="plan-trip"),
]
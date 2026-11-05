from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("users/<int:record_id>/action/", views.record_action, name="record-action"),
    path("change-manager/", views.change_l1_manager, name="change-l1-manager"),
]

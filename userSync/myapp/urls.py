from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("change-manager/", views.change_l1_manager, name="change-l1-manager"),
]

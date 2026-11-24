from django.urls import path

from . import views

urlpatterns = [
    # Core Dashboard & Sync operations
    path("", views.index, name="index"),
    path("users/<int:record_id>/action/", views.record_action, name="record-action"),
    path("change-manager/", views.change_l1_manager, name="change-l1-manager"),

    # Authentication
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("auth/sso/", views.ms_sso_start, name="ms-sso-start"),
    path("auth/callback/", views.ms_sso_callback, name="ms-sso-callback"),

    # User Management & RBAC
    path("user-management/", views.user_management_list, name="user-management"),
    path("user-management/create/", views.user_create, name="user-create"),
    path("user-management/<int:user_id>/edit/", views.user_edit, name="user-edit"),
    path("user-management/<int:user_id>/toggle/", views.user_toggle_status, name="user-toggle"),
    path("user-management/<int:user_id>/reset-password/", views.user_reset_password, name="user-reset-password"),
    path("user-management/<int:user_id>/delete/", views.user_delete, name="user-delete"),
]

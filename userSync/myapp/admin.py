from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import EmployeeSyncRecord, SyncLog, SyncRun, UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "User Profile (RBAC)"
    fields = ("role", "department", "phone")


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = ("username", "email", "first_name", "last_name", "get_role", "is_staff", "is_active")
    list_select_related = ("profile",)

    @admin.display(description="Role")
    def get_role(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.get_role_display() if profile else ("Super Admin" if obj.is_superuser else "Standard")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "department", "phone", "updated_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email", "department")


class SyncLogInline(admin.TabularInline):
    model = SyncLog
    extra = 0
    can_delete = False
    fields = ("created_at", "source_employee_id", "status", "level", "error_code", "message")
    readonly_fields = fields
    ordering = ("-created_at",)
    show_change_link = True


@admin.register(EmployeeSyncRecord)
class EmployeeSyncRecordAdmin(admin.ModelAdmin):
    list_display = ("source_employee_id", "email", "last_status", "attempt_count", "updated_at")
    search_fields = ("source_employee_id", "email")
    list_filter = ("last_status",)


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "started_at", "finished_at", "total_records", "synced_records", "failed_records")
    list_filter = ("status",)
    ordering = ("-started_at",)
    inlines = (SyncLogInline,)


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ("sync_run", "source_employee_id", "status", "level", "error_code", "message_preview", "created_at")
    search_fields = ("source_employee_id", "message", "error_code")
    list_filter = ("status", "level")
    ordering = ("-created_at",)
    list_select_related = ("sync_run",)
    date_hierarchy = "created_at"
    list_per_page = 100

    @admin.display(description="Details")
    def message_preview(self, obj):
        if len(obj.message) > 120:
            return f"{obj.message[:117]}..."
        return obj.message

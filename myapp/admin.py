from django.contrib import admin

from .models import EmployeeSyncRecord, SyncLog, SyncRun


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


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ("sync_run", "source_employee_id", "status", "level", "created_at")
    search_fields = ("source_employee_id", "message")
    list_filter = ("status", "level")
    ordering = ("-created_at",)

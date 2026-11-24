from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


class EmployeeSyncRecord(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_SYNCED = "SYNCED"
    STATUS_FAILED = "FAILED"
    STATUS_SKIPPED = "SKIPPED"

    source_employee_id = models.CharField(max_length=100)
    email = models.EmailField(blank=True, null=True)
    first_name = models.CharField(max_length=150, blank=True, null=True)
    last_name = models.CharField(max_length=150, blank=True, null=True)
    department = models.CharField(max_length=200, blank=True, null=True)
    designation = models.CharField(max_length=200, blank=True, null=True)
    l1_manager_code = models.CharField(max_length=100, blank=True, null=True)
    l1_manager_name = models.CharField(max_length=200, blank=True, null=True)
    l1_manager_email = models.EmailField(blank=True, null=True)
    l1_manager_object_id = models.CharField(max_length=200, blank=True, null=True)
    manager_sync_status = models.CharField(max_length=30, blank=True, default="")
    manager_last_synced_at = models.DateTimeField(blank=True, null=True)
    manager_error_message = models.TextField(blank=True, default="")
    payload = models.JSONField(blank=True, default=dict)
    graph_payload = models.JSONField(blank=True, default=dict)
    attempt_history = models.JSONField(blank=True, default=list)
    last_status = models.CharField(max_length=30, default=STATUS_PENDING)
    last_error_message = models.TextField(blank=True, default="")
    last_error_code = models.CharField(max_length=50, blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "myapp_employee_sync_record"
        constraints = [
            models.UniqueConstraint(fields=["source_employee_id", "email"], name="unique_employee_sync_record")
        ]

    def __str__(self) -> str:
        return self.source_employee_id or self.email or "Sync record"



class SyncRun(models.Model):
    STATUS_RUNNING = "RUNNING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"

    status = models.CharField(max_length=20, default=STATUS_RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(blank=True, null=True)
    total_records = models.PositiveIntegerField(default=0)
    synced_records = models.PositiveIntegerField(default=0)
    failed_records = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "myapp_sync_run"

    def __str__(self) -> str:
        return f"SyncRun {self.id} ({self.status})"


class SyncLog(models.Model):
    LEVEL_INFO = "INFO"
    LEVEL_WARNING = "WARNING"
    LEVEL_ERROR = "ERROR"

    sync_run = models.ForeignKey(SyncRun, related_name="logs", on_delete=models.CASCADE)
    source_employee_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=20, default="SUCCESS")
    message = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=50, blank=True, default="")
    level = models.CharField(max_length=20, default=LEVEL_INFO)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "myapp_sync_log"

    def __str__(self) -> str:
        return f"{self.status}::{self.source_employee_id}"


class UserProfile(models.Model):
    ROLE_SUPER_ADMIN = "SUPER_ADMIN"
    ROLE_ADMIN = "ADMIN"
    ROLE_STANDARD = "STANDARD"
    ROLE_READ_ONLY = "READ_ONLY"

    ROLE_CHOICES = (
        (ROLE_SUPER_ADMIN, "Super Admin"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_STANDARD, "Standard User"),
        (ROLE_READ_ONLY, "Read-Only User"),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default=ROLE_STANDARD)
    department = models.CharField(max_length=150, blank=True, default="")
    phone = models.CharField(max_length=50, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "myapp_user_profile"

    def __str__(self) -> str:
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_super_admin(self) -> bool:
        return self.user.is_superuser or self.role == self.ROLE_SUPER_ADMIN

    @property
    def is_admin(self) -> bool:
        return self.is_super_admin or self.role == self.ROLE_ADMIN

    @property
    def can_manage_users(self) -> bool:
        return self.is_admin

    @property
    def can_retry_sync(self) -> bool:
        return self.is_admin or self.role == self.ROLE_STANDARD

    @property
    def can_deactivate(self) -> bool:
        return self.is_admin

    @property
    def can_change_manager(self) -> bool:
        return self.is_admin or self.role == self.ROLE_STANDARD

    @property
    def is_read_only(self) -> bool:
        return not self.is_admin and self.role == self.ROLE_READ_ONLY


def get_user_profile(user) -> UserProfile:
    """Safely retrieve or create UserProfile for any user."""
    if not user or not user.is_authenticated:
        return None
    profile = getattr(user, "profile", None)
    if profile is None:
        role = (
            UserProfile.ROLE_SUPER_ADMIN
            if user.is_superuser
            else UserProfile.ROLE_ADMIN
            if user.is_staff
            else UserProfile.ROLE_STANDARD
        )
        profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"role": role})
    return profile


@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        role = (
            UserProfile.ROLE_SUPER_ADMIN
            if instance.is_superuser
            else UserProfile.ROLE_ADMIN
            if instance.is_staff
            else UserProfile.ROLE_STANDARD
        )
        UserProfile.objects.get_or_create(user=instance, defaults={"role": role})

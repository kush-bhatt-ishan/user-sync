from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import render
from django.utils import timezone

from .models import EmployeeSyncRecord, SyncLog, SyncRun
from .sync_services import MicrosoftGraphClient


def index(request):
    return render(request, "myapp/index.html")


@staff_member_required
def change_l1_manager(request):
    employees = EmployeeSyncRecord.objects.exclude(email__isnull=True).exclude(email="").order_by(
        "source_employee_id"
    )
    context = {
        "employees": employees,
        "selected_employee_id": "",
        "manager_email": "",
    }

    if request.method != "POST":
        return render(request, "myapp/change_manager.html", context)

    employee_id = request.POST.get("employee_id", "").strip()
    manager_email = request.POST.get("manager_email", "").strip()
    action = request.POST.get("action", "")
    context.update({"selected_employee_id": employee_id, "manager_email": manager_email})

    employee = employees.filter(pk=employee_id).first()
    if employee is None:
        context["error"] = "Select a synced employee with an email address."
        return render(request, "myapp/change_manager.html", context)

    try:
        validate_email(manager_email)
    except ValidationError:
        context["error"] = "Enter a valid manager email address."
        return render(request, "myapp/change_manager.html", context)

    if action not in {"preview", "change"}:
        context["error"] = "Choose whether to look up the manager or confirm the manager change."
        return render(request, "myapp/change_manager.html", context)

    graph_client = MicrosoftGraphClient()
    try:
        employee_user = graph_client.get_user_by_email(employee.email)
        manager_user = graph_client.get_user_by_email(manager_email)
    except (RuntimeError, ValueError) as exc:
        context["error"] = str(exc)
        return render(request, "myapp/change_manager.html", context)

    if employee_user["id"] == manager_user["id"]:
        context["error"] = "An employee cannot be assigned as their own manager."
        return render(request, "myapp/change_manager.html", context)

    context.update(
        {
            "employee_preview": employee_user,
            "manager_preview": manager_user,
        }
    )
    if action == "preview":
        return render(request, "myapp/change_manager.html", context)

    if (
        request.POST.get("expected_employee_oid") != employee_user["id"]
        or request.POST.get("expected_manager_oid") != manager_user["id"]
    ):
        context["error"] = "The employee or manager account changed since preview. Review the current details and confirm again."
        return render(request, "myapp/change_manager.html", context)

    sync_run = SyncRun.objects.create(status=SyncRun.STATUS_RUNNING, total_records=1)
    SyncLog.objects.create(
        sync_run=sync_run,
        source_employee_id=employee.source_employee_id,
        status="STARTED",
        level=SyncLog.LEVEL_INFO,
        message=(
            f"Manual L1 manager change started; employee={employee.email}; "
            f"new_manager={manager_email}"
        ),
    )
    try:
        graph_client.update_user_manager(employee_user["id"], manager_user["id"])
        verified_manager = graph_client.get_user_manager(employee_user["id"])
        if not verified_manager:
            raise RuntimeError(
                "Microsoft Graph accepted the update, but no manager was returned during verification."
            )
        if verified_manager.get("id") != manager_user["id"]:
            raise RuntimeError(
                "Microsoft Graph returned a different manager during verification; the change could not be confirmed."
            )
    except (RuntimeError, ValueError) as exc:
        employee.manager_sync_status = "FAILED"
        employee.manager_error_message = str(exc)
        employee.save(update_fields=["manager_sync_status", "manager_error_message", "updated_at"])
        sync_run.status = SyncRun.STATUS_FAILED
        sync_run.failed_records = 1
        sync_run.notes = str(exc)
        sync_run.finished_at = timezone.now()
        sync_run.save(update_fields=["status", "failed_records", "notes", "finished_at"])
        SyncLog.objects.create(
            sync_run=sync_run,
            source_employee_id=employee.source_employee_id,
            status="FAILED",
            level=SyncLog.LEVEL_ERROR,
            error_code="MANAGER_CHANGE_FAILED",
            message=(
                f"Manual L1 manager change failed: {exc}; employee={employee.email}; "
                f"requested_manager={manager_email}"
            ),
        )
        context["error"] = str(exc)
        return render(request, "myapp/change_manager.html", context)

    manager_display_email = (
        verified_manager.get("mail") or verified_manager.get("userPrincipalName") or manager_email
    )
    employee.l1_manager_code = None
    employee.l1_manager_name = verified_manager.get("displayName") or ""
    employee.l1_manager_email = manager_display_email
    employee.l1_manager_object_id = verified_manager["id"]
    employee.manager_sync_status = "UPDATED"
    employee.manager_last_synced_at = timezone.now()
    employee.manager_error_message = ""
    employee.save(
        update_fields=[
            "l1_manager_code",
            "l1_manager_name",
            "l1_manager_email",
            "l1_manager_object_id",
            "manager_sync_status",
            "manager_last_synced_at",
            "manager_error_message",
            "updated_at",
        ]
    )

    sync_run.status = SyncRun.STATUS_COMPLETED
    sync_run.synced_records = 1
    sync_run.finished_at = timezone.now()
    sync_run.save(update_fields=["status", "synced_records", "finished_at"])
    SyncLog.objects.create(
        sync_run=sync_run,
        source_employee_id=employee.source_employee_id,
        status="SUCCESS",
        level=SyncLog.LEVEL_INFO,
        message=(
            f"Manual L1 manager change verified; employee={employee.email}; "
            f"manager={verified_manager.get('displayName') or manager_display_email}; "
            f"manager_email={manager_display_email}; manager_object_id={verified_manager['id']}"
        ),
    )
    context["success"] = {
        "employee": employee_user,
        "manager": verified_manager,
        "sync_run_id": sync_run.id,
    }
    return render(request, "myapp/change_manager.html", context)

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import EmployeeSyncRecord, SyncLog, SyncRun
from .sync_services import (
    MicrosoftGraphClient,
    build_graph_payload,
    check_employment_status_for_microsoft_sync,
    update_sync_record_status,
)


@staff_member_required
def index(request):
    records = list(EmployeeSyncRecord.objects.order_by("-updated_at"))
    latest_attempts = {record.pk: _latest_attempt(record) for record in records}
    missing_log_ids = {
        record.source_employee_id
        for record in records
        if record.source_employee_id
        if not latest_attempts[record.pk].get("action")
    }
    latest_logs = {}
    if missing_log_ids:
        for log in (
            SyncLog.objects.order_by("-created_at")
            .values("source_employee_id", "status", "message", "error_code", "created_at")
            .iterator(chunk_size=500)
        ):
            employee_id = log["source_employee_id"]
            if employee_id in missing_log_ids and employee_id not in latest_logs:
                latest_logs[employee_id] = log
                if len(latest_logs) == len(missing_log_ids):
                    break

    dashboard_records = []
    for record in records:
        payload = record.payload if isinstance(record.payload, dict) else {}
        graph_payload = record.graph_payload if isinstance(record.graph_payload, dict) else {}
        latest_attempt = latest_attempts[record.pk]
        action = latest_attempt.get("action") or _action_from_log(
            latest_logs.get(record.source_employee_id)
        )
        status = (record.last_status or "").upper()
        if status == EmployeeSyncRecord.STATUS_FAILED:
            action_label = "Needs attention"
        elif status == EmployeeSyncRecord.STATUS_SKIPPED:
            action_label = "Skipped"
        else:
            action_label = {
                "created": "Created",
                "updated": "Updated",
                "deactivated": "Deactivated",
                "not-found": "Not found in Microsoft",
                "manager": "Manager updated",
            }.get(action, "Synced" if status == EmployeeSyncRecord.STATUS_SYNCED else "Pending")
        name = " ".join(part for part in (record.first_name, record.last_name) if part).strip()
        name = name or payload.get("EmployeeName") or payload.get("employeeName") or ""
        dashboard_records.append(
            {
                "record": record,
                "name": name or record.email or f"Employee {record.source_employee_id}",
                "action": action,
                "action_label": action_label,
                "status": status,
                "employment_status": (
                    payload.get("EmploymentStatus")
                    or payload.get("employmentStatus")
                    or "Not provided"
                ),
                "company": payload.get("Company") or "",
                "office": payload.get("Office") or "",
                "city": payload.get("City") or "",
                "state": payload.get("State") or "",
                "country": payload.get("Country") or "",
                "mobile": payload.get("MobileNo") or "",
                "manager_name": record.l1_manager_name or payload.get("L1ManagerName") or "",
                "manager_email": record.l1_manager_email or payload.get("L1ManagerEmail") or "",
                "account_enabled": (
                    None if action == "not-found" else graph_payload.get("accountEnabled")
                ),
                "account_not_found": action == "not-found",
            }
        )

    all_dashboard_records = dashboard_records
    counts = {
        "total": len(dashboard_records),
        "created": sum(item["action"] == "created" for item in dashboard_records),
        "updated": sum(item["action"] == "updated" for item in dashboard_records),
        "deactivated": sum(item["action"] == "deactivated" for item in dashboard_records),
        "failed": sum(item["status"] == EmployeeSyncRecord.STATUS_FAILED for item in dashboard_records),
    }
    recent_failures = [
        item
        for item in dashboard_records
        if item["status"] == EmployeeSyncRecord.STATUS_FAILED
    ][:5]

    query = request.GET.get("q", "").strip()
    selected_filter = request.GET.get("filter", "all")
    valid_filters = {"all", "created", "updated", "deactivated", "failed", "skipped"}
    if selected_filter not in valid_filters:
        selected_filter = "all"
    if query:
        needle = query.casefold()
        dashboard_records = [
            item
            for item in dashboard_records
            if needle
            in " ".join(
                str(value or "")
                for value in (
                    item["name"],
                    item["record"].email,
                    item["record"].source_employee_id,
                    item["record"].department,
                    item["record"].designation,
                )
            ).casefold()
        ]
    if selected_filter in {"created", "updated", "deactivated"}:
        dashboard_records = [
            item for item in dashboard_records if item["action"] == selected_filter
        ]
    elif selected_filter == "failed":
        dashboard_records = [
            item
            for item in dashboard_records
            if item["status"] == EmployeeSyncRecord.STATUS_FAILED
        ]
    elif selected_filter == "skipped":
        dashboard_records = [
            item
            for item in dashboard_records
            if item["status"] == EmployeeSyncRecord.STATUS_SKIPPED
        ]

    record_page = Paginator(dashboard_records, 25).get_page(request.GET.get("page"))
    log_page = Paginator(
        SyncLog.objects.select_related("sync_run").order_by("-created_at"), 50
    ).get_page(request.GET.get("log_page"))
    employees_by_id = {}
    for item in all_dashboard_records:
        if item["record"].source_employee_id:
            employees_by_id.setdefault(item["record"].source_employee_id, item)
    log_entries = []
    for log in log_page:
        employee = employees_by_id.get(log.source_employee_id)
        log_entries.append(
            {
                "log": log,
                "employee_name": employee["name"] if employee else "",
                "employee_email": employee["record"].email if employee else "",
            }
        )
    runs = SyncRun.objects.order_by("-started_at")[:8]
    return render(
        request,
        "myapp/index.html",
        {
            "counts": counts,
            "record_page": record_page,
            "log_page": log_page,
            "log_entries": log_entries,
            "runs": runs,
            "recent_failures": recent_failures,
            "query": query,
            "selected_filter": selected_filter,
        },
    )


def _latest_attempt(record):
    history = record.attempt_history
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        return history[-1]
    return {}


def _action_from_log(log):
    if not log:
        return ""
    message = log["message"].lower()
    if "action=created" in message:
        return "created"
    if "action=updated" in message:
        return "updated"
    if "action=deactivated" in message:
        return "deactivated"
    if "action=not-found" in message:
        return "not-found"
    if "manual l1 manager change" in message:
        return "manager"
    return ""


@staff_member_required
def record_action(request, record_id):
    if request.method != "POST":
        return redirect("index")
    record = get_object_or_404(EmployeeSyncRecord, pk=record_id)
    action = request.POST.get("action", "")
    if action not in {"retry", "deactivate"}:
        messages.error(request, "Choose a valid account action.")
        return redirect(f"{reverse('index')}#users")

    employee = record.payload or {
        "employeeCode": record.source_employee_id,
        "firstName": record.first_name or "",
        "lastName": record.last_name or "",
        "email": record.email or "",
        "department": record.department or "",
        "designation": record.designation or "",
    }
    graph_payload = build_graph_payload(employee)
    action_name = "sync retry" if action == "retry" else "deactivation"
    sync_run = SyncRun.objects.create(status=SyncRun.STATUS_RUNNING, total_records=1)
    SyncLog.objects.create(
        sync_run=sync_run,
        source_employee_id=record.source_employee_id,
        status="STARTED",
        level=SyncLog.LEVEL_INFO,
        message=f"Manual {action_name} started; email={record.email or '-'}",
    )

    if action == "retry":
        is_allowed, _, skip_reason = check_employment_status_for_microsoft_sync(employee)
        if not is_allowed:
            update_sync_record_status(
                record,
                EmployeeSyncRecord.STATUS_SKIPPED,
                message=skip_reason,
                error_code="STATUS_NOT_MATCHED",
                source_payload=employee,
                graph_payload=graph_payload,
                action="skipped",
            )
            sync_run.status = SyncRun.STATUS_COMPLETED
            sync_run.notes = skip_reason
            sync_run.finished_at = timezone.now()
            sync_run.save(update_fields=["status", "notes", "finished_at"])
            SyncLog.objects.create(
                sync_run=sync_run,
                source_employee_id=record.source_employee_id,
                status="SKIPPED",
                level=SyncLog.LEVEL_WARNING,
                error_code="STATUS_NOT_MATCHED",
                message=skip_reason,
            )
            messages.warning(request, f"Sync was skipped: {skip_reason}")
            return redirect(f"{reverse('index')}#users")

    try:
        graph_client = MicrosoftGraphClient()
        if action == "retry":
            result = graph_client.upsert_user(employee)
            manager_result = getattr(graph_client, "manager_sync_result", {})
            record.manager_sync_status = str(manager_result.get("status", "")).upper()
            record.manager_error_message = manager_result.get("reason", "")
            if manager_result.get("status") in {"updated", "unchanged"}:
                record.manager_last_synced_at = timezone.now()
            record.save(
                update_fields=[
                    "manager_sync_status",
                    "manager_error_message",
                    "manager_last_synced_at",
                ]
            )
            success_message = (
                "User created successfully in Microsoft"
                if result == "created"
                else "User updated successfully in Microsoft"
            )
            persisted_action = result
            log_message = f"{success_message}; action={result}"
        else:
            result = graph_client.deactivate_user(record.email or "")
            persisted_action = result
            if result == "deactivated":
                graph_payload["accountEnabled"] = False
                success_message = "User deactivated successfully in Microsoft"
            else:
                success_message = "No Microsoft account was found; there was nothing to deactivate"
            log_message = f"{success_message}; action={result}; email={record.email or '-'}"
        record.graph_payload = graph_payload
        record.save(update_fields=["graph_payload"])
    except Exception as exc:
        error_message = str(exc) or "The Microsoft account action could not be completed."
        update_sync_record_status(
            record,
            EmployeeSyncRecord.STATUS_FAILED,
            message=error_message,
            error_code="MANUAL_ACTION_FAILED",
            source_payload=employee,
            graph_payload=graph_payload,
            action=action,
        )
        sync_run.status = SyncRun.STATUS_FAILED
        sync_run.failed_records = 1
        sync_run.notes = error_message
        sync_run.finished_at = timezone.now()
        sync_run.save(
            update_fields=["status", "failed_records", "notes", "finished_at"]
        )
        SyncLog.objects.create(
            sync_run=sync_run,
            source_employee_id=record.source_employee_id,
            status="FAILED",
            level=SyncLog.LEVEL_ERROR,
            error_code="MANUAL_ACTION_FAILED",
            message=f"Manual {action_name} failed: {error_message}",
        )
        messages.error(request, f"Action failed: {error_message}")
        return redirect(f"{reverse('index')}#users")

    update_sync_record_status(
        record,
        EmployeeSyncRecord.STATUS_SYNCED,
        message=success_message,
        source_payload=employee,
        graph_payload=graph_payload,
        action=persisted_action,
    )
    sync_run.status = SyncRun.STATUS_COMPLETED
    sync_run.synced_records = 1
    sync_run.finished_at = timezone.now()
    sync_run.save(update_fields=["status", "synced_records", "finished_at"])
    SyncLog.objects.create(
        sync_run=sync_run,
        source_employee_id=record.source_employee_id,
        status="SUCCESS",
        level=SyncLog.LEVEL_INFO,
        message=log_message,
    )
    messages.success(request, success_message)
    return redirect(f"{reverse('index')}#users")


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

import uuid
from typing import Optional

import msal
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import (
    EmployeeSyncRecord,
    SyncLog,
    SyncRun,
    UserProfile,
    get_user_profile,
)
from .sync_services import (
    MicrosoftGraphClient,
    build_graph_payload,
    check_employment_status_for_microsoft_sync,
    update_sync_record_status,
)


# ── SSO Helpers ───────────────────────────────────────────────────────────────

def _msal_app() -> Optional[msal.ConfidentialClientApplication]:
    tenant_id = getattr(settings, "AZ_TENANT_ID", "")
    client_id = getattr(settings, "AZ_SSO_CLIENT_ID", "") or getattr(settings, "AZ_CLIENT_ID", "")
    client_secret = getattr(settings, "AZ_SSO_CLIENT_SECRET", "") or getattr(settings, "AZ_CLIENT_SECRET", "")

    if not tenant_id or not client_id or not client_secret:
        return None

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    return msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )


# ── Authentication Views ──────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect(request.GET.get("next") or "index")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        remember_me = request.POST.get("remember_me") in ["on", "true", "1", "True"]

        user = authenticate(request, username=username, password=password)
        if user is not None:
            if not user.is_active:
                messages.error(
                    request,
                    "Your account has been deactivated. Please contact a system administrator.",
                )
                return render(request, "myapp/login.html", {"username": username})

            login(request, user)

            # Session expiration handling
            if remember_me:
                request.session.set_expiry(2592000)  # 30 days
            else:
                request.session.set_expiry(0)  # Expires on browser close

            next_url = request.GET.get("next") or request.POST.get("next") or "index"
            return redirect(next_url)
        else:
            # Check if user exists and credentials match but account was deactivated
            u = User.objects.filter(username=username).first()
            if u and u.check_password(password) and not u.is_active:
                messages.error(
                    request,
                    "Your account has been deactivated. Please contact a system administrator.",
                )
            else:
                messages.error(request, "Invalid username or password. Please try again.")

    return render(
        request,
        "myapp/login.html",
        {
            "next": request.GET.get("next", ""),
            "sso_configured": bool(getattr(settings, "AZ_SSO_CLIENT_ID", "") or getattr(settings, "AZ_CLIENT_ID", "")),
        },
    )


def logout_view(request):
    logout(request)
    messages.success(request, "You have been securely signed out.")
    return redirect("login")


def ms_sso_start(request):
    """Initiate Microsoft Entra ID single sign-on."""
    app = _msal_app()
    if not app:
        messages.error(
            request,
            "Microsoft SSO is not configured in environment settings. Please use username and password.",
        )
        return redirect("login")

    state = str(uuid.uuid4())
    request.session["ms_state"] = state
    redirect_uri = getattr(settings, "AZ_SSO_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback/")
    flow = app.initiate_auth_code_flow(
        scopes=["User.Read"],
        redirect_uri=redirect_uri,
        state=state,
    )
    request.session["ms_flow"] = flow
    return redirect(flow["auth_uri"])


def ms_sso_callback(request):
    """Callback receiver for Azure AD OAuth2."""
    flow = request.session.pop("ms_flow", None)
    if not flow:
        messages.error(request, "SSO session expired or invalid. Please try again.")
        return redirect("login")

    app = _msal_app()
    if not app:
        messages.error(request, "SSO client is not configured.")
        return redirect("login")

    try:
        result = app.acquire_token_by_auth_code_flow(flow, dict(request.GET))
    except Exception as exc:
        messages.error(request, f"Microsoft SSO authentication failed: {exc}")
        return redirect("login")

    if "error" in result:
        err_msg = result.get("error_description") or result.get("error")
        messages.error(request, f"Microsoft SSO error: {err_msg}")
        return redirect("login")

    id_claims = result.get("id_token_claims", {})
    email = (id_claims.get("preferred_username") or id_claims.get("email") or "").lower()
    name = id_claims.get("name", email)

    if not email:
        messages.error(request, "Microsoft did not return a valid email address.")
        return redirect("login")

    # Get or create local user
    user, created = User.objects.get_or_create(
        username=email,
        defaults={
            "email": email,
            "first_name": name.split()[0] if name else "",
            "last_name": " ".join(name.split()[1:]) if " " in name else "",
        },
    )

    if not user.is_active:
        messages.error(request, "Your account is deactivated. Contact an administrator.")
        return redirect("login")

    # Ensure profile
    profile = get_user_profile(user)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, f"Signed in as {name or email} via Microsoft SSO.")
    return redirect("index")


# ── Dashboard View ────────────────────────────────────────────────────────────

@login_required
def index(request):
    profile = get_user_profile(request.user)

    records = list(EmployeeSyncRecord.objects.order_by("-updated_at"))
    latest_attempts = {record.pk: _latest_attempt(record) for record in records}
    missing_log_ids = {
        record.source_employee_id
        for record in records
        if record.source_employee_id and not latest_attempts[record.pk].get("action")
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
        "skipped": sum(item["status"] == EmployeeSyncRecord.STATUS_SKIPPED for item in dashboard_records),
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

    context = {
        "profile": profile,
        "counts": counts,
        "record_page": record_page,
        "log_page": log_page,
        "log_entries": log_entries,
        "runs": runs,
        "recent_failures": recent_failures,
        "query": query,
        "selected_filter": selected_filter,
        "active_tab": "overview",
        # Permission shortcuts for UI
        "can_retry": profile.can_retry_sync if profile else False,
        "can_deactivate": profile.can_deactivate if profile else False,
        "can_change_mgr": profile.can_change_manager if profile else False,
        "can_manage_users": profile.can_manage_users if profile else False,
        "is_read_only": profile.is_read_only if profile else True,
    }
    return render(request, "myapp/index.html", context)


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


# ── Record Actions (Retry Sync / Deactivate) ──────────────────────────────────

@login_required
def record_action(request, record_id):
    if request.method != "POST":
        return redirect("index")

    profile = get_user_profile(request.user)
    action = request.POST.get("action", "").strip()

    if action not in {"retry", "deactivate"}:
        messages.error(request, "Choose a valid account action.")
        return redirect(f"{reverse('index')}#users")

    # Backend Permission Enforcement
    if action == "retry" and not (profile and profile.can_retry_sync):
        messages.error(request, "Permission denied: Your role does not allow retrying employee synchronization.")
        return redirect(f"{reverse('index')}#users")

    if action == "deactivate" and not (profile and profile.can_deactivate):
        messages.error(request, "Permission denied: Your role does not allow deactivating Microsoft accounts.")
        return redirect(f"{reverse('index')}#users")

    record = get_object_or_404(EmployeeSyncRecord, pk=record_id)
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
        message=f"Manual {action_name} triggered by user {request.user.username}; email={record.email or '-'}",
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
        sync_run.save(update_fields=["status", "failed_records", "notes", "finished_at"])
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


# ── Change L1 Manager View ────────────────────────────────────────────────────

@login_required
def change_l1_manager(request):
    profile = get_user_profile(request.user)
    if not (profile and profile.can_change_manager):
        messages.error(request, "Permission denied: Your role does not allow changing employee managers.")
        return redirect("index")

    employees = EmployeeSyncRecord.objects.exclude(email__isnull=True).exclude(email="").order_by(
        "source_employee_id"
    )
    context = {
        "profile": profile,
        "employees": employees,
        "selected_employee_id": "",
        "manager_email": "",
        "active_tab": "change_manager",
        "can_retry": profile.can_retry_sync,
        "can_deactivate": profile.can_deactivate,
        "can_change_mgr": profile.can_change_manager,
        "can_manage_users": profile.can_manage_users,
        "is_read_only": profile.is_read_only,
    }

    if request.method != "POST":
        return render(request, "myapp/change_manager.html", context)

    employee_id = request.POST.get("employee_id", "").strip()
    manager_email = request.POST.get("manager_email", "").strip()
    action = request.POST.get("action", "").strip()
    context.update({"selected_employee_id": employee_id, "manager_email": manager_email})

    employee = employees.filter(pk=employee_id).first()
    if employee is None:
        context["error"] = "Select a valid synced employee with an email address."
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
        context["error"] = "The employee or manager account changed since preview. Review current details and confirm again."
        return render(request, "myapp/change_manager.html", context)

    sync_run = SyncRun.objects.create(status=SyncRun.STATUS_RUNNING, total_records=1)
    SyncLog.objects.create(
        sync_run=sync_run,
        source_employee_id=employee.source_employee_id,
        status="STARTED",
        level=SyncLog.LEVEL_INFO,
        message=(
            f"Manual L1 manager change started by {request.user.username}; "
            f"employee={employee.email}; new_manager={manager_email}"
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


# ── User Management & RBAC Module ─────────────────────────────────────────────

@login_required
def user_management_list(request):
    profile = get_user_profile(request.user)
    if not (profile and profile.can_manage_users):
        messages.error(request, "Permission denied: User management is restricted to Administrators.")
        return redirect("index")

    query = request.GET.get("q", "").strip()
    role_filter = request.GET.get("role", "all")

    users_qs = User.objects.select_related("profile").order_by("-date_joined")
    if query:
        users_qs = users_qs.filter(
            username__icontains=query
        ) | users_qs.filter(email__icontains=query) | users_qs.filter(first_name__icontains=query) | users_qs.filter(last_name__icontains=query)

    if role_filter != "all":
        users_qs = users_qs.filter(profile__role=role_filter)

    # Make sure every user in listing has profile loaded/created
    users_list = []
    for u in users_qs:
        p = get_user_profile(u)
        users_list.append({"user": u, "profile": p})

    # Compute user stats for summary cards
    all_users = list(User.objects.select_related("profile").all())
    for u in all_users:
        get_user_profile(u)

    user_counts = {
        "total": len(all_users),
        "super_admin": sum(1 for u in all_users if (hasattr(u, "profile") and u.profile.role == UserProfile.ROLE_SUPER_ADMIN) or u.is_superuser),
        "admin": sum(1 for u in all_users if hasattr(u, "profile") and u.profile.role == UserProfile.ROLE_ADMIN and not u.is_superuser),
        "standard": sum(1 for u in all_users if hasattr(u, "profile") and u.profile.role == UserProfile.ROLE_STANDARD and not u.is_superuser and not u.is_staff),
        "read_only": sum(1 for u in all_users if hasattr(u, "profile") and u.profile.role == UserProfile.ROLE_READ_ONLY),
        "inactive": sum(1 for u in all_users if not u.is_active),
    }

    paginator = Paginator(users_list, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "profile": profile,
        "page_obj": page_obj,
        "query": query,
        "role_filter": role_filter,
        "role_choices": UserProfile.ROLE_CHOICES,
        "user_counts": user_counts,
        "active_tab": "user_management",
        "can_retry": profile.can_retry_sync,
        "can_deactivate": profile.can_deactivate,
        "can_change_mgr": profile.can_change_manager,
        "can_manage_users": profile.can_manage_users,
        "is_read_only": profile.is_read_only,
    }
    return render(request, "myapp/user_management.html", context)


@login_required
def user_create(request):
    if request.method != "POST":
        return redirect("user-management")

    profile = get_user_profile(request.user)
    if not (profile and profile.can_manage_users):
        messages.error(request, "Permission denied.")
        return redirect("index")

    username = request.POST.get("username", "").strip()
    email = request.POST.get("email", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    password = request.POST.get("password", "")
    role = request.POST.get("role", UserProfile.ROLE_STANDARD).strip()
    department = request.POST.get("department", "").strip()
    phone = request.POST.get("phone", "").strip()
    is_active = request.POST.get("is_active") in ["on", "true", "1", "True"]

    if not username or not password:
        messages.error(request, "Username and password are required.")
        return redirect("user-management")

    if User.objects.filter(username=username).exists():
        messages.error(request, f"User with username '{username}' already exists.")
        return redirect("user-management")

    # Security check: Non-superadmins cannot create Super Admin accounts
    if role == UserProfile.ROLE_SUPER_ADMIN and not profile.is_super_admin:
        messages.error(request, "Only Super Admins can assign the Super Admin role.")
        role = UserProfile.ROLE_ADMIN

    with transaction.atomic():
        is_staff = role in [UserProfile.ROLE_SUPER_ADMIN, UserProfile.ROLE_ADMIN]
        is_superuser = role == UserProfile.ROLE_SUPER_ADMIN
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_active=is_active,
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
        user_prof = get_user_profile(user)
        user_prof.role = role
        user_prof.department = department
        user_prof.phone = phone
        user_prof.save()

    messages.success(request, f"User '{username}' created successfully.")
    return redirect("user-management")


@login_required
def user_edit(request, user_id):
    if request.method != "POST":
        return redirect("user-management")

    current_profile = get_user_profile(request.user)
    if not (current_profile and current_profile.can_manage_users):
        messages.error(request, "Permission denied.")
        return redirect("index")

    target_user = get_object_or_404(User, pk=user_id)
    target_profile = get_user_profile(target_user)

    # Security: Only superadmin can edit another superadmin
    if target_profile.is_super_admin and not current_profile.is_super_admin:
        messages.error(request, "Only Super Admins can modify Super Admin accounts.")
        return redirect("user-management")

    email = request.POST.get("email", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    role = request.POST.get("role", target_profile.role).strip()
    department = request.POST.get("department", "").strip()
    phone = request.POST.get("phone", "").strip()
    is_active = request.POST.get("is_active") in ["on", "true", "1", "True"]

    # Security: Non-superadmin cannot elevate role to Super Admin
    if role == UserProfile.ROLE_SUPER_ADMIN and not current_profile.is_super_admin:
        role = target_profile.role

    # Prevent deactivating yourself
    if target_user.pk == request.user.pk and not is_active:
        messages.warning(request, "You cannot deactivate your own account.")
        is_active = True

    with transaction.atomic():
        target_user.email = email
        target_user.first_name = first_name
        target_user.last_name = last_name
        target_user.is_active = is_active
        target_user.is_staff = role in [UserProfile.ROLE_SUPER_ADMIN, UserProfile.ROLE_ADMIN]
        target_user.is_superuser = role == UserProfile.ROLE_SUPER_ADMIN
        target_user.save()

        target_profile.role = role
        target_profile.department = department
        target_profile.phone = phone
        target_profile.save()

    messages.success(request, f"User '{target_user.username}' updated successfully.")
    return redirect("user-management")


@login_required
def user_toggle_status(request, user_id):
    if request.method != "POST":
        return redirect("user-management")

    current_profile = get_user_profile(request.user)
    if not (current_profile and current_profile.can_manage_users):
        messages.error(request, "Permission denied.")
        return redirect("index")

    target_user = get_object_or_404(User, pk=user_id)
    if target_user.pk == request.user.pk:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("user-management")

    target_profile = get_user_profile(target_user)
    if target_profile.is_super_admin and not current_profile.is_super_admin:
        messages.error(request, "Only Super Admins can alter Super Admin status.")
        return redirect("user-management")

    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=["is_active"])
    status_label = "activated" if target_user.is_active else "deactivated"
    messages.success(request, f"User '{target_user.username}' was {status_label}.")
    return redirect("user-management")


@login_required
def user_reset_password(request, user_id):
    if request.method != "POST":
        return redirect("user-management")

    current_profile = get_user_profile(request.user)
    if not (current_profile and current_profile.can_manage_users):
        messages.error(request, "Permission denied.")
        return redirect("index")

    target_user = get_object_or_404(User, pk=user_id)
    target_profile = get_user_profile(target_user)

    if target_profile.is_super_admin and not current_profile.is_super_admin:
        messages.error(request, "Only Super Admins can reset a Super Admin's password.")
        return redirect("user-management")

    new_password = request.POST.get("new_password", "").strip()
    if not new_password or len(new_password) < 6:
        messages.error(request, "Password must be at least 6 characters long.")
        return redirect("user-management")

    target_user.set_password(new_password)
    target_user.save(update_fields=["password"])
    messages.success(request, f"Password for user '{target_user.username}' has been reset successfully.")
    return redirect("user-management")


@login_required
def user_delete(request, user_id):
    if request.method != "POST":
        return redirect("user-management")

    current_profile = get_user_profile(request.user)
    if not (current_profile and current_profile.is_super_admin):
        messages.error(request, "Permission denied: Only Super Admins can delete user accounts.")
        return redirect("user-management")

    target_user = get_object_or_404(User, pk=user_id)
    if target_user.pk == request.user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user-management")

    username = target_user.username
    target_user.delete()
    messages.success(request, f"User '{username}' was permanently deleted.")
    return redirect("user-management")

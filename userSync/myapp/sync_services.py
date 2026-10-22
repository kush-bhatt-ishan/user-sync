import base64
import copy
import importlib
import json
import logging
import os
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, parse, request

from django.conf import settings
from django.db.models import Q

from .models import EmployeeSyncRecord, SyncLog, SyncRun

logger = logging.getLogger(__name__)


def _load_env_file() -> None:
    settings_module = importlib.import_module(settings.SETTINGS_MODULE)
    env_paths = (
        Path(settings.BASE_DIR) / ".env",
        Path(settings_module.__file__).resolve().parent / ".env",
    )
    env_path = next((path for path in env_paths if path.exists()), env_paths[0])
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not os.environ.get(key, "").strip():
            os.environ[key] = value


_load_env_file()


def _get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key)
    if value and value.strip():
        return value
    return getattr(settings, key, default)


ALLOWED_EMPLOYMENT_STATUSES = {"active", "inactive"}


def get_employment_status(employee: Dict[str, Any]) -> str:
    if not isinstance(employee, dict):
        return ""
    flattened = _flatten_employee_payload(employee)
    return str(flattened.get("EmploymentStatus") or "").strip()


def check_employment_status_for_microsoft_sync(employee: Dict[str, Any]) -> tuple[bool, str, str]:
    """
    Check if employee's EmploymentStatus is 'Active' or 'Inactive' (case-insensitive).
    Returns (is_allowed, raw_status, reason_message).
    """
    raw_status = get_employment_status(employee)
    normalized = raw_status.lower()
    if normalized in ALLOWED_EMPLOYMENT_STATUSES:
        return True, raw_status, ""

    if raw_status:
        reason = (
            f"Skipped sync to Microsoft: EmploymentStatus is '{raw_status}'. "
            "Only records with EmploymentStatus 'Active' or 'Inactive' are updated in Microsoft."
        )
    else:
        reason = (
            "Skipped sync to Microsoft: EmploymentStatus is missing or blank. "
            "Only records with EmploymentStatus 'Active' or 'Inactive' are updated in Microsoft."
        )
    return False, raw_status, reason


GRAPH_FIELD_MAPPINGS = [
    ("accountEnabled", ["EmploymentStatus"], lambda employee, field_names: _normalize_account_enabled(_resolve_field_value(employee, field_names))),
    ("displayName", ["EmployeeName", "FirstName", "LastName", "firstName", "lastName"], lambda employee, field_names: _resolve_display_name(employee, field_names)),
    ("givenName", ["FirstName", "firstName"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("surname", ["LastName", "lastName"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("mail", ["OfficialMailID", "email", "workEmail"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("userPrincipalName", ["OfficialMailID", "email", "workEmail"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("jobTitle", ["DesignationName", "designation", "jobTitle"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("department", ["Department", "department"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("companyName", ["Company", "company", "companyName"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("officeLocation", ["Office", "office", "officeLocation"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("city", ["City", "city"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("state", ["State", "state"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("country", ["Country", "country"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("employeeId", ["EmployeeCode", "employeeCode", "employeeId", "employee_id"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
    ("mobilePhone", ["MobileNo", "mobilePhone", "mobileNo"], lambda employee, field_names: _resolve_string(_resolve_field_value(employee, field_names))),
]


def _resolve_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _resolve_display_name(employee: Dict[str, Any], field_names: Any) -> str:
    if isinstance(field_names, (list, tuple)):
        direct_name = _resolve_field_value(employee, ["EmployeeName", "employeeName"])
        if direct_name:
            return _resolve_string(direct_name)
        first_name = _resolve_string(_resolve_field_value(employee, ["FirstName", "firstName"]))
        last_name = _resolve_string(_resolve_field_value(employee, ["LastName", "lastName"]))
        if first_name or last_name:
            return f"{first_name} {last_name}".strip()
    return ""


def _normalize_account_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"active", "true", "yes", "y", "probation", "probationary", "1"}:
            return True
        if normalized in {"inactive", "false", "no", "n", "0"}:
            return False
    return True


def _resolve_field_value(employee: Dict[str, Any], field_names: Any) -> Any:
    if isinstance(field_names, (list, tuple)):
        for field_name in field_names:
            value = _resolve_field_value(employee, field_name)
            if value not in (None, ""):
                return value
        return None

    if not isinstance(employee, dict):
        return None

    value = employee.get(field_names)
    if value not in (None, ""):
        return value

    nested_basic_detail = employee.get("BasicDetail")
    if isinstance(nested_basic_detail, dict):
        value = nested_basic_detail.get(field_names)
        if value not in (None, ""):
            return value

    nested_basic_details = employee.get("BasicDetails")
    if isinstance(nested_basic_details, dict):
        detail = nested_basic_details.get("BasicDetail")
        if isinstance(detail, dict):
            value = detail.get(field_names)
            if value not in (None, ""):
                return value

    nested_employee = employee.get("employee")
    if isinstance(nested_employee, dict):
        value = _resolve_field_value(nested_employee, field_names)
        if value not in (None, ""):
            return value

    return None


def build_graph_payload(employee: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for target_field, source_fields, transformer in GRAPH_FIELD_MAPPINGS:
        payload[target_field] = transformer(employee, source_fields)

    email = _resolve_string(payload.get("mail") or payload.get("userPrincipalName"))
    payload.setdefault("mail", email)
    payload.setdefault("userPrincipalName", email)
    payload.setdefault("mailNickname", email.split("@")[0] if "@" in email else email)
    payload.setdefault("accountEnabled", True)
    default_company_name = getattr(settings, "DEFAULT_COMPANY_NAME", "Ishan Technologies")
    payload["companyName"] = _resolve_string(payload.get("companyName")) or default_company_name

    if not payload.get("displayName"):
        first_name = _resolve_string(payload.get("givenName"))
        last_name = _resolve_string(payload.get("surname"))
        payload["displayName"] = f"{first_name} {last_name}".strip()

    # Graph rejects empty-string values for optional string fields (must be
    # a real value or omitted entirely) — strip anything blank before sending.
    required_even_if_blank = {"mail", "userPrincipalName", "mailNickname", "accountEnabled"}
    payload = {
        key: value
        for key, value in payload.items()
        if key in required_even_if_blank or value not in (None, "")
    }

    return payload


def get_retry_candidates() -> Iterable[EmployeeSyncRecord]:
    return EmployeeSyncRecord.objects.filter(Q(last_status="FAILED") | Q(last_status="PENDING_RETRY")).order_by("updated_at")


def _flatten_employee_payload(employee: Dict[str, Any]) -> Dict[str, Any]:
    flattened = copy.deepcopy(employee)
    if not isinstance(flattened, dict):
        return {}

    direct = flattened.get("employee") if isinstance(flattened.get("employee"), dict) else flattened
    if isinstance(direct.get("BasicDetails"), dict) and isinstance(direct["BasicDetails"].get("BasicDetail"), dict):
        basic_detail = direct["BasicDetails"]["BasicDetail"]
        flattened.update({
            "EmployeeName": basic_detail.get("EmployeeName") or basic_detail.get("employeeName") or flattened.get("EmployeeName") or "",
            "EmployeeCode": basic_detail.get("EmployeeCode") or basic_detail.get("employeeCode") or flattened.get("EmployeeCode") or "",
            "FirstName": basic_detail.get("FirstName") or basic_detail.get("firstName") or flattened.get("FirstName") or "",
            "LastName": basic_detail.get("LastName") or basic_detail.get("lastName") or flattened.get("LastName") or "",
            "OfficialMailID": basic_detail.get("OfficialMailID") or basic_detail.get("email") or basic_detail.get("workEmail") or flattened.get("OfficialMailID") or "",
            "Department": basic_detail.get("Department") or basic_detail.get("department") or flattened.get("Department") or "",
            "DesignationName": basic_detail.get("DesignationName") or basic_detail.get("designation") or basic_detail.get("jobTitle") or flattened.get("DesignationName") or "",
            "Company": basic_detail.get("Company") or basic_detail.get("company") or flattened.get("Company") or "",
            "Office": basic_detail.get("Office") or basic_detail.get("office") or flattened.get("Office") or "",
            "City": basic_detail.get("City") or basic_detail.get("city") or flattened.get("City") or "",
            "State": basic_detail.get("State") or basic_detail.get("state") or flattened.get("State") or "",
            "Country": basic_detail.get("Country") or basic_detail.get("country") or flattened.get("Country") or "",
            "MobileNo": basic_detail.get("MobileNo") or basic_detail.get("mobileNo") or basic_detail.get("mobilePhone") or flattened.get("MobileNo") or "",
            "EmploymentStatus": basic_detail.get("EmploymentStatus") or basic_detail.get("employmentStatus") or flattened.get("EmploymentStatus") or "",
            "L1ManagerCode": basic_detail.get("L1ManagerCode") or basic_detail.get("l1ManagerCode") or flattened.get("L1ManagerCode") or "",
            "L1ManagerName": basic_detail.get("L1ManagerName") or basic_detail.get("l1ManagerName") or flattened.get("L1ManagerName") or "",
            "L1ManagerEmail": basic_detail.get("L1ManagerEmail") or basic_detail.get("l1ManagerEmail") or flattened.get("L1ManagerEmail") or "",
            "HRManagerEmail": basic_detail.get("HRManagerEmail") or basic_detail.get("hrManagerEmail") or flattened.get("HRManagerEmail") or "",
            "HrManagerCode": basic_detail.get("HrManagerCode") or basic_detail.get("hrManagerCode") or flattened.get("HrManagerCode") or "",
            "HrManagerName": basic_detail.get("HrManagerName") or basic_detail.get("hrManagerName") or flattened.get("HrManagerName") or "",
            "employeeCode": basic_detail.get("EmployeeCode") or basic_detail.get("employeeCode") or flattened.get("employeeCode") or "",
            "firstName": basic_detail.get("FirstName") or basic_detail.get("firstName") or flattened.get("firstName") or "",
            "lastName": basic_detail.get("LastName") or basic_detail.get("lastName") or flattened.get("lastName") or "",
            "email": basic_detail.get("OfficialMailID") or basic_detail.get("email") or basic_detail.get("workEmail") or flattened.get("email") or "",
            "department": basic_detail.get("Department") or basic_detail.get("department") or flattened.get("department") or "",
            "designation": basic_detail.get("DesignationName") or basic_detail.get("designation") or basic_detail.get("jobTitle") or flattened.get("designation") or "",
        })
    else:
        flattened.update({
            "EmployeeName": direct.get("EmployeeName") or direct.get("employeeName") or flattened.get("EmployeeName") or "",
            "EmployeeCode": direct.get("employeeCode") or direct.get("employeeId") or direct.get("employee_code") or flattened.get("EmployeeCode") or "",
            "FirstName": direct.get("firstName") or direct.get("first_name") or flattened.get("FirstName") or "",
            "LastName": direct.get("lastName") or direct.get("last_name") or flattened.get("LastName") or "",
            "OfficialMailID": direct.get("OfficialMailID") or direct.get("email") or direct.get("workEmail") or flattened.get("OfficialMailID") or "",
            "Department": direct.get("Department") or direct.get("department") or direct.get("departmentName") or flattened.get("Department") or "",
            "DesignationName": direct.get("DesignationName") or direct.get("designation") or direct.get("jobTitle") or flattened.get("DesignationName") or "",
            "Company": direct.get("Company") or direct.get("company") or flattened.get("Company") or "",
            "Office": direct.get("Office") or direct.get("office") or flattened.get("Office") or "",
            "City": direct.get("City") or direct.get("city") or flattened.get("City") or "",
            "State": direct.get("State") or direct.get("state") or flattened.get("State") or "",
            "Country": direct.get("Country") or direct.get("country") or flattened.get("Country") or "",
            "MobileNo": direct.get("MobileNo") or direct.get("mobileNo") or direct.get("mobilePhone") or flattened.get("MobileNo") or "",
            "EmploymentStatus": direct.get("EmploymentStatus") or direct.get("employmentStatus") or flattened.get("EmploymentStatus") or "",
            "L1ManagerCode": direct.get("L1ManagerCode") or direct.get("l1ManagerCode") or direct.get("managerCode") or flattened.get("L1ManagerCode") or "",
            "L1ManagerName": direct.get("L1ManagerName") or direct.get("l1ManagerName") or direct.get("managerName") or flattened.get("L1ManagerName") or "",
            "L1ManagerEmail": direct.get("L1ManagerEmail") or direct.get("l1ManagerEmail") or direct.get("managerEmail") or flattened.get("L1ManagerEmail") or "",
            "HRManagerEmail": direct.get("HRManagerEmail") or direct.get("hrManagerEmail") or flattened.get("HRManagerEmail") or "",
            "HrManagerCode": direct.get("HrManagerCode") or direct.get("hrManagerCode") or flattened.get("HrManagerCode") or "",
            "HrManagerName": direct.get("HrManagerName") or direct.get("hrManagerName") or flattened.get("HrManagerName") or "",
            "employeeCode": direct.get("employeeCode") or direct.get("employeeId") or direct.get("employee_code") or flattened.get("employeeCode") or "",
            "firstName": direct.get("firstName") or direct.get("first_name") or flattened.get("firstName") or "",
            "lastName": direct.get("lastName") or direct.get("last_name") or flattened.get("lastName") or "",
            "email": direct.get("email") or direct.get("workEmail") or flattened.get("email") or "",
            "department": direct.get("department") or direct.get("departmentName") or flattened.get("department") or "",
            "designation": direct.get("designation") or direct.get("jobTitle") or flattened.get("designation") or "",
        })

    return flattened


def get_l1_manager_details(employee: Dict[str, Any]) -> Dict[str, str]:
    flattened = _flatten_employee_payload(copy.deepcopy(employee))
    manager_code = (
        flattened.get("L1ManagerCode")
        or flattened.get("l1ManagerCode")
        or flattened.get("managerCode")
        or ""
    ).strip()
    manager_name = (
        flattened.get("L1ManagerName")
        or flattened.get("l1ManagerName")
        or flattened.get("managerName")
        or ""
    ).strip()
    manager_email = (
        flattened.get("L1ManagerEmail")
        or flattened.get("l1ManagerEmail")
        or flattened.get("managerEmail")
        or ""
    ).strip()
    return {
        "l1_manager_code": manager_code,
        "l1_manager_name": manager_name,
        "l1_manager_email": manager_email,
    }


def get_or_create_sync_record(employee: Dict[str, Any]) -> EmployeeSyncRecord:
    flattened_payload = _flatten_employee_payload(copy.deepcopy(employee))
    manager_details = get_l1_manager_details(employee)
    payload_for_storage = {
        key: value
        for key, value in flattened_payload.items()
        if key in {
            "EmployeeName",
            "EmployeeCode",
            "FirstName",
            "LastName",
            "OfficialMailID",
            "Department",
            "DesignationName",
            "Company",
            "Office",
            "City",
            "State",
            "Country",
            "MobileNo",
            "EmploymentStatus",
            "employeeCode",
            "firstName",
            "lastName",
            "email",
            "department",
            "designation",
            "L1ManagerCode",
            "L1ManagerName",
            "L1ManagerEmail",
            "HRManagerEmail",
            "HrManagerCode",
            "HrManagerName",
        }
    }
    graph_payload = build_graph_payload(flattened_payload)
    source_employee_id = (
        flattened_payload.get("employeeCode")
        or flattened_payload.get("employee_id")
        or flattened_payload.get("id")
        or flattened_payload.get("employeeId")
        or ""
    ).strip()
    email = (flattened_payload.get("email") or flattened_payload.get("OfficialMailID") or "").strip()
    first_name = (flattened_payload.get("firstName") or flattened_payload.get("FirstName") or flattened_payload.get("first_name") or "").strip()
    last_name = (flattened_payload.get("lastName") or flattened_payload.get("LastName") or flattened_payload.get("last_name") or "").strip()
    department = (flattened_payload.get("department") or flattened_payload.get("Department") or "").strip()
    designation = (flattened_payload.get("designation") or flattened_payload.get("DesignationName") or flattened_payload.get("jobTitle") or "").strip()
    manager_code = manager_details["l1_manager_code"]
    manager_name = manager_details["l1_manager_name"]
    manager_email = manager_details["l1_manager_email"]

    record, created = EmployeeSyncRecord.objects.get_or_create(
        source_employee_id=source_employee_id,
        email=email or None,
        defaults={
            "first_name": first_name,
            "last_name": last_name,
            "department": department,
            "designation": designation,
            "l1_manager_code": manager_code or None,
            "l1_manager_name": manager_name or None,
            "l1_manager_email": manager_email or None,
            "payload": payload_for_storage,
            "graph_payload": graph_payload,
            "last_status": EmployeeSyncRecord.STATUS_PENDING,
            "attempt_count": 0,
        },
    )
    if created:
        return record

    updated_fields = []
    if not record.email and email:
        record.email = email
        updated_fields.append("email")
    if not record.first_name and first_name:
        record.first_name = first_name
        updated_fields.append("first_name")
    if not record.last_name and last_name:
        record.last_name = last_name
        updated_fields.append("last_name")
    if not record.department and department:
        record.department = department
        updated_fields.append("department")
    if not record.designation and designation:
        record.designation = designation
        updated_fields.append("designation")
    if record.l1_manager_code != (manager_code or None):
        record.l1_manager_code = manager_code or None
        updated_fields.append("l1_manager_code")
    if record.l1_manager_name != (manager_name or None):
        record.l1_manager_name = manager_name or None
        updated_fields.append("l1_manager_name")
    if record.l1_manager_email != (manager_email or None):
        record.l1_manager_email = manager_email or None
        updated_fields.append("l1_manager_email")
    if not record.payload:
        record.payload = payload_for_storage
        updated_fields.append("payload")
    elif record.payload != payload_for_storage:
        record.payload = payload_for_storage
        record.graph_payload = graph_payload
        updated_fields.extend(["payload", "graph_payload"])
    elif not record.graph_payload:
        record.graph_payload = graph_payload
        updated_fields.append("graph_payload")
    if updated_fields:
        record.save(update_fields=updated_fields)
    return record


def update_sync_record_status(
    record: EmployeeSyncRecord,
    status: str,
    *,
    message: Optional[str] = None,
    error_code: Optional[str] = None,
    source_payload: Optional[Dict[str, Any]] = None,
    graph_payload: Optional[Dict[str, Any]] = None,
) -> EmployeeSyncRecord:
    from django.utils import timezone

    record.last_status = status
    record.last_error_message = message or ""
    record.last_error_code = error_code or ""
    record.attempt_count += 1
    if status == EmployeeSyncRecord.STATUS_SYNCED:
        record.last_synced_at = timezone.now()

    history_entry = {
        "status": status,
        "message": message or "",
        "error_code": error_code or "",
        "attempt_count": record.attempt_count,
        "attempted_at": timezone.now().isoformat(),
    }
    if source_payload is not None:
        history_entry["source_payload"] = copy.deepcopy(source_payload)
    if graph_payload is not None:
        history_entry["graph_payload"] = copy.deepcopy(graph_payload)

    history = list(record.attempt_history or [])
    history.append(history_entry)
    record.attempt_history = history
    record.save()
    return record


def create_sync_log(sync_run: SyncRun, record: EmployeeSyncRecord, status: str, message: str, level: str = "INFO", error_code: str = "") -> SyncLog:
    return SyncLog.objects.create(
        sync_run=sync_run,
        source_employee_id=record.source_employee_id,
        status=status,
        message=message,
        error_code=error_code,
        level=level,
    )


class PeopleStrongClient:
    def __init__(self) -> None:
        self.token_url = _get_setting("PS_TOKEN_URL") or ""
        self.api_url = _get_setting("PS_API_URL") or ""
        self.api_key = _get_setting("PS_APIKEY") or ""
        self.client_id = _get_setting("PS_CLIENT_ID") or ""
        self.client_secret = _get_setting("PS_CLIENT_SECRET") or ""
        self.integration_name = _get_setting("PS_INTEGRATION_NAME") or ""

    def get_access_token(self) -> str:
        if not self.token_url:
            raise ValueError("PS_TOKEN_URL is missing or empty in .env configuration.")
        auth_header = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        payload = parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
        req = request.Request(
            self.token_url,
            data=payload,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            return str(data.get("access_token") or "")

    def fetch_employees(self) -> List[Dict[str, Any]]:
        if not self.api_url:
            raise ValueError("PS_API_URL is missing or empty in .env configuration.")
        token = self.get_access_token()
        payload = {
            "integrationMasterName": self.integration_name,
            "pageNo": 1,
            "pageSize": 1000,
        }
        req = request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "apikey": self.api_key,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        return self._normalize_employees(body)

    def _normalize_employees(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [self._normalize_employee(item) for item in payload]
        if isinstance(payload, dict):
            for key in ("data", "items", "result", "employees", "records", "EmployeeMasterData"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [self._normalize_employee(item) for item in value]
                if isinstance(value, dict):
                    nested = value.get("data") or value.get("items") or value.get("EmployeeMasterData")
                    if isinstance(nested, list):
                        return [self._normalize_employee(item) for item in nested]

            root = payload.get("root")
            if isinstance(root, dict):
                employee_master = root.get("EmployeeMaster")
                if isinstance(employee_master, dict):
                    employee_data = employee_master.get("EmployeeMasterData")
                    if isinstance(employee_data, list):
                        return [self._normalize_employee(item) for item in employee_data]

            return [self._normalize_employee(payload)]
        return []

    def _normalize_employee(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}

        normalized = copy.deepcopy(raw)
        direct = raw.get("employee") if isinstance(raw.get("employee"), dict) else raw

        if isinstance(direct.get("BasicDetails"), dict) and isinstance(direct["BasicDetails"].get("BasicDetail"), dict):
            basic_detail = direct["BasicDetails"]["BasicDetail"]
            normalized.setdefault("BasicDetail", basic_detail)
            normalized.setdefault("BasicDetails", direct.get("BasicDetails"))
            normalized.setdefault("employee", direct)
            normalized["EmployeeName"] = basic_detail.get("EmployeeName") or basic_detail.get("employeeName") or normalized.get("EmployeeName") or ""
            normalized["EmployeeCode"] = basic_detail.get("EmployeeCode") or basic_detail.get("employeeCode") or normalized.get("EmployeeCode") or ""
            normalized["FirstName"] = basic_detail.get("FirstName") or basic_detail.get("firstName") or normalized.get("FirstName") or ""
            normalized["LastName"] = basic_detail.get("LastName") or basic_detail.get("lastName") or normalized.get("LastName") or ""
            normalized["OfficialMailID"] = basic_detail.get("OfficialMailID") or basic_detail.get("email") or basic_detail.get("workEmail") or normalized.get("OfficialMailID") or ""
            normalized["Department"] = basic_detail.get("Department") or basic_detail.get("department") or normalized.get("Department") or ""
            normalized["DesignationName"] = basic_detail.get("DesignationName") or basic_detail.get("designation") or basic_detail.get("jobTitle") or normalized.get("DesignationName") or ""
            normalized["Company"] = basic_detail.get("Company") or basic_detail.get("company") or normalized.get("Company") or ""
            normalized["Office"] = basic_detail.get("Office") or basic_detail.get("office") or normalized.get("Office") or ""
            normalized["City"] = basic_detail.get("City") or basic_detail.get("city") or normalized.get("City") or ""
            normalized["State"] = basic_detail.get("State") or basic_detail.get("state") or normalized.get("State") or ""
            normalized["Country"] = basic_detail.get("Country") or basic_detail.get("country") or normalized.get("Country") or ""
            normalized["MobileNo"] = basic_detail.get("MobileNo") or basic_detail.get("mobileNo") or basic_detail.get("mobilePhone") or normalized.get("MobileNo") or ""
            normalized["EmploymentStatus"] = basic_detail.get("EmploymentStatus") or basic_detail.get("employmentStatus") or normalized.get("EmploymentStatus") or ""
            normalized["employeeCode"] = basic_detail.get("EmployeeCode") or basic_detail.get("employeeCode") or normalized.get("employeeCode") or ""
            normalized["firstName"] = basic_detail.get("FirstName") or basic_detail.get("firstName") or normalized.get("firstName") or ""
            normalized["lastName"] = basic_detail.get("LastName") or basic_detail.get("lastName") or normalized.get("lastName") or ""
            normalized["email"] = basic_detail.get("OfficialMailID") or basic_detail.get("email") or basic_detail.get("workEmail") or normalized.get("email") or ""
            normalized["department"] = basic_detail.get("Department") or basic_detail.get("department") or normalized.get("department") or ""
            normalized["designation"] = basic_detail.get("DesignationName") or basic_detail.get("designation") or basic_detail.get("jobTitle") or normalized.get("designation") or ""
            return normalized

        normalized["EmployeeName"] = direct.get("EmployeeName") or direct.get("employeeName") or normalized.get("EmployeeName") or ""
        normalized["EmployeeCode"] = direct.get("employeeCode") or direct.get("employeeId") or direct.get("employee_code") or raw.get("employeeCode") or raw.get("employeeId") or normalized.get("EmployeeCode") or ""
        normalized["FirstName"] = direct.get("firstName") or direct.get("first_name") or raw.get("firstName") or raw.get("first_name") or normalized.get("FirstName") or ""
        normalized["LastName"] = direct.get("lastName") or direct.get("last_name") or raw.get("lastName") or raw.get("last_name") or normalized.get("LastName") or ""
        normalized["OfficialMailID"] = direct.get("OfficialMailID") or direct.get("email") or direct.get("workEmail") or raw.get("OfficialMailID") or raw.get("email") or raw.get("workEmail") or normalized.get("OfficialMailID") or ""
        normalized["Department"] = direct.get("Department") or direct.get("department") or direct.get("departmentName") or raw.get("Department") or raw.get("department") or raw.get("departmentName") or normalized.get("Department") or ""
        normalized["DesignationName"] = direct.get("DesignationName") or direct.get("designation") or direct.get("jobTitle") or raw.get("DesignationName") or raw.get("designation") or raw.get("jobTitle") or normalized.get("DesignationName") or ""
        normalized["Company"] = direct.get("Company") or direct.get("company") or raw.get("Company") or raw.get("company") or normalized.get("Company") or ""
        normalized["Office"] = direct.get("Office") or direct.get("office") or raw.get("Office") or raw.get("office") or normalized.get("Office") or ""
        normalized["City"] = direct.get("City") or direct.get("city") or raw.get("City") or raw.get("city") or normalized.get("City") or ""
        normalized["State"] = direct.get("State") or direct.get("state") or raw.get("State") or raw.get("state") or normalized.get("State") or ""
        normalized["Country"] = direct.get("Country") or direct.get("country") or raw.get("Country") or raw.get("country") or normalized.get("Country") or ""
        normalized["MobileNo"] = direct.get("MobileNo") or direct.get("mobileNo") or direct.get("mobilePhone") or raw.get("MobileNo") or raw.get("mobileNo") or raw.get("mobilePhone") or normalized.get("MobileNo") or ""
        normalized["EmploymentStatus"] = direct.get("EmploymentStatus") or direct.get("employmentStatus") or raw.get("EmploymentStatus") or raw.get("employmentStatus") or normalized.get("EmploymentStatus") or ""
        normalized["employeeCode"] = direct.get("employeeCode") or direct.get("employeeId") or direct.get("employee_code") or raw.get("employeeCode") or raw.get("employeeId") or normalized.get("employeeCode") or ""
        normalized["firstName"] = direct.get("firstName") or direct.get("first_name") or raw.get("firstName") or raw.get("first_name") or normalized.get("firstName") or ""
        normalized["lastName"] = direct.get("lastName") or direct.get("last_name") or raw.get("lastName") or raw.get("last_name") or normalized.get("lastName") or ""
        normalized["email"] = direct.get("email") or direct.get("workEmail") or raw.get("email") or raw.get("workEmail") or normalized.get("email") or ""
        normalized["department"] = direct.get("department") or direct.get("departmentName") or raw.get("department") or raw.get("departmentName") or normalized.get("department") or ""
        normalized["designation"] = direct.get("designation") or direct.get("jobTitle") or raw.get("jobTitle") or raw.get("designation") or normalized.get("designation") or ""
        return normalized


class MicrosoftGraphClient:
    def __init__(self) -> None:
        self.tenant_id = _get_setting("AZ_TENANT_ID") or _get_setting("AZURE_TENANT_ID") or ""
        self.client_id = _get_setting("AZ_CLIENT_ID") or _get_setting("AZURE_CLIENT_ID") or ""
        self.client_secret = _get_setting("AZ_CLIENT_SECRET") or _get_setting("AZURE_CLIENT_SECRET") or ""
        self.temp_password = _get_setting("AZ_TEMP_PASSWORD", "TempP@ss1234!")
        self.manager_sync_result: Dict[str, str] = {}

    def get_access_token(self) -> str:
        payload = parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }).encode("utf-8")
        req = request.Request(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            return str(data.get("access_token") or "")

    def find_user(self, email: str) -> Optional[Dict[str, Any]]:
        email = (email or "").strip()
        if not email:
            return None

        token = self.get_access_token()
        filter_query = parse.quote(f"mail eq '{email}' or userPrincipalName eq '{email}'")
        req = request.Request(
            f"https://graph.microsoft.com/v1.0/users?$filter={filter_query}&$top=10",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                users = data.get("value") if isinstance(data, dict) else None
                if not isinstance(users, list):
                    return None

                exact_matches = []
                normalized_email = email.lower()
                for user in users:
                    if not isinstance(user, dict):
                        continue
                    user_mail = str(user.get("mail") or "").strip().lower()
                    user_upn = str(user.get("userPrincipalName") or "").strip().lower()
                    if user_mail == normalized_email or user_upn == normalized_email:
                        exact_matches.append(user)

                if len(exact_matches) == 1:
                    return exact_matches[0]
                if len(exact_matches) > 1:
                    logger.warning("Multiple Microsoft users matched email %s; skipping ambiguous lookup.", email)
                    return None
                if len(users) == 1:
                    return users[0]
                return None
        except Exception:
            logger.exception("Failed to look up Microsoft user for %s", email)
            return None

    def get_user_by_email(self, email: str) -> Dict[str, Any]:
        email = (email or "").strip()
        if not email:
            raise ValueError("An email address is required to look up a Microsoft Entra user.")

        try:
            token = self.get_access_token()
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_graph_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not authenticate with Microsoft Graph: {exc}") from exc
        encoded_email = parse.quote(email, safe="@")
        req = request.Request(
            "https://graph.microsoft.com/v1.0/users/"
            f"{encoded_email}?$select=id,displayName,mail,userPrincipalName",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                user = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                raise RuntimeError(f"No Microsoft Entra user was found for {email}.") from exc
            raise RuntimeError(self._format_graph_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not look up Microsoft Entra user {email}: {exc}") from exc

        if not isinstance(user, dict) or not user.get("id"):
            raise RuntimeError(f"Microsoft Graph returned no user details for {email}.")
        return user

    def get_user_manager(self, user_id: str) -> Optional[Dict[str, Any]]:
        if not user_id:
            raise ValueError("An employee Microsoft Entra object ID is required.")

        try:
            token = self.get_access_token()
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_graph_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not authenticate with Microsoft Graph: {exc}") from exc
        req = request.Request(
            f"https://graph.microsoft.com/v1.0/users/{parse.quote(user_id, safe='')}"
            "?$select=id,displayName,mail,userPrincipalName",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                manager = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                return None
            raise RuntimeError(self._format_graph_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not verify the employee's manager: {exc}") from exc

        if not isinstance(manager, dict) or not manager.get("id"):
            return None
        return manager

    def get_user_manager_object_id(self, user_id: str) -> Optional[str]:
        if not user_id:
            return None

        try:
            token = self.get_access_token()
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_graph_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not authenticate with Microsoft Graph: {exc}") from exc
        req = request.Request(
            f"https://graph.microsoft.com/v1.0/users/{parse.quote(user_id)}/manager?$select=id",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                return str(data.get("id") or "").strip() or None
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        except Exception:
            return None

    def update_user_manager(self, user_id: str, manager_object_id: str) -> bool:
        if not user_id or not manager_object_id:
            raise ValueError("Both employee and manager object IDs are required to update a manager relationship.")

        try:
            token = self.get_access_token()
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_graph_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not authenticate with Microsoft Graph: {exc}") from exc
        body = json.dumps({"@odata.id": f"https://graph.microsoft.com/v1.0/users/{manager_object_id}"}).encode("utf-8")
        req = request.Request(
            f"https://graph.microsoft.com/v1.0/users/{parse.quote(user_id, safe='')}/manager/$ref",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                response.read()
                if response.getcode() not in (200, 204):
                    raise RuntimeError(
                        f"Microsoft Graph manager update returned unexpected HTTP {response.getcode()}."
                    )
                return True
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = self._format_graph_error(raw, exc.code)
            raise RuntimeError(message) from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not update the employee's manager: {exc}") from exc

    def sync_manager_for_employee(self, employee: Dict[str, Any]) -> Dict[str, str]:
        employee_email = _resolve_string(
            _resolve_field_value(employee, ["OfficialMailID", "email", "workEmail"])
        )
        manager_email = _resolve_string(
            _resolve_field_value(employee, ["L1ManagerEmail", "l1ManagerEmail", "managerEmail"])
        )

        if not employee_email or not manager_email:
            missing_fields = []
            if not employee_email:
                missing_fields.append("employee email (OfficialMailID)")
            if not manager_email:
                missing_fields.append("L1 manager email (L1ManagerEmail)")
            return {"status": "skipped", "reason": f"Missing {' and '.join(missing_fields)}."}

        employee_user = self.find_user(employee_email)
        if not employee_user or not employee_user.get("id"):
            return {"status": "not_found", "reason": f"Employee user not found for email {employee_email}."}

        manager_user = self.find_user(manager_email)
        if not manager_user or not manager_user.get("id"):
            return {"status": "not_found", "reason": f"Manager user not found for email {manager_email}."}

        if employee_user.get("id") == manager_user.get("id"):
            return {"status": "unchanged", "reason": "Employee and manager reference the same user."}

        current_manager_id = self.get_user_manager_object_id(employee_user["id"])
        if current_manager_id == manager_user["id"]:
            return {"status": "unchanged", "reason": "Employee manager already matches the PeopleStrong L1 manager."}

        self.update_user_manager(employee_user["id"], manager_user["id"])
        return {"status": "updated", "reason": "Employee manager relationship updated successfully."}
    def create_user(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = self.get_access_token()
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            "https://graph.microsoft.com/v1.0/users",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = self._format_graph_error(raw, exc.code)
            raise RuntimeError(message) from exc

    def update_user(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = self.get_access_token()
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"https://graph.microsoft.com/v1.0/users/{parse.quote(user_id)}",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )

        try:
            with request.urlopen(req, timeout=60) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = self._format_graph_error(raw, exc.code)
            raise RuntimeError(message) from exc

    def deactivate_user(self, email: str) -> str:
        if not email:
            raise ValueError("Employee email is required for Microsoft user deactivation")

        existing = self.find_user(email)
        if not existing or not existing.get("id"):
            logger.info("Microsoft user not found for %s; nothing to deactivate", email)
            return "not-found"

        self.update_user(existing["id"], {"accountEnabled": False})
        return "deactivated"

    def _format_graph_error(self, raw: str, status_code: int) -> str:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        error_payload = payload.get("error") if isinstance(payload, dict) else None
        try:
            status_text = HTTPStatus(status_code).phrase
        except ValueError:
            status_text = "Unknown status"
        if isinstance(error_payload, dict):
            code = error_payload.get("code") or "UNKNOWN"
            message = error_payload.get("message") or "Microsoft Graph request failed"
            if status_code == 400:
                return f"Microsoft Graph rejected the manager change (HTTP 400 Bad Request, {code}): {message}"
            if status_code == 401:
                return f"Microsoft Graph authentication failed (HTTP 401 Unauthorized, {code}): {message}"
            if status_code == 403:
                return (
                    f"Microsoft Graph permission denied (HTTP 403 Forbidden, {code}): {message}. "
                    "Grant the app the User.ReadWrite.All (and Directory.ReadWrite.All if needed) permission "
                    "and ensure admin consent is completed."
                )
            if status_code == 404:
                return f"Microsoft Graph user or manager was not found (HTTP 404 Not Found, {code}): {message}"
            return f"Microsoft Graph error (HTTP {status_code} {status_text}, {code}): {message}"

        return f"Microsoft Graph request failed with HTTP {status_code} {status_text}: {raw}"

    def upsert_user(self, employee: Dict[str, Any]) -> str:
        is_allowed, raw_status, skip_reason = check_employment_status_for_microsoft_sync(employee)
        if not is_allowed:
            raise ValueError(skip_reason)

        payload = build_graph_payload(employee)
        email = payload.get("userPrincipalName") or payload.get("mail") or ""

        if not email:
            raise ValueError("Employee email is required for Microsoft user sync")

        existing = self.find_user(email)
        if existing and existing.get("id"):
            logger.info("Microsoft user already exists for %s; updating", email)
            update_payload = {
                key: value for key, value in payload.items()
                if key in {"accountEnabled", "jobTitle", "department", "companyName", "employeeId", "officeLocation", "city", "state", "country", "mobilePhone"}
            }
            self.update_user(existing["id"], update_payload)
            action = "updated"
        else:
            logger.info("Microsoft user not found for %s; creating", email)
            create_payload = dict(payload)
            create_payload.pop("mail", None)
            create_payload["passwordProfile"] = {
                "forceChangePasswordNextSignIn": True,
                "password": self.temp_password,
            }
            self.create_user(create_payload)
            action = "created"

        try:
            result = self.sync_manager_for_employee(employee)
            self.manager_sync_result = result
            logger.info("Manager sync result for %s: %s (%s)", email, result["status"], result["reason"])
        except Exception as exc:  # pragma: no cover - manager sync should not block employee sync
            self.manager_sync_result = {"status": "failed", "reason": str(exc)}
            logger.warning("Manager sync failed for employee %s: %s", email, exc)

        return action

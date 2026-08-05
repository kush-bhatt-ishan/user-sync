import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, parse, request

from django.conf import settings
from django.db.models import Q

from .models import EmployeeSyncRecord, SyncLog, SyncRun

logger = logging.getLogger(__name__)


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(key, getattr(settings, key, default))


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
            value = employee.get(field_name)
            if value not in (None, ""):
                return value
        return None
    return employee.get(field_names)


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


def get_or_create_sync_record(employee: Dict[str, Any]) -> EmployeeSyncRecord:
    source_payload = employee.copy()
    graph_payload = build_graph_payload(source_payload)
    source_employee_id = (
        source_payload.get("employeeCode")
        or source_payload.get("employee_id")
        or source_payload.get("id")
        or source_payload.get("employeeId")
        or ""
    ).strip()
    email = (source_payload.get("email") or source_payload.get("OfficialMailID") or "").strip()
    first_name = (source_payload.get("firstName") or source_payload.get("FirstName") or source_payload.get("first_name") or "").strip()
    last_name = (source_payload.get("lastName") or source_payload.get("LastName") or source_payload.get("last_name") or "").strip()
    department = (source_payload.get("department") or source_payload.get("Department") or "").strip()
    designation = (source_payload.get("designation") or source_payload.get("DesignationName") or source_payload.get("jobTitle") or "").strip()

    record, created = EmployeeSyncRecord.objects.get_or_create(
        source_employee_id=source_employee_id,
        email=email or None,
        defaults={
            "first_name": first_name,
            "last_name": last_name,
            "department": department,
            "designation": designation,
            "payload": source_payload,
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
    if not record.payload:
        record.payload = source_payload
        updated_fields.append("payload")
    if not record.graph_payload:
        record.graph_payload = graph_payload
        updated_fields.append("graph_payload")
    elif record.payload != source_payload:
        record.payload = source_payload
        record.graph_payload = graph_payload
        updated_fields.extend(["payload", "graph_payload"])
    if updated_fields:
        record.save(update_fields=updated_fields)
    return record


def update_sync_record_status(record: EmployeeSyncRecord, status: str, *, message: Optional[str] = None, error_code: Optional[str] = None) -> EmployeeSyncRecord:
    record.last_status = status
    record.last_error_message = message or ""
    record.last_error_code = error_code or ""
    record.attempt_count += 1
    if status == EmployeeSyncRecord.STATUS_SYNCED:
        from django.utils import timezone

        record.last_synced_at = timezone.now()
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

        direct = raw.get("employee") if isinstance(raw.get("employee"), dict) else raw
        if isinstance(direct.get("BasicDetails"), dict) and isinstance(direct["BasicDetails"].get("BasicDetail"), dict):
            basic_detail = direct["BasicDetails"]["BasicDetail"]
            return {
                "EmployeeName": basic_detail.get("EmployeeName") or basic_detail.get("employeeName") or "",
                "EmployeeCode": basic_detail.get("EmployeeCode") or basic_detail.get("employeeCode") or "",
                "FirstName": basic_detail.get("FirstName") or basic_detail.get("firstName") or "",
                "LastName": basic_detail.get("LastName") or basic_detail.get("lastName") or "",
                "OfficialMailID": basic_detail.get("OfficialMailID") or basic_detail.get("email") or basic_detail.get("workEmail") or "",
                "Department": basic_detail.get("Department") or basic_detail.get("department") or "",
                "DesignationName": basic_detail.get("DesignationName") or basic_detail.get("designation") or basic_detail.get("jobTitle") or "",
                "Company": basic_detail.get("Company") or basic_detail.get("company") or "",
                "Office": basic_detail.get("Office") or basic_detail.get("office") or "",
                "City": basic_detail.get("City") or basic_detail.get("city") or "",
                "State": basic_detail.get("State") or basic_detail.get("state") or "",
                "Country": basic_detail.get("Country") or basic_detail.get("country") or "",
                "MobileNo": basic_detail.get("MobileNo") or basic_detail.get("mobileNo") or basic_detail.get("mobilePhone") or "",
                "EmploymentStatus": basic_detail.get("EmploymentStatus") or basic_detail.get("employmentStatus") or "",
                "employeeCode": basic_detail.get("EmployeeCode") or basic_detail.get("employeeCode") or "",
                "firstName": basic_detail.get("FirstName") or basic_detail.get("firstName") or "",
                "lastName": basic_detail.get("LastName") or basic_detail.get("lastName") or "",
                "email": basic_detail.get("OfficialMailID") or basic_detail.get("email") or basic_detail.get("workEmail") or "",
                "department": basic_detail.get("Department") or basic_detail.get("department") or "",
                "designation": basic_detail.get("DesignationName") or basic_detail.get("designation") or basic_detail.get("jobTitle") or "",
            }

        return {
            "EmployeeName": direct.get("EmployeeName") or direct.get("employeeName") or "",
            "EmployeeCode": direct.get("employeeCode") or direct.get("employeeId") or direct.get("employee_code") or raw.get("employeeCode") or raw.get("employeeId") or "",
            "FirstName": direct.get("firstName") or direct.get("first_name") or raw.get("firstName") or raw.get("first_name") or "",
            "LastName": direct.get("lastName") or direct.get("last_name") or raw.get("lastName") or raw.get("last_name") or "",
            "OfficialMailID": direct.get("OfficialMailID") or direct.get("email") or direct.get("workEmail") or raw.get("OfficialMailID") or raw.get("email") or raw.get("workEmail") or "",
            "Department": direct.get("Department") or direct.get("department") or direct.get("departmentName") or raw.get("Department") or raw.get("department") or raw.get("departmentName") or "",
            "DesignationName": direct.get("DesignationName") or direct.get("designation") or direct.get("jobTitle") or raw.get("DesignationName") or raw.get("designation") or raw.get("jobTitle") or "",
            "Company": direct.get("Company") or direct.get("company") or raw.get("Company") or raw.get("company") or "",
            "Office": direct.get("Office") or direct.get("office") or raw.get("Office") or raw.get("office") or "",
            "City": direct.get("City") or direct.get("city") or raw.get("City") or raw.get("city") or "",
            "State": direct.get("State") or direct.get("state") or raw.get("State") or raw.get("state") or "",
            "Country": direct.get("Country") or direct.get("country") or raw.get("Country") or raw.get("country") or "",
            "MobileNo": direct.get("MobileNo") or direct.get("mobileNo") or direct.get("mobilePhone") or raw.get("MobileNo") or raw.get("mobileNo") or raw.get("mobilePhone") or "",
            "EmploymentStatus": direct.get("EmploymentStatus") or direct.get("employmentStatus") or raw.get("EmploymentStatus") or raw.get("employmentStatus") or "",
            "employeeCode": direct.get("employeeCode") or direct.get("employeeId") or direct.get("employee_code") or raw.get("employeeCode") or raw.get("employeeId") or "",
            "firstName": direct.get("firstName") or direct.get("first_name") or raw.get("firstName") or raw.get("first_name") or "",
            "lastName": direct.get("lastName") or direct.get("last_name") or raw.get("lastName") or raw.get("last_name") or "",
            "email": direct.get("email") or direct.get("workEmail") or raw.get("email") or raw.get("workEmail") or "",
            "department": direct.get("department") or direct.get("departmentName") or raw.get("department") or raw.get("departmentName") or "",
            "designation": direct.get("designation") or direct.get("jobTitle") or raw.get("jobTitle") or raw.get("designation") or "",
        }


class MicrosoftGraphClient:
    def __init__(self) -> None:
        self.tenant_id = _get_setting("AZ_TENANT_ID") or _get_setting("AZURE_TENANT_ID") or ""
        self.client_id = _get_setting("AZ_CLIENT_ID") or _get_setting("AZURE_CLIENT_ID") or ""
        self.client_secret = _get_setting("AZ_CLIENT_SECRET") or _get_setting("AZURE_CLIENT_SECRET") or ""
        self.temp_password = _get_setting("AZ_TEMP_PASSWORD", "TempP@ss1234!")

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
        token = self.get_access_token()
        encoded_email = parse.quote(email)
        filter_query = parse.quote(f"mail eq '{email}' or userPrincipalName eq '{email}'")
        req = request.Request(
            f"https://graph.microsoft.com/v1.0/users?$filter={filter_query}&$top=1",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                users = data.get("value") if isinstance(data, dict) else None
                if isinstance(users, list) and users:
                    return users[0]
                return None
        except Exception:
            return None

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

    def _format_graph_error(self, raw: str, status_code: int) -> str:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        error_payload = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error_payload, dict):
            code = error_payload.get("code") or "UNKNOWN"
            message = error_payload.get("message") or "Microsoft Graph request failed"
            if status_code == 403 and code == "Authorization_RequestDenied":
                return (
                    f"Microsoft Graph permission denied ({code}): {message}. "
                    "Grant the app the User.ReadWrite.All (and Directory.ReadWrite.All if needed) permission "
                    "and ensure admin consent is completed."
                )
            return f"Microsoft Graph error ({code}): {message}"

        return f"Microsoft Graph request failed with HTTP {status_code}: {raw}"

    def upsert_user(self, employee: Dict[str, Any]) -> Dict[str, Any]:
        payload = build_graph_payload(employee)
        email = payload.get("userPrincipalName") or payload.get("mail") or ""

        if not email:
            raise ValueError("Employee email is required for Microsoft user sync")

        existing = self.find_user(email)
        if existing and existing.get("id"):
            logger.info("Microsoft user already exists for %s; updating", email)
            update_payload = {
                key: value for key, value in payload.items()
                if key in {"accountEnabled", "displayName", "givenName", "surname", "jobTitle", "department", "companyName", "employeeId", "officeLocation", "city", "state", "country", "mobilePhone"}
            }
            # response = self.update_user(existing["id"], update_payload)
            # print(f"existingID: {existing['id']}")
            # print(f"response: {response}")
            # exit()
            return self.update_user(existing["id"], update_payload)

        logger.info("Microsoft user not found for %s; creating", email)
        create_payload = dict(payload)
        create_payload.pop("mail", None)
        create_payload["passwordProfile"] = {
            "forceChangePasswordNextSignIn": True,
            "password": self.temp_password,
        }
        return self.create_user(create_payload)

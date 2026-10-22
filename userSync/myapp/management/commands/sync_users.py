import logging
from typing import Any, Dict, List, Optional

from django.core.management.base import BaseCommand

from myapp.models import EmployeeSyncRecord, SyncLog, SyncRun
from myapp.sync_services import (
    MicrosoftGraphClient,
    PeopleStrongClient,
    build_graph_payload,
    check_employment_status_for_microsoft_sync,
    create_sync_log,
    get_or_create_sync_record,
    get_retry_candidates,
    update_sync_record_status,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync PeopleStrong employees to Microsoft Entra ID"

    def add_arguments(self, parser):
        parser.add_argument("--employee-id", dest="employee_id", default=None, help="Sync a single employee ID")
        parser.add_argument(
            "--employee-ids",
            dest="employee_ids",
            nargs="+",
            default=None,
            help="Sync multiple employee IDs in one run",
        )
        parser.add_argument("--retry-only", action="store_true", dest="retry_only")
        parser.add_argument(
            "--limit",
            dest="limit",
            type=int,
            default=None,
            help="Only sync the first N employees (omit to sync all)",
        )
        parser.add_argument(
            "--deactivate",
            action="store_true",
            dest="deactivate",
            help="Deactivate existing Microsoft users instead of creating/updating them",
        )

    def handle(self, *args, **options):
        sync_run = SyncRun.objects.create(status=SyncRun.STATUS_RUNNING)
        self.stdout.write(self.style.SUCCESS("Starting PeopleStrong sync"))
        self.stdout.write(self.style.HTTP_INFO(f"Run id: {sync_run.id}"))

        employee_id = options.get("employee_id")
        employee_ids = options.get("employee_ids") or []
        retry_only = options.get("retry_only")
        limit = options.get("limit")
        deactivate = options.get("deactivate")

        requested_ids = []
        if employee_id:
            requested_ids.append(str(employee_id).strip())
        if employee_ids:
            requested_ids.extend(str(item).strip() for item in employee_ids if str(item).strip())
        requested_ids = [rid for rid in requested_ids if rid]
        mode = "deactivate" if deactivate else "retry-only" if retry_only else "sync"
        self._log_run_event(
            sync_run,
            "STARTED",
            f"Sync started; mode={mode}; requested_employee_ids={','.join(requested_ids) or 'all'}; limit={limit or 'none'}",
        )

        if deactivate:
            self.stdout.write(self.style.WARNING("Deactivate mode enabled: existing Microsoft users will be disabled."))
            records = []
            if requested_ids:
                normalized_ids = [str(item).strip() for item in requested_ids if str(item).strip()]
                records = list(
                    EmployeeSyncRecord.objects.filter(source_employee_id__in=normalized_ids).order_by("updated_at")
                )
            else:
                records = list(EmployeeSyncRecord.objects.all().order_by("updated_at"))

            sync_run.total_records = len(records)
            sync_run.save(update_fields=["total_records"])
            for record in records:
                self._deactivate_user(sync_run, record)

            sync_run.status = SyncRun.STATUS_COMPLETED
            sync_run.finished_at = self._now()
            sync_run.save(update_fields=["status", "finished_at"])
            self._log_run_completion(sync_run)
            self._print_run_summary(sync_run)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Run completed: synced={sync_run.synced_records}, failed={sync_run.failed_records}, total={len(records)}"
                )
            )
            return

        retry_records = []
        if retry_only:
            retry_records = list(self._get_failed_records_for_requested_ids(requested_ids)) if requested_ids else list(get_retry_candidates())
            if retry_records:
                sync_run.total_records = len(retry_records)
                sync_run.save(update_fields=["total_records"])
                for record in retry_records:
                    self._retry_record(sync_run, record)
                sync_run.status = SyncRun.STATUS_COMPLETED
                sync_run.finished_at = self._now()
                sync_run.save(update_fields=["status", "finished_at"])
                self._log_run_completion(sync_run)
                self._print_run_summary(sync_run)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Run completed: synced={sync_run.synced_records}, failed={sync_run.failed_records}, total={sync_run.total_records}"
                    )
                )
            else:
                sync_run.status = SyncRun.STATUS_COMPLETED
                sync_run.notes = "No failed employee records found for retry"
                sync_run.finished_at = self._now()
                sync_run.save(update_fields=["status", "notes", "finished_at"])
                self._log_run_completion(sync_run)
                self.stdout.write(self.style.WARNING("No failed employee records found for retry"))
            return

        failed_records = list(self._get_failed_records_for_requested_ids(requested_ids)) if requested_ids else []
        failed_ids = {record.source_employee_id for record in failed_records}
        remaining_employee_id = employee_id if employee_id and str(employee_id).strip() not in failed_ids else None
        remaining_employee_ids = [rid for rid in requested_ids if rid not in failed_ids]

        if failed_records:
            self.stdout.write(self.style.WARNING(f"Retrying {len(failed_records)} previously failed employee(s)."))
            for record in failed_records:
                self._retry_record(sync_run, record)
            sync_run.total_records = len(failed_records)
            sync_run.save(update_fields=["total_records"])
            if not remaining_employee_id and not remaining_employee_ids:
                sync_run.status = SyncRun.STATUS_COMPLETED
                sync_run.finished_at = self._now()
                sync_run.save(update_fields=["status", "finished_at"])
                self._log_run_completion(sync_run)
                self._print_run_summary(sync_run)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Run completed: synced={sync_run.synced_records}, failed={sync_run.failed_records}, total={sync_run.total_records}"
                    )
                )
                return

        try:
            employees = self._fetch_people_strong_employees(
                employee_id=remaining_employee_id,
                employee_ids=remaining_employee_ids,
                retry_only=False,
            )
            if limit is not None:
                employees = employees[:limit]
            self.stdout.write(self.style.SUCCESS(f"Fetched {len(employees)} employees from PeopleStrong"))
            self._log_run_event(sync_run, "INFO", f"Fetched {len(employees)} employees from PeopleStrong")
        except Exception as exc:  # pragma: no cover - command-level safety
            sync_run.status = SyncRun.STATUS_FAILED
            sync_run.notes = str(exc)
            sync_run.finished_at = self._now()
            sync_run.save(update_fields=["status", "notes", "finished_at"])
            create_sync_log(sync_run, EmployeeSyncRecord(), "FAILED", str(exc), level="ERROR", error_code="FETCH_FAILED")
            self._log_run_completion(sync_run)
            self.stdout.write(self.style.ERROR(f"PeopleStrong fetch failed: {exc}"))
            return

        if not employees:
            sync_run.status = SyncRun.STATUS_COMPLETED
            sync_run.notes = "No employees returned from PeopleStrong"
            sync_run.finished_at = self._now()
            sync_run.save(update_fields=["status", "notes", "finished_at"])
            self._log_run_completion(sync_run)
            self.stdout.write(self.style.WARNING("No employees returned from PeopleStrong"))
            return

        sync_run.total_records = len(employees)
        sync_run.save(update_fields=["total_records"])

        for employee in employees:
            self._announce_employee(employee)
            self._process_employee(sync_run, employee)

        if retry_only:
            self.stdout.write(self.style.WARNING("Retry-only mode: processed failed records only."))
        else:
            self.stdout.write(self.style.HTTP_INFO("Fresh sync mode: only the current fetched employees were processed."))

        sync_run.status = SyncRun.STATUS_COMPLETED
        sync_run.finished_at = self._now()
        sync_run.save(update_fields=["status", "finished_at"])
        self._log_run_completion(sync_run)
        self._print_run_summary(sync_run)
        self.stdout.write(
            self.style.SUCCESS(
                f"Run completed: synced={sync_run.synced_records}, failed={sync_run.failed_records}, total={sync_run.total_records}"
            )
        )

    def _fetch_people_strong_employees(
        self,
        *,
        employee_id: Optional[str] = None,
        employee_ids: Optional[List[str]] = None,
        retry_only: bool = False,
    ) -> List[Dict[str, Any]]:
        if retry_only:
            records = list(get_retry_candidates())
            return [{
                "employeeCode": record.source_employee_id,
                "firstName": record.first_name or "",
                "lastName": record.last_name or "",
                "email": record.email or "",
                "department": record.department or "",
                "designation": record.designation or "",
            } for record in records]

        client = PeopleStrongClient()
        employees = client.fetch_employees()
        requested_ids = []
        if employee_id:
            requested_ids.append(employee_id)
        if employee_ids:
            requested_ids.extend(employee_ids)

        if requested_ids:
            normalized_ids = {str(item).strip().lower() for item in requested_ids if str(item).strip()}
            employees = [
                employee
                for employee in employees
                if str(employee.get("employeeCode") or "").strip().lower() in normalized_ids
            ]
        return employees

    def _announce_employee(self, employee: Dict[str, Any]) -> None:
        employee_id = str(employee.get("employeeCode") or employee.get("employee_id") or employee.get("id") or employee.get("employeeId") or "-").strip()
        email = str(employee.get("email") or "-").strip()
        self.stdout.write(self.style.HTTP_INFO(f"FETCHED | employee={employee_id} | email={email}"))

    def _get_failed_records_for_requested_ids(self, employee_ids: list[str]) -> list[EmployeeSyncRecord]:
        if not employee_ids:
            return []
        normalized_ids = [str(item).strip() for item in employee_ids if str(item).strip()]
        return list(
            EmployeeSyncRecord.objects.filter(source_employee_id__in=normalized_ids, last_status=EmployeeSyncRecord.STATUS_FAILED).order_by("updated_at")
        )

    def _process_employee(self, sync_run: SyncRun, employee: Dict[str, Any]) -> None:
        record = get_or_create_sync_record(employee)
        graph_payload = build_graph_payload(record.payload or employee)

        is_allowed, raw_status, skip_reason = check_employment_status_for_microsoft_sync(employee)
        if not is_allowed:
            update_sync_record_status(
                record,
                EmployeeSyncRecord.STATUS_SKIPPED,
                message=skip_reason,
                error_code="STATUS_NOT_MATCHED",
                source_payload=employee,
                graph_payload=graph_payload,
            )
            create_sync_log(
                sync_run,
                record,
                "SKIPPED",
                f"{skip_reason} | {self._employee_log_details(record, employee)}",
                level="WARNING",
                error_code="STATUS_NOT_MATCHED",
            )
            self.stdout.write(
                self.style.WARNING(
                    f"SKIPPED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | status=SKIPPED | reason={skip_reason}"
                )
            )
            return

        try:
            action = self._upsert_user(record, employee)
        except Exception as exc:  # pragma: no cover - command-level safety
            update_sync_record_status(
                record,
                "FAILED",
                message=str(exc),
                error_code="SYNC_FAILED",
                source_payload=employee,
                graph_payload=graph_payload,
            )
            create_sync_log(
                sync_run,
                record,
                "FAILED",
                f"Microsoft sync failed: {exc} | {self._employee_log_details(record, employee)}",
                level="ERROR",
                error_code="SYNC_FAILED",
            )
            sync_run.failed_records += 1
            sync_run.save(update_fields=["failed_records"])
            logger.error("Sync failed for employee %s: %s", record.source_employee_id or "<unknown>", exc)
            self.stdout.write(
                self.style.ERROR(
                    f"FAILED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | error={exc}"
                )
            )
        else:
            success_message = "User created successfully in Microsoft" if action == "created" else "User synced successfully to Microsoft"
            update_sync_record_status(
                record,
                "SYNCED",
                message=success_message,
                source_payload=employee,
                graph_payload=graph_payload,
            )
            create_sync_log(
                sync_run,
                record,
                "SUCCESS",
                f"{success_message}; action={action} | {self._employee_log_details(record, employee)}",
                level="INFO",
            )
            sync_run.synced_records += 1
            sync_run.save(update_fields=["synced_records"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"SYNCED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | status=SYNCED | action={action}"
                )
            )

    def _retry_record(self, sync_run: SyncRun, record: EmployeeSyncRecord) -> None:
        employee = record.payload or {}
        if not employee:
            employee = {
                "employeeCode": record.source_employee_id,
                "firstName": record.first_name or "",
                "lastName": record.last_name or "",
                "email": record.email or "",
                "department": record.department or "",
                "designation": record.designation or "",
            }
        graph_payload = build_graph_payload(record.payload or employee)

        is_allowed, raw_status, skip_reason = check_employment_status_for_microsoft_sync(employee)
        if not is_allowed:
            update_sync_record_status(
                record,
                EmployeeSyncRecord.STATUS_SKIPPED,
                message=skip_reason,
                error_code="STATUS_NOT_MATCHED",
                source_payload=employee,
                graph_payload=graph_payload,
            )
            create_sync_log(
                sync_run,
                record,
                "SKIPPED",
                f"{skip_reason} | {self._employee_log_details(record, employee)}",
                level="WARNING",
                error_code="STATUS_NOT_MATCHED",
            )
            self.stdout.write(
                self.style.WARNING(
                    f"SKIPPED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | status=SKIPPED | reason={skip_reason}"
                )
            )
            return

        try:
            action = self._upsert_user(record, employee)
        except Exception as exc:  # pragma: no cover - command-level safety
            update_sync_record_status(
                record,
                "FAILED",
                message=str(exc),
                error_code="RETRY_FAILED",
                source_payload=employee,
                graph_payload=graph_payload,
            )
            create_sync_log(
                sync_run,
                record,
                "FAILED",
                f"Retry failed: {exc} | {self._employee_log_details(record, employee)}",
                level="WARNING",
                error_code="RETRY_FAILED",
            )
            sync_run.failed_records += 1
            sync_run.save(update_fields=["failed_records"])
            logger.warning("Retry failed for employee %s: %s", record.source_employee_id or "<unknown>", exc)
            self.stdout.write(
                self.style.ERROR(
                    f"RETRY FAILED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | error={exc}"
                )
            )
        else:
            success_message = "User created successfully in Microsoft" if action == "created" else "Retry synced successfully to Microsoft"
            update_sync_record_status(
                record,
                "SYNCED",
                message=success_message,
                source_payload=employee,
                graph_payload=graph_payload,
            )
            create_sync_log(
                sync_run,
                record,
                "SUCCESS",
                f"{success_message}; action={action} | {self._employee_log_details(record, employee)}",
                level="INFO",
            )
            sync_run.synced_records += 1
            sync_run.save(update_fields=["synced_records"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"RETRY SYNCED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | status=SYNCED | action={action}"
                )
            )

    def _upsert_user(self, record: EmployeeSyncRecord, employee: Dict[str, Any]) -> str:
        graph_client = MicrosoftGraphClient()
        action = graph_client.upsert_user(employee)
        manager_result = graph_client.manager_sync_result
        manager_status = manager_result.get("status", "")
        record.manager_sync_status = manager_status.upper()
        record.manager_error_message = manager_result.get("reason", "")
        if manager_status in {"updated", "unchanged"}:
            record.manager_last_synced_at = self._now()
        record.save(update_fields=[
            "manager_sync_status",
            "manager_error_message",
            "manager_last_synced_at",
        ])
        return action

    def _deactivate_user(self, sync_run: SyncRun, record: EmployeeSyncRecord) -> None:
        graph_client = MicrosoftGraphClient()
        try:
            action = graph_client.deactivate_user(record.email or "")
        except Exception as exc:  # pragma: no cover - command-level safety
            update_sync_record_status(record, "FAILED", message=str(exc), error_code="DEACTIVATE_FAILED")
            create_sync_log(
                sync_run,
                record,
                "FAILED",
                f"Deactivate failed: {exc} | email={record.email or '-'}",
                level="WARNING",
                error_code="DEACTIVATE_FAILED",
            )
            sync_run.failed_records += 1
            sync_run.save(update_fields=["failed_records"])
            self.stdout.write(
                self.style.ERROR(
                    f"DEACTIVATE FAILED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | error={exc}"
                )
            )
        else:
            update_sync_record_status(record, "SYNCED", message="User deactivated successfully in Microsoft")
            create_sync_log(
                sync_run,
                record,
                "SUCCESS",
                f"User deactivated successfully in Microsoft; action={action} | email={record.email or '-'}",
                level="INFO",
            )
            sync_run.synced_records += 1
            sync_run.save(update_fields=["synced_records"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"DEACTIVATED | employee={record.source_employee_id or '-'} | email={record.email or '-'} | status=SYNCED | action={action}"
                )
            )

    def _employee_log_details(self, record: EmployeeSyncRecord, employee: Dict[str, Any]) -> str:
        employment_status = str(employee.get("EmploymentStatus") or employee.get("employmentStatus") or "").strip()
        details = [
            f"email={record.email or '-'}",
            f"employment_status={employment_status or '-'}",
            f"l1_manager_email={record.l1_manager_email or '-'}",
            f"manager_sync_status={record.manager_sync_status or 'NOT_ATTEMPTED'}",
        ]
        if record.manager_error_message:
            details.append(f"manager_details={record.manager_error_message}")
        return " | ".join(details)

    def _log_run_event(self, sync_run: SyncRun, status: str, message: str, *, level: str = "INFO") -> None:
        create_sync_log(sync_run, EmployeeSyncRecord(), status, message, level=level)

    def _log_run_completion(self, sync_run: SyncRun) -> None:
        message = (
            f"Run finished; status={sync_run.status}; total={sync_run.total_records}; "
            f"synced={sync_run.synced_records}; failed={sync_run.failed_records}"
        )
        if sync_run.notes:
            message = f"{message}; notes={sync_run.notes}"
        level = "ERROR" if sync_run.status == SyncRun.STATUS_FAILED else "INFO"
        self._log_run_event(sync_run, sync_run.status, message, level=level)

    def _print_run_summary(self, sync_run: SyncRun) -> None:
        logs = list(sync_run.logs.order_by("-created_at")[:10])
        if not logs:
            self.stdout.write(self.style.WARNING("No sync logs were recorded for this run."))
            return

        self.stdout.write(self.style.HTTP_INFO("Latest sync log entries:"))
        for log in logs:
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"- {log.created_at} | {log.level} | employee={log.source_employee_id or '-'} | status={log.status} | {log.message}"
                )
            )

    def _now(self):
        from django.utils import timezone

        return timezone.now()

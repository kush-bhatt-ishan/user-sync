import io
import json
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from myapp.management.commands.sync_users import Command
from myapp.models import EmployeeSyncRecord, SyncRun
from myapp.sync_services import (
    MicrosoftGraphClient,
    build_graph_payload,
    check_employment_status_for_microsoft_sync,
    get_or_create_sync_record,
    get_retry_candidates,
    update_sync_record_status,
)


class SyncServiceTests(TestCase):
    def test_build_graph_payload_uses_people_strong_fields(self):
        employee = {
            "employeeCode": "E1001",
            "firstName": "John",
            "lastName": "Doe",
            "email": "john.doe@example.com",
            "department": "Engineering",
            "designation": "Software Engineer",
        }

        payload = build_graph_payload(employee)

        self.assertEqual(payload["userPrincipalName"], "john.doe@example.com")
        self.assertEqual(payload["displayName"], "John Doe")
        self.assertEqual(payload["department"], "Engineering")
        self.assertEqual(payload["jobTitle"], "Software Engineer")
        self.assertTrue(payload["accountEnabled"])

    def test_build_graph_payload_uses_dynamic_people_strong_field_mapping(self):
        employee = {
            "EmployeeName": "John Doe",
            "FirstName": "John",
            "LastName": "Doe",
            "OfficialMailID": "john.doe@example.com",
            "DesignationName": "Software Engineer",
            "Department": "Engineering",
            "Company": "Ishan Technologies",
            "Office": "HQ",
            "City": "Ahmedabad",
            "State": "Gujarat",
            "Country": "India",
            "EmployeeCode": "E1001",
            "MobileNo": "9876543210",
            "EmploymentStatus": "Probation",
        }

        payload = build_graph_payload(employee)

        self.assertEqual(payload["displayName"], "John Doe")
        self.assertEqual(payload["givenName"], "John")
        self.assertEqual(payload["surname"], "Doe")
        self.assertEqual(payload["mail"], "john.doe@example.com")
        self.assertEqual(payload["userPrincipalName"], "john.doe@example.com")
        self.assertEqual(payload["jobTitle"], "Software Engineer")
        self.assertEqual(payload["department"], "Engineering")
        self.assertEqual(payload["companyName"], "Ishan Technologies")
        self.assertEqual(payload["officeLocation"], "HQ")
        self.assertEqual(payload["city"], "Ahmedabad")
        self.assertEqual(payload["state"], "Gujarat")
        self.assertEqual(payload["country"], "India")
        self.assertEqual(payload["employeeId"], "E1001")
        self.assertEqual(payload["mobilePhone"], "9876543210")
        self.assertTrue(payload["accountEnabled"])

    def test_get_or_create_sync_record_stores_graph_payload(self):
        employee = {
            "employeeCode": "E2001",
            "firstName": "Jane",
            "lastName": "Smith",
            "email": "jane.smith@example.com",
        }

        record = get_or_create_sync_record(employee)

        self.assertEqual(record.graph_payload["displayName"], "Jane Smith")
        self.assertEqual(record.graph_payload["givenName"], "Jane")
        self.assertEqual(record.graph_payload["surname"], "Smith")
        self.assertEqual(record.graph_payload["userPrincipalName"], "jane.smith@example.com")

    def test_get_or_create_sync_record_stores_only_flat_employee_payload(self):
        employee = {
            "BasicDetails": {
                "BasicDetail": {
                    "EmployeeCode": "09607",
                    "FirstName": "Zala Vishnuji Gobarsang",
                    "LastName": "",
                    "OfficialMailID": "",
                    "Department": "SDC1",
                    "DesignationName": "Field Support Engineer",
                    "Company": "Ishan Netsol Pvt Ltd",
                    "Office": "Mehsana",
                    "City": "Mehsana",
                    "State": "Gujarat",
                    "Country": "India",
                    "EmploymentStatus": "Inactive",
                    "EmployeeName": "Zala Vishnuji Gobarsang",
                }
            },
            "employee": {
                "BasicDetails": {
                    "BasicDetail": {
                        "EmployeeCode": "09607",
                        "FirstName": "Zala Vishnuji Gobarsang",
                        "LastName": "",
                        "OfficialMailID": "",
                        "Department": "SDC1",
                        "DesignationName": "Field Support Engineer",
                        "Company": "Ishan Netsol Pvt Ltd",
                        "Office": "Mehsana",
                        "City": "Mehsana",
                        "State": "Gujarat",
                        "Country": "India",
                        "EmploymentStatus": "Inactive",
                        "EmployeeName": "Zala Vishnuji Gobarsang",
                    }
                }
            },
        }

        record = get_or_create_sync_record(employee)

        self.assertEqual(record.payload["EmployeeCode"], "09607")
        self.assertEqual(record.payload["EmployeeName"], "Zala Vishnuji Gobarsang")
        self.assertEqual(record.payload["Department"], "SDC1")
        self.assertNotIn("BasicDetails", record.payload)
        self.assertNotIn("BasicDetail", record.payload)
        self.assertNotIn("employee", record.payload)

    def test_get_or_create_sync_record_updates_existing_record_payload(self):
        employee = {
            "employeeCode": "E2002",
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jane.doe@example.com",
            "department": "Engineering",
        }

        record = get_or_create_sync_record(employee)
        self.assertEqual(record.payload["department"], "Engineering")

        updated_employee = {
            "employeeCode": "E2002",
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jane.doe@example.com",
            "department": "Product",
        }

        updated_record = get_or_create_sync_record(updated_employee)
        self.assertEqual(updated_record.id, record.id)
        self.assertEqual(updated_record.payload["department"], "Product")

    def test_get_or_create_sync_record_flattens_nested_payload(self):
        employee = {
            "BasicDetails": {
                "BasicDetail": {
                    "EmployeeCode": "09607",
                    "FirstName": "Zala Vishnuji Gobarsang",
                    "LastName": "",
                    "OfficialMailID": "",
                    "Department": "SDC1",
                    "DesignationName": "Field Support Engineer",
                    "Company": "Ishan Netsol Pvt Ltd",
                    "Office": "Mehsana",
                    "City": "Mehsana",
                    "State": "Gujarat",
                    "Country": "India",
                    "EmploymentStatus": "Inactive",
                    "EmployeeName": "Zala Vishnuji Gobarsang",
                }
            },
            "employee": {
                "BasicDetails": {
                    "BasicDetail": {
                        "EmployeeCode": "09607",
                        "FirstName": "Zala Vishnuji Gobarsang",
                        "LastName": "",
                        "OfficialMailID": "",
                        "Department": "SDC1",
                        "DesignationName": "Field Support Engineer",
                        "Company": "Ishan Netsol Pvt Ltd",
                        "Office": "Mehsana",
                        "City": "Mehsana",
                        "State": "Gujarat",
                        "Country": "India",
                        "EmploymentStatus": "Inactive",
                        "EmployeeName": "Zala Vishnuji Gobarsang",
                    }
                }
            },
        }

        record = get_or_create_sync_record(employee)

        stored_payload = record.payload
        self.assertEqual(stored_payload["EmployeeCode"], "09607")
        self.assertEqual(stored_payload["EmployeeName"], "Zala Vishnuji Gobarsang")
        self.assertEqual(stored_payload["Department"], "SDC1")
        self.assertEqual(stored_payload["email"], "")
        self.assertNotIn("BasicDetails", stored_payload)
        self.assertNotIn("employee", stored_payload)

    def test_find_user_uses_graph_filter_query(self):
        client = MicrosoftGraphClient()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"value": [{"id": "123", "mail": "john@example.com"}]}'

        with patch.object(client, "get_access_token", return_value="token"), patch("myapp.sync_services.request.urlopen", return_value=response) as urlopen:
            found = client.find_user("john@example.com")

        self.assertEqual(found["id"], "123")
        self.assertIn("users?$filter=", urlopen.call_args.args[0].full_url)

    def test_get_user_by_email_uses_graph_user_resource_endpoint(self):
        client = MicrosoftGraphClient()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (
            b'{"id":"manager-oid","displayName":"Manager Name",'
            b'"mail":"manager@example.com","userPrincipalName":"manager@example.com"}'
        )

        with patch.object(client, "get_access_token", return_value="access-token"), patch(
            "myapp.sync_services.request.urlopen", return_value=response
        ) as urlopen:
            manager = client.get_user_by_email("manager@example.com")

        request = urlopen.call_args.args[0]
        self.assertIn(
            "/users/manager@example.com?$select=id,displayName,mail,userPrincipalName",
            request.full_url,
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer access-token")
        self.assertEqual(manager["id"], "manager-oid")

    def test_get_user_by_email_reports_graph_http_errors(self):
        client = MicrosoftGraphClient()
        graph_error = b'{"error":{"code":"Authorization_RequestDenied","message":"Denied"}}'
        http_error = HTTPError(
            "https://graph.microsoft.com/v1.0/users/manager%40example.com",
            403,
            "Forbidden",
            {},
            io.BytesIO(graph_error),
        )

        with patch.object(client, "get_access_token", return_value="access-token"), patch(
            "myapp.sync_services.request.urlopen", side_effect=http_error
        ), self.assertRaisesRegex(RuntimeError, "HTTP 403 Forbidden"):
            client.get_user_by_email("manager@example.com")

    def test_graph_error_messages_cover_manager_change_status_codes(self):
        client = MicrosoftGraphClient()
        response_body = '{"error":{"code":"RequestFailed","message":"Request failed"}}'
        expected_messages = {
            400: "HTTP 400 Bad Request",
            401: "HTTP 401 Unauthorized",
            403: "HTTP 403 Forbidden",
            404: "HTTP 404 Not Found",
        }

        for status_code, expected_message in expected_messages.items():
            with self.subTest(status_code=status_code):
                message = client._format_graph_error(response_body, status_code)
                self.assertIn(expected_message, message)

    def test_get_user_by_email_reports_not_found_without_a_user_fallback(self):
        client = MicrosoftGraphClient()
        http_error = HTTPError(
            "https://graph.microsoft.com/v1.0/users/missing@example.com",
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"error":{"code":"Request_ResourceNotFound","message":"Not found"}}'),
        )

        with patch.object(client, "get_access_token", return_value="access-token"), patch(
            "myapp.sync_services.request.urlopen", side_effect=http_error
        ), self.assertRaisesRegex(RuntimeError, "No Microsoft Entra user was found"):
            client.get_user_by_email("missing@example.com")

    def test_update_user_manager_accepts_no_content_and_sends_reference(self):
        client = MicrosoftGraphClient()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.getcode.return_value = 204

        with patch.object(client, "get_access_token", return_value="access-token"), patch(
            "myapp.sync_services.request.urlopen", return_value=response
        ) as urlopen:
            updated = client.update_user_manager("employee-oid", "manager-oid")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(
            request.full_url,
            "https://graph.microsoft.com/v1.0/users/employee-oid/manager/$ref",
        )
        self.assertEqual(
            json.loads(request.data),
            {"@odata.id": "https://graph.microsoft.com/v1.0/users/manager-oid"},
        )
        self.assertTrue(updated)

    def test_manager_change_page_requires_staff_login(self):
        response = self.client.get(reverse("change-l1-manager"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_manager_change_workflow_updates_verifies_and_persists(self):
        record = EmployeeSyncRecord.objects.create(
            source_employee_id="E4001",
            email="employee@example.com",
        )
        staff_user = get_user_model().objects.create_user(
            username="manager-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(staff_user)
        employee_user = {
            "id": "employee-oid",
            "displayName": "Employee Name",
            "mail": "employee@example.com",
            "userPrincipalName": "employee@example.com",
        }
        manager_user = {
            "id": "manager-oid",
            "displayName": "Manager Name",
            "mail": "manager@example.com",
            "userPrincipalName": "manager@example.com",
        }

        with patch("myapp.views.MicrosoftGraphClient") as graph_client_class:
            graph_client = graph_client_class.return_value
            graph_client.get_user_by_email.side_effect = [employee_user, manager_user]
            graph_client.update_user_manager.return_value = True
            graph_client.get_user_manager.return_value = manager_user

            preview = self.client.post(
                reverse("change-l1-manager"),
                {
                    "employee_id": record.pk,
                    "manager_email": "manager@example.com",
                    "action": "preview",
                },
            )
            self.assertEqual(preview.status_code, 200)
            self.assertContains(preview, "Manager Name")
            graph_client.update_user_manager.assert_not_called()

            graph_client.get_user_by_email.side_effect = [employee_user, manager_user]
            changed = self.client.post(
                reverse("change-l1-manager"),
                {
                    "employee_id": record.pk,
                    "manager_email": "manager@example.com",
                    "expected_employee_oid": "employee-oid",
                    "expected_manager_oid": "manager-oid",
                    "action": "change",
                },
            )

        self.assertEqual(changed.status_code, 200)
        self.assertContains(changed, "Manager change completed and verified")
        graph_client.update_user_manager.assert_called_once_with("employee-oid", "manager-oid")
        graph_client.get_user_manager.assert_called_once_with("employee-oid")
        record.refresh_from_db()
        self.assertEqual(record.l1_manager_email, "manager@example.com")
        self.assertEqual(record.l1_manager_name, "Manager Name")
        self.assertEqual(record.l1_manager_object_id, "manager-oid")
        self.assertEqual(record.manager_sync_status, "UPDATED")
        self.assertEqual(SyncRun.objects.filter(status=SyncRun.STATUS_COMPLETED).count(), 1)

    def test_manager_change_does_not_update_when_manager_is_not_found(self):
        record = EmployeeSyncRecord.objects.create(
            source_employee_id="E4002",
            email="employee@example.com",
        )
        staff_user = get_user_model().objects.create_user(
            username="manager-admin-not-found",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(staff_user)
        employee_user = {
            "id": "employee-oid",
            "displayName": "Employee Name",
            "mail": "employee@example.com",
            "userPrincipalName": "employee@example.com",
        }

        with patch("myapp.views.MicrosoftGraphClient") as graph_client_class:
            graph_client = graph_client_class.return_value
            graph_client.get_user_by_email.side_effect = [
                employee_user,
                RuntimeError("No Microsoft Entra user was found for missing@example.com."),
            ]
            response = self.client.post(
                reverse("change-l1-manager"),
                {
                    "employee_id": record.pk,
                    "manager_email": "missing@example.com",
                    "action": "preview",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Microsoft Entra user was found")
        graph_client.update_user_manager.assert_not_called()
        self.assertEqual(SyncRun.objects.count(), 0)

    def test_upsert_user_omits_unsupported_mail_property_on_create(self):
        client = MicrosoftGraphClient()
        employee = {
            "EmployeeName": "John Doe",
            "FirstName": "John",
            "LastName": "Doe",
            "OfficialMailID": "john.doe@example.com",
            "DesignationName": "Software Engineer",
            "Department": "Engineering",
            "Company": "Ishan Technologies",
            "Office": "HQ",
            "City": "Ahmedabad",
            "State": "Gujarat",
            "Country": "India",
            "EmployeeCode": "E1001",
            "MobileNo": "9876543210",
            "EmploymentStatus": "Active",
        }

        with patch.object(client, "find_user", return_value=None), patch.object(client, "create_user", return_value={"id": "123"}) as create_user:
            client.upsert_user(employee)

        payload = create_user.call_args.args[0]
        self.assertNotIn("mail", payload)
        self.assertEqual(payload["userPrincipalName"], "john.doe@example.com")
        self.assertIn("passwordProfile", payload)

    def test_upsert_user_create_payload_uses_company_and_mailnickname_defaults(self):
        client = MicrosoftGraphClient()
        employee = {
            "FirstName": "John",
            "LastName": "Doe",
            "email": "john.doe9@ishantechnologies.com",
            "employeeCode": "D1002",
            "EmploymentStatus": "Active",
        }

        with patch.object(client, "find_user", return_value=None), patch.object(client, "create_user", return_value={"id": "123"}) as create_user:
            client.upsert_user(employee)

        payload = create_user.call_args.args[0]
        self.assertEqual(payload["companyName"], "Ishan Technologies")
        self.assertEqual(payload["mailNickname"], "john.doe9")
        self.assertEqual(payload["userPrincipalName"], "john.doe9@ishantechnologies.com")

    def test_command_persists_manager_sync_result(self):
        record = EmployeeSyncRecord.objects.create(source_employee_id="E3010", email="employee@example.com")
        command = Command()
        graph_client = Mock()
        graph_client.upsert_user.return_value = "updated"
        graph_client.manager_sync_result = {
            "status": "updated",
            "reason": "Employee manager relationship updated successfully.",
        }

        with patch("myapp.management.commands.sync_users.MicrosoftGraphClient", return_value=graph_client):
            action = command._upsert_user(record, {"email": "employee@example.com"})

        record.refresh_from_db()
        self.assertEqual(action, "updated")
        self.assertEqual(record.manager_sync_status, "UPDATED")
        self.assertEqual(
            record.manager_error_message,
            "Employee manager relationship updated successfully.",
        )
        self.assertIsNotNone(record.manager_last_synced_at)

    def test_update_sync_record_status_records_attempt_history(self):
        record = EmployeeSyncRecord.objects.create(source_employee_id="E3001", email="jane.doe@example.com")

        update_sync_record_status(
            record,
            "SYNCED",
            message="User synced successfully",
            source_payload={"employeeCode": "E3001", "email": "jane.doe@example.com"},
            graph_payload={"userPrincipalName": "jane.doe@example.com", "displayName": "Jane Doe"},
        )

        self.assertEqual(record.attempt_count, 1)
        self.assertEqual(record.last_status, "SYNCED")
        self.assertEqual(record.attempt_history[0]["status"], "SYNCED")
        self.assertEqual(record.attempt_history[0]["source_payload"]["email"], "jane.doe@example.com")
        self.assertEqual(record.attempt_history[0]["graph_payload"]["displayName"], "Jane Doe")

    def test_failed_records_are_returned_for_retry(self):
        failed_record = EmployeeSyncRecord.objects.create(
            source_employee_id="E1001",
            email="john.doe@example.com",
            last_status="FAILED",
            attempt_count=1,
        )
        EmployeeSyncRecord.objects.create(
            source_employee_id="E1002",
            email="jane.doe@example.com",
            last_status="SYNCED",
            attempt_count=1,
        )

        retry_records = list(get_retry_candidates())

        self.assertEqual(retry_records, [failed_record])

    def test_requested_failed_employee_id_is_retried_and_not_refetched(self):
        failed_record = EmployeeSyncRecord.objects.create(
            source_employee_id="08241",
            email="failed.user@example.com",
            last_status="FAILED",
            attempt_count=1,
        )

        command = Command()
        command.stdout = io.StringIO()
        command.stderr = io.StringIO()

        with patch.object(command, "_retry_record") as retry_record, patch.object(command, "_fetch_people_strong_employees") as fetch_employees:
            command.handle(employee_ids=["08241"])

        self.assertEqual(retry_record.call_count, 1)
        self.assertEqual(retry_record.call_args.args[1].source_employee_id, "08241")
        self.assertFalse(fetch_employees.called)

    def test_limit_option_processes_only_requested_count(self):
        command = Command()
        command.stdout = io.StringIO()
        command.stderr = io.StringIO()

        with patch.object(
            command,
            "_fetch_people_strong_employees",
            return_value=[{"employeeCode": "E1001"}, {"employeeCode": "E1002"}],
        ), patch.object(command, "_process_employee") as process_employee:
            command.handle(limit=1)

        self.assertEqual(process_employee.call_count, 1)

    def test_sync_run_stores_run_and_employee_detail_logs(self):
        command = Command()
        command.stdout = io.StringIO()
        command.stderr = io.StringIO()
        employee = {
            "employeeCode": "E1010",
            "email": "employee@example.com",
            "EmploymentStatus": "Active",
            "L1ManagerEmail": "manager@example.com",
        }

        with patch.object(command, "_fetch_people_strong_employees", return_value=[employee]), patch.object(
            command, "_upsert_user", return_value="updated"
        ):
            command.handle()

        sync_run = SyncRun.objects.latest("id")
        logs = list(sync_run.logs.order_by("created_at"))
        self.assertEqual(sync_run.total_records, 1)
        self.assertEqual(len(logs), 4)
        self.assertEqual(logs[0].status, "STARTED")
        self.assertIn("Fetched 1 employees", logs[1].message)
        self.assertEqual(logs[2].source_employee_id, "E1010")
        self.assertIn("employee@example.com", logs[2].message)
        self.assertIn("l1_manager_email=manager@example.com", logs[2].message)
        self.assertIn("action=updated", logs[2].message)
        self.assertIn("Run finished; status=COMPLETED; total=1; synced=1; failed=0", logs[3].message)

    def test_deactivate_mode_calls_graph_client_with_email(self):
        record = EmployeeSyncRecord.objects.create(source_employee_id="E1001", email="john.doe@example.com")
        command = Command()
        command.stdout = io.StringIO()
        command.stderr = io.StringIO()

        with patch.object(command, "_deactivate_user") as deactivate_user:
            command.handle(deactivate=True)

        self.assertEqual(deactivate_user.call_count, 1)
        self.assertEqual(deactivate_user.call_args.args[1].source_employee_id, record.source_employee_id)

    def test_multiple_employee_ids_are_filtered(self):
        command = Command()

        with patch("myapp.management.commands.sync_users.PeopleStrongClient") as client_cls:
            client = client_cls.return_value
            client.fetch_employees.return_value = [
                {"employeeCode": "E1001"},
                {"employeeCode": "E1002"},
                {"employeeCode": "E1003"},
            ]

            employees = command._fetch_people_strong_employees(employee_ids=["E1001", "E1003"])

        self.assertEqual([employee["employeeCode"] for employee in employees], ["E1001", "E1003"])

    def test_people_strong_nested_payload_is_parsed(self):
        from myapp.sync_services import PeopleStrongClient

        client = PeopleStrongClient()
        payload = {
            "root": {
                "EmployeeMaster": {
                    "EmployeeMasterData": [
                        {
                            "BasicDetails": {
                                "BasicDetail": {
                                    "EmployeeCode": "09499",
                                    "FirstName": "Kapadi Mohit Shambhubhai",
                                    "LastName": "",
                                    "OfficialMailID": "kapadi.mohit@ishantechnologies.com",
                                    "Department": "Network Operations and Maintenance",
                                    "DesignationName": "Specialist",
                                }
                            }
                        }
                    ]
                }
            }
        }

        employees = client._normalize_employees(payload)

        self.assertEqual(len(employees), 1)
        self.assertEqual(employees[0]["employeeCode"], "09499")
        self.assertEqual(employees[0]["email"], "kapadi.mohit@ishantechnologies.com")
        self.assertEqual(employees[0]["department"], "Network Operations and Maintenance")
        self.assertEqual(employees[0]["designation"], "Specialist")

    def test_check_employment_status_allows_active_and_inactive(self):
        is_allowed, raw_status, reason = check_employment_status_for_microsoft_sync({"EmploymentStatus": "Active"})
        self.assertTrue(is_allowed)
        self.assertEqual(raw_status, "Active")
        self.assertEqual(reason, "")

        is_allowed, raw_status, reason = check_employment_status_for_microsoft_sync({"EmploymentStatus": "inactive"})
        self.assertTrue(is_allowed)
        self.assertEqual(raw_status, "inactive")
        self.assertEqual(reason, "")

    def test_check_employment_status_skips_non_matching_status_with_error_message(self):
        is_allowed, raw_status, reason = check_employment_status_for_microsoft_sync({"EmploymentStatus": "Probation"})
        self.assertFalse(is_allowed)
        self.assertEqual(raw_status, "Probation")
        self.assertIn("EmploymentStatus is 'Probation'", reason)
        self.assertIn("Only records with EmploymentStatus 'Active' or 'Inactive' are updated in Microsoft.", reason)

        is_allowed, raw_status, reason = check_employment_status_for_microsoft_sync({"EmploymentStatus": ""})
        self.assertFalse(is_allowed)
        self.assertEqual(raw_status, "")
        self.assertIn("EmploymentStatus is missing or blank", reason)

    def test_get_or_create_sync_record_stores_l1_manager_fields(self):
        employee = {
            "BasicDetails": {
                "BasicDetail": {
                    "EmployeeCode": "10967",
                    "EmployeeName": "Shaikh Mohammadnadim A. SHAIKH",
                    "OfficialMailID": "shaikh.nadim@ishantechnologies.com",
                    "EmploymentStatus": "Active",
                    "L1ManagerCode": "06823",
                    "L1ManagerName": "Dharmesh Thakorbhai Lad",
                    "L1ManagerEmail": "lad.dharmesh@ishantechnologies.com",
                    "HRManagerEmail": "achhava.arif@ishantechnologies.com",
                    "HrManagerCode": "06648",
                    "HrManagerName": "Arif Idrishbhai Achhava",
                }
            }
        }

        record = get_or_create_sync_record(employee)

        self.assertEqual(record.l1_manager_code, "06823")
        self.assertEqual(record.l1_manager_name, "Dharmesh Thakorbhai Lad")
        self.assertEqual(record.l1_manager_email, "lad.dharmesh@ishantechnologies.com")
        self.assertIn("L1ManagerEmail", record.payload)
        self.assertEqual(record.payload["L1ManagerEmail"], "lad.dharmesh@ishantechnologies.com")

    def test_manager_sync_reads_nested_peoplestrong_email_fields(self):
        from myapp.sync_services import PeopleStrongClient

        employee = PeopleStrongClient()._normalize_employee(
            {
                "BasicDetails": {
                    "BasicDetail": {
                        "EmployeeCode": "E4010",
                        "OfficialMailID": "employee@example.com",
                        "EmploymentStatus": "Active",
                        "L1ManagerEmail": "manager@example.com",
                    }
                }
            }
        )
        client = MicrosoftGraphClient()
        employee_user = {"id": "employee-oid"}
        manager_user = {"id": "manager-oid"}

        with patch.object(
            client, "find_user", side_effect=[employee_user, manager_user]
        ) as find_user, patch.object(
            client, "get_user_manager_object_id", return_value=None
        ), patch.object(client, "update_user_manager", return_value=True) as update_manager:
            result = client.sync_manager_for_employee(employee)

        self.assertEqual(result["status"], "updated")
        self.assertEqual(
            [call.args[0] for call in find_user.call_args_list],
            ["employee@example.com", "manager@example.com"],
        )
        update_manager.assert_called_once_with("employee-oid", "manager-oid")

    def test_process_employee_skips_microsoft_upsert_when_employment_status_not_matched(self):
        command = Command()
        employee = {
            "employeeCode": "E9999",
            "firstName": "Test",
            "lastName": "User",
            "email": "test.user@example.com",
            "EmploymentStatus": "Probation",
        }
        sync_run = SyncRun.objects.create(status=SyncRun.STATUS_RUNNING)

        with patch.object(command, "_upsert_user") as mock_upsert:
            command._process_employee(sync_run, employee)
            mock_upsert.assert_not_called()

        record = EmployeeSyncRecord.objects.get(source_employee_id="E9999")
        self.assertEqual(record.last_status, EmployeeSyncRecord.STATUS_SKIPPED)
        self.assertIn("EmploymentStatus is 'Probation'", record.last_error_message)
        self.assertEqual(record.last_error_code, "STATUS_NOT_MATCHED")

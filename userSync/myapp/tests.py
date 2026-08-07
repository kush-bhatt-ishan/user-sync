import io
from unittest.mock import Mock, patch

from django.test import TestCase

from myapp.management.commands.sync_users import Command
from myapp.models import EmployeeSyncRecord
from myapp.sync_services import (
    MicrosoftGraphClient,
    build_graph_payload,
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
        }

        with patch.object(client, "find_user", return_value=None), patch.object(client, "create_user", return_value={"id": "123"}) as create_user:
            client.upsert_user(employee)

        payload = create_user.call_args.args[0]
        self.assertEqual(payload["companyName"], "Ishan Technologies")
        self.assertEqual(payload["mailNickname"], "john.doe9")
        self.assertEqual(payload["userPrincipalName"], "john.doe9@ishantechnologies.com")

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

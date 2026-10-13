import asyncio
import copy
import json
import os
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "attendance_contract_tests"

import main
from helpers.helper import IST


class ASGIResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self.content = body
        self.text = body.decode("utf-8")

    def json(self):
        return json.loads(self.text)


class ASGITestClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, json_body=None, params=None):
        body = b""
        headers = []
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers.append((b"content-type", b"application/json"))

        query_string = urlencode(params or {}, doseq=True).encode("ascii")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        messages = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        asyncio.run(self.app(scope, receive, send))
        status_code = next(m["status"] for m in messages if m["type"] == "http.response.start")
        response_headers = dict(
            (k.decode("latin-1"), v.decode("latin-1"))
            for m in messages
            if m["type"] == "http.response.start"
            for k, v in m.get("headers", [])
        )
        response_body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return ASGIResponse(status_code, response_headers, response_body)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, json=None):
        return self.request("POST", path, json_body=json)

    def patch(self, path, json=None):
        return self.request("PATCH", path, json_body=json)


class InsertOneResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class UpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class FakeCursor:
    def __init__(self, docs, collection):
        self.docs = docs
        self.collection = collection

    def sort(self, field_or_list, direction=None):
        if isinstance(field_or_list, list):
            sorts = field_or_list
        else:
            sorts = [(field_or_list, direction)]
        for field, order in reversed(sorts):
            self.docs.sort(key=lambda doc: doc.get(field), reverse=order == -1)
        return self

    def skip(self, count):
        self.docs = self.docs[count:]
        return self

    def limit(self, count):
        self.docs = self.docs[:count]
        return self

    def __iter__(self):
        for item in self.docs:
            self.collection.iterated_documents += 1
            yield copy.deepcopy(item)


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self.next_id = 1
        self.indexes = []
        self.last_find_query = None
        self.iterated_documents = 0

    def create_index(self, keys, unique=False):
        self.indexes.append((tuple(keys), unique))

    def insert_one(self, doc):
        stored = copy.deepcopy(doc)
        if "_id" not in stored:
            stored["_id"] = self.next_id
            self.next_id += 1
        self.docs.append(stored)
        return InsertOneResult(stored["_id"])

    def find_one(self, query, projection=None, sort=None):
        docs = [doc for doc in self.docs if self._matches(doc, query)]
        if sort:
            for field, order in reversed(sort):
                docs.sort(key=lambda doc: doc.get(field), reverse=order == -1)
        if not docs:
            return None
        return self._project(copy.deepcopy(docs[0]), projection)

    def find(self, query=None, projection=None, sort=None):
        self.last_find_query = copy.deepcopy(query or {})
        docs = [self._project(copy.deepcopy(doc), projection) for doc in self.docs if self._matches(doc, query or {})]
        cursor = FakeCursor(docs, self)
        if sort:
            cursor.sort(sort)
        return cursor

    def count_documents(self, query):
        return sum(1 for doc in self.docs if self._matches(doc, query))

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                for field, value in update.get("$set", {}).items():
                    doc[field] = copy.deepcopy(value)
                for field, value in update.get("$push", {}).items():
                    doc.setdefault(field, []).append(copy.deepcopy(value))
                return UpdateResult(1)
        return UpdateResult(0)

    def _project(self, doc, projection):
        if projection and projection.get("_id") == 0:
            doc.pop("_id", None)
        return doc

    def _matches(self, doc, query):
        for field, expected in query.items():
            actual = doc.get(field)
            if isinstance(expected, dict):
                if "$exists" in expected:
                    exists = field in doc
                    if exists != expected["$exists"]:
                        return False
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$gte" in expected and not (actual >= expected["$gte"]):
                    return False
                if "$gt" in expected and not (actual > expected["$gt"]):
                    return False
                if "$lte" in expected and not (actual <= expected["$lte"]):
                    return False
                if "$lt" in expected and not (actual < expected["$lt"]):
                    return False
            elif actual != expected:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.employees = FakeCollection("employees")
        self.attendance_logs = FakeCollection("attendance_logs")

    def command(self, *args, **kwargs):
        return {
            "queryPlanner": {
                "winningPlan": {
                    "stage": "FETCH",
                    "inputStage": {"stage": "IXSCAN", "indexName": "fake_index"},
                }
            },
            "executionStats": {"totalDocsExamined": 10},
        }


def ms_ist(day, clock):
    hour, minute, second = [int(part) for part in clock.split(":")]
    value = datetime.fromisoformat(day).replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=900000,
        tzinfo=IST,
    )
    return int(value.timestamp() * 1000)


def dt_ist(day, clock):
    return datetime.fromtimestamp(ms_ist(day, clock) / 1000, tz=timezone.utc).replace(microsecond=0, tzinfo=None)


def weekday_dates(start_day, count):
    current = date.fromisoformat(start_day)
    values = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


class APITestCase(unittest.TestCase):
    def setUp(self):
        self.original_db = main.db
        self.db = FakeDB()
        main.db = self.db
        main.create_indexes()
        self.client = ASGITestClient(main.app)

    def tearDown(self):
        main.db = self.original_db

    def create_employee(self, emp_code="EMP0001", **overrides):
        payload = {
            "emp_code": emp_code,
            "name": f"Employee {emp_code}",
            "email": f"{emp_code.lower()}@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-01",
        }
        payload.update(overrides)
        response = self.client.post("/employees", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def insert_employee(self, emp_code="EMP0001", **overrides):
        doc = {
            "emp_code": emp_code,
            "name": f"Employee {emp_code}",
            "email": f"{emp_code.lower()}@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
            "created_at": datetime(2026, 7, 1),
        }
        doc.update(overrides)
        self.db.employees.insert_one(doc)
        return doc

    def insert_log(self, emp_code="EMP0001", day="2026-07-01", **overrides):
        doc = {
            "emp_code": emp_code,
            "date": day,
            "status": "PRESENT",
            "punch_in": dt_ist(day, "09:30:00"),
            "punch_out": dt_ist(day, "18:30:00"),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        doc.update(overrides)
        self.db.attendance_logs.insert_one(doc)
        return doc


class EmployeeEndpointTests(APITestCase):
    def test_health_and_startup_indexes(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertIn(((("emp_code", 1),), True), self.db.employees.indexes)

    def test_create_employee_defaults_created_at_and_duplicate(self):
        created = self.create_employee()
        self.assertEqual(created["shift_start"], "09:30")
        self.assertEqual(created["shift_end"], "18:30")
        self.assertIsInstance(created["created_at"], int)
        self.assertNotIn("_id", created)

        duplicate = self.client.post(
            "/employees",
            json={
                "emp_code": "EMP0001",
                "name": "Duplicate",
                "email": "duplicate@example.com",
                "department": "Engineering",
                "joined_on": "2026-07-01",
            },
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_employee_validation_edge_cases(self):
        invalid_payloads = [
            {"emp_code": "BAD1", "name": "A", "email": "a@example.com", "department": "Eng", "joined_on": "2026-07-01"},
            {"emp_code": "EMP0002", "name": "", "email": "a@example.com", "department": "Eng", "joined_on": "2026-07-01"},
            {"emp_code": "EMP0003", "name": "A", "email": "not-email", "department": "Eng", "joined_on": "2026-07-01"},
            {"emp_code": "EMP0004", "name": "A", "email": "a@example.com", "department": "Eng", "shift_start": "09:30", "shift_end": "09:30", "joined_on": "2026-07-01"},
            {"emp_code": "EMP0005", "name": "A", "email": "a@example.com", "department": "Eng", "shift_start": "99:99", "joined_on": "2026-07-01"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/employees", json=payload).status_code, 422)

    def test_list_employees_filters_paginates_and_counts_filtered_total(self):
        self.create_employee("EMP0002", department="Sales")
        self.create_employee("EMP0001", department="Engineering")
        self.create_employee("EMP0003", department="Engineering")

        response = self.client.get("/employees", params={"department": "Engineering", "page": 1, "page_size": 1})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["items"][0]["emp_code"], "EMP0001")
        self.assertEqual(self.client.get("/employees", params={"page": 0}).status_code, 422)
        self.assertEqual(self.client.get("/employees", params={"page_size": 101}).status_code, 422)


class AttendanceEndpointTests(APITestCase):
    def test_punch_in_grace_period_defaults_duplicate_unknown_and_validation(self):
        self.create_employee()
        punched_at = ms_ist("2026-07-01", "09:40:00")
        response = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": punched_at},
        )
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(body["date"], "2026-07-01")
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["punch_in"], punched_at - 900)
        self.assertIsNone(body["punch_out"])
        self.assertEqual(body["history"], [])

        duplicate = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": punched_at},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            self.client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": punched_at}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT", "punched_at": punched_at}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500}).status_code,
            422,
        )

    def test_punch_in_late_threshold_and_overnight_attendance_date(self):
        self.create_employee("EMP0001")
        late = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-02", "09:40:01")},
        ).json()
        self.assertEqual(late["late_minutes"], 10)

        self.create_employee("EMP0002", shift_start="22:00", shift_end="06:00")
        overnight = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0002", "punched_at": ms_ist("2026-07-02", "05:30:00")},
        ).json()
        self.assertEqual(overnight["date"], "2026-07-01")

    def test_punch_out_computes_work_half_day_overtime_and_conflicts(self):
        self.create_employee()
        self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-01", "09:30:00")},
        )
        out = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-01", "19:00:00")},
        )
        body = out.json()
        self.assertEqual(out.status_code, 200)
        self.assertEqual(body["work_hours"], 9.5)
        self.assertEqual(body["overtime_minutes"], 30)
        self.assertFalse(body["half_day"])
        self.assertEqual(
            self.client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-01", "19:30:00")}).status_code,
            409,
        )

    def test_punch_out_rejects_invalid_times_and_missing_records(self):
        self.create_employee()
        self.assertEqual(
            self.client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-01", "18:00:00")}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": ms_ist("2026-07-01", "18:00:00")}).status_code,
            404,
        )
        self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-01", "09:30:00")},
        )
        self.assertEqual(
            self.client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-01", "09:30:00")}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_ist("2026-07-02", "09:30:01")}).status_code,
            422,
        )

    def test_list_attendance_filters_sorting_pagination_validation_and_query_shape(self):
        self.insert_employee("EMP0001")
        self.insert_employee("EMP0002")
        self.insert_log("EMP0002", "2026-07-02", status="WFH")
        self.insert_log("EMP0001", "2026-07-03", status="LEAVE", punch_in=None, punch_out=None, work_hours=None)
        self.insert_log("EMP0001", "2026-07-02", status="PRESENT")
        self.insert_log("EMP0001", "2026-06-30", status="PRESENT")

        response = self.client.get(
            "/attendance",
            params={"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 1, "page_size": 10},
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total"], 3)
        self.assertEqual([(item["date"], item["emp_code"]) for item in body["items"]], [
            ("2026-07-03", "EMP0001"),
            ("2026-07-02", "EMP0001"),
            ("2026-07-02", "EMP0002"),
        ])
        self.assertIn("date", self.db.attendance_logs.last_find_query)
        self.assertEqual(self.client.get("/attendance", params={"date_from": "2026-08-01", "date_to": "2026-07-01"}).status_code, 422)
        self.assertEqual(self.client.get("/attendance", params={"page_size": 101}).status_code, 422)

    def test_attendance_list_handles_100000_records_with_indexable_query(self):
        self.insert_employee("EMP0001")
        start = date(2026, 1, 1)
        for index in range(100000):
            day = (start + timedelta(days=index % 365)).isoformat()
            self.db.attendance_logs.docs.append(
                {
                    "_id": index + 1,
                    "emp_code": f"EMP{index % 5000:04d}",
                    "date": day,
                    "status": "PRESENT",
                    "punch_in": datetime(2026, 1, 1, 4, 0),
                    "punch_out": datetime(2026, 1, 1, 13, 0),
                    "work_hours": 9.0,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                }
            )

        started = time.perf_counter()
        response = self.client.get(
            "/attendance",
            params={"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "page_size": 100},
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 1.0)
        self.assertLessEqual(self.db.attendance_logs.iterated_documents, 100)
        self.assertEqual(
            self.db.attendance_logs.last_find_query,
            {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}},
        )

    def test_regularize_success_absent_leave_history_and_noop(self):
        self.insert_employee()
        self.insert_log(day="2026-07-01", punch_in=dt_ist("2026-07-01", "10:05:00"), late_minutes=35)
        response = self.client.patch(
            "/attendance/EMP0001/2026-07-01",
            json={
                "punch_in": ms_ist("2026-07-01", "09:30:00"),
                "reason": "Forgot original correction",
                "regularized_by": "manager",
            },
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(len(body["history"]), 1)
        self.assertEqual(set(body["history"][0]["changes"]), {"punch_in", "late_minutes"})

        noop = self.client.patch(
            "/attendance/EMP0001/2026-07-01",
            json={"reason": "No real change", "regularized_by": "manager"},
        )
        self.assertEqual(noop.status_code, 422)

        absent = self.client.patch(
            "/attendance/EMP0001/2026-07-01",
            json={"status": "ABSENT", "reason": "Approved absence correction", "regularized_by": "manager"},
        )
        self.assertEqual(absent.status_code, 200)
        self.assertIsNone(absent.json()["punch_in"])
        self.assertIsNone(absent.json()["punch_out"])

    def test_regularize_validation_edges(self):
        self.insert_employee()
        self.insert_log(day="2026-07-01")
        cases = [
            ("/attendance/EMP9999/2026-07-01", {"status": "LEAVE", "reason": "Valid reason", "regularized_by": "mgr"}, 404),
            ("/attendance/EMP0001/2026-07-02", {"status": "LEAVE", "reason": "Valid reason", "regularized_by": "mgr"}, 404),
            ("/attendance/EMP0001/not-a-date", {"status": "LEAVE", "reason": "Valid reason", "regularized_by": "mgr"}, 422),
            ("/attendance/EMP0001/2026-07-01", {"status": "ABSENT", "punch_in": ms_ist("2026-07-01", "09:30:00"), "reason": "Valid reason", "regularized_by": "mgr"}, 422),
            ("/attendance/EMP0001/2026-07-01", {"punch_in": ms_ist("2026-07-02", "09:30:00"), "reason": "Valid reason", "regularized_by": "mgr"}, 422),
            ("/attendance/EMP0001/2026-07-01", {"punch_out": ms_ist("2026-07-01", "09:00:00"), "reason": "Valid reason", "regularized_by": "mgr"}, 422),
            ("/attendance/EMP0001/2026-07-01", {"punch_out": ms_ist("2026-07-02", "09:30:01"), "reason": "Valid reason", "regularized_by": "mgr"}, 422),
            ("/attendance/EMP0001/2026-07-01", {"status": "PRESENT", "reason": "bad", "regularized_by": "mgr"}, 422),
        ]
        for path, payload, expected_status in cases:
            with self.subTest(path=path, payload=payload):
                self.assertEqual(self.client.patch(path, json=payload).status_code, expected_status)


class AnalyticsEndpointTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.insert_employee("EMP0001", department="Engineering", joined_on="2026-07-15")
        self.insert_employee("EMP0002", department="Engineering", joined_on="2026-07-01")
        self.insert_employee("EMP0003", department="Sales", joined_on="2026-07-01")

    def test_employee_monthly_counts_working_days_presence_leave_late_and_overtime(self):
        self.insert_log("EMP0001", "2026-07-15", late_minutes=10, overtime_minutes=30)
        self.insert_log("EMP0001", "2026-07-16", half_day=True, work_hours=4.25)
        self.insert_log("EMP0001", "2026-07-18", status="PRESENT")
        self.insert_log("EMP0001", "2026-07-20", status="LEAVE", punch_in=None, punch_out=None, work_hours=None)

        response = self.client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["working_days"], len(weekday_dates("2026-07-15", 13)))
        self.assertEqual(body["present_days"], 1.5)
        self.assertEqual(body["leave_days"], 1)
        self.assertEqual(body["late_count"], 1)
        self.assertEqual(body["total_late_minutes"], 10)
        self.assertEqual(body["total_overtime_minutes"], 30)
        self.assertEqual(self.client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code, 404)
        self.assertEqual(self.client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code, 422)

    def test_department_summary_includes_zero_log_headcount_and_department_filter(self):
        self.insert_log("EMP0001", "2026-07-15", work_hours=8.0, late_minutes=5)
        self.insert_log("EMP0002", "2026-07-15", status="ON_DUTY", work_hours=10.0)
        self.insert_log("EMP0002", "2026-07-16", status="LEAVE", punch_in=None, punch_out=None, work_hours=None)

        response = self.client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"})
        items = response.json()["items"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["department"], "Engineering")
        self.assertEqual(item["headcount"], 2)
        self.assertEqual(item["present_days"], 2.0)
        self.assertEqual(item["avg_work_hours"], 9.0)
        self.assertEqual(item["late_count"], 1)
        self.assertEqual(item["leave_count"], 1)
        self.assertEqual(item["on_duty_count"], 1)

    def test_late_leaderboard_ranking_ties_limit_and_department(self):
        self.insert_log("EMP0001", "2026-07-15", late_minutes=30)
        self.insert_log("EMP0002", "2026-07-15", late_minutes=20)
        self.insert_log("EMP0002", "2026-07-16", late_minutes=10)
        self.insert_log("EMP0003", "2026-07-15", late_minutes=30)

        all_departments = self.client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()
        self.assertEqual([(item["rank"], item["emp_code"], item["total_late_minutes"]) for item in all_departments["items"]], [
            (1, "EMP0001", 30),
            (1, "EMP0002", 30),
            (1, "EMP0003", 30),
        ])

        engineering = self.client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-07", "department": "Engineering", "limit": 1},
        ).json()
        self.assertEqual([item["emp_code"] for item in engineering["items"]], ["EMP0001", "EMP0002"])
        self.assertEqual(self.client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code, 422)

    def test_department_trend_gap_fill_weekends_rates_moving_average_and_errors(self):
        self.insert_log("EMP0001", "2026-07-15", late_minutes=5)
        self.insert_log("EMP0002", "2026-07-15")
        self.insert_log("EMP0001", "2026-07-16", half_day=True)

        response = self.client.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-07-15", "to": "2026-07-19"},
        )
        rows = response.json()["items"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["date"] for row in rows], ["2026-07-15", "2026-07-16", "2026-07-17", "2026-07-18", "2026-07-19"])
        self.assertEqual(rows[0]["headcount"], 2)
        self.assertEqual(rows[0]["present_count"], 2.0)
        self.assertEqual(rows[0]["attendance_rate"], 1.0)
        self.assertEqual(rows[0]["late_count"], 1)
        self.assertEqual(rows[1]["present_count"], 0.5)
        self.assertEqual(rows[1]["attendance_rate"], 0.25)
        self.assertIsNone(rows[3]["attendance_rate"])
        self.assertEqual(rows[2]["moving_avg_7d"], 0.4167)
        self.assertEqual(self.client.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code, 404)
        self.assertEqual(self.client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code, 422)
        self.assertEqual(self.client.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-30"}).status_code, 422)


class AdminExplainEndpointTests(APITestCase):
    def test_explain_endpoint_validation_and_supported_queries(self):
        self.insert_employee("EMP0001")
        self.insert_log("EMP0001", "2026-07-01")
        supported = [
            ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07", "department": "Engineering"}),
            ("late_leaderboard", {"month": "2026-07", "limit": 10}),
            ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-03"}),
        ]
        for endpoint, params in supported:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(f"/admin/explain/{endpoint}", params=params)
                body = response.json()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(body["endpoint"], endpoint)
                self.assertIn("explain", body)
                self.assertIn(body["collection"], {"employees", "attendance_logs"})

        self.assertEqual(self.client.get("/admin/explain/unknown", params={"month": "2026-07"}).status_code, 422)
        self.assertEqual(self.client.get("/admin/explain/employee_monthly").status_code, 422)
        self.assertEqual(self.client.get("/admin/explain/department_trend", params={"department": "Engineering"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()

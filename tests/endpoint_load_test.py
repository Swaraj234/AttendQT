"""
Endpoint integration and load script.

This script talks to a running API over HTTP and creates data only through the
public endpoints. By default it creates 100,000 attendance records.

Start the API first, for example:
    hr_attend\\Scripts\\uvicorn.exe main:app --port 8000

Then run:
    hr_attend\\Scripts\\python.exe tests\\endpoint_load_test.py

Use a smaller run while iterating:
    hr_attend\\Scripts\\python.exe tests\\endpoint_load_test.py --records 1000 --employees 50
"""

import argparse
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
DEPARTMENTS = ("Engineering", "Sales", "Support", "Finance")


class APIError(RuntimeError):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path} returned {status}: {body}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class APIClient:
    def __init__(self, base_url, timeout):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method, path, payload=None, params=None, ok=(200, 201)):
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                parsed = json.loads(body) if body else None
                if response.status not in ok:
                    raise APIError(method, path, response.status, parsed)
                return response.status, parsed
        except HTTPError as error:
            body = error.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = body
            if error.code in ok:
                return error.code, parsed
            raise APIError(method, path, error.code, parsed) from error
        except URLError as error:
            raise RuntimeError(f"Could not connect to {self.base_url}: {error}") from error

    def get(self, path, params=None, ok=(200,)):
        return self.request("GET", path, params=params, ok=ok)

    def post(self, path, payload, ok=(200, 201)):
        return self.request("POST", path, payload=payload, ok=ok)

    def patch(self, path, payload, ok=(200,)):
        return self.request("PATCH", path, payload=payload, ok=ok)


def epoch_ms_ist(day, hour, minute, second=0):
    instant = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(
        hour=hour, minute=minute, second=second
    )
    return int(instant.timestamp() * 1000)


def employee_code(start_code, index):
    value = start_code + index
    if value > 999999:
        raise ValueError("start-code + employees must stay within EMP999999")
    return f"EMP{value:06d}"


def create_employees(client, start_code, count, progress_every):
    created = 0
    existing = 0
    codes = []
    started = time.perf_counter()
    for index in range(count):
        code = employee_code(start_code, index)
        codes.append(code)
        payload = {
            "emp_code": code,
            "name": f"Load Employee {code}",
            "email": f"{code.lower()}@load.test",
            "department": DEPARTMENTS[index % len(DEPARTMENTS)],
            "joined_on": "2026-01-01",
        }
        status, _ = client.post("/employees", payload, ok=(201, 409))
        if status == 201:
            created += 1
        else:
            existing += 1
        if progress_every and (index + 1) % progress_every == 0:
            print(f"employees: {index + 1}/{count}")
    elapsed = time.perf_counter() - started
    print(f"employees done: created={created}, existing={existing}, elapsed={elapsed:.2f}s")
    return codes


def attendance_payloads(codes, records, start_day):
    for index in range(records):
        code = codes[index % len(codes)]
        day = start_day + timedelta(days=index // len(codes))
        status = PRESENCE_STATUSES[index % len(PRESENCE_STATUSES)]
        pattern = index % 6
        if pattern == 0:
            punch_in = epoch_ms_ist(day, 9, 30)
            punch_out = epoch_ms_ist(day, 18, 30)
        elif pattern == 1:
            punch_in = epoch_ms_ist(day, 9, 40, 1)
            punch_out = epoch_ms_ist(day, 18, 30)
        elif pattern == 2:
            punch_in = epoch_ms_ist(day, 10, 5)
            punch_out = epoch_ms_ist(day, 19, 15)
        elif pattern == 3:
            punch_in = epoch_ms_ist(day, 9, 30)
            punch_out = epoch_ms_ist(day, 13, 15)
        elif pattern == 4:
            punch_in = epoch_ms_ist(day, 9, 35)
            punch_out = epoch_ms_ist(day, 20, 0)
        else:
            punch_in = epoch_ms_ist(day, 9, 45)
            punch_out = epoch_ms_ist(day, 18, 0)
        yield code, day.isoformat(), status, punch_in, punch_out


def create_attendance(client, codes, records, start_day, progress_every):
    punched_in = 0
    punched_out = 0
    existing = 0
    started = time.perf_counter()
    for index, (code, _, status, punch_in, punch_out) in enumerate(
        attendance_payloads(codes, records, start_day), start=1
    ):
        in_status, _ = client.post(
            "/attendance/punch-in",
            {"emp_code": code, "status": status, "punched_at": punch_in},
            ok=(201, 409),
        )
        if in_status == 201:
            punched_in += 1
        else:
            existing += 1

        out_status, _ = client.post(
            "/attendance/punch-out",
            {"emp_code": code, "punched_at": punch_out},
            ok=(200, 409),
        )
        if out_status == 200:
            punched_out += 1

        if progress_every and index % progress_every == 0:
            elapsed = time.perf_counter() - started
            print(
                f"attendance: {index}/{records}, "
                f"created={punched_in}, closed={punched_out}, existing={existing}, "
                f"elapsed={elapsed:.2f}s"
            )
    elapsed = time.perf_counter() - started
    print(
        f"attendance done: created={punched_in}, closed={punched_out}, "
        f"existing={existing}, elapsed={elapsed:.2f}s"
    )


def ensure_special_cases(client, start_code):
    overnight_code = employee_code(start_code, 0)
    day = date(2026, 8, 3)
    status, _ = client.post(
        "/employees",
        {
            "emp_code": overnight_code,
            "name": f"Overnight Employee {overnight_code}",
            "email": f"{overnight_code.lower()}@load.test",
            "department": "NightOps",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-08-01",
        },
        ok=(201, 409),
    )
    client.post(
        "/attendance/punch-in",
        {
            "emp_code": overnight_code,
            "status": "ON_DUTY",
            "punched_at": epoch_ms_ist(day + timedelta(days=1), 5, 30),
        },
        ok=(201, 409),
    )
    client.post(
        "/attendance/punch-out",
        {
            "emp_code": overnight_code,
            "punched_at": epoch_ms_ist(day + timedelta(days=1), 6, 45),
        },
        ok=(200, 409),
    )
    return overnight_code, day.isoformat(), status


def exercise_all_endpoints(client, codes, start_day, records, special_code, special_day):
    last_day = start_day + timedelta(days=math.ceil(records / len(codes)) - 1)
    sample_code = codes[0]
    sample_day = start_day.isoformat()
    month = start_day.strftime("%Y-%m")
    department = DEPARTMENTS[0]

    checks = [
        ("GET", "/health", None, None, (200,)),
        ("GET", "/employees", {"department": department, "page": 1, "page_size": 20}, None, (200,)),
        (
            "GET",
            "/attendance",
            {
                "emp_code": sample_code,
                "date_from": start_day.isoformat(),
                "date_to": last_day.isoformat(),
                "page": 1,
                "page_size": 100,
            },
            None,
            (200,),
        ),
        ("GET", f"/analytics/employees/{sample_code}/monthly", {"month": month}, None, (200,)),
        ("GET", "/analytics/departments/summary", {"month": month}, None, (200,)),
        ("GET", "/analytics/leaderboard/late", {"month": month, "limit": 10}, None, (200,)),
        (
            "GET",
            f"/analytics/departments/{department}/trend",
            {"from": start_day.isoformat(), "to": min(last_day, start_day + timedelta(days=30)).isoformat()},
            None,
            (200,),
        ),
        (
            "GET",
            "/admin/explain/attendance_list",
            {"date_from": start_day.isoformat(), "date_to": last_day.isoformat(), "page_size": 100},
            None,
            (200,),
        ),
        (
            "GET",
            "/admin/explain/employee_monthly",
            {"emp_code": sample_code, "month": month},
            None,
            (200,),
        ),
        ("GET", "/admin/explain/department_summary", {"month": month}, None, (200,)),
        ("GET", "/admin/explain/late_leaderboard", {"month": month, "limit": 10}, None, (200,)),
        (
            "GET",
            "/admin/explain/department_trend",
            {
                "department": department,
                "from": start_day.isoformat(),
                "to": min(last_day, start_day + timedelta(days=7)).isoformat(),
            },
            None,
            (200,),
        ),
    ]

    for method, path, params, payload, ok in checks:
        if method == "GET":
            status, body = client.get(path, params=params, ok=ok)
        elif method == "PATCH":
            status, body = client.patch(path, payload, ok=ok)
        else:
            status, body = client.post(path, payload, ok=ok)
        print(f"{method} {path}: {status}")
        if path == "/attendance":
            print(f"attendance page total={body['total']} items={len(body['items'])}")

    regularize_payload = {
        "punch_in": epoch_ms_ist(start_day, 9, 30),
        "reason": "Endpoint load script correction",
        "regularized_by": "load-script",
    }
    status, _ = client.patch(
        f"/attendance/{sample_code}/{sample_day}",
        regularize_payload,
        ok=(200, 422),
    )
    print(f"PATCH /attendance/{sample_code}/{sample_day}: {status}")

    status, body = client.get(
        "/attendance",
        params={"emp_code": special_code, "date_from": special_day, "date_to": special_day},
        ok=(200,),
    )
    print(f"overnight attendance lookup: {status}, total={body['total']}")


def parse_args():
    parser = argparse.ArgumentParser(description="Create and verify endpoint-backed load data.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--records", type=int, default=100000)
    parser.add_argument("--employees", type=int, default=500)
    parser.add_argument("--start-code", type=int, default=800000)
    parser.add_argument("--special-start-code", type=int, default=799900)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--progress-every", type=int, default=1000)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.records < 1:
        raise ValueError("--records must be at least 1")
    if args.employees < 1:
        raise ValueError("--employees must be at least 1")
    if args.employees > args.records:
        args.employees = args.records

    start_day = date.fromisoformat(args.start_date)
    client = APIClient(args.base_url, args.timeout)

    print(f"checking API at {args.base_url}")
    client.get("/health")

    print(f"creating {args.employees} employees")
    codes = create_employees(client, args.start_code, args.employees, args.progress_every)

    print(f"creating {args.records} attendance records through punch-in and punch-out endpoints")
    create_attendance(client, codes, args.records, start_day, args.progress_every)

    print("creating overnight special-case data")
    special_code, special_day, _ = ensure_special_cases(client, args.special_start_code)

    print("exercising remaining endpoints")
    exercise_all_endpoints(client, codes, start_day, args.records, special_code, special_day)

    print("endpoint load script completed successfully")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(error, file=sys.stderr)
        raise

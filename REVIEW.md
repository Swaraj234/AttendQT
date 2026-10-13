# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `models/schemas.py` / employee request schema | Employee inputs were plain strings, so invalid `emp_code`, empty names, bad emails, invalid shift times, equal shifts, and bad dates were accepted. | Contract tests sending malformed employee payloads returned `201` instead of `422`. | Added Pydantic field constraints and validators for `emp_code`, `name`, `email`, `department`, `shift_start`, `shift_end`, `joined_on`, and shift equality. |
| 2 | `models/schemas.py` / punch timestamp fields | `punched_at`, `punch_in`, and `punch_out` accepted seconds-looking values, floats/strings, and out-of-range timestamps. | Requests with timestamps such as `1783312500` were accepted instead of rejected. | Added shared epoch-millisecond validation with the OpenAPI min/max bounds. |
| 3 | `main.py` / `create_employee` | `created_at` was returned as an ISO datetime string instead of epoch milliseconds. | `POST /employees`p response had `"created_at": "2026-..."`. | Store a UTC datetime internally and convert it to epoch milliseconds in the API response. |
| 4 | `main.py` / `create_employee` | Duplicate employee handling used a check-then-insert pattern, which is unsafe under concurrent requests. | Two concurrent duplicate creates could both pass the initial lookup. | Added duplicate-key handling around `insert_one` and rely on the unique `emp_code` index. |
| 5 | `main.py` / `list_employees` | `total` ignored filters and always counted every employee. | `GET /employees?department=Engineering` returned the correct page but the total for all employees. | Changed `count_documents({})` to `count_documents(q)`. |
| 6 | `main.py` / `list_employees` | Pagination accepted invalid values such as `page=0` and `page_size=101`. | Invalid pagination did not return `422`. | Added shared pagination validation enforcing `page >= 1` and `1 <= page_size <= 100`. |
| 7 | `main.py` / `punch_in` | Punch-in datetimes were returned as ISO strings instead of epoch milliseconds. | `POST /attendance/punch-in` returned `"punch_in": "2026-..."`. | Reused `api_record()` so attendance responses serialize datetimes as epoch milliseconds. |
| 8 | `main.py` / `punch_in` | Punch-in timestamps were not truncated to whole seconds before storing/returning. | A request at `09:40:00.900` came back with millisecond precision. | Added whole-second truncation for supplied and default punch-in times. |
| 9 | `main.py` / `punch_in` | Attendance date was calculated from the UTC date instead of IST and did not handle overnight shifts. | Overnight shift punch-in before shift end was assigned to the wrong calendar date. | Added `_attendance_date()` using IST and the overnight-shift rule from the contract. |
| 10 | `helpers/helper.py` / `compute_late_minutes` | The grace-period threshold used floored minutes, so `09:40:01` after a `09:30` shift was not counted late. | A punch-in strictly more than 10 minutes late returned `late_minutes = 0`. | Compare elapsed seconds against `10 * 60`, then return floored whole late minutes. |
| 11 | `main.py` / `punch_in` | Duplicate attendance records used check-then-insert and the attendance index was not unique. | Concurrent same-day punch-ins could both create records. | Made `(emp_code, date)` unique and catch duplicate-key errors as `409`. |
| 12 | `main.py` / `punch_out` | Some response serialization was hand-written and inconsistent with other attendance responses. | Risk of returning mixed datetime formats or leaking internal fields. | Return `api_record(record)` after updating punch-out data. |
| 13 | `main.py` / `list_attendance` | `date_from` and `date_to` were typed as `datetime`, while the contract uses `YYYY-MM-DD` strings. | Date filtering mixed datetime parsing with stored date strings. | Parse and validate query dates as ISO calendar-date strings. |
| 14 | `main.py` / `list_attendance` | Date filters were applied in Python after loading records from MongoDB. | The 100,000-record efficiency test showed the DB query did not include the date range. | Push date filters into the MongoDB query with `$gte`/`$lte`. |
| 15 | `main.py` / `list_attendance` | Sorting used `reverse=True` on `(date, emp_code)`, making both fields descending. | Records on the same date were sorted by `emp_code` descending. | Use Mongo sort `[("date", -1), ("emp_code", 1)]`. |
| 16 | `main.py` / `list_attendance` | Pagination was done after loading and filtering all matching documents. | Large datasets required unnecessary in-memory work. | Use Mongo cursor `sort().skip().limit()` and `count_documents(q)`. |
| 17 | `main.py` / `regularize_attendance` | Regularized `punch_in` date validation only checked the simple IST date and did not use the overnight-shift rule. | Overnight shift corrections could be rejected or assigned inconsistently. | Reused `_attendance_date()` for regularization validation. |
| 18 | `main.py` / `regularize_attendance` | Timestamp conversion used a separate helper path from punch-in/punch-out. | Risk of inconsistent truncation and epoch handling across endpoints. | Reused `_utc_from_epoch_ms()` for regularization timestamps. |
| 19 | `main.py` / `health` | Health returned `200` without checking MongoDB readiness. | `/health` reported ok even if the database was unavailable. | Added `db.command("ping")` and return `503` on failure. |
| 20 | `main.py` / indexes and explain support | Attendance indexes did not enforce the natural key uniqueness required by the contract. | Duplicate attendance records for the same employee/date were possible. | Made the `(emp_code, date)` index unique and kept query shapes aligned with explain support. |

## Tests Added

Created `tests/test_api_contract.py`, a standard-library `unittest` suite with a small ASGI client and in-memory fake MongoDB layer. It covers all endpoints, validation edges, date/time business rules, analytics calculations, admin explain responses, and a 100,000-record attendance-list efficiency case.

Run command:

```powershell
hr_attend\Scripts\python.exe -m unittest discover -s tests -v
```

Current result:

```text
Ran 17 tests in 0.757s

OK
```

## Not Defects

- MongoDB `_id` is still kept internally because the public API uses `emp_code` and `(emp_code, date)` as business keys.
- Weekend attendance records are retained because the contract says they can still contribute to late and overtime totals.
- A missing attendance record is not auto-created as `ABSENT`; the contract treats no record and an explicit `ABSENT` record differently.
- Analytics still read stored derived values rather than recomputing them, matching the OpenAPI contract.

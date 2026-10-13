"""Attendance API (FastAPI + MongoDB).

Conventions
-----------
* All timestamps cross the API as epoch **milliseconds** (int) and are stored in
  MongoDB as naive UTC datetimes truncated to whole seconds.
* ``date`` on an attendance log is the IST *attendance date* (``YYYY-MM-DD``),
  which for overnight shifts can be the day before the calendar date of a punch.
* Analytics only count logs that fall on a working day (Mon-Fri) on or after the
  employee's ``joined_on`` date. Weekend logs are ignored.
"""
import calendar
import json
import os
import re
from bisect import bisect_right
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

try:
    from helpers.helper import IST
except ImportError:  # pragma: no cover - fallback if helpers package is absent
    IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1)

GRACE_MINUTES = 10  # punch-in this many minutes after shift start is not "late"
HALF_DAY_HOURS = 4.5  # worked less than this -> half day
MAX_SHIFT = timedelta(hours=24)  # punch-out must be within this of punch-in
MAX_TREND_DAYS = 92
MS_MIN, MS_MAX = 10**11, 10**14  # rejects second-based timestamps

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
OFF_STATUSES = ("ABSENT", "LEAVE")

PunchStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AnyStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1)])


@asynccontextmanager
async def lifespan(_app):
    create_indexes()
    yield


app = FastAPI(title="Attendance API", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def ms_to_utc(ms: int) -> datetime:
    """Epoch ms -> naive UTC datetime truncated to whole seconds."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(value: datetime) -> int:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def as_date(value: Any, default: Optional[date] = None) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return default


def parse_iso_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")


def month_bounds(month: str):
    match = re.fullmatch(r"(\d{4})-(\d{2})", month or "")
    if not match:
        raise HTTPException(422, "month must be in YYYY-MM format")
    year, mon = int(match.group(1)), int(match.group(2))
    try:
        first = date(year, mon, 1)
    except ValueError:
        raise HTTPException(422, "month must be in YYYY-MM format")
    return first, date(year, mon, calendar.monthrange(year, mon)[1])


def validate_trend_range(start: date, end: date):
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"date range must not exceed {MAX_TREND_DAYS} days")


# --------------------------------------------------------------------------- #
# Shift / attendance helpers
# --------------------------------------------------------------------------- #
def shift_times(emp: dict):
    return (
        parse_hhmm(emp.get("shift_start", "09:30")),
        parse_hhmm(emp.get("shift_end", "18:30")),
    )


def shift_bounds(day: date, emp: dict):
    """Shift start/end as IST-aware datetimes for the given attendance date."""
    start_t, end_t = shift_times(emp)
    start = datetime.combine(day, start_t, tzinfo=IST)
    end = datetime.combine(day, end_t, tzinfo=IST)
    if end <= start:  # overnight shift
        end += timedelta(days=1)
    return start, end


def attendance_date(local_dt: datetime, emp: dict) -> date:
    """IST attendance date for a punch; early-morning punches of an overnight
    shift belong to the previous day."""
    start_t, end_t = shift_times(emp)
    day = local_dt.date()
    if end_t <= start_t and local_dt.time() <= end_t:
        day -= timedelta(days=1)
    return day


def compute_derived(emp: dict, day: date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    late, work, overtime, half = 0, None, 0, False
    if punch_in is not None:
        start, _ = shift_bounds(day, emp)
        seconds = (to_ist(punch_in) - start).total_seconds()
        late = 0 if seconds <= GRACE_MINUTES * 60 else int(seconds // 60)
    if punch_in is not None and punch_out is not None:
        work = round((punch_out - punch_in).total_seconds() / 3600, 2)
        _, end = shift_bounds(day, emp)
        overtime = max(0, int((to_ist(punch_out) - end).total_seconds() // 60))
        half = work < HALF_DAY_HOURS
    return {
        "late_minutes": late,
        "work_hours": work,
        "overtime_minutes": overtime,
        "half_day": half,
    }


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value


def serialize_employee(doc: dict) -> dict:
    return to_jsonable(doc)


def serialize_log(doc: dict) -> dict:
    out = to_jsonable(doc)
    out.setdefault("history", [])
    return out


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"Employee {emp_code} not found")
    return emp


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: date

    @field_validator("name", "department")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)
    status: PunchStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[AnyStatus] = None
    punch_in: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)
    punch_out: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)
    reason: str = Field(min_length=5, max_length=500)
    regularized_by: str = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------------- #
# Health + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeIn):
    if db.employees.find_one({"emp_code": payload.emp_code}, {"_id": 0}) is not None:
        raise HTTPException(409, f"Employee {payload.emp_code} already exists")
    doc = {
        "emp_code": payload.emp_code,
        "name": payload.name,
        "email": payload.email,
        "department": payload.department,
        "shift_start": payload.shift_start,
        "shift_end": payload.shift_end,
        "joined_on": payload.joined_on.isoformat(),
        "created_at": now_utc(),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee {payload.emp_code} already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: Dict[str, Any] = {}
    if department:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort([("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [serialize_employee(doc) for doc in cursor],
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in_endpoint(payload: PunchInRequest):
    emp = get_employee_or_404(payload.emp_code)
    punched = ms_to_utc(payload.punched_at) if payload.punched_at is not None else now_utc().replace(microsecond=0)
    day = attendance_date(to_ist(punched), emp)
    day_s = day.isoformat()

    if db.attendance_logs.find_one({"emp_code": payload.emp_code, "date": day_s}, {"_id": 0}) is not None:
        raise HTTPException(409, f"Attendance for {payload.emp_code} on {day_s} already exists")

    doc = {
        "emp_code": payload.emp_code,
        "date": day_s,
        "status": payload.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
        "late_minutes": compute_derived(emp, day, punched, None)["late_minutes"],
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Attendance for {payload.emp_code} on {day_s} already exists")
    return serialize_log(doc)


@app.post("/attendance/punch-out")
def punch_out_endpoint(payload: PunchOutRequest):
    emp = get_employee_or_404(payload.emp_code)
    punched = ms_to_utc(payload.punched_at) if payload.punched_at is not None else now_utc().replace(microsecond=0)
    day = attendance_date(to_ist(punched), emp)

    log = None
    for candidate in (day, day - timedelta(days=1)):
        found = db.attendance_logs.find_one(
            {"emp_code": payload.emp_code, "date": candidate.isoformat()}, {"_id": 0}
        )
        if found is not None and found.get("punch_in") is not None:
            log = found
            break
    if log is None:
        raise HTTPException(404, "No punch-in found for this employee")
    if log.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    elapsed = punched - log["punch_in"]
    if elapsed <= timedelta(0):
        raise HTTPException(422, "punch_out must be after punch_in")
    if elapsed > MAX_SHIFT:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    derived = compute_derived(emp, as_date(log["date"]), log["punch_in"], punched)
    fields = {"punch_out": punched, **{k: v for k, v in derived.items() if k != "late_minutes"}}
    db.attendance_logs.update_one(
        {"emp_code": payload.emp_code, "date": log["date"]}, {"$set": fields}
    )
    log.update(fields)
    return serialize_log(log)


def build_attendance_query(
    emp_code: Optional[str],
    status: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
) -> dict:
    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    date_filter: Dict[str, str] = {}
    if date_from:
        date_filter["$gte"] = date_from.isoformat()
    if date_to:
        date_filter["$lte"] = date_to.isoformat()
    if date_filter:
        query["date"] = date_filter
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    status: Optional[AnyStatus] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query = build_attendance_query(emp_code, status, date_from, date_to)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [serialize_log(doc) for doc in cursor],
    }


@app.patch("/attendance/{emp_code}/{day}")
def regularize_attendance(emp_code: str, day: str, payload: RegularizeRequest):
    log_day = parse_iso_date(day, "date")
    emp = get_employee_or_404(emp_code)
    key = {"emp_code": emp_code, "date": log_day.isoformat()}
    log = db.attendance_logs.find_one(key, {"_id": 0})
    if log is None:
        raise HTTPException(404, f"No attendance record for {emp_code} on {key['date']}")

    changes: Dict[str, dict] = {}
    touched = any(v is not None for v in (payload.status, payload.punch_in, payload.punch_out))
    if touched:
        new_status = payload.status or log.get("status") or "PRESENT"
        if new_status in OFF_STATUSES:
            if payload.punch_in is not None or payload.punch_out is not None:
                raise HTTPException(422, f"{new_status} records cannot have punch times")
            new_in = new_out = None
        else:
            new_in = ms_to_utc(payload.punch_in) if payload.punch_in is not None else log.get("punch_in")
            new_out = ms_to_utc(payload.punch_out) if payload.punch_out is not None else log.get("punch_out")
            if payload.punch_in is not None and attendance_date(to_ist(new_in), emp) != log_day:
                raise HTTPException(422, "punch_in does not belong to this attendance date")
            if new_out is not None:
                if new_in is None:
                    raise HTTPException(422, "punch_out requires a punch_in")
                elapsed = new_out - new_in
                if elapsed <= timedelta(0):
                    raise HTTPException(422, "punch_out must be after punch_in")
                if elapsed > MAX_SHIFT:
                    raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

        new_values = {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            **compute_derived(emp, log_day, new_in, new_out),
        }
        for field, new in new_values.items():
            old = log.get(field)
            if old != new:
                changes[field] = {"from": old, "to": new}

    if not changes:
        raise HTTPException(422, "Regularization would not change the record")

    entry = {
        "changed_at": now_utc(),
        "regularized_by": payload.regularized_by,
        "reason": payload.reason,
        "changes": changes,
    }
    db.attendance_logs.update_one(
        key,
        {"$set": {field: change["to"] for field, change in changes.items()}, "$push": {"history": entry}},
    )
    return serialize_log(db.attendance_logs.find_one(key, {"_id": 0}))


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def joined_date(emp: dict) -> date:
    return as_date(emp.get("joined_on"), date(1970, 1, 1))


def counts_for_analytics(log_day: Optional[date], joined: date) -> bool:
    return log_day is not None and log_day.weekday() < 5 and log_day >= joined


def present_weight(log: dict) -> float:
    if log.get("status") not in PRESENT_STATUSES:
        return 0.0
    return 0.5 if log.get("half_day") else 1.0


def count_working_days(start: date, end: date) -> int:
    count, current = 0, start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def logs_query(codes: Optional[List[str]], start: date, end: date, extra: Optional[dict] = None) -> dict:
    query: Dict[str, Any] = {}
    if codes is not None:
        query["emp_code"] = {"$in": codes}
    query["date"] = {"$gte": start.isoformat(), "$lte": end.isoformat()}
    if extra:
        query.update(extra)
    return query


def fetch_logs(codes: Optional[List[str]], start: date, end: date, extra: Optional[dict] = None) -> List[dict]:
    if codes is not None and not codes:
        return []
    return list(db.attendance_logs.find(logs_query(codes, start, end, extra), {"_id": 0}))


def department_employees(department: str) -> List[dict]:
    return list(db.employees.find({"department": department}, {"_id": 0}))


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    joined = joined_date(emp)

    working_days = count_working_days(max(first, joined), last)
    present = 0.0
    leave = late_count = total_late = total_overtime = 0
    query = build_attendance_query(emp_code, None, first, last)
    for log in db.attendance_logs.find(query, {"_id": 0}):
        if not counts_for_analytics(as_date(log.get("date")), joined):
            continue
        present += present_weight(log)
        if log.get("status") == "LEAVE":
            leave += 1
        late = log.get("late_minutes") or 0
        if late > 0:
            late_count += 1
            total_late += late
        total_overtime += log.get("overtime_minutes") or 0

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round(present, 2),
        "leave_days": leave,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    first, last = month_bounds(month)
    emp_query: Dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        emp_query["department"] = department
    employees = {e["emp_code"]: e for e in db.employees.find(emp_query, {"_id": 0})}

    stats: Dict[str, dict] = {}
    for emp in employees.values():
        s = stats.setdefault(
            emp["department"],
            {"headcount": 0, "present": 0.0, "hours": 0.0, "hours_n": 0, "late": 0, "leave": 0, "on_duty": 0},
        )
        s["headcount"] += 1

    codes = list(employees) if department else None
    for log in fetch_logs(codes, first, last):
        emp = employees.get(log.get("emp_code"))
        if emp is None or not counts_for_analytics(as_date(log.get("date")), joined_date(emp)):
            continue
        s = stats[emp["department"]]
        weight = present_weight(log)
        s["present"] += weight
        if weight > 0 and log.get("work_hours") is not None:
            s["hours"] += log["work_hours"]
            s["hours_n"] += 1
        if (log.get("late_minutes") or 0) > 0:
            s["late"] += 1
        if log.get("status") == "LEAVE":
            s["leave"] += 1
        if log.get("status") == "ON_DUTY":
            s["on_duty"] += 1

    items = [
        {
            "department": name,
            "headcount": s["headcount"],
            "present_days": round(s["present"], 2),
            "avg_work_hours": round(s["hours"] / s["hours_n"], 2) if s["hours_n"] else None,
            "late_count": s["late"],
            "leave_count": s["leave"],
            "on_duty_count": s["on_duty"],
        }
        for name, s in sorted(stats.items())
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    first, last = month_bounds(month)
    emp_query: Dict[str, Any] = {}
    if department:
        emp_query["department"] = department
    employees = {e["emp_code"]: e for e in db.employees.find(emp_query, {"_id": 0})}

    totals: Dict[str, Dict[str, int]] = {}
    codes = list(employees) if department else None
    for log in fetch_logs(codes, first, last, {"late_minutes": {"$gt": 0}}):
        emp = employees.get(log.get("emp_code"))
        if emp is None or not counts_for_analytics(as_date(log.get("date")), joined_date(emp)):
            continue
        entry = totals.setdefault(emp["emp_code"], {"minutes": 0, "count": 0})
        entry["minutes"] += log.get("late_minutes") or 0
        entry["count"] += 1

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1]["minutes"], kv[0]))
    items, rank, previous = [], 0, None
    for index, (code, entry) in enumerate(ranked, start=1):
        if entry["minutes"] != previous:  # standard competition ranking: 1, 1, 3
            rank, previous = index, entry["minutes"]
        if rank > limit:
            break
        items.append(
            {
                "rank": rank,
                "emp_code": code,
                "name": employees[code].get("name"),
                "department": employees[code].get("department"),
                "total_late_minutes": entry["minutes"],
                "late_count": entry["count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
):
    validate_trend_range(from_, to)
    employees = department_employees(department)
    if not employees:
        raise HTTPException(404, f"Department {department} not found")
    by_code = {e["emp_code"]: e for e in employees}
    joined_sorted = sorted(joined_date(e) for e in employees)

    present_by_day: Dict[date, float] = {}
    late_by_day: Dict[date, int] = {}
    for log in fetch_logs(list(by_code), from_, to):
        emp = by_code.get(log.get("emp_code"))
        log_day = as_date(log.get("date"))
        if emp is None or not counts_for_analytics(log_day, joined_date(emp)):
            continue
        present_by_day[log_day] = present_by_day.get(log_day, 0.0) + present_weight(log)
        if (log.get("late_minutes") or 0) > 0:
            late_by_day[log_day] = late_by_day.get(log_day, 0) + 1

    rows: List[dict] = []
    rates: List[Optional[float]] = []
    current = from_
    while current <= to:
        headcount = bisect_right(joined_sorted, current)
        working = current.weekday() < 5
        present = present_by_day.get(current, 0.0) if working else 0.0
        rate = round(present / headcount, 4) if working and headcount else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append(
            {
                "date": current.isoformat(),
                "headcount": headcount,
                "present_count": round(present, 2),
                "attendance_rate": rate,
                "late_count": late_by_day.get(current, 0) if working else 0,
                "moving_avg_7d": round(sum(window) / len(window), 4) if working and window else None,
            }
        )
        current += timedelta(days=1)
    return {"department": department, "from": from_.isoformat(), "to": to.isoformat(), "items": rows}


# --------------------------------------------------------------------------- #
# Admin: query plans
# --------------------------------------------------------------------------- #
EXPLAINABLE = (
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
)


def _require(params, name: str) -> str:
    value = params.get(name)
    if value in (None, ""):
        raise HTTPException(422, f"Query parameter '{name}' is required")
    return value


def _optional_date(params, name: str) -> Optional[date]:
    value = params.get(name)
    return parse_iso_date(value, name) if value else None


@app.get("/admin/explain/{endpoint}")
def explain_query(endpoint: str, request: Request):
    if endpoint not in EXPLAINABLE:
        raise HTTPException(422, f"endpoint must be one of {', '.join(EXPLAINABLE)}")
    params = request.query_params
    collection = "attendance_logs"
    sort = None

    if endpoint == "attendance_list":
        date_from, date_to = _optional_date(params, "date_from"), _optional_date(params, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        status = params.get("status")
        if status and status not in PRESENT_STATUSES + OFF_STATUSES:
            raise HTTPException(422, "invalid status")
        query = build_attendance_query(params.get("emp_code"), status, date_from, date_to)
        sort = ATTENDANCE_SORT
    elif endpoint == "employee_monthly":
        emp_code = _require(params, "emp_code")
        first, last = month_bounds(_require(params, "month"))
        query = build_attendance_query(emp_code, None, first, last)
    elif endpoint == "department_summary":
        first, last = month_bounds(_require(params, "month"))
        department = params.get("department")
        codes = [e["emp_code"] for e in department_employees(department)] if department else None
        query = logs_query(codes, first, last)
    elif endpoint == "late_leaderboard":
        first, last = month_bounds(_require(params, "month"))
        try:
            limit = int(params.get("limit", 10))
        except ValueError:
            raise HTTPException(422, "limit must be an integer")
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit must be between 1 and 100")
        department = params.get("department")
        codes = [e["emp_code"] for e in department_employees(department)] if department else None
        query = logs_query(codes, first, last, {"late_minutes": {"$gt": 0}})
    else:  # department_trend
        department = _require(params, "department")
        start = parse_iso_date(_require(params, "from"), "from")
        end = parse_iso_date(_require(params, "to"), "to")
        validate_trend_range(start, end)
        employees = department_employees(department)
        if not employees:
            raise HTTPException(404, f"Department {department} not found")
        query = logs_query([e["emp_code"] for e in employees], start, end)

    command: Dict[str, Any] = {"explain": {"find": collection, "filter": query}, "verbosity": "executionStats"}
    if sort:
        command["explain"]["sort"] = dict(sort)
    result = db.command(command)
    return {
        "endpoint": endpoint,
        "collection": collection,
        "query": query,
        "explain": json.loads(json_util.dumps(result)),
    }
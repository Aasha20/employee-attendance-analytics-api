"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import datetime, timedelta, timezone, date, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_employee_attendance_date",
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_employee_punchin",
    )

    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined",
    )
    
    db.employees.create_index(
        [("joined_on", 1)],
        name="ix_employee_joined_on",
    )




# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


def parse_epoch_ms(value: Optional[int]) -> datetime:
    """Convert integer epoch milliseconds to a UTC datetime."""
    if value is None:
        result = datetime.now(timezone.utc)
    else:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                status_code=422,
                detail="Timestamp must be an integer in epoch milliseconds",
            )
        if value < 100000000000:
            raise HTTPException(
                status_code=422,
                detail="Timestamp must be in epoch milliseconds, not seconds",
            )
        try:
            result = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(status_code=422, detail="Invalid timestamp")

    return result.replace(microsecond=0)


def validate_status(status: str, allowed: set[str]) -> None:
    if status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Status must be one of: {', '.join(sorted(allowed))}",
        )


def ist_date_for_punch(punch_in: datetime, employee: dict) -> str:
    """Return the attendance date using the employee's shift and IST."""
    local_time = punch_in.astimezone(IST)
    shift_start = datetime.strptime(employee["shift_start"], "%H:%M").time()
    shift_end = datetime.strptime(employee["shift_end"], "%H:%M").time()

    overnight = shift_end <= shift_start
    if overnight and local_time.time() < shift_end:
        return (local_time.date() - timedelta(days=1)).isoformat()

    return local_time.date().isoformat()


def compute_late_for_shift(punch_in: datetime, shift_start: str, date_str: str) -> int:
    local_punch = punch_in.astimezone(IST)
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    start = datetime.combine(date.fromisoformat(date_str), start_time, tzinfo=IST)

    if local_punch < start:
        return 0

    elapsed_minutes = int((local_punch - start).total_seconds() // 60)
    return elapsed_minutes if (local_punch - start).total_seconds() > 600 else 0


def compute_overtime_for_shift(
    punch_out: datetime, shift_end: str, date_str: str, overnight: bool
) -> int:
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    end_date = date.fromisoformat(date_str)
    if overnight:
        end_date += timedelta(days=1)

    shift_end_dt = datetime.combine(end_date, end_time, tzinfo=IST)
    elapsed = (punch_out.astimezone(IST) - shift_end_dt).total_seconds()

    return int(elapsed // 60) if elapsed >= 1800 else 0


def recompute_attendance(record: dict, employee: dict) -> dict:
    """Recalculate derived fields after a punch-out or correction."""
    
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")

    # Normalize MongoDB's naive UTC datetimes to timezone-aware UTC.
    if isinstance(punch_in, datetime) and punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
        record["punch_in"] = punch_in

    if isinstance(punch_out, datetime) and punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
        record["punch_out"] = punch_out

    status = record.get("status", "PRESENT")


    record["late_minutes"] = 0
    record["overtime_minutes"] = 0
    record["work_hours"] = None
    record["half_day"] = False

    if status in {"ABSENT", "LEAVE"}:
        record["punch_in"] = None
        record["punch_out"] = None
        return record

    if punch_in is None:
        return record

    record["late_minutes"] = compute_late_for_shift(
        punch_in, employee["shift_start"], record["date"]
    )

    if punch_out is not None:
        seconds = (punch_out - punch_in).total_seconds()
        if seconds <= 0 or seconds > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in and within 24 hours",
            )

        record["work_hours"] = float(
            (Decimal(str(seconds / 3600)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ))
        )
        record["half_day"] = record["work_hours"] < 4.50

        shift_start = datetime.strptime(employee["shift_start"], "%H:%M").time()
        shift_end = datetime.strptime(employee["shift_end"], "%H:%M").time()
        overnight = shift_end <= shift_start

        record["overtime_minutes"] = compute_overtime_for_shift(
            punch_out, employee["shift_end"], record["date"], overnight
        )

    return record



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError(
                "emp_code must be EMP followed by 4 to 6 digits"
            )
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Shift time must use HH:MM format")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_end(cls, value, info):
        start = info.data.get("shift_start", "09:30")
        if value == start:
            raise ValueError("shift_start and shift_end must differ")
        return value



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class AttendanceCorrectionIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        created_at = item.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            item["created_at"] = int(created_at.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    validate_status(body.status, {"PRESENT", "WFH", "ON_DUTY"})

    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = parse_epoch_ms(body.punched_at)
    attendance_date = ist_date_for_punch(punched_at, employee)

    record = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "late_minutes": compute_late_for_shift(
            punched_at, employee["shift_start"], attendance_date
        ),
        "overtime_minutes": 0,
        "work_hours": None,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already exists for this date",
        )

    record.pop("_id", None)
    record["punch_in"] = int(punched_at.timestamp() * 1000)
    return record


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = parse_epoch_ms(body.punched_at)

    # Find the most recent attendance record whose punch-in is not in the future.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No matching punch-in record found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    if record.get("status") in {"ABSENT", "LEAVE"}:
        raise HTTPException(
            status_code=409,
            detail="Cannot punch out an absent or leave record",
        )

    # MongoDB returns naive datetimes by default; interpret them as UTC.
    stored_punch_in = record["punch_in"]
    if stored_punch_in.tzinfo is None:
        stored_punch_in = stored_punch_in.replace(tzinfo=timezone.utc)

    record["punch_in"] = stored_punch_in
    seconds = (punched_at - stored_punch_in).total_seconds()

    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    record["punch_out"] = punched_at
    record = recompute_attendance(record, employee)

    # Update only if another request has not already punched out this record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": record["punch_out"],
                "late_minutes": record["late_minutes"],
                "overtime_minutes": record["overtime_minutes"],
                "work_hours": record["work_hours"],
                "half_day": record["half_day"],
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already updated",
        )

    
    record.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        value = record.get(field)
        record[field] = int(value.timestamp() * 1000) if value else None

    for entry in record.get("history", []):
        value = entry.get("at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            entry["at"] = int(value.timestamp() * 1000)

    return record


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if status is not None:
        validate_status(
            status,
            {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"},
        )
        query["status"] = status

    if date_from is not None:
        try:
            parsed_from = date.fromisoformat(date_from)
            if parsed_from.isoformat() != date_from:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="date_from must use YYYY-MM-DD format",
            )

    if date_to is not None:
        try:
            parsed_to = date.fromisoformat(date_to)
            if parsed_to.isoformat() != date_to:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="date_to must use YYYY-MM-DD format",
            )

    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)

    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        for field in ("punch_in", "punch_out"):
            value = item.get(field)
            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                item[field] = int(value.timestamp() * 1000)

        for entry in item.get("history", []):
            value = entry.get("at")
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                entry["at"] = int(value.timestamp() * 1000)

            for change in entry.get("changes", {}).values():
                if isinstance(change, dict):
                    for side in ("from", "to"):
                        value = change.get(side)
                        if isinstance(value, datetime):
                            if value.tzinfo is None:
                                value = value.replace(tzinfo=timezone.utc)
                            change[side] = int(value.timestamp() * 1000)
        
        
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date_str}")
def correct_attendance(
    emp_code: str,
    date_str: str,
    body: AttendanceCorrectionIn,
):
    try:
        parsed_date = date.fromisoformat(date_str)
        if parsed_date.isoformat() != date_str:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format",
        )

    reason = body.reason.strip()
    regularized_by = body.regularized_by.strip()

    if not 5 <= len(reason) <= 200:
        raise HTTPException(
            status_code=422,
            detail="reason must contain 5 to 200 characters",
        )

    if not 1 <= len(regularized_by) <= 50:
        raise HTTPException(
            status_code=422,
            detail="regularized_by must contain 1 to 50 characters",
        )

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date_str}
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    changes = {}
    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    requested.pop("regularized_by", None)

    allowed_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

    for field, value in requested.items():
        if field == "status":
            validate_status(value, allowed_statuses)
            old_value = record.get("status")
            if old_value != value:
                changes[field] = {"from": old_value, "to": value}

        elif field in {"punch_in", "punch_out"}:
            new_value = parse_epoch_ms(value) if value is not None else None
            old_value = record.get(field)

            if isinstance(old_value, datetime) and old_value.tzinfo is None:
                old_value = old_value.replace(tzinfo=timezone.utc)

           
            if old_value != new_value:
                changes[field] = {
                    "from": old_value,
                    "to": new_value,
                }


    if not changes:
        raise HTTPException(
            status_code=422,
            detail="No attendance fields changed",
        )

    updated = dict(record)
    for field in requested:
        if field in changes:
            value = requested[field]
            if field in {"punch_in", "punch_out"} and value is not None:
                value = parse_epoch_ms(value)
            updated[field] = value

    if updated.get("status") in {"ABSENT", "LEAVE"}:
        if requested.get("punch_in") is not None or requested.get("punch_out") is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE records cannot have punch timestamps",
            )
        updated["punch_in"] = None
        updated["punch_out"] = None
    elif updated.get("punch_in") is None:
        raise HTTPException(
            status_code=422,
            detail="Presence statuses require punch_in",
        )

    
    updated = recompute_attendance(updated, employee)

    # Include every changed field in the audit trail.
    for field in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        old_value = record.get(field)
        new_value = updated.get(field)

        if isinstance(old_value, datetime) and old_value.tzinfo is None:
            old_value = old_value.replace(tzinfo=timezone.utc)
        if isinstance(new_value, datetime) and new_value.tzinfo is None:
            new_value = new_value.replace(tzinfo=timezone.utc)

        if old_value != new_value and field not in changes:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }
            
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": regularized_by,
        "reason": reason,
        "changes": changes,
    }

    # Compare-and-set avoids overwriting a concurrent correction.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": {
                "status": updated["status"],
                "punch_in": updated.get("punch_in"),
                "punch_out": updated.get("punch_out"),
                "late_minutes": updated["late_minutes"],
                "overtime_minutes": updated["overtime_minutes"],
                "work_hours": updated["work_hours"],
                "half_day": updated["half_day"],
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the correction",
        )

    refreshed = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    for field in ("punch_in", "punch_out"):
        value = refreshed.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            refreshed[field] = int(value.timestamp() * 1000)

    
    for entry in refreshed.get("history", []):
        value = entry.get("at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            entry["at"] = int(value.timestamp() * 1000)

        for change in entry.get("changes", {}).values():
            if isinstance(change, dict):
                for side in ("from", "to"):
                    value = change.get(side)
                    if isinstance(value, datetime):
                        if value.tzinfo is None:
                            value = value.replace(tzinfo=timezone.utc)
                        change[side] = int(value.timestamp() * 1000)

    return refreshed



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    try:
        year, month_number = map(int, month.split("-"))
        if not 1 <= month_number <= 12:
            raise ValueError
        month_start = date(year, month_number, 1)
        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(month_start, joined_on)

    working_days = 0
    current_day = effective_start
    while current_day < next_month:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}

    present_days = round(float(stats.get("present_days", 0)), 2)
    attendance_pct = (
        round(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    try:
        year, month_number = map(int, month.split("-"))
        if not 1 <= month_number <= 12:
            raise ValueError

        month_start = date(year, month_number, 1)
        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    employee_match = {
        "joined_on": {"$lt": next_month.isoformat()}
    }
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid_hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid_hours"}, 0]},
                                {"$avg": "$$valid_hours"},
                                None,
                            ]
                        },
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    try:
        year, month_number = map(int, month.split("-"))
        if not 1 <= month_number <= 12:
            raise ValueError

        month_start = date(year, month_number, 1)
        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {
                    "total_late_minutes": -1,
                },
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$limit": limit},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)

        if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )
    
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"_id": {"$ne": None}}},
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$set": {
                "date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start_day, time.min),
                        datetime.combine(end_day, time.max),
                    ],
                },
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "from": from_date, "to": to_date, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=404,
            detail="Unknown endpoint",
        )

    def require(value, name):
        if value is None or value == "":
            raise HTTPException(
                status_code=422,
                detail=f"{name} is required",
            )
        return value

    def month_bounds(value):
        try:
            if len(value) != 7:
                raise ValueError
            year, month_number = map(int, value.split("-"))
            if not 1 <= month_number <= 12:
                raise ValueError

            start = date(year, month_number, 1)
            if month_number == 12:
                end = date(year + 1, 1, 1)
            else:
                end = date(year, month_number + 1, 1)

            return start.isoformat(), end.isoformat()
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

    try:
        if endpoint == "attendance_list":
            collection = db.attendance_logs
            query = {}

            if emp_code is not None:
                query["emp_code"] = emp_code

            if status is not None:
                validate_status(
                    status,
                    {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"},
                )
                query["status"] = status

            if date_from is not None or date_to is not None:
                date_filter = {}

                if date_from is not None:
                    date.fromisoformat(date_from)
                    date_filter["$gte"] = date_from

                if date_to is not None:
                    date.fromisoformat(date_to)
                    date_filter["$lte"] = date_to

                if (
                    date_from is not None
                    and date_to is not None
                    and date_from > date_to
                ):
                    raise HTTPException(
                        status_code=422,
                        detail="date_from must be on or before date_to",
                    )

                query["date"] = date_filter

            command = {
                "find": collection.name,
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            }

            result = collection.database.command(
                "explain",
                command,
                verbosity="executionStats",
            )

        elif endpoint == "employee_monthly":
            emp_code = require(emp_code, "emp_code")
            month = require(month, "month")
            start, end = month_bounds(month)

            collection = db.attendance_logs
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": start, "$lt": end},
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "leave_days": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$status", "LEAVE"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [
                                    {"$gt": ["$late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "total_overtime_minutes": {
                            "$sum": "$overtime_minutes"
                        },
                    }
                },
            ]

            result = collection.database.command(
                "explain",
                {
                    "aggregate": collection.name,
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )

        elif endpoint == "department_summary":
            month = require(month, "month")
            start, end = month_bounds(month)

            collection = db.employees
            employee_match = {"joined_on": {"$lt": end}}

            if department is not None:
                employee_match["department"] = department

            pipeline = [
                {"$match": employee_match},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$emp_code", "$$code"]},
                                            {"$gte": ["$date", start]},
                                            {"$lt": ["$date", end]},
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },
                {
                    "$unwind": {
                        "path": "$logs",
                        "preserveNullAndEmptyArrays": True,
                    }
                },
                {
                    "$group": {
                        "_id": "$department",
                        "headcount_codes": {"$addToSet": "$emp_code"},
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$logs.half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [
                                    {"$gt": ["$logs.late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "total_late_minutes": {
                            "$sum": {
                                "$ifNull": ["$logs.late_minutes", 0]
                            }
                        },
                        "leave_count": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "on_duty_count": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
                {"$sort": {"_id": 1}},
            ]

            result = collection.database.command(
                "explain",
                {
                    "aggregate": collection.name,
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )

        elif endpoint == "late_leaderboard":
            month = require(month, "month")
            start, end = month_bounds(month)

            collection = db.attendance_logs
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": start, "$lt": end},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
            ]

            if department is not None:
                pipeline.append(
                    {"$match": {"employee.department": department}}
                )

            pipeline.extend([
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                        "total_late_minutes": 1,
                        "rank": 1,
                    }
                },
                {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {"$limit": limit},
            ])

            result = collection.database.command(
                "explain",
                {
                    "aggregate": collection.name,
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )

        else:
            department = require(department, "department")
            from_date = require(from_date, "from")
            to_date = require(to_date, "to")

            try:
                start_day = date.fromisoformat(from_date)
                end_day = date.fromisoformat(to_date)

                if (
                    start_day.isoformat() != from_date
                    or end_day.isoformat() != to_date
                    or end_day < start_day
                    or (end_day - start_day).days + 1 > 92
                ):
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid date range; use YYYY-MM-DD and a maximum of 92 days",
                )

            if not db.employees.find_one({"department": department}):
                raise HTTPException(
                    status_code=404,
                    detail="Department not found",
                )

            collection = db.employees
            
            pipeline = [
                # 1. Select employees in the requested department.
                {"$match": {"department": department}},

                # 2. Fetch attendance records for the requested date range.
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$emp_code", "$$code"]},
                                            {"$gte": ["$date", from_date]},
                                            {"$lte": ["$date", to_date]},
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },

                # 3. Preserve employees without attendance records.
                {
                    "$unwind": {
                        "path": "$logs",
                        "preserveNullAndEmptyArrays": True,
                    }
                },

                # 4. Aggregate daily presence and lateness.
                {
                    "$group": {
                        "_id": "$logs.date",
                        "present_count": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$logs.half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [
                                    {"$gt": ["$logs.late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },

                # 5. Remove the group created for employees with no logs.
                {"$match": {"_id": {"$ne": None}}},

                # 6. Convert the date string into a BSON date.
                {
                    "$project": {
                        "_id": 0,
                        "date": {
                            "$dateFromString": {
                                "dateString": "$_id",
                                "format": "%Y-%m-%d",
                            }
                        },
                        "present_count": 1,
                        "late_count": 1,
                    }
                },

                # 7. Fill gaps so every date in the range is represented.
                {
                    "$densify": {
                        "field": "date",
                        "range": {
                            "step": 1,
                            "unit": "day",
                            "bounds": [
                                datetime.combine(start_day, time.min),
                                datetime.combine(end_day, time.max),
                            ],
                        },
                    }
                },

                # 8. Identify weekdays and fill missing daily counts.
                {
                    "$set": {
                        "is_working_day": {
                            "$and": [
                                {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                                {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                            ]
                        },
                        "present_count": {
                            "$ifNull": ["$present_count", 0]
                        },
                        "late_count": {"$ifNull": ["$late_count", 0]},
                    }
                },

                # 9. Calculate the employee headcount for each date.
                {
                    "$lookup": {
                        "from": "employees",
                        "let": {"day": "$date"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$department",
                                                    department,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    "$joined_on",
                                                    {
                                                        "$dateToString": {
                                                            "format": "%Y-%m-%d",
                                                            "date": "$$day",
                                                        }
                                                    },
                                                ]
                                            },
                                        ]
                                    }
                                }
                            },
                            {"$count": "count"},
                        ],
                        "as": "headcount_result",
                    }
                },

                # 10. Default headcount to zero if no employees qualify.
                {
                    "$set": {
                        "headcount": {
                            "$ifNull": [
                                {
                                    "$arrayElemAt": [
                                        "$headcount_result.count",
                                        0,
                                    ]
                                },
                                0,
                            ]
                        }
                    }
                },

                # 11. Calculate attendance rate on working days only.
                {
                    "$set": {
                        "attendance_rate": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$is_working_day",
                                        {"$gt": ["$headcount", 0]},
                                    ]
                                },
                                {
                                    "$round": [
                                        {
                                            "$divide": [
                                                "$present_count",
                                                "$headcount",
                                            ]
                                        },
                                        4,
                                    ]
                                },
                                None,
                            ]
                        }
                    }
                },

                # 12. Calculate the trailing seven-day moving average.
                {
                    "$setWindowFields": {
                        "sortBy": {"date": 1},
                        "output": {
                            "moving_avg_7d": {
                                "$avg": "$attendance_rate",
                                "window": {"documents": [-6, 0]},
                            }
                        },
                    }
                },

                # 13. Return the same output fields as the trend endpoint.
                {
                    "$project": {
                        "_id": 0,
                        "date": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$date",
                            }
                        },
                        "is_working_day": 1,
                        "headcount": 1,
                        "present_count": 1,
                        "late_count": 1,
                        "attendance_rate": 1,
                        "moving_avg_7d": 1,
                    }
                },

                # 14. Return dates in ascending order.
                {"$sort": {"date": 1}},
            ]
            result = collection.database.command(
                "explain",
                {
                    "aggregate": collection.name,
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )

        return {
            "endpoint": endpoint,
            "collection": collection.name,
            "explain": result,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Explain failed: {str(exc)}",
        )

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

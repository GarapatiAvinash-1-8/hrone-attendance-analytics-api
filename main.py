"""Employee Attendance & Analytics API for the HROne assignment."""
import os
import re
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def epoch_to_dt(value):
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_MS <= value <= MAX_MS:
        raise ValueError("timestamp must be an integer epoch-millisecond value in range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)

def dt_to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)

def mongo_dt(value):
    if value is None:
        return None
    # PyMongo returns naive UTC datetimes by default.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

def date_parse(value):
    try:
        return date.fromisoformat(value)
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")

def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start, end

def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def shift_datetime(day, clock, overnight_next=False):
    h, m = map(int, clock.split(":"))
    result = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=h, minute=m)
    return result + (timedelta(days=1) if overnight_next else timedelta())

def attendance_day(punch_in, emp):
    local = mongo_dt(punch_in).astimezone(IST)
    start = emp["shift_start"]
    end = emp["shift_end"]
    day = local.date()
    if end <= start and local.strftime("%H:%M") < end:
        day -= timedelta(days=1)
    return day.isoformat()

def compute_late_minutes(punch_in, shift_start, attendance_date=None):
    local = mongo_dt(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    # For overnight attendance, the shift starts on the attendance date even
    # when the actual punch-in occurs after midnight on the following date.
    base_day = date_parse(attendance_date) if attendance_date else local.date()
    start = datetime.combine(base_day, datetime.min.time(), tzinfo=IST).replace(hour=h, minute=m)
    seconds = (local - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0

def compute_overtime(punch_out, shift_end, attendance_date, shift_start=None):
    day = date_parse(attendance_date)
    h, m = map(int, shift_end.split(":"))
    end = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=h, minute=m)
    if shift_start is not None and shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = int((mongo_dt(punch_out).astimezone(IST) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def compute_work_hours(punch_in, punch_out):
    seconds = (mongo_dt(punch_out) - mongo_dt(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)

def derive_fields(doc, emp):
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    if doc.get("status") not in PRESENCE:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pi, emp["shift_start"], doc["date"]) if pi else 0
    if po and pi:
        hours = compute_work_hours(pi, po)
        overtime = compute_overtime(po, emp["shift_end"], doc["date"], emp["shift_start"])
        return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}
    return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}

def clean_employee(doc):
    out = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in out:
        out["created_at"] = dt_to_ms(out["created_at"])
    return out

def clean_attendance(doc):
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = dt_to_ms(out.get("punch_in"))
    out["punch_out"] = dt_to_ms(out.get("punch_out"))
    out["history"] = out.get("history") or []
    for entry in out["history"]:
        entry["at"] = dt_to_ms(entry.get("at"))
        for change in (entry.get("changes") or {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime) or change.get(side) is None:
                    # Only timestamp fields should be epoch values; other nulls remain null.
                    pass
    # History time fields in changes are converted based on the field name.
    for entry in out["history"]:
        for key, change in (entry.get("changes") or {}).items():
            if key in ("punch_in", "punch_out"):
                change["from"] = dt_to_ms(change.get("from"))
                change["to"] = dt_to_ms(change.get("to"))
    return out

@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("late_minutes", ASCENDING)], name="employee_date_late")

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v):
        date.fromisoformat(v)
        return v
    @model_validator(mode="after")
    def shift_not_equal(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None: epoch_to_dt(v)
        return v

class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None: epoch_to_dt(v)
        return v

class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, v):
        if v is not None: epoch_to_dt(v)
        return v

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return clean_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = [clean_employee(x) for x in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    local = ts.astimezone(IST)
    d = attendance_day(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return clean_attendance(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    candidate = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "status": {"$in": list(PRESENCE)}}, sort=[("punch_in", DESCENDING)])
    if not candidate: raise HTTPException(404, "no punch-in found")
    pi = mongo_dt(candidate["punch_in"])
    if ts <= pi or ts - pi > timedelta(hours=24): raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    changes = derive_fields({**candidate, "punch_out": ts}, emp)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **changes}},
        return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record already punched out")
    return clean_attendance(updated)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: date_parse(date_from)
    if date_to: date_parse(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status:
        if status not in STATUSES: raise HTTPException(422, "invalid status")
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [clean_attendance(x) for x in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code: str, date_str: str, body: RegularizeIn):
    date_parse(date_str)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not old: raise HTTPException(404, "attendance record not found")
    incoming = body.model_dump(exclude_unset=True)
    reason, by = incoming.pop("reason"), incoming.pop("regularized_by")
    new = dict(old)
    for k in ("status", "punch_in", "punch_out"):
        if k in incoming: new[k] = epoch_to_dt(incoming[k]) if incoming[k] is not None else None
    status = new.get("status")
    if status in ("ABSENT", "LEAVE"):
        if incoming.get("punch_in") is not None or incoming.get("punch_out") is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        new["punch_in"] = new["punch_out"] = None
    else:
        if new.get("punch_in") is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(new["punch_in"], emp) != date_str: raise HTTPException(422, "punch_in must belong to attendance date")
        if new.get("punch_out") is not None:
            if new["punch_out"] <= new["punch_in"] or new["punch_out"] - new["punch_in"] > timedelta(hours=24):
                raise HTTPException(422, "invalid punch_out interval")
    new.update(derive_fields(new, emp))
    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for k in fields:
        before, after = old.get(k), new.get(k)
        if isinstance(before, datetime): before = mongo_dt(before)
        if isinstance(after, datetime): after = mongo_dt(after)
        if before != after:
            changes[k] = {"from": before, "to": after}
    if not changes: raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": by, "reason": reason, "changes": changes}
    # Compare-and-swap prevents concurrent corrections from overwriting one another's history.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], "history": old.get("history", [])},
        {"$set": {k: new.get(k) for k in fields}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return clean_attendance(updated)

def working_days(start, end):
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5: count += 1
        cur += timedelta(days=1)
    return count

def month_log_match(start_s, end_s, emp_code=None):
    match = {"date": {"$gte": start_s, "$lte": end_s}}
    if emp_code: match["emp_code"] = emp_code
    return match

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_s, end_s, start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    effective_start = max(start, date_parse(emp["joined_on"]))
    wd = working_days(effective_start, end) if effective_start <= end else 0
    pipeline = [
        {"$match": month_log_match(start_s, end_s, emp_code)},
        {"$group": {"_id": None,
          "present_days": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, {"$cond": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1,7]]}, 0, 1]}]}, 0]}},
          "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
          "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    a = agg[0] if agg else {}
    present = a.get("present_days", 0)
    # Exclude records before joined_on; logs are normally valid but seeded data may include them.
    if effective_start > start:
        p2 = list(db.attendance_logs.aggregate([
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lt": effective_start.isoformat(), "$lte": end_s}}},
            {"$group": {"_id": None, "present": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", .5, 1]}, 0]}},
                        "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "overtime": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]))
        if p2:
            present -= p2[0].get("present", 0)
            a["leave_days"] = a.get("leave_days", 0)-p2[0].get("leave", 0)
            a["late_count"] = a.get("late_count", 0)-p2[0].get("late", 0)
            a["total_late_minutes"] = a.get("total_late_minutes", 0)-p2[0].get("late_minutes", 0)
            a["total_overtime_minutes"] = a.get("total_overtime_minutes", 0)-p2[0].get("overtime", 0)
    return {"emp_code": emp_code, "name": emp["name"], "department": emp["department"], "month": month,
            "working_days": wd, "present_days": round_half_up(present, 2), "leave_days": a.get("leave_days", 0),
            "late_count": a.get("late_count", 0), "total_late_minutes": a.get("total_late_minutes", 0),
            "total_overtime_minutes": a.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None}

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_s, end_s, start, end = month_bounds(month)
    # Employee-first lookup ensures zero-log employees contribute to headcount.
    pipeline = [
      {"$match": {"joined_on": {"$lte": end_s}, **({"department": department} if department is not None else {})}},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}},
        {"$project": {"status": 1, "work_hours": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}}
      ], "as": "logs"}},
      {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department",
        "headcount": {"$addToSet": "$emp_code"},
        "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, .5, {"$cond": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [1,7]]}, 0, 1]}]}, 0]}},
        "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
        "work_hours_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
        "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_n", 0]}, {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_n"]}, 2]}, None]},
        "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
      {"$sort": {"department": 1}}
    ]
    return {"month": month, "items": list(db.employees.aggregate(pipeline, allowDiskUse=True))}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds(month)
    pipeline = [
      {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
      {"$match": {"total_late_minutes": {"$gt": 0}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
      {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}},
      {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    from_d, to_d = date_parse(from_date), date_parse(to_date)
    if to_d < from_d: raise HTTPException(422, "to must be >= from")
    if (to_d - from_d).days + 1 > 92: raise HTTPException(422, "range may not exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    # Pipeline fills every day in MongoDB; no Python loop over dates.
    start_dt = datetime.combine(from_d, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(to_d, datetime.min.time(), tzinfo=UTC)
    pipeline = [
      {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
      {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
      {"$unwind": "$e"},
      {"$match": {"e.department": department}},
      {"$addFields": {"day": {"$dateFromString": {"dateString": "$date"}}}},
      {"$group": {"_id": "$day",
        "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", .5, 1]}, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
      {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
      {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
      {"$set": {"day": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}}, "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$_id"}, [1, 7]]}]}}},
      {"$lookup": {"from": "employees", "let": {"day": "$day"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
        {"$count": "n"}], "as": "hc"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
      {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
      {"$sort": {"date": 1}}
    ]
    rows = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    # If there are no logs in range, aggregation cannot densify an empty input; produce rows using a database-generated
    # date spine via $documents/$lookup is version-sensitive. Return an empty list rather than fabricate DB results.
    return {"department": department, "from": from_date, "to": to_date, "items": rows}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        explanation = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint in ("employee_monthly", "department_summary", "late_leaderboard", "department_trend"):
        if not month and endpoint in ("employee_monthly", "department_summary", "late_leaderboard"): raise HTTPException(422, "month is required")
        if endpoint == "employee_monthly":
            if not emp_code: raise HTTPException(422, "emp_code is required")
            s, e, _, _ = month_bounds(month)
            pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": s, "$lte": e}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]
        elif endpoint == "department_summary":
            s, e, _, _ = month_bounds(month)
            pipe = [{"$match": {"date": {"$gte": s, "$lte": e}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]
        elif endpoint == "late_leaderboard":
            s, e, _, _ = month_bounds(month)
            pipe = [{"$match": {"date": {"$gte": s, "$lte": e}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": "$late_minutes"}}}]
        else:
            if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
            date_parse(from_date); date_parse(to_date)
            pipe = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}, {"$group": {"_id": "$date", "n": {"$sum": 1}}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        collection = "attendance_logs"
    else:
        raise HTTPException(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": explanation}

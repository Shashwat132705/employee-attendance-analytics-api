"""Employee Attendance & Analytics API for the HROne assignment."""
import os, re, math
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ---------- date/time and serialization helpers ----------
def trunc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)

def from_ms(ms: int) -> datetime:
    if isinstance(ms, bool) or not isinstance(ms, int) or ms < 100_000_000_000 or ms > 4_102_444_800_000:
        raise ValueError("timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)

def ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def utc(dt):
    if dt is None: return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

def round_half_up(value, places=2):
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def time_parts(s):
    h, m = map(int, s.split(":")); return h, m

def shift_datetimes(date_str, shift_start, shift_end):
    d = date.fromisoformat(date_str); sh, sm = time_parts(shift_start); eh, em = time_parts(shift_end)
    start = datetime.combine(d, time(sh, sm), IST)
    end_date = d + timedelta(days=1) if (eh, em) <= (sh, sm) else d
    end = datetime.combine(end_date, time(eh, em), IST)
    return start.astimezone(UTC).replace(microsecond=0), end.astimezone(UTC).replace(microsecond=0)

def attendance_date(ts, emp):
    local = ts.astimezone(IST)
    d = local.date()
    eh, em = time_parts(emp["shift_end"]); sh, sm = time_parts(emp["shift_start"])
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em): d -= timedelta(days=1)
    return d.isoformat()

def compute_late_minutes(punch_in, shift_start, date_str=None):
    if punch_in is None: return 0
    local = utc(punch_in).astimezone(IST)
    if date_str is None: date_str = local.date().isoformat()
    h, m = time_parts(shift_start)
    start = datetime.combine(date.fromisoformat(date_str), time(h,m), IST).astimezone(UTC)
    elapsed = int((utc(punch_in) - start).total_seconds() // 60)
    return elapsed if (utc(punch_in) - start).total_seconds() > 600 else 0

def compute_work_hours(punch_in, punch_out):
    return round_half_up((utc(punch_out)-utc(punch_in)).total_seconds()/3600, 2)

def compute_overtime(punch_out, shift_end, date_str, shift_start="09:30"):
    _, end = shift_datetimes(date_str, shift_start, shift_end)
    minutes = int((utc(punch_out)-end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def public_attendance(doc):
    d = {k:v for k,v in doc.items() if k != "_id"}
    for k in ("punch_in", "punch_out"):
        d[k] = ms(d.get(k))
    d.setdefault("work_hours", None); d.setdefault("late_minutes", 0); d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False); d.setdefault("history", [])
    for h in d["history"]:
        h["at"] = ms(h.get("at"))
        for ch in h.get("changes", {}).values():
            for k in ("from", "to"):
                if isinstance(ch.get(k), datetime): ch[k] = ms(ch[k])
    return d

def public_employee(doc):
    d = {k:v for k,v in doc.items() if k != "_id"}
    d["created_at"] = ms(d.get("created_at")); return d

def require_date(s):
    try:
        if not isinstance(s, str) or date.fromisoformat(s).isoformat() != s: raise ValueError()
        return date.fromisoformat(s)
    except Exception: raise HTTPException(422, "date must be YYYY-MM-DD")

def require_month(s):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""): raise HTTPException(422, "month must be YYYY-MM")
    y,m = map(int,s.split("-")); return date(y,m,1), (date(y+1,1,1) if m==12 else date(y,m+1,1))

def working_days(start, end):
    if start > end: return 0
    return sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday()<5)

def present_value(d):
    if d.get("status") not in PRESENCE: return 0.0
    return 0.5 if d.get("half_day", False) else 1.0

def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")

@app.on_event("startup")
def startup():
    client.admin.command("ping")
    indexes()

# ---------- request models ----------
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls,v):
        require_date(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at", mode="before")
    @classmethod
    def integer_ms(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int)): raise ValueError("punched_at must be integer epoch milliseconds")
        return v

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    @field_validator("punched_at", mode="before")
    @classmethod
    def integer_ms(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int)): raise ValueError("punched_at must be integer epoch milliseconds")
        return v

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def integer_ms(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int)): raise ValueError("time must be integer epoch milliseconds")
        return v

# ---------- health and employee endpoints ----------
@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except PyMongoError: raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return public_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str]=None, page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q)
    items=[public_employee(x) for x in db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

# ---------- attendance endpoints ----------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    try: ts=trunc_dt(from_ms(body.punched_at)) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    except ValueError as e: raise HTTPException(422,str(e))
    day=attendance_date(ts,emp)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,
         "late_minutes":compute_late_minutes(ts,emp["shift_start"],day),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return public_attendance(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    try: ts=trunc_dt(from_ms(body.punched_at)) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    except ValueError as e: raise HTTPException(422,str(e))
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"no punch-in found")
    pin=utc(rec.get("punch_in"))
    if ts<=pin or (ts-pin).total_seconds()>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    hours=compute_work_hours(pin,ts); overtime=compute_overtime(ts,emp["shift_end"],rec["date"],emp["shift_start"]); half=hours<4.5
    updated=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,"work_hours":hours,"overtime_minutes":overtime,"half_day":half}},return_document=ReturnDocument.AFTER)
    if not updated:
        latest=db.attendance_logs.find_one({"_id":rec["_id"]})
        if latest and latest.get("punch_out") is not None: raise HTTPException(409,"already punched out")
        raise HTTPException(409,"record changed; retry")
    return public_attendance(updated)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str]=None,date_from: Optional[str]=None,date_to: Optional[str]=None,status: Optional[str]=None,
                    page: int=Query(1,ge=1),page_size: int=Query(20,ge=1,le=100)):
    if date_from: require_date(date_from)
    if date_to: require_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    if status and status not in STATUSES: raise HTTPException(422,"invalid status")
    q={}
    if emp_code: q["emp_code"]=emp_code
    if status: q["status"]=status
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[public_attendance(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str,date: str,body: RegularizeIn):
    require_date(date)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    old=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not old: raise HTTPException(404,"attendance record not found")
    old.setdefault("history",[]); old.setdefault("late_minutes",0); old.setdefault("overtime_minutes",0); old.setdefault("half_day",False)
    fields=body.model_dump(exclude_unset=True); reason=fields.pop("reason"); who=fields.pop("regularized_by")
    if not fields: raise HTTPException(422,"no attendance fields supplied")
    new={k:old.get(k) for k in old}
    for k,v in fields.items():
        if k in ("punch_in","punch_out") and v is not None:
            try: v=from_ms(v)
            except ValueError as e: raise HTTPException(422,str(e))
        if k in ("punch_in","punch_out") and v is None and k not in old: new[k]=None
        else: new[k]=v
    if new.get("status") in ("ABSENT","LEAVE"):
        if "punch_in" in fields and fields["punch_in"] is not None or "punch_out" in fields and fields["punch_out"] is not None:
            raise HTTPException(422,"ABSENT/LEAVE cannot have punch times")
        new["punch_in"]=None; new["punch_out"]=None
    elif new.get("punch_in") is None: raise HTTPException(422,"presence status requires punch_in")
    if new.get("punch_in") is not None:
        pin=utc(new["punch_in"])
        if attendance_date(pin,emp)!=date: raise HTTPException(422,"punch_in must belong to attendance date")
        new["punch_in"]=pin
    if new.get("punch_out") is not None:
        pout=utc(new["punch_out"]); pin=utc(new.get("punch_in"))
        if pin is None or pout<=pin or (pout-pin).total_seconds()>86400: raise HTTPException(422,"invalid punch_out")
        new["punch_out"]=pout
    if new.get("status") in ("ABSENT","LEAVE"):
        new.update(work_hours=None,late_minutes=0,overtime_minutes=0,half_day=False)
    else:
        new["late_minutes"]=compute_late_minutes(new.get("punch_in"),emp["shift_start"],date)
        if new.get("punch_out") is None: new.update(work_hours=None,overtime_minutes=0,half_day=False)
        else:
            new["work_hours"]=compute_work_hours(new["punch_in"],new["punch_out"])
            new["overtime_minutes"]=compute_overtime(new["punch_out"],emp["shift_end"],date,emp["shift_start"])
            new["half_day"]=new["work_hours"]<4.5
    track=["status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day"]
    changes={}
    for k in track:
        a=old.get(k); b=new.get(k)
        if isinstance(a,datetime): a=utc(a)
        if isinstance(b,datetime): b=utc(b)
        if a!=b: changes[k]={"from":old.get(k),"to":new.get(k)}
    if not changes: raise HTTPException(422,"request changes nothing")
    entry={"at":datetime.now(UTC).replace(microsecond=0),"by":who,"reason":reason,"changes":changes}
    # Atomic compare-and-swap avoids overwriting concurrent corrections/history.
    filt={"_id":old["_id"],"history":old.get("history",[])}
    sets={k:new.get(k) for k in track}
    updated=db.attendance_logs.find_one_and_update(filt,{"$set":sets,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record changed concurrently; retry")
    return public_attendance(updated)

# ---------- analytics helpers ----------
def month_match(month):
    start,end=require_month(month); return start.isoformat(),(end-timedelta(days=1)).isoformat(),start,end

def attendance_projection():
    return {"_id":0,"emp_code":1,"date":1,"status":1,"work_hours":1,"late_minutes":1,"overtime_minutes":1,"half_day":1}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    first, last, msd, med = month_match(month)

    joined = max(date.fromisoformat(emp["joined_on"]), msd)
    wd = working_days(joined, med - timedelta(days=1))

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first, "$lte": last}
            }
        },
        {
            "$addFields": {
                "_is_weekday": {
                    "$lte": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$date"
                                }
                            }
                        },
                        6
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", PRESENCE]},
                                    "$_is_weekday"
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    r = result[0] if result else {}

    pd = r.get("present_days", 0)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(pd),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": (
            None if wd == 0
            else round_half_up(pd / wd * 100)
        )
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),department: Optional[str]=None):
    first,last,msd,med=month_match(month)
    eq=[{"$eq":["$status","PRESENT"]},{"$eq":["$status","WFH"]},{"$eq":["$status","ON_DUTY"]}]
    pipeline=[{"$match":{"date":{"$gte":first,"$lte":last}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},{"$unwind":"$emp"}]
    if department is not None: pipeline.append({"$match":{"emp.department":department}})
    pipeline += [{"$group":{"_id":"$emp.department","present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",PRESENCE]},{"$and":[
    {"$gt":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},1]},
    {"$lt":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},7]}
]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},
       "hours":{"$push":{"$cond":[{"$and":[{"$in":["$status",PRESENCE]},{"$ne":["$work_hours",None]}]},"$work_hours",None]}},
       "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"leave_count":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$status","ON_DUTY"]},1,0]}}}},
       {"$project":{"department":"$_id","present_days":1,"avg_work_hours":{"$avg":"$hours"},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}}]
    logs={x["department"]:x for x in db.attendance_logs.aggregate(pipeline)}
    ematch={"joined_on":{"$lte":last}}
    if department is not None: ematch["department"]=department
    heads=list(db.employees.aggregate([{"$match":ematch},{"$group":{"_id":"$department","headcount":{"$sum":1}}}]))
    items=[]
    for h in heads:
        dep=h["_id"]; l=logs.get(dep,{})
        items.append({"department":dep,"headcount":h["headcount"],"present_days":round_half_up(l.get("present_days",0)),"avg_work_hours":None if l.get("avg_work_hours") is None else round_half_up(l["avg_work_hours"]),"late_count":l.get("late_count",0),"total_late_minutes":l.get("total_late_minutes",0),"leave_count":l.get("leave_count",0),"on_duty_count":l.get("on_duty_count",0)})
    items.sort(key=lambda x:x["department"])
    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    first,last,_,_=month_match(month)
    pipeline=[{"$match":{"date":{"$gte":first,"$lte":last},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},{"$match":{"total_late_minutes":{"$gt":0}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},{"$unwind":"$emp"}]
    if department is not None:pipeline.append({"$match":{"emp.department":department}})
    pipeline += [{"$set":{"emp_code":"$_id","name":"$emp.name","department":"$emp.department"}},{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"emp_code":1}},{"$project":{"_id":0,"rank":1,"emp_code":1,"name":1,"department":1,"total_late_minutes":1,"late_count":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    start = require_date(from_date)
    end = require_date(to_date)

    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range; maximum 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "unknown department")

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENCE]},
                            {"$cond": [
                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                0.5, 1
                            ]},
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1, 0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$set": {
                "d": {"$dateFromString": {"dateString": "$date"}}
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min),
                        datetime.combine(end, time.min)
                    ]
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$d"
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {"$sort": {"date": 1}},
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$d"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$d"}, 6]}
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$current_date"]
                            }
                        }
                    },
                    {"$count": "headcount"}
                ],
                "as": "hc"
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$hc.headcount", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        None
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
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$round": ["$moving_avg_7d", 4]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    return {"department": department, "items": items}


# ---------- explain ----------
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

    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed: raise HTTPException(422,"unsupported endpoint")
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        ex=db.command("explain",{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size},verbosity="executionStats")
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="employee_monthly":
        if not emp_code or not month:raise HTTPException(422,"emp_code and month are required")
        first,last,_,_=month_match(month); pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":first,"$lte":last}}},{"$sort":{"date":1}}]
        ex=db.command("explain",{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},verbosity="executionStats")
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="department_summary":
        if not month:raise HTTPException(422,"month is required")
        first,last,_,_=month_match(month); pipe=[{"$match":{"date":{"$gte":first,"$lte":last}}},{"$sort":{"date":1}}]
        ex=db.command("explain",{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},verbosity="executionStats")
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="late_leaderboard":
        if not month:raise HTTPException(422,"month is required")
        first,last,_,_=month_match(month); pipe=[{"$match":{"date":{"$gte":first,"$lte":last},"late_minutes":{"$gt":0}}},{"$sort":{"date":1}}]
        ex=db.command("explain",{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},verbosity="executionStats")
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    
    if not department or not from_date or not to_date:
        raise HTTPException(422, "department, from and to are required")

    start = require_date(from_date)
    end = require_date(to_date)

    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range; maximum 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "unknown department")

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENCE]},
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    0.5, 1
                ]},
                0
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                1, 0
            ]}}
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "present_count": 1,
            "late_count": 1
        }},
        {"$set": {"d": {"$dateFromString": {"dateString": "$date"}}}},
        {"$densify": {
            "field": "d",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [
                    datetime.combine(start, time.min),
                    datetime.combine(end, time.min)
                ]
            }
        }},
        {"$set": {
            "date": {"$dateToString": {
                "format": "%Y-%m-%d", "date": "$d"
            }},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]}
        }},
        {"$sort": {"date": 1}},
        {"$set": {"is_working_day": {"$and": [
            {"$gte": [{"$dayOfWeek": "$d"}, 2]},
            {"$lte": [{"$dayOfWeek": "$d"}, 6]}
        ]}}},
        {"$lookup": {
            "from": "employees",
            "let": {"current_date": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$current_date"]}
                }},
                {"$count": "headcount"}
            ],
            "as": "hc"
        }},
        {"$set": {"headcount": {"$ifNull": [
            {"$arrayElemAt": ["$hc.headcount", 0]}, 0
        ]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$round": [
                {"$divide": ["$present_count", "$headcount"]}, 4
            ]},
            None
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}
        }},
        {"$sort": {"date": 1}}
    ]

    ex = db.command(
        "explain",
        {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats"
    )
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": ex
    }


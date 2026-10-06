import csv
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import services
from .database import Base, SessionLocal, engine, get_db
from .models import (AuditLog, Handover, QuarantineRecord, ReadingCorrection,
                     Sample, TemperatureReading)
from .services import DomainError

BASE_DIR = Path(__file__).resolve().parent

EVENT_NAMES = {
    "SAMPLE_REGISTERED": "样品录入",
    "HANDOVER_CREATED": "交接发起",
    "HANDOVER_CONFIRMED": "交接确认",
    "HANDOVER_INVALIDATED": "交接失效",
    "READING_RECORDED": "温度记录",
    "READING_CORRECTED": "读数更正",
    "QUARANTINED": "样品隔离",
    "QUARANTINE_RELEASED": "隔离解除",
}


def init_db() -> None:
    """初始化表结构，等待数据库就绪（Compose 一键启动时数据库可能仍在启动）。"""
    for _ in range(30):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except OperationalError:
            time.sleep(1)
    raise RuntimeError("无法连接数据库，初始化失败")


def seed_demo() -> None:
    """可选的演示数据（SEED_DEMO=true 时启用）。"""
    db = SessionLocal()
    try:
        if db.scalar(select(Sample).limit(1)):
            return
        s1 = services.register_sample(db, "DEMO-001", 2.0, 8.0, "冷库A-01", "system")
        s2 = services.register_sample(db, "DEMO-002", -20.0, -10.0, "冷库B-02", "system")
        services.record_temperature(db, s1.id, 5.2, "张三")
        services.create_handover(db, s2.id, "张三", "李四", "实验室C-101", "例行转移")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.getenv("SEED_DEMO", "").lower() in ("1", "true", "yes"):
        seed_demo()
    yield


app = FastAPI(title="实验室样品流转系统", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def fmt_dt(value) -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        return value
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["dt"] = fmt_dt
templates.env.filters["json_detail"] = lambda d: json.dumps(d or {}, ensure_ascii=False)


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("level", request.query_params.get("level", "success"))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, msg: str | None = None, level: str = "success") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={quote(msg)}&level={level}"
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    samples = db.scalars(select(Sample).order_by(Sample.id.desc())).all()
    pending_counts = dict(db.execute(
        select(Handover.sample_id, func.count())
        .where(Handover.status == "PENDING")
        .group_by(Handover.sample_id)
    ).all())
    return render(request, "index.html", samples=samples, pending_counts=pending_counts)


@app.get("/samples/new", response_class=HTMLResponse)
def new_sample(request: Request):
    return render(request, "new_sample.html")


@app.get("/samples/{sample_id}", response_class=HTMLResponse)
def sample_detail(sample_id: int, request: Request, db: Session = Depends(get_db)):
    sample = db.get(Sample, sample_id)
    if sample is None:
        return redirect("/", "样品不存在", "error")
    handovers = db.scalars(
        select(Handover).where(Handover.sample_id == sample_id)
        .order_by(Handover.id.desc())).all()
    readings = db.scalars(
        select(TemperatureReading).where(TemperatureReading.sample_id == sample_id)
        .order_by(TemperatureReading.id.desc())).all()
    corrections = db.scalars(
        select(ReadingCorrection).join(TemperatureReading)
        .where(TemperatureReading.sample_id == sample_id)
        .order_by(ReadingCorrection.id)).all()
    corrections_by_reading: dict[int, list] = {}
    effective: dict[int, float] = {}
    for r in readings:
        effective[r.id] = r.value
    for c in corrections:
        corrections_by_reading.setdefault(c.reading_id, []).append(c)
        effective[c.reading_id] = c.new_value
    quarantines = db.scalars(
        select(QuarantineRecord).where(QuarantineRecord.sample_id == sample_id)
        .order_by(QuarantineRecord.id.desc())).all()
    audits = db.scalars(
        select(AuditLog).where(AuditLog.sample_id == sample_id)
        .order_by(AuditLog.id.desc())).all()
    pending = next((h for h in handovers if h.status == "PENDING"), None)
    return render(request, "sample_detail.html",
                  sample=sample, handovers=handovers, readings=readings,
                  corrections_by_reading=corrections_by_reading, effective=effective,
                  quarantines=quarantines, audits=audits, pending=pending,
                  event_names=EVENT_NAMES)


# ---------------------------------------------------------------- 操作

@app.post("/samples")
def create_sample(code: str = Form(...), temp_min: float = Form(...),
                  temp_max: float = Form(...), location: str = Form(...),
                  operator: str = Form(""), db: Session = Depends(get_db)):
    try:
        sample = services.register_sample(db, code, temp_min, temp_max,
                                          location, operator or None)
    except DomainError as e:
        return redirect("/samples/new", e.message, "error")
    except IntegrityError:
        db.rollback()
        return redirect("/samples/new", f"样品编号 {code} 已存在", "error")
    return redirect(f"/samples/{sample.id}", "样品录入成功")


@app.post("/samples/{sample_id}/handovers")
def create_handover(sample_id: int, from_person: str = Form(...),
                    to_person: str = Form(...), destination: str = Form(...),
                    note: str = Form(""), db: Session = Depends(get_db)):
    try:
        services.create_handover(db, sample_id, from_person, to_person, destination, note)
    except DomainError as e:
        return redirect(f"/samples/{sample_id}", e.message, "error")
    return redirect(f"/samples/{sample_id}", "交接已创建，等待接收人确认（确认前位置不变）")


@app.post("/handovers/{handover_id}/confirm")
def confirm_handover(handover_id: int, db: Session = Depends(get_db)):
    handover = db.get(Handover, handover_id)
    sample_id = handover.sample_id if handover else None
    try:
        services.confirm_handover(db, handover_id)
    except DomainError as e:
        return redirect(f"/samples/{sample_id}" if sample_id else "/", e.message, "error")
    return redirect(f"/samples/{sample_id}", "交接已确认，样品位置已更新")


@app.post("/samples/{sample_id}/readings")
def add_reading(sample_id: int, value: float = Form(...),
                recorded_by: str = Form(...), db: Session = Depends(get_db)):
    try:
        _, out_of_range = services.record_temperature(db, sample_id, value, recorded_by)
    except DomainError as e:
        return redirect(f"/samples/{sample_id}", e.message, "error")
    if out_of_range:
        return redirect(f"/samples/{sample_id}",
                        f"读数 {value}°C 越界：样品已立即隔离，待确认交接已失效", "error")
    return redirect(f"/samples/{sample_id}", "温度读数已记录")


@app.post("/readings/{reading_id}/corrections")
def correct_reading(reading_id: int, new_value: float = Form(...),
                    reason: str = Form(...), operator: str = Form(...),
                    db: Session = Depends(get_db)):
    try:
        _, sample_id = services.correct_reading(db, reading_id, new_value, reason, operator)
    except DomainError as e:
        reading = db.get(TemperatureReading, reading_id)
        url = f"/samples/{reading.sample_id}" if reading else "/"
        return redirect(url, e.message, "error")
    return redirect(f"/samples/{sample_id}",
                    "读数更正已保存（原值保留；更正不会自动解除隔离）")


@app.post("/samples/{sample_id}/release")
def release_quarantine(sample_id: int, reason: str = Form(...),
                       released_by: str = Form(...), expected_version: int = Form(...),
                       db: Session = Depends(get_db)):
    try:
        services.release_quarantine(db, sample_id, reason, released_by, expected_version)
    except DomainError as e:
        return redirect(f"/samples/{sample_id}", e.message, "error")
    return redirect(f"/samples/{sample_id}", "隔离已解除，样品恢复在库状态")


# ---------------------------------------------------------------- 导出

def _audit_csv(audits) -> str:
    buf = io.StringIO()
    buf.write("﻿")  # BOM，便于 Excel 打开中文
    writer = csv.writer(buf)
    writer.writerow(["id", "时间", "事件类型", "操作人", "详情"])
    for a in audits:
        writer.writerow([
            a.id,
            a.created_at.isoformat() if a.created_at else "",
            EVENT_NAMES.get(a.event_type, a.event_type),
            a.actor or "",
            json.dumps(a.detail or {}, ensure_ascii=False),
        ])
    return buf.getvalue()


@app.get("/samples/{sample_id}/export.csv")
def export_sample(sample_id: int, db: Session = Depends(get_db)):
    sample = db.get(Sample, sample_id)
    if sample is None:
        return redirect("/", "样品不存在", "error")
    audits = db.scalars(
        select(AuditLog).where(AuditLog.sample_id == sample_id)
        .order_by(AuditLog.id)).all()
    return Response(
        content=_audit_csv(audits), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit_{sample.code}.csv"'})


@app.get("/export.csv")
def export_all(db: Session = Depends(get_db)):
    audits = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    return Response(
        content=_audit_csv(audits), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit_all.csv"'})


@app.get("/health")
def health():
    return {"status": "ok"}

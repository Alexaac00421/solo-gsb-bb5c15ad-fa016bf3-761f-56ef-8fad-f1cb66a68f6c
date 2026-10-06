"""业务逻辑：所有状态变更均在事务内完成。

并发控制策略：
- 样品状态变更前先 SELECT ... FOR UPDATE 锁定样品行，串行化互斥操作；
- 交接确认、隔离解除使用条件 UPDATE（WHERE status=... / version=...），
  只有基于最新状态的操作才能生效，rowcount=0 即冲突，返回明确提示。
"""
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import (AuditLog, Handover, QuarantineRecord, ReadingCorrection,
                     Sample, TemperatureReading)


class DomainError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ConflictError(DomainError):
    """状态冲突（他人已先行变更），HTTP 409。"""

    def __init__(self, message: str):
        super().__init__(message, 409)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, sample_id: int, event_type: str, actor: str | None, detail: dict):
    db.add(AuditLog(sample_id=sample_id, event_type=event_type,
                    actor=actor, detail=detail or {}))


def _lock_sample(db: Session, sample_id: int) -> Sample:
    sample = db.execute(
        select(Sample).where(Sample.id == sample_id).with_for_update()
    ).scalar_one_or_none()
    if sample is None:
        raise DomainError("样品不存在", 404)
    return sample


# ---------------------------------------------------------------- 样品

def register_sample(db: Session, code: str, temp_min: float, temp_max: float,
                    location: str, operator: str | None = None) -> Sample:
    code = (code or "").strip()
    location = (location or "").strip()
    if not code:
        raise DomainError("样品编号不能为空")
    if temp_min >= temp_max:
        raise DomainError("允许温区下限必须小于上限")
    if not location:
        raise DomainError("当前位置不能为空")
    if db.scalar(select(Sample).where(Sample.code == code)):
        raise ConflictError(f"样品编号 {code} 已存在")
    sample = Sample(code=code, temp_min=temp_min, temp_max=temp_max,
                    current_location=location, status="ACTIVE", version=1)
    db.add(sample)
    db.flush()
    _audit(db, sample.id, "SAMPLE_REGISTERED", operator or "system", {
        "code": code, "temp_min": temp_min, "temp_max": temp_max, "location": location})
    db.commit()
    return sample


# ---------------------------------------------------------------- 交接

def create_handover(db: Session, sample_id: int, from_person: str, to_person: str,
                    destination: str, note: str = "") -> Handover:
    sample = _lock_sample(db, sample_id)
    from_person = (from_person or "").strip()
    to_person = (to_person or "").strip()
    destination = (destination or "").strip()
    if not from_person or not to_person or not destination:
        raise DomainError("交出人、接收人、目的地均为必填")
    if sample.status == "QUARANTINED":
        raise ConflictError("样品处于隔离状态，禁止发起转移")
    pending = db.scalar(
        select(func.count()).select_from(Handover).where(
            Handover.sample_id == sample_id, Handover.status == "PENDING")
    )
    if pending:
        raise ConflictError("已存在待确认的交接，确认或失效前不能重复发起")
    handover = Handover(sample_id=sample_id, from_person=from_person,
                        to_person=to_person, destination=destination,
                        note=(note or "").strip(), status="PENDING")
    db.add(handover)
    db.flush()
    _audit(db, sample_id, "HANDOVER_CREATED", from_person, {
        "handover_id": handover.id, "from": from_person, "to": to_person,
        "destination": destination, "note": note})
    db.commit()
    return handover


def confirm_handover(db: Session, handover_id: int, actor: str | None = None) -> Handover:
    handover = db.get(Handover, handover_id)
    if handover is None:
        raise DomainError("交接记录不存在", 404)
    # 先锁样品行，与温度记录/隔离串行化
    sample = _lock_sample(db, handover.sample_id)
    if sample.status == "QUARANTINED":
        raise ConflictError("样品已隔离，待确认交接已失效，无法确认")
    # 条件更新：只有仍处于 PENDING 的交接才能被确认，并发下仅一个请求生效
    res = db.execute(
        update(Handover)
        .where(Handover.id == handover_id, Handover.status == "PENDING")
        .values(status="CONFIRMED", decided_at=_now())
    )
    if res.rowcount == 0:
        db.rollback()
        raise ConflictError("该交接已被确认或已失效，本次操作未生效，请刷新查看最新状态")
    sample.current_location = handover.destination
    sample.version += 1
    _audit(db, sample.id, "HANDOVER_CONFIRMED", actor or handover.to_person, {
        "handover_id": handover.id, "from": handover.from_person,
        "to": handover.to_person, "destination": handover.destination})
    db.commit()
    return handover


# ---------------------------------------------------------------- 温度

def record_temperature(db: Session, sample_id: int, value: float,
                       recorded_by: str) -> tuple[TemperatureReading, bool]:
    sample = _lock_sample(db, sample_id)
    recorded_by = (recorded_by or "").strip() or "unknown"
    out_of_range = value < sample.temp_min or value > sample.temp_max
    reading = TemperatureReading(sample_id=sample_id, value=value,
                                 recorded_by=recorded_by, is_out_of_range=out_of_range)
    db.add(reading)
    db.flush()
    _audit(db, sample_id, "READING_RECORDED", recorded_by, {
        "reading_id": reading.id, "value": value,
        "allowed": [sample.temp_min, sample.temp_max], "out_of_range": out_of_range})
    if out_of_range:
        # 越界：待确认交接立即失效
        pending = db.scalars(
            select(Handover).where(Handover.sample_id == sample_id,
                                   Handover.status == "PENDING")
        ).all()
        for h in pending:
            h.status = "INVALIDATED"
            h.decided_at = _now()
            _audit(db, sample_id, "HANDOVER_INVALIDATED", "system", {
                "handover_id": h.id, "reason": f"温度越界读数 #{reading.id}"})
        # 越界：样品立即隔离
        if sample.status != "QUARANTINED":
            sample.status = "QUARANTINED"
            sample.version += 1
            db.add(QuarantineRecord(
                sample_id=sample_id,
                reason=f"温度越界：{value}°C 超出允许温区 "
                       f"[{sample.temp_min}, {sample.temp_max}]°C（读数 #{reading.id}）"))
            _audit(db, sample_id, "QUARANTINED", "system", {
                "reading_id": reading.id, "value": value,
                "invalidated_handovers": [h.id for h in pending]})
    db.commit()
    return reading, out_of_range


def correct_reading(db: Session, reading_id: int, new_value: float,
                    reason: str, operator: str) -> tuple[ReadingCorrection, int]:
    reading = db.get(TemperatureReading, reading_id)
    if reading is None:
        raise DomainError("温度读数不存在", 404)
    reason = (reason or "").strip()
    operator = (operator or "").strip()
    if not reason:
        raise DomainError("更正读数必须填写理由")
    if not operator:
        raise DomainError("更正读数必须填写操作者")
    # 原值 = 上一次更正后的值（或原始值），形成完整更正链
    last = db.scalars(
        select(ReadingCorrection).where(ReadingCorrection.reading_id == reading_id)
        .order_by(ReadingCorrection.id.desc())
    ).first()
    old_value = last.new_value if last else reading.value
    correction = ReadingCorrection(reading_id=reading_id, old_value=old_value,
                                   new_value=new_value, reason=reason, operator=operator)
    db.add(correction)
    db.flush()
    # 注意：更正不会自动解除隔离，也不改变样品状态
    _audit(db, reading.sample_id, "READING_CORRECTED", operator, {
        "reading_id": reading_id, "old_value": old_value, "new_value": new_value,
        "reason": reason, "note": "更正不自动解除隔离"})
    db.commit()
    return correction, reading.sample_id


# ---------------------------------------------------------------- 隔离

def release_quarantine(db: Session, sample_id: int, reason: str,
                       released_by: str, expected_version: int) -> None:
    reason = (reason or "").strip()
    released_by = (released_by or "").strip()
    if not reason:
        raise DomainError("解除隔离必须填写注明理由的复核结论")
    if not released_by:
        raise DomainError("请填写复核人")
    # 条件更新：仅当样品仍处于隔离状态且版本号与页面读取时一致才生效
    res = db.execute(
        update(Sample)
        .where(Sample.id == sample_id,
               Sample.status == "QUARANTINED",
               Sample.version == expected_version)
        .values(status="ACTIVE", version=Sample.version + 1)
    )
    if res.rowcount == 0:
        db.rollback()
        raise ConflictError("样品状态已变更（可能已被他人处理），本次解除未生效，请刷新后重试")
    now = _now()
    active = db.scalars(
        select(QuarantineRecord).where(QuarantineRecord.sample_id == sample_id,
                                       QuarantineRecord.status == "ACTIVE")
    ).all()
    for q in active:
        q.status = "RELEASED"
        q.released_at = now
        q.release_reason = reason
        q.released_by = released_by
    _audit(db, sample_id, "QUARANTINE_RELEASED", released_by, {
        "reason": reason, "quarantine_ids": [q.id for q in active]})
    db.commit()

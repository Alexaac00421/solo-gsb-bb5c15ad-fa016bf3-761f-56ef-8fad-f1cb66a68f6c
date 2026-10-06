"""业务逻辑：所有状态变更均在事务内完成。

并发控制策略：
- 样品状态变更前先 SELECT ... FOR UPDATE 锁定样品行，串行化互斥操作；
- 交接确认、隔离解除使用条件 UPDATE（WHERE status=... / version=...），
  只有基于最新状态的操作才能生效，rowcount=0 即冲突，返回明确提示。
"""
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import (AuditLog, EquipmentFault, FaultImpact, Handover,
                     QuarantineRecord, ReadingCorrection, Sample,
                     TemperatureReading)


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


# ---------------------------------------------------------------- 设备故障

def _aware(dt: datetime) -> datetime:
    """统一为带时区时间（SQLite 读回为 naive，按 UTC 处理）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_dt(raw: str, label: str) -> datetime:
    """解析表单时间（datetime-local）；缺失或格式无效时明确报错。"""
    raw = (raw or "").strip()
    if not raw:
        raise DomainError(f"{label}为必填项")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise DomainError(f"{label}格式无效：{raw}")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 未带时区时按服务器本地时区
    return dt


def _location_segments(db: Session, sample: Sample):
    """由样品登记与已确认交接重建位置时间段 [(location, start, end)]。

    所有时间段左闭右开；最后一段 end=None 表示至今。
    """
    reg = db.scalars(
        select(AuditLog).where(AuditLog.sample_id == sample.id,
                               AuditLog.event_type == "SAMPLE_REGISTERED")
        .order_by(AuditLog.id)
    ).first()
    location = (reg.detail or {}).get("location") if reg else None
    if not location:
        location = sample.current_location
    confirmed = db.scalars(
        select(Handover).where(Handover.sample_id == sample.id,
                               Handover.status == "CONFIRMED")
        .order_by(Handover.decided_at, Handover.id)
    ).all()
    segments = []
    start = _aware(sample.created_at)
    for h in confirmed:
        segments.append((location, start, _aware(h.decided_at)))
        location = h.destination
        start = _aware(h.decided_at)
    segments.append((location, start, None))
    return segments


def register_fault(db: Session, code: str, location: str,
                   start_at: datetime, end_at: datetime,
                   reason: str, operator: str) -> tuple[EquipmentFault, bool]:
    """登记冷库设备故障，返回 (故障记录, 是否新建)。

    - 按样品登记与已确认交接形成的位置时间段（左闭右开）找出与故障时段
      有交集的样品，一次性固定影响清单及各自重叠时段；
    - 当前未隔离的样品立即隔离、待确认交接失效；已隔离样品仅关联该故障，
      不重复生成有效隔离；
    - 锁定全部样品行（按 id 升序），与交接确认/录温串行化，单事务原子完成；
    - 相同编号及登记字段重复提交返回原结果；编号相同字段不同报 409 冲突；
      没有受影响样品也保存空清单。
    """
    code = (code or "").strip()
    location = (location or "").strip()
    reason = (reason or "").strip()
    operator = (operator or "").strip()
    if not code:
        raise DomainError("故障编号不能为空")
    if not location:
        raise DomainError("库位不能为空")
    if not reason:
        raise DomainError("故障原因不能为空")
    if not operator:
        raise DomainError("操作者不能为空")
    if start_at is None or end_at is None:
        raise DomainError("故障起止时间均为必填项")
    start_at, end_at = _aware(start_at), _aware(end_at)
    if not start_at < end_at:
        raise DomainError("故障开始时间必须早于结束时间（时间段左闭右开）")
    now = _now()
    if end_at > now:
        raise DomainError("故障结束时间不得晚于提交时刻")

    existing = db.scalar(select(EquipmentFault).where(EquipmentFault.code == code))
    if existing is not None:
        same = (existing.location == location
                and _aware(existing.start_at) == start_at
                and _aware(existing.end_at) == end_at
                and existing.reason == reason
                and existing.operator == operator)
        if same:
            return existing, False  # 重复提交：返回原结果，不重算、不重复隔离
        raise ConflictError(f"故障编号 {code} 已存在且登记字段不同，提交冲突")

    # 锁定全部样品行（按 id 升序），与交接确认/录温互斥，基于最新状态计算
    samples = db.scalars(select(Sample).order_by(Sample.id).with_for_update()).all()
    fault = EquipmentFault(code=code, location=location, start_at=start_at,
                           end_at=end_at, reason=reason, operator=operator)
    db.add(fault)
    db.flush()
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    for sample in samples:
        overlaps = []  # (location, overlap_start, overlap_end)
        for seg_loc, seg_start, seg_end in _location_segments(db, sample):
            if seg_loc != location:
                continue
            seg_end = seg_end or far_future
            if seg_start < end_at and start_at < seg_end:  # 左闭右开区间相交
                overlaps.append((seg_loc, max(seg_start, start_at),
                                 min(seg_end, end_at)))
        if not overlaps:
            continue
        already = sample.status == "QUARANTINED"
        quarantine_id = None
        overlap_json = [[o[1].isoformat(), o[2].isoformat()] for o in overlaps]
        if not already:
            # 未隔离：待确认交接失效 + 立即隔离
            pendings = db.scalars(
                select(Handover).where(Handover.sample_id == sample.id,
                                       Handover.status == "PENDING")
            ).all()
            for h in pendings:
                h.status = "INVALIDATED"
                h.decided_at = now
                _audit(db, sample.id, "HANDOVER_INVALIDATED", "system", {
                    "handover_id": h.id, "reason": f"设备故障 {code}"})
            quarantine = QuarantineRecord(
                sample_id=sample.id,
                reason=f"设备故障 {code}（库位 {location}）：{reason}")
            db.add(quarantine)
            db.flush()
            quarantine_id = quarantine.id
            sample.status = "QUARANTINED"
            sample.version += 1
            _audit(db, sample.id, "QUARANTINED", operator, {
                "fault_id": fault.id, "fault_code": code, "overlaps": overlap_json,
                "invalidated_handovers": [h.id for h in pendings]})
        else:
            # 已隔离：仅关联现有有效隔离，不重复生成
            active_q = db.scalars(
                select(QuarantineRecord).where(
                    QuarantineRecord.sample_id == sample.id,
                    QuarantineRecord.status == "ACTIVE")
                .order_by(QuarantineRecord.id)
            ).first()
            quarantine_id = active_q.id if active_q else None
            _audit(db, sample.id, "FAULT_LINKED", operator, {
                "fault_id": fault.id, "fault_code": code, "overlaps": overlap_json,
                "note": "样品已隔离，仅关联故障，不重复生成有效隔离"})
        for seg_loc, ov_start, ov_end in overlaps:
            db.add(FaultImpact(fault_id=fault.id, sample_id=sample.id,
                               location=seg_loc, overlap_start=ov_start,
                               overlap_end=ov_end, already_quarantined=already,
                               quarantine_id=quarantine_id))
    db.commit()
    return fault, True

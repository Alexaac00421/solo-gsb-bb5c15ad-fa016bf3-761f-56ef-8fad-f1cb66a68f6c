from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Integer, String, func)

from .database import Base


class Sample(Base):
    __tablename__ = "samples"

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    temp_min = Column(Float, nullable=False)
    temp_max = Column(Float, nullable=False)
    current_location = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")  # ACTIVE / QUARANTINED
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Handover(Base):
    """交接单：接收人确认前样品位置不变；越界隔离会使 PENDING 交接失效。"""

    __tablename__ = "handovers"

    id = Column(Integer, primary_key=True)
    sample_id = Column(Integer, ForeignKey("samples.id"), nullable=False, index=True)
    from_person = Column(String(64), nullable=False)
    to_person = Column(String(64), nullable=False)
    destination = Column(String(128), nullable=False)
    note = Column(String(256), default="")
    status = Column(String(16), nullable=False, default="PENDING")  # PENDING / CONFIRMED / INVALIDATED
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    decided_at = Column(DateTime(timezone=True))


class TemperatureReading(Base):
    """温度读数：原始值永不修改，更正通过 ReadingCorrection 追加。"""

    __tablename__ = "temperature_readings"

    id = Column(Integer, primary_key=True)
    sample_id = Column(Integer, ForeignKey("samples.id"), nullable=False, index=True)
    value = Column(Float, nullable=False)
    recorded_by = Column(String(64), nullable=False)
    is_out_of_range = Column(Boolean, nullable=False, default=False)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReadingCorrection(Base):
    """读数更正：保留原值、新值、理由与操作者；不会自动解除隔离。"""

    __tablename__ = "reading_corrections"

    id = Column(Integer, primary_key=True)
    reading_id = Column(Integer, ForeignKey("temperature_readings.id"), nullable=False, index=True)
    old_value = Column(Float, nullable=False)
    new_value = Column(Float, nullable=False)
    reason = Column(String(256), nullable=False)
    operator = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class QuarantineRecord(Base):
    """隔离记录：越界自动创建；仅能通过注明理由的复核结论解除。"""

    __tablename__ = "quarantine_records"

    id = Column(Integer, primary_key=True)
    sample_id = Column(Integer, ForeignKey("samples.id"), nullable=False, index=True)
    reason = Column(String(256), nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")  # ACTIVE / RELEASED
    quarantined_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_at = Column(DateTime(timezone=True))
    release_reason = Column(String(256))
    released_by = Column(String(64))


class AuditLog(Base):
    """审计日志：所有状态变更的完整流水，可导出。"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    sample_id = Column(Integer, ForeignKey("samples.id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    actor = Column(String(64))
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

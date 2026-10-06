"""设备故障登记的业务规则验证（SQLite 内存库）。

运行方式：
    python tests/test_faults.py        # 直接运行全部场景
    pytest tests/test_faults.py        # 或用 pytest
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import services
from app.database import Base
from app.models import EquipmentFault, FaultImpact, QuarantineRecord, Sample
from app.services import ConflictError, DomainError, _aware

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def aw(dt):
    """SQLite 读回为 naive，统一为带时区后比较（生产 PostgreSQL 本即带时区）。"""
    return _aware(dt)


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def impacts_of(db, fault_id):
    return db.scalars(select(FaultImpact).where(FaultImpact.fault_id == fault_id)
                      .order_by(FaultImpact.id)).all()


def quarantines_of(db, sample_id):
    return db.scalars(select(QuarantineRecord)
                      .where(QuarantineRecord.sample_id == sample_id)).all()


def new_sample_at(db, code, location, created_at):
    s = services.register_sample(db, code, 2.0, 8.0, location, "op")
    s.created_at = created_at
    db.commit()
    return s


def confirm_at(db, handover, decided_at):
    services.confirm_handover(db, handover.id)
    handover.decided_at = decided_at
    db.commit()


def test_fault_quarantines_and_invalidates_pending():
    db = make_db()
    s = new_sample_at(db, "S1", "冷库A-01", T0)
    h = services.create_handover(db, s.id, "张三", "李四", "实验室C-1", "")
    fault, created = services.register_fault(
        db, "F1", "冷库A-01", T0 + 1 * H, T0 + 2 * H, "压缩机故障", "管理员")
    assert created
    assert s.status == "QUARANTINED" and s.version == 2
    assert h.status == "INVALIDATED" and h.decided_at is not None
    qs = quarantines_of(db, s.id)
    assert len(qs) == 1 and qs[0].status == "ACTIVE"
    assert "F1" in qs[0].reason and "冷库A-01" in qs[0].reason
    impacts = impacts_of(db, fault.id)
    assert len(impacts) == 1
    imp = impacts[0]
    assert imp.sample_id == s.id and imp.location == "冷库A-01"
    assert aw(imp.overlap_start) == T0 + 1 * H and aw(imp.overlap_end) == T0 + 2 * H
    assert imp.already_quarantined is False
    assert imp.quarantine_id == qs[0].id
    print("ok - 故障隔离受影响样品并失效待确认交接")


def test_left_closed_right_open_boundaries():
    db = make_db()
    s = new_sample_at(db, "S1", "冷库A-01", T0)
    h = services.create_handover(db, s.id, "张三", "李四", "冷库B-01", "")
    confirm_at(db, h, T0 + 5 * H)  # A-01: [T0, T0+5h)  B-01: [T0+5h, inf)

    # 故障开始于离开时刻（段右开）：不在 A-01
    f1, _ = services.register_fault(db, "F-A-OPEN", "冷库A-01",
                                    T0 + 5 * H, T0 + 6 * H, "r", "op")
    assert impacts_of(db, f1.id) == []
    # 故障结束于到达时刻（段左闭）：不在 B-01
    f2, _ = services.register_fault(db, "F-B-OPEN", "冷库B-01",
                                    T0 + 2 * H, T0 + 5 * H, "r", "op")
    assert impacts_of(db, f2.id) == []
    # 故障结束于离开时刻：仍在 A-01（重叠 [T0+4h, T0+5h)）
    f3, _ = services.register_fault(db, "F-A-HIT", "冷库A-01",
                                    T0 + 4 * H, T0 + 5 * H, "r", "op")
    imp = impacts_of(db, f3.id)
    assert len(imp) == 1 and aw(imp[0].overlap_start) == T0 + 4 * H
    assert aw(imp[0].overlap_end) == T0 + 5 * H
    # 故障开始于到达时刻：已在 B-01（左闭）
    f4, _ = services.register_fault(db, "F-B-HIT", "冷库B-01",
                                    T0 + 5 * H, T0 + 6 * H, "r", "op")
    imp = impacts_of(db, f4.id)
    assert len(imp) == 1 and aw(imp[0].overlap_start) == T0 + 5 * H
    print("ok - 所有时间段左闭右开，边界判定正确")


def test_multi_segment_overlap_single_quarantine():
    db = make_db()
    s = new_sample_at(db, "S1", "冷库A-01", T0)
    h1 = services.create_handover(db, s.id, "a", "b", "冷库B-01", "")
    confirm_at(db, h1, T0 + 3 * H)
    h2 = services.create_handover(db, s.id, "b", "c", "冷库A-01", "")
    confirm_at(db, h2, T0 + 6 * H)  # A: [T0,3h) [6h,inf)   B: [3h,6h)
    fault, _ = services.register_fault(db, "F-MULTI", "冷库A-01",
                                       T0 + 1 * H, T0 + 7 * H, "r", "op")
    impacts = impacts_of(db, fault.id)
    assert len(impacts) == 2  # 两段重叠各自固定
    assert (aw(impacts[0].overlap_start), aw(impacts[0].overlap_end)) == (T0 + 1 * H, T0 + 3 * H)
    assert (aw(impacts[1].overlap_start), aw(impacts[1].overlap_end)) == (T0 + 6 * H, T0 + 7 * H)
    assert len(quarantines_of(db, s.id)) == 1  # 只隔离一次
    print("ok - 多段重叠逐段固定，样品仅隔离一次")


def test_idempotent_resubmit_and_conflict():
    db = make_db()
    s = new_sample_at(db, "S1", "冷库A-01", T0)
    args = ("F-IDEM", "冷库A-01", T0 + 1 * H, T0 + 2 * H, "原因", "操作者")
    fault, created = services.register_fault(db, *args)
    assert created
    again, created = services.register_fault(db, *args)
    assert not created and again.id == fault.id
    assert len(impacts_of(db, fault.id)) == 1
    assert len(quarantines_of(db, s.id)) == 1  # 未重复隔离
    # 编号相同、字段不同 → 409 冲突，且不留任何变更
    try:
        services.register_fault(db, "F-IDEM", "冷库A-01", T0 + 1 * H,
                                T0 + 3 * H, "别的原因", "操作者")
        raise AssertionError("应抛出 ConflictError")
    except ConflictError:
        pass
    assert len(impacts_of(db, fault.id)) == 1
    assert db.scalar(select(func.count()).select_from(EquipmentFault)) == 1
    print("ok - 重复提交返回原结果，字段不同提示冲突且无部分变更")


def test_empty_impact_list_saved():
    db = make_db()
    new_sample_at(db, "S1", "冷库A-01", T0)
    fault, created = services.register_fault(db, "F-EMPTY", "冷库Z-99",
                                             T0 + 1 * H, T0 + 2 * H, "r", "op")
    assert created and impacts_of(db, fault.id) == []
    assert db.get(EquipmentFault, fault.id) is not None
    print("ok - 无受影响样品也保存空清单")


def test_already_quarantined_only_linked():
    db = make_db()
    s = new_sample_at(db, "S1", "冷库A-01", T0)
    services.record_temperature(db, s.id, 99.0, "张三")  # 越界 → 已隔离
    assert s.status == "QUARANTINED"
    existing_q = quarantines_of(db, s.id)[0]
    fault, _ = services.register_fault(db, "F-LINK", "冷库A-01",
                                       T0 + 1 * H, T0 + 2 * H, "r", "op")
    impacts = impacts_of(db, fault.id)
    assert len(impacts) == 1
    assert impacts[0].already_quarantined is True
    assert impacts[0].quarantine_id == existing_q.id  # 关联现有隔离
    assert len(quarantines_of(db, s.id)) == 1  # 不重复生成有效隔离
    print("ok - 已隔离样品仅关联故障，不重复生成有效隔离")


def test_validation_errors_leave_nothing():
    db = make_db()
    s = new_sample_at(db, "S1", "冷库A-01", T0)
    now = datetime.now(timezone.utc)
    bad_calls = [
        ("", "冷库A-01", T0, T0 + H, "r", "op"),                # 缺编号
        ("F", "", T0, T0 + H, "r", "op"),                       # 缺库位
        ("F", "冷库A-01", T0, T0 + H, "", "op"),                # 缺原因
        ("F", "冷库A-01", T0, T0 + H, "r", ""),                 # 缺操作者
        ("F", "冷库A-01", T0 + H, T0 + H, "r", "op"),           # 起止相等
        ("F", "冷库A-01", T0 + 2 * H, T0 + H, "r", "op"),       # 起晚于止
        ("F", "冷库A-01", now - H, now + H, "r", "op"),         # 结束晚于提交时刻
    ]
    for args in bad_calls:
        try:
            services.register_fault(db, *args)
            raise AssertionError(f"应报错：{args}")
        except DomainError:
            db.rollback()
    assert db.scalar(select(func.count()).select_from(EquipmentFault)) == 0
    assert db.scalar(select(func.count()).select_from(FaultImpact)) == 0
    assert db.get(Sample, s.id).status == "ACTIVE"  # 无部分状态变更
    # parse_dt：缺失与格式无效
    for raw in ("", "  ", "not-a-time"):
        try:
            services.parse_dt(raw, "故障开始时间")
            raise AssertionError("应报错")
        except DomainError:
            pass
    assert services.parse_dt("2026-10-01T08:00", "t").tzinfo is not None
    print("ok - 必填缺失/起止无效明确报错，失败不留部分清单或状态变更")


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in ALL_TESTS:
        t()
    print(f"\n全部 {len(ALL_TESTS)} 个场景通过")

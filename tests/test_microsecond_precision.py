"""微秒采样时刻下的时长判定回归测试（纯函数）。

背景：采样时刻可带微秒，半开区段实际时长因此可能出现亚毫秒差异
（如 1.0004s）。旧实现先把最长连续超限时长/未知时长舍入到毫秒再与阈值
比较，会把 1.0004s vs 阈值 1.0002s 误判为未超限，并把不足 0.0005s 的
正时长未知区段舍成 0 而漏判待定。本轮起：

- 单窗审定与跨窗复核一律按原始半开区段实际时长判定：严格超过阈值才
  不合格；任意正时长未知区段都判待定；已有超限不合格时不合格优先；
- 时长输出以微秒（6 位小数）呈现，影响结论的微秒差异可见。

运行：PYTHONPATH=. python -m pytest tests/test_microsecond_precision.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.cross_window import build_cross_window_review
from app.evaluation import evaluate
from app.timeutils import to_iso

T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


def at(**kw) -> datetime:
    return T0 + timedelta(**kw)


# ---------------------------------------------------------------- 单窗审定

def test_single_window_submillisecond_exceed_is_fail():
    # 持续 1.0004s、阈值 1.0002s：旧逻辑毫秒舍入后 1.0 <= 1.0002 误判 pass
    t1 = at(microseconds=1_000_400)
    t2 = at(seconds=2)
    r = evaluate(T0, t2, 18.0, 22.0, 1.0002, 1000.0,
                 [(T0, 25.0), (t1, 20.0), (t2, 20.0)])
    assert r["verdict"] == "fail"
    assert r["max_continuous_exceeded_seconds"] == 1.0004
    assert r["exceeded_segments"][0]["duration_seconds"] == 1.0004
    assert r["exceeded_segments"][0]["end"].endswith(".000400Z")


def test_single_window_exceed_exactly_at_submillisecond_limit_is_not_fail():
    # 恰好等于限值（1.0004 == 1.0004）不算严格超过；满覆盖 -> 合格
    t1 = at(microseconds=1_000_400)
    t2 = at(seconds=2)
    r = evaluate(T0, t2, 18.0, 22.0, 1.0004, 1000.0,
                 [(T0, 25.0), (t1, 20.0), (t2, 20.0)])
    assert r["verdict"] == "pass"
    assert r["has_unknown"] is False


def test_single_window_submillisecond_unknown_is_pending():
    # 首读在起点后 0.0004s：旧逻辑 round(0.0004, 3) == 0.0 漏判未知
    t_first = at(microseconds=400)
    t2 = at(seconds=2)
    r = evaluate(T0, t2, 18.0, 22.0, 1000.0, 1000.0,
                 [(t_first, 20.0), (t2, 20.0)])
    assert r["verdict"] == "pending"
    assert r["has_unknown"] is True
    assert r["unknown_segments"][0]["duration_seconds"] == 0.0004


def test_single_window_fail_takes_priority_over_submillisecond_unknown():
    # 已有超限不合格时，即使存在微秒未知区段仍以不合格优先
    t_first = at(microseconds=400)
    t2 = at(seconds=2)
    r = evaluate(T0, t2, 18.0, 22.0, 0.0001, 1000.0,
                 [(t_first, 25.0), (t2, 20.0)])
    assert r["verdict"] == "fail"
    assert r["has_unknown"] is True


# ---------------------------------------------------------------- 跨窗复核

def _rd(rid, t, temp):
    return {"reading_id": str(rid), "revision": 1, "measured_at": to_iso(t),
            "temperature": temp, "status": "active", "created_at": to_iso(T0)}


def make_cert(cid, ws, we, readings, max_exceed=1000.0):
    parsed = [
        (datetime.fromisoformat(r["measured_at"].replace("Z", "+00:00")),
         r["temperature"])
        for r in readings
    ]
    ev = evaluate(ws, we, 18.0, 22.0, max_exceed, 1000.0, parsed)
    snap = {
        "window": {
            "id": cid, "name": f"case-{cid}",
            "start_at": to_iso(ws), "end_at": to_iso(we),
            "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
            "max_continuous_exceed_seconds": max_exceed,
            "max_sample_interval_seconds": 1000.0, "rule_revision": 1,
        },
        "evaluation": ev, "readings": readings,
    }
    return {"id": cid, "window_id": cid, "version": 1,
            "verdict": ev["verdict"], "verdict_label_zh": "x",
            "issued_at": to_iso(T0), "note": None,
            "revoked": False, "revocation": None, "snapshot": snap}


def _exceed_chain_certs():
    # W1[0,2)：[0,1) 正常、[1,2) 超限 1s；
    # W2[2,4)：[2,3.0004) 超限 1.0004s、其后正常 -> 跨边界连续 2.0004s
    w1s, w1e = at(seconds=0), at(seconds=2)
    c1 = make_cert(1, w1s, w1e, [
        _rd(1, at(seconds=0), 20.0), _rd(2, at(seconds=1), 25.0),
        _rd(3, at(seconds=2), 25.0)])
    c2 = make_cert(2, w1e, at(seconds=4), [
        _rd(4, at(seconds=2), 25.0),
        _rd(5, at(seconds=3, microseconds=400), 20.0),
        _rd(6, at(seconds=4), 20.0)])
    return c1, c2


def test_cross_window_submillisecond_exceed_is_fail():
    c1, c2 = _exceed_chain_certs()
    res = build_cross_window_review([c1, c2], 2.0002)
    assert res["verdict"] == "fail"
    assert res["max_continuous_exceeded_seconds"] == 2.0004
    # 微秒差异在逐段输出中可见，跨窗边界不跨证合并
    boundary_segs = [s for s in res["segments"] if s["status"] == "exceeded"]
    assert [s["duration_seconds"] for s in boundary_segs] == [1.0, 1.0004]


def test_cross_window_exceed_exactly_at_submillisecond_limit_is_pass():
    c1, c2 = _exceed_chain_certs()
    res = build_cross_window_review([c1, c2], 2.0004)
    assert res["verdict"] == "pass"


def test_cross_window_submillisecond_unknown_is_pending():
    # W2 首读在其窗口起点后 0.0004s：边界未知被旧毫秒舍入吞掉
    c3 = make_cert(3, at(seconds=0), at(seconds=2), [
        _rd(1, at(seconds=0), 20.0), _rd(2, at(seconds=1), 20.0),
        _rd(3, at(seconds=2), 20.0)])
    c4 = make_cert(4, at(seconds=2), at(seconds=4), [
        _rd(4, at(seconds=2, microseconds=400), 20.0),
        _rd(5, at(seconds=3), 20.0), _rd(6, at(seconds=4), 20.0)])
    res = build_cross_window_review([c3, c4], 1000.0)
    assert res["verdict"] == "pending"
    assert res["has_unknown"] is True
    assert res["unknown_seconds"] == 0.0004


def test_cross_window_fail_priority_over_submillisecond_unknown():
    c1, _ = _exceed_chain_certs()
    c4 = make_cert(4, at(seconds=2), at(seconds=4), [
        _rd(4, at(seconds=2, microseconds=400), 20.0),
        _rd(5, at(seconds=3), 20.0), _rd(6, at(seconds=4), 20.0)])
    # W1 末段超限 1s（未知在其对侧边界切断累计），阈值 0.5s -> 不合格优先
    res = build_cross_window_review([c1, c4], 0.5)
    assert res["verdict"] == "fail"
    assert res["has_unknown"] is True

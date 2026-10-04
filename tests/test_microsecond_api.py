"""HTTP API 集成测试：微秒采样时刻下的单窗审定与跨窗复核（端到端）。

覆盖本轮修复的两条判定链路（经真实建窗/提交/签发/复核 HTTP 流程）：
- 单窗：持续 1.0004s、阈值 1.0002s 必须判 fail；恰好等阈值判 pass；
  0.0004s 正时长边界未知必须判 pending；fail 优先于 pending；
- 跨窗：跨边界连续 2.0004s、全程阈值 2.0002s 判 fail；等阈值 pass；
  0.0004s 未知判 pending；fail 优先；
- 时长字段以微秒呈现影响结论的差异，字段结构不变；
- 非法输入仍按既有语义拒绝（422）。

运行：PYTHONPATH=. python -m pytest tests/test_microsecond_api.py -q
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_window(client, wid_name, start, end, max_exceed, *,
                tmin=18.0, tmax=22.0, max_gap=1000):
    r = client.post("/api/v1/windows", json={
        "name": wid_name, "start_at": start, "end_at": end,
        "allowed_temp_min": tmin, "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def rd(client, wid, rid, ts, temp, revision=1):
    r = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": rid, "revision": revision,
        "measured_at": ts, "temperature": temp})
    assert r.status_code == 201, r.text
    return r.json()


def issue(client, wid):
    r = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 单窗审定

def test_single_window_microsecond_exceed_fails_over_http(client):
    # 窗口 [00:00:00, 00:00:02]，阈值 1.0002s；首段超限恰好 1.0004s
    wid = make_window(client, "微秒单窗-超限",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0002)
    rd(client, wid, "a", "2026-10-01T00:00:00.000000Z", 25.0)
    rd(client, wid, "b", "2026-10-01T00:00:01.000400Z", 20.0)
    rd(client, wid, "c", "2026-10-01T00:00:02.000000Z", 20.0)

    r = client.get(f"/api/v1/windows/{wid}/evaluation")
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 1.0004
    assert ev["exceeded_segments"][0]["duration_seconds"] == 1.0004
    assert ev["exceeded_segments"][0]["end"] == "2026-10-01T00:00:01.000400Z"


def test_single_window_microsecond_exceed_equal_limit_passes(client):
    wid = make_window(client, "微秒单窗-等值",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0004)
    rd(client, wid, "a", "2026-10-01T00:00:00Z", 25.0)
    rd(client, wid, "b", "2026-10-01T00:00:01.000400Z", 20.0)
    rd(client, wid, "c", "2026-10-01T00:00:02Z", 20.0)
    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "pass"
    assert ev["max_continuous_exceeded_seconds"] == 1.0004


def test_single_window_submillisecond_unknown_pending(client):
    wid = make_window(client, "微秒单窗-未知",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1000)
    # 首读晚于起点 0.0004s（旧毫秒舍入会把该未知段呈现为 0）
    rd(client, wid, "a", "2026-10-01T00:00:00.000400Z", 20.0)
    rd(client, wid, "b", "2026-10-01T00:00:02Z", 20.0)
    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "pending"
    assert ev["has_unknown"] is True
    assert ev["unknown_segments"][0]["duration_seconds"] == 0.0004


def test_single_window_fail_priority_over_unknown(client):
    wid = make_window(client, "微秒单窗-优先级",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=0.0001)
    rd(client, wid, "a", "2026-10-01T00:00:00.000400Z", 25.0)
    rd(client, wid, "b", "2026-10-01T00:00:02Z", 20.0)
    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "fail"  # 超限不合格优先于待定


# ---------------------------------------------------------------- 跨窗复核

def _issue_chain(client, *, w2_first_read_offset_us=None, w2_first_temp=25.0):
    # W1 [0,2)：[0,1) 正常、[1,2) 超限 1s（满覆盖，单窗 pass，自身限值大）
    w1 = make_window(client, "微秒跨窗-W1",
                     "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                     max_exceed=1000)
    rd(client, w1, "a", "2026-10-01T00:00:00Z", 20.0)
    rd(client, w1, "b", "2026-10-01T00:00:01Z", 25.0)
    rd(client, w1, "c", "2026-10-01T00:00:02Z", w2_first_temp)
    c1 = issue(client, w1)

    # W2 [2,4)：默认 [2,3.0004) 超限 1.0004s
    w2 = make_window(client, "微秒跨窗-W2",
                     "2026-10-01T00:00:02Z", "2026-10-01T00:00:04Z",
                     max_exceed=1000)
    if w2_first_read_offset_us is None:
        first_ts = "2026-10-01T00:00:02.000000Z"
    else:
        first_ts = f"2026-10-01T00:00:02.{w2_first_read_offset_us:06d}Z"
    rd(client, w2, "d", first_ts, w2_first_temp)
    rd(client, w2, "e", "2026-10-01T00:00:03.000400Z", 20.0)
    rd(client, w2, "f", "2026-10-01T00:00:04Z", 20.0)
    c2 = issue(client, w2)
    return w1, w2, c1, c2


def test_cross_window_microsecond_exceed_fails_over_http(client):
    _, _, c1, c2 = _issue_chain(client)
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [c1["id"], c2["id"]],
                          "max_continuous_exceed_seconds": 2.0002})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["verdict"] == "fail"
    assert d["max_continuous_exceeded_seconds"] == 2.0004
    exceeded = [s for s in d["segments"] if s["status"] == "exceeded"]
    assert [s["duration_seconds"] for s in exceeded] == [1.0, 1.0004]


def test_cross_window_microsecond_exceed_equal_limit_passes(client):
    _, _, c1, c2 = _issue_chain(client)
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [c1["id"], c2["id"]],
                          "max_continuous_exceed_seconds": 2.0004})
    d = r.json()
    assert r.status_code == 201, r.text
    assert d["verdict"] == "pass"


def test_cross_window_submillisecond_unknown_pending(client):
    # W2 首读晚 0.0004s：边界未知；两窗温度均正常 -> 待定
    _, _, c1, c2 = _issue_chain(
        client, w2_first_read_offset_us=400, w2_first_temp=20.0)
    # W1 也需要全正常：把 W1 末读温度置 20（c 覆盖终止，其自身时长为 0）
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [c1["id"], c2["id"]],
                          "max_continuous_exceed_seconds": 1000})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["verdict"] == "pending"
    assert d["has_unknown"] is True
    assert d["unknown_seconds"] == 0.0004
    assert d["sources"][1]["unknown_seconds"] == 0.0004


def test_cross_window_fail_priority_over_submillisecond_unknown(client):
    # W1 末段超限 1s；W2 首读晚 0.0004s（未知在边界处切断累计）；
    # 阈值 0.5s -> W1 的 1s 已严格超限 -> 不合格优先
    _, _, c1, c2 = _issue_chain(
        client, w2_first_read_offset_us=400, w2_first_temp=20.0)
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [c1["id"], c2["id"]],
                          "max_continuous_exceed_seconds": 0.5})
    d = r.json()
    assert r.status_code == 201, r.text
    assert d["verdict"] == "fail"
    assert d["has_unknown"] is True


# ------------------------------------------------- 已签发快照不追改/流程不回归

def test_issued_certificate_snapshot_not_retroactively_changed(client):
    # 用窗口构造一个“微秒未知”场景，先签发证书（签发时审定即按修复后口径），
    # 随后不做任何变更再次签发：两张证书结论/快照完全一致、版本递增；
    # 已签发证书的结论与不可变快照在本轮判定口径切换下不被追改。
    wid = make_window(client, "微秒快照-不追改",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1000)
    rd(client, wid, "a", "2026-10-01T00:00:00.000400Z", 20.0)
    rd(client, wid, "b", "2026-10-01T00:00:02Z", 20.0)
    c1 = issue(client, wid)
    assert c1["version"] == 1 and c1["verdict"] == "pending"
    snap1_eval = c1["snapshot"]["evaluation"]
    assert snap1_eval["unknown_segments"][0]["duration_seconds"] == 0.0004

    # 再次签发形成新版本：快照内容（除无字段差异外）保持同一已提交状态
    c2 = issue(client, wid)
    assert c2["version"] == 2 and c2["verdict"] == "pending"
    assert c2["snapshot"]["evaluation"] == snap1_eval

    # 旧版本仍可读且原样保留；最新版本未撤销 -> 当前可用为 v2
    got1 = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert got1["verdict"] == "pending"
    assert got1["snapshot"]["evaluation"] == snap1_eval
    cur = client.get(f"/api/v1/windows/{wid}/certificates/current").json()
    assert cur["id"] == c2["id"] and cur["version"] == 2


def test_subsequent_issue_seal_and_verification_flow_works(client):
    # 微秒未知窗口：后续签发 -> 封存（快照与当前状态一致，可封存）->
    # 出具核验包并核验，整条流程按现有语义工作。
    wid = make_window(client, "微秒流程-签发封存核验",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1000)
    rd(client, wid, "a", "2026-10-01T00:00:00.000400Z", 20.0)
    rd(client, wid, "b", "2026-10-01T00:00:02Z", 20.0)
    cert = issue(client, wid)
    assert cert["verdict"] == "pending"

    # 封存：不可变快照与当前状态一致（同一微秒口径），封存成功
    r = client.post(f"/api/v1/windows/{wid}/seals",
                    json={"certificate_id": cert["id"], "reason": "微秒口径封存"})
    assert r.status_code == 201, r.text
    seal = r.json()["seal"]
    assert seal["evidence"]["verdict"] == "pending"
    assert (seal["evidence"]["snapshot"]["evaluation"]
            ["unknown_segments"][0]["duration_seconds"]) == 0.0004

    # 封存期间窗口只读：读数提交 409（既有冻结语义不变）
    blocked = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": "x", "revision": 1,
        "measured_at": "2026-10-01T00:00:01Z", "temperature": 20.0})
    assert blocked.status_code == 409

    # 核验包出具 + 核验：签名/快照可信且证书仍为当前最新未撤销版本
    pkg = client.post(
        f"/api/v1/certificates/{cert['id']}/verification-package")
    assert pkg.status_code == 201, pkg.text
    bundle = pkg.json()
    vr = client.post("/api/v1/verification-packages/verify", json=bundle)
    assert vr.status_code == 200, vr.text
    vd = vr.json()
    assert vd["signature_check"]["trusted"] is True
    assert vd["snapshot_check"]["trusted"] is True
    assert vd["certificate"]["state"] == "current"
    assert vd["package_trusted"] is True


# ---------------------------------------------------------------- 非法输入语义不变

def test_cross_window_invalid_inputs_still_rejected(client):
    _, _, c1, c2 = _issue_chain(client)
    # 非正上限 -> 422
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [c1["id"], c2["id"]],
                          "max_continuous_exceed_seconds": 0})
    assert r.status_code == 422
    # 不足两张 -> 422
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [c1["id"]],
                          "max_continuous_exceed_seconds": 1})
    assert r.status_code == 422
    # 缺时区读数仍 -> 422
    wid = make_window(client, "微秒单窗-缺时区",
                      "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1)
    r = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": "naive", "revision": 1,
        "measured_at": "2026-10-01T00:00:01", "temperature": 20.0})
    assert r.status_code == 422

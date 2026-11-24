"""完工草稿与最小调减改数单的端到端测试。

覆盖：
- 草稿提交校验（非负、键集合、合计对底账）、失败保留原稿、替换与查询；
- 无档案/部分档案/档案查全三种状态下的改数单，手工核对最小调减与边值；
- 已查回数照录、自由边 0..楼栋总数、旧区合计对上底账、新区增减守恒；
- 大汇总容量（远大于栋数枚举可行范围）下按汇总数快速求解；
- 登记/清空档案后再查即按新记录出单（同一快照语义）；
- 真实 uvicorn 下并发提交草稿，写锁串行化、只留一稿且合计始终对底账。
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def server():
    config = uvicorn.Config(app, host="127.0.0.1", port=8766, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:8766/docs", timeout=1.0)
            break
        except httpx.TransportError:
            threading.Event().wait(0.1)
    yield "http://127.0.0.1:8766"
    server.should_exit = True
    thread.join(timeout=5)


def _period(code: str, old, new, overlaps):
    return {
        "code": code,
        "name": code,
        "old_districts": [
            {"code": o, "name": o, "completed": c} for o, c in old
        ],
        "new_districts": [
            {"code": n, "name": n, "fee": f} for n, f in new
        ],
        "overlaps": [
            {"new_code": n, "old_code": o, "building_total": t}
            for n, o, t in overlaps
        ],
    }


def _check_invariants(body, period):
    """改数单通用不变量：自由边界内、旧区对底账、增减守恒、合计对底账。"""
    assert body["status"] == "ok", body
    caps = {(o_["new_code"], o_["old_code"]): o_["building_total"]
            for o_ in period["overlaps"]}
    ledger = {o["code"]: o["completed"] for o in period["old_districts"]}
    by_old: dict[str, int] = {o: 0 for o in ledger}
    for item in body["overlap_completed"]:
        key = (item["new_code"], item["old_code"])
        assert 0 <= item["completed_count"] <= caps[key]
        by_old[item["old_code"]] += item["completed_count"]
    assert by_old == ledger
    # 各新区最终数 = 其各边之和；增减数与草稿一致
    draft = {a["new_code"]: a["draft_completed"] for a in body["adjustments"]}
    edge_sum: dict[str, int] = {n: 0 for n in draft}
    for item in body["overlap_completed"]:
        edge_sum[item["new_code"]] += item["completed_count"]
    red = inc = 0
    for a in body["adjustments"]:
        assert a["final_completed"] == edge_sum[a["new_code"]]
        assert a["delta"] == a["final_completed"] - a["draft_completed"]
        if a["delta"] < 0:
            red -= a["delta"]
        else:
            inc += a["delta"]
    assert red == body["total_reduction"]
    assert inc == body["total_increase"]
    assert red == inc  # 草稿合计等于底账合计，调减必等于调增
    assert body["final_total"] == sum(ledger.values())


# ---------------------------------------------------------------- 草稿校验

def test_draft_validation_and_replacement_keeps_old_on_failure(client):
    period = _period(
        "DRAFT1",
        [("O1", 10), ("O2", 6)],
        [("N1", 1), ("N2", 1)],
        [
            ("N1", "O1", 8), ("N1", "O2", 6),
            ("N2", "O1", 6), ("N2", "O2", 4),
        ],
    )
    assert client.post("/periods/", json=period).status_code == 201
    base = "/periods/DRAFT1"

    # 无草稿
    got = client.get(f"{base}/draft").json()
    assert got["submitted_at"] is None and got["items"] == []
    assert client.get(f"{base}/change-order").json()["status"] == "no_draft"

    # 负数被拒（Pydantic 422）
    r = client.post(f"{base}/draft", json={"counts": {"N1": -1, "N2": 17}})
    assert r.status_code == 422

    # 键集合不符
    r = client.post(f"{base}/draft", json={"counts": {"N1": 16}})
    assert r.status_code == 422 and "全部新区" in r.json()["detail"]

    # 合计不等于底账合计 16
    r = client.post(f"{base}/draft", json={"counts": {"N1": 10, "N2": 5}})
    assert r.status_code == 422 and "底账" in r.json()["detail"]

    # 首次提交成功
    r = client.post(f"{base}/draft", json={"counts": {"N1": 12, "N2": 4}})
    assert r.status_code == 200 and r.json()["replaced"] is False

    # 非法替换：原稿保留且仍可查
    r = client.post(f"{base}/draft", json={"counts": {"N1": 99, "N2": 1}})
    assert r.status_code == 422
    got = client.get(f"{base}/draft").json()
    assert {i["new_code"]: i["completed"] for i in got["items"]} == {
        "N1": 12,
        "N2": 4,
    }
    assert got["total"] == 16 and got["ledger_total"] == 16

    # 合法替换
    r = client.post(f"{base}/draft", json={"counts": {"N1": 0, "N2": 16}})
    assert r.status_code == 200 and r.json()["replaced"] is True
    got = client.get(f"{base}/draft").json()
    assert {i["new_code"]: i["completed"] for i in got["items"]} == {
        "N1": 0,
        "N2": 16,
    }


def test_draft_on_missing_period_404(client):
    r = client.post(
        "/periods/NOPE/draft", json={"counts": {"N1": 1}}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------- 无档案改数单

def test_change_order_without_retrievals_hand_checked(client):
    # 单旧区 O1=10；N1 边容量 4，N2 容量 10。草稿 N1=8,N2=2。
    # N1 最多承担 4 -> 至少调减 4；N2 补到 6（调增 4）。R=4。
    period = _period(
        "CO1",
        [("O1", 10)],
        [("N1", 1), ("N2", 1)],
        [("N1", "O1", 4), ("N2", "O1", 10)],
    )
    assert client.post("/periods/", json=period).status_code == 201
    base = "/periods/CO1"
    assert client.post(
        f"{base}/draft", json={"counts": {"N1": 8, "N2": 2}}
    ).status_code == 200

    body = client.get(f"{base}/change-order").json()
    _check_invariants(body, period)
    assert body["total_reduction"] == 4
    adj = {a["new_code"]: a for a in body["adjustments"]}
    assert (adj["N1"]["final_completed"], adj["N1"]["delta"]) == (4, -4)
    assert (adj["N2"]["final_completed"], adj["N2"]["delta"]) == (6, 4)
    edges = {(e["new_code"], e["old_code"]): e["completed_count"]
             for e in body["overlap_completed"]}
    assert edges == {("N1", "O1"): 4, ("N2", "O1"): 6}
    assert all(e["source"] == "solved" for e in body["overlap_completed"])


def test_change_order_zero_reduction_draft(client):
    # 草稿本身可行 -> R=0，所有增减数为 0，边值给出一套可行分配
    period = _period(
        "CO2",
        [("O1", 10), ("O2", 6)],
        [("N1", 1), ("N2", 1)],
        [
            ("N1", "O1", 8), ("N1", "O2", 6),
            ("N2", "O1", 6), ("N2", "O2", 4),
        ],
    )
    assert client.post("/periods/", json=period).status_code == 201
    base = "/periods/CO2"
    assert client.post(
        f"{base}/draft", json={"counts": {"N1": 10, "N2": 6}}
    ).status_code == 200
    body = client.get(f"{base}/change-order").json()
    _check_invariants(body, period)
    assert body["total_reduction"] == 0
    assert all(a["delta"] == 0 for a in body["adjustments"])


def test_change_order_multi_old_hand_checked(client):
    # O1=10, O2=10；边容量 N1-O1=4, N1-O2=10, N2-O1=10, N2-O2=4。
    # 草稿 N1=16,N2=4。N1 总容量 14 -> 至少调减 2，R=2。
    period = _period(
        "CO3",
        [("O1", 10), ("O2", 10)],
        [("N1", 1), ("N2", 1)],
        [
            ("N1", "O1", 4), ("N1", "O2", 10),
            ("N2", "O1", 10), ("N2", "O2", 4),
        ],
    )
    assert client.post("/periods/", json=period).status_code == 201
    base = "/periods/CO3"
    assert client.post(
        f"{base}/draft", json={"counts": {"N1": 16, "N2": 4}}
    ).status_code == 200
    body = client.get(f"{base}/change-order").json()
    _check_invariants(body, period)
    assert body["total_reduction"] == 2
    adj = {a["new_code"]: a for a in body["adjustments"]}
    assert adj["N1"]["final_completed"] == 14 and adj["N1"]["delta"] == -2
    assert adj["N2"]["final_completed"] == 6 and adj["N2"]["delta"] == 2


# ---------------------------------------------------------------- 档案照录

def test_retrieved_edges_are_recorded_verbatim(client):
    # CO1：登记 N2 档案 O1=6（照录，不可改）。草稿 N1=8,N2=2。
    # O1 余数 4 正好由 N1（容量 4）填满，登记与底账相容。
    # N1 调减 4；N2 最终 6（全为照录），调增 4。R=4。
    base = "/periods/CO1"
    client.delete(f"{base}/retrievals")
    r = client.post(
        f"{base}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 6}},
    )
    assert r.status_code == 201
    body = client.get(f"{base}/change-order").json()
    period = _period(
        "CO1", [("O1", 10)], [("N1", 1), ("N2", 1)],
        [("N1", "O1", 4), ("N2", "O1", 10)],
    )
    _check_invariants(body, period)
    assert body["total_reduction"] == 4
    n2_edge = next(
        e for e in body["overlap_completed"]
        if (e["new_code"], e["old_code"]) == ("N2", "O1")
    )
    assert n2_edge["completed_count"] == 6 and n2_edge["source"] == "retrieved"
    n1_edge = next(
        e for e in body["overlap_completed"]
        if (e["new_code"], e["old_code"]) == ("N1", "O1")
    )
    assert n1_edge["completed_count"] == 4 and n1_edge["source"] == "solved"
    adj = {a["new_code"]: a for a in body["adjustments"]}
    assert adj["N1"]["final_completed"] == 4 and adj["N1"]["delta"] == -4
    assert adj["N2"]["final_completed"] == 6 and adj["N2"]["delta"] == 4


def test_reregister_after_reset_reissues_order(client):
    # 核对期间登记/清空档案后再查，按新记录出单
    base = "/periods/CO1"
    before = client.get(f"{base}/change-order").json()["total_reduction"]
    client.delete(f"{base}/retrievals")
    after_reset = client.get(f"{base}/change-order").json()
    assert after_reset["total_reduction"] == before == 4
    # 登记另一份档案后结果变化：N2 固定 9，O1 余 1 给 N1
    client.post(
        f"{base}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 9}},
    )
    body = client.get(f"{base}/change-order").json()
    # N1 草稿 8 只完成 1 -> 调减 7；N2 草稿 2 完成 9 -> 调增 7。R=7
    assert body["total_reduction"] == 7
    n2_edge = next(
        e for e in body["overlap_completed"]
        if (e["new_code"], e["old_code"]) == ("N2", "O1")
    )
    assert n2_edge["completed_count"] == 9 and n2_edge["source"] == "retrieved"
    n1_edge = next(
        e for e in body["overlap_completed"]
        if (e["new_code"], e["old_code"]) == ("N1", "O1")
    )
    assert n1_edge["completed_count"] == 1
    client.delete(f"{base}/retrievals")
    assert client.get(f"{base}/change-order").json()["total_reduction"] == 4


def test_infeasible_archives_reported_not_500(client):
    # 绕过 API 注入与底账不相容的脏档案，改数单返回 infeasible
    from app.db import connect
    period = _period(
        "COBAD", [("O1", 5)], [("N1", 1), ("N2", 1)],
        [("N1", "O1", 10), ("N2", "O1", 10)],
    )
    assert client.post("/periods/", json=period).status_code == 201
    client.post(
        "/periods/COBAD/draft", json={"counts": {"N1": 3, "N2": 2}}
    )
    conn = connect()
    try:
        pid = conn.execute(
            "SELECT id FROM period WHERE code='COBAD'"
        ).fetchone()["id"]
        nid = conn.execute(
            "SELECT id FROM new_district WHERE period_id=? AND code='N1'", (pid,)
        ).fetchone()["id"]
        oid = conn.execute(
            "SELECT id FROM old_district WHERE period_id=? AND code='O1'", (pid,)
        ).fetchone()["id"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO retrieval(period_id, new_district_id, old_district_id, "
            "completed_count) VALUES (?, ?, ?, ?)",
            (pid, nid, oid, 9),
        )
        conn.commit()
    finally:
        conn.close()
    body = client.get("/periods/COBAD/change-order").json()
    assert body["status"] == "infeasible" and body["has_draft"] is True
    # 草稿仍可查
    assert client.get("/periods/COBAD/draft").json()["total"] == 5


# ---------------------------------------------------------------- 汇总数性能

def test_large_aggregate_capacities_solved_without_building_expansion(client):
    # 容量取百万级；若逐栋展开不可行，Dinic 按汇总容量应在毫秒级完成
    period = _period(
        "COBIG",
        [("O1", 1_000_000), ("O2", 1_000_000)],
        [(f"N{i}", 1) for i in range(6)],
        [
            (f"N{i}", "O1", 400_000) for i in range(6)
        ] + [
            (f"N{i}", "O2", 400_000) for i in range(6)
        ],
    )
    assert client.post("/periods/", json=period).status_code == 201
    base = "/periods/COBIG"
    # 草稿全压在 N1：200 万；N1 总容量仅 80 万 -> 至少调减 120 万
    counts = {f"N{i}": 0 for i in range(6)}
    counts["N0"] = 2_000_000
    assert client.post(f"{base}/draft", json={"counts": counts}).status_code == 200
    start = time.perf_counter()
    body = client.get(f"{base}/change-order").json()
    elapsed = time.perf_counter() - start
    _check_invariants(body, period)
    assert body["total_reduction"] == 1_200_000
    assert elapsed < 1.0


# ---------------------------------------------------------------- 演示期全流程

def test_demo_period_full_flow_with_draft(client):
    base = "/periods/DEMO-2023"
    client.delete(f"{base}/retrievals")
    # 种子草稿已随播种写入
    draft = client.get(f"{base}/draft").json()
    assert {i["new_code"]: i["completed"] for i in draft["items"]} == {
        "N1": 32, "N2": 18, "N3": 14, "N4": 18,
    }
    # 无档案：N1 受重叠容量限制至少调减 2，R=2
    body0 = client.get(f"{base}/change-order").json()
    assert body0["status"] == "ok" and body0["total_reduction"] == 2
    adj0 = {a["new_code"]: a for a in body0["adjustments"]}
    assert adj0["N1"]["delta"] == -2 and adj0["N4"]["delta"] == 2

    # 登记 N2(O1=10,O3=6)：查回数照录，N2 锁定为 16（草稿 18，调减 2）
    client.post(
        f"{base}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
    )
    body1 = client.get(f"{base}/change-order").json()
    assert body1["status"] == "ok" and body1["total_reduction"] == 4
    n2_edges = sorted(
        (e for e in body1["overlap_completed"] if e["new_code"] == "N2"),
        key=lambda e: e["old_code"],
    )
    assert [(e["old_code"], e["completed_count"], e["source"]) for e in n2_edges] == [
        ("O1", 10, "retrieved"),
        ("O3", 6, "retrieved"),
    ]
    adj1 = {a["new_code"]: a for a in body1["adjustments"]}
    assert adj1["N2"]["final_completed"] == 16 and adj1["N2"]["delta"] == -2

    # 再登记 N3(O2=14,O3=2)：查全后各新区数被唯一确定为
    # N1=30,N2=16,N3=16,N4=20；最小调减仍为 4（N1、N2 各减 2）
    client.post(
        f"{base}/retrievals",
        json={"new_code": "N3", "counts": {"O2": 14, "O3": 2}},
    )
    body2 = client.get(f"{base}/change-order").json()
    adj = {a["new_code"]: a for a in body2["adjustments"]}
    assert adj["N1"]["final_completed"] == 30 and adj["N1"]["delta"] == -2
    assert adj["N2"]["final_completed"] == 16 and adj["N2"]["delta"] == -2
    assert adj["N3"]["final_completed"] == 16 and adj["N3"]["delta"] == 2
    assert adj["N4"]["final_completed"] == 20 and adj["N4"]["delta"] == 2
    assert body2["total_reduction"] == 4
    assert body2["final_total"] == 82
    # 已查边全部照录
    retrieved = {
        (e["new_code"], e["old_code"]): e["completed_count"]
        for e in body2["overlap_completed"] if e["source"] == "retrieved"
    }
    assert retrieved == {
        ("N2", "O1"): 10, ("N2", "O3"): 6,
        ("N3", "O2"): 14, ("N3", "O3"): 2,
    }
    # 与 /accounting 唯一核算结果一致
    acc = client.get(f"{base}/accounting").json()
    acc_map = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    for a in body2["adjustments"]:
        assert acc_map[a["new_code"]] == a["final_completed"]

    # 清空档案后再查，回到无档案结果
    client.delete(f"{base}/retrievals")
    assert client.get(f"{base}/change-order").json()["total_reduction"] == 2


# ---------------------------------------------------------------- 并发提交

def test_concurrent_draft_submits_serialized(client, server):
    # 两个线程并发提交不同草稿，写锁串行化：恰好一稿胜出，且合计对底账
    base = f"{server}/periods/DRAFT1"
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    payloads = [
        {"counts": {"N1": 12, "N2": 4}},
        {"counts": {"N1": 0, "N2": 16}},
    ]

    def post(payload):
        barrier.wait()
        r = httpx.post(f"{base}/draft", json=payload, timeout=10)
        statuses.append(r.status_code)

    t1 = threading.Thread(target=post, args=(payloads[0],))
    t2 = threading.Thread(target=post, args=(payloads[1],))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(statuses) == [200, 200]
    got = httpx.get(f"{base}/draft", timeout=10).json()
    totals = {sum(p["counts"].values()) for p in payloads}
    assert got["total"] in totals and got["total"] == got["ledger_total"] == 16
    # 库里每个新区恰一行，没有出现两稿混写
    assert len(got["items"]) == 2
    winner = {i["new_code"]: i["completed"] for i in got["items"]}
    assert winner in [p["counts"] for p in payloads]


def test_change_order_reads_consistent_snapshot_under_writes(client, server):
    # 只读改数单与登记/草稿写并发：读不阻塞写，且每次响应自身守恒
    base = f"{server}/periods/CO2"
    client.delete(f"{base}/retrievals")
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            b = httpx.get(f"{base}/change-order", timeout=10).json()
            if b["status"] == "ok":
                if b["total_reduction"] != b["total_increase"]:
                    errors.append("reduction != increase")
                if b["final_total"] != b["ledger_total"]:
                    errors.append("final total mismatch")

    th = threading.Thread(target=reader)
    th.start()
    for _ in range(5):
        httpx.post(
            f"{base}/retrievals",
            json={"new_code": "N1", "counts": {"O1": 3, "O2": 2}},
            timeout=10,
        )
        httpx.delete(f"{base}/retrievals", timeout=10)
    stop.set()
    th.join()
    assert not errors, errors

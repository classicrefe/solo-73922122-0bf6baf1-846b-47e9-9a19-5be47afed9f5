"""完工草稿与最小调减改数单。

业务问题
========
年报员按现行新区界提交完工草稿 d_N（每个新区一个非负整数，合计等于各旧区
底账完工数之和 T）。档案可能尚未查全：

- 已查回的重叠范围 (N, O)，完工数 x_{N,O} 照录，不可更改；
- 其余（未查）重叠范围可填 0 <= x_{N,O} <= t_{N,O} 的整数，t 为该范围楼栋总数；
- 各旧区合计须对上底账：Σ_N x_{N,O} = c_O。

新区最终完工数 y_N = Σ_O x_{N,O}；相对草稿的增减数 Δ_N = y_N − d_N。
目标：最小化调减总数 R = Σ_N max(0, d_N − y_N)。

按楼栋汇总数求解（不逐栋展开）
==============================
把已查档案从问题中剥离：
- f_N = Σ已查边 x（新区 N 已查回的完工数）；
- 自由边（未查范围）容量为该范围楼栋总数 t_{N,O}；
- 旧区 O 尚需由自由边凑足的余数 r_O = c_O − Σ已查 x；
- 自由流总量 T' = Σ_O r_O；新区自由部分 w_N = y_N − f_N（无自由边则 w_N=0）；
- g_N = d_N − f_N，可吸收余量为 max(0, g_N)。

利用恒等式 max(0, g − w) = max(0, g) − min(max(0, g), w)，调减总量

    R = Σ_N max(0, g_N − w_N) = H_free − M_A + H_trapped

- H_free = Σ_{N 有自由边} max(0, g_N)；
- H_trapped = Σ_{N 无自由边} max(0, g_N)：已查全、最终数锁定为 f_N，
  草稿高出部分必然调减；
- M_A 是受限网络最大流：源 → 有自由边且 g_N>0 的新区（容量 g_N），
  新区 → 旧区自由边（容量 t_{N,O}），旧区 → 汇（容量 r_O）。

证明：Σ min(max(0,g_N), w_N) 正是 M_A——受限源边把每个有自由边新区的
可吸收部分截到 g_N 以内，最大化被吸收流量即最小化调减；g_N<0 时
max(0,g_N)=0，其调减 f_N−d_N 在最终增减数中自然体现，不进网络公式。
阶段二再给每个有自由边的新区补一条无限容量源边把旧区余数填满（底账相容性
保证可行），即可重建一套达到最小调减的完整边流量方案。

容量均为汇总楼栋数，Dinic 算法复杂度只取决于节点/边数（每期新区 ≤ 18），
与单栋规模无关，天然避免逐栋展开。

确定性 tie-break：网络建边按新区/旧区编码排序，Dinic DFS 按邻接表顺序
增广；最小调减总数唯一，具体改数单在等值最优解中按此顺序确定。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .db import immediate_transaction
from .schemas import DraftIn
from .services import AppError, PeriodData, _load_period, _old_state


# ---------------------------------------------------------------- 最大流

class _Edge:
    __slots__ = ("to", "rev", "cap", "name")

    def __init__(self, to: int, rev: int, cap: int, name: str | None = None):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.name = name  # 调试/重建用边标记


class Dinic:
    """整数容量 Dinic 最大流；容量按汇总数给出，复杂度与容量大小无关。"""

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]
        self.level = [-1] * n
        self.it = [0] * n

    def add_edge(self, fr: int, to: int, cap: int, name: str | None = None) -> None:
        forward = _Edge(to, len(self.g[to]), cap, name)
        backward = _Edge(fr, len(self.g[fr]), 0)
        self.g[fr].append(forward)
        self.g[to].append(backward)

    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * self.n
        self.level[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            for e in self.g[v]:
                if e.cap > 0 and self.level[e.to] < 0:
                    self.level[e.to] = self.level[v] + 1
                    q.append(e.to)
        return self.level[t] >= 0

    def _dfs(self, v: int, t: int, f: int) -> int:
        if v == t:
            return f
        while self.it[v] < len(self.g[v]):
            e = self.g[v][self.it[v]]
            if e.cap > 0 and self.level[e.to] == self.level[v] + 1:
                pushed = self._dfs(e.to, t, min(f, e.cap))
                if pushed:
                    e.cap -= pushed
                    self.g[e.to][e.rev].cap += pushed
                    return pushed
            self.it[v] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: int | None = None) -> int:
        flow = 0
        INF = 10**30
        budget = limit if limit is not None else INF
        while budget > 0 and self._bfs(s, t):
            self.it = [0] * self.n
            while budget > 0:
                f = self._dfs(s, t, budget)
                if f == 0:
                    break
                flow += f
                budget -= f
        return flow

    def edge_flow_to(self, fr: int, to: int) -> int:
        """取 fr -> to 唯一业务边的已送流量（源边可能分两条，故按节点匹配）。"""
        for e in self.g[fr]:
            if e.to == to and e.name is not None and not e.name.startswith(
                ("cap-", "open-")
            ):
                return self.g[e.to][e.rev].cap
        raise KeyError(f"业务边不存在: {fr}->{to}")


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class ChangeResult:
    status: str
    total_reduction: int | None
    total_increase: int | None
    # new_code -> (draft, final, delta)
    adjustments: dict[str, tuple[int, int, int]] | None
    # (new_code, old_code) -> (building_total, completed_count, source)
    overlaps: dict[tuple[str, str], tuple[int, int, str]] | None
    ledger_total: int
    final_total: int | None
    message: str


# ---------------------------------------------------------------- 草稿读写

def load_draft(conn, period_code: str) -> tuple[dict[str, int], str | None]:
    """返回 (各新区草稿数, 最近提交时间)；无草稿时为 ({}, None)。"""
    prow = conn.execute(
        "SELECT id FROM period WHERE code = ?", (period_code,)
    ).fetchone()
    if prow is None:
        raise AppError(404, f"期次不存在: {period_code}")
    rows = conn.execute(
        """
        SELECT n.code AS new_code, d.completed, d.submitted_at
        FROM draft d
        JOIN new_district n ON n.id = d.new_district_id
        WHERE d.period_id = ?
        ORDER BY n.id
        """,
        (prow["id"],),
    ).fetchall()
    if not rows:
        return {}, None
    return {r["new_code"]: r["completed"] for r in rows}, rows[0]["submitted_at"]


def submit_draft(conn, period_code: str, payload: DraftIn) -> tuple[dict[str, int], str, bool]:
    """提交/替换完工草稿。

    校验（全部在同一个 BEGIN IMMEDIATE 事务内，失败 ROLLBACK、原稿保留）：
    1. 期次存在；草稿键集合恰好等于该期全部新区；
    2. 每个值为非负整数（Pydantic 已保证，这里再防御一次）；
    3. 合计等于各旧区底账完工数之和。
    通过后整稿替换原稿，返回 (草稿, 提交时间, 是否替换)。
    """
    with immediate_transaction(conn):
        data = _load_period(conn, period_code)
        got = set(payload.counts)
        expected = set(data.new_codes)
        if got != expected:
            missing = sorted(expected - got)
            extra = sorted(got - expected)
            raise AppError(
                422,
                f"草稿必须恰好覆盖全部新区 {sorted(expected)}；缺少 {missing}，多余 {extra}",
            )
        for ncode, value in payload.counts.items():
            if value < 0:
                raise AppError(422, f"草稿完工数不能为负: {ncode}={value}")

        total = sum(payload.counts.values())
        ledger_total = sum(data.old_completed.values())
        if total != ledger_total:
            raise AppError(
                422,
                f"草稿合计 {total} 必须等于各旧区底账完工数之和 {ledger_total}",
            )

        pid = conn.execute(
            "SELECT id FROM period WHERE code = ?", (period_code,)
        ).fetchone()["id"]
        existed = conn.execute(
            "SELECT COUNT(*) AS c FROM draft WHERE period_id = ?", (pid,)
        ).fetchone()["c"]
        # 整稿替换：先清空旧稿再插新稿，同事务内完成，并发提交由写锁串行化
        conn.execute("DELETE FROM draft WHERE period_id = ?", (pid,))
        for ncode in data.new_codes:
            nid = conn.execute(
                "SELECT id FROM new_district WHERE period_id = ? AND code = ?",
                (pid, ncode),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO draft(period_id, new_district_id, completed) "
                "VALUES (?, ?, ?)",
                (pid, nid, payload.counts[ncode]),
            )
        submitted_at = conn.execute(
            "SELECT submitted_at FROM draft WHERE period_id = ? ORDER BY id LIMIT 1",
            (pid,),
        ).fetchone()["submitted_at"]
        return dict(payload.counts), submitted_at, bool(existed)


# ---------------------------------------------------------------- 改数单

def solve_change_order(data: PeriodData, draft: dict[str, int]) -> ChangeResult:
    """在同一已提交状态（底账 + 档案 + 草稿）上求最小调减改数单。"""
    ledger_total = sum(data.old_completed.values())

    # 1) 先做底账相容性判断（档案没查全也允许，只看已查部分是否相容）
    try:
        state = _old_state(data, data.registered)
        for old, s in state.items():
            remaining = int(s["remaining"])
            if remaining < 0:
                raise AppError(
                    409,
                    f"旧区 {old} 已查完工数 {s['used']} 超过底账 "
                    f"{data.old_completed[old]}，与底账不相容",
                )
            if remaining > int(s["free_capacity"]):
                raise AppError(
                    409,
                    f"旧区 {old} 完工余数 {remaining} 无法由未查楼栋 "
                    f"（总容量 {s['free_capacity']}）凑足，与底账不相容",
                )
    except AppError as exc:
        return ChangeResult(
            status="infeasible",
            total_reduction=None,
            total_increase=None,
            adjustments=None,
            overlaps=None,
            ledger_total=ledger_total,
            final_total=None,
            message=f"当前档案与底账不相容，无法出单：{exc.detail}",
        )

    old_codes = list(data.old_codes)
    new_codes = list(data.new_codes)
    old_idx = {o: i for i, o in enumerate(old_codes)}
    new_idx = {n: i for i, n in enumerate(new_codes)}
    n_new, n_old = len(new_codes), len(old_codes)

    # 2) 已查边照录；剥离自由边与旧区余数
    overlaps: dict[tuple[str, str], tuple[int, int, str]] = {}
    free_edges: list[tuple[str, str, int]] = []  # (new, old, capacity)
    for old in old_codes:
        for ncode, total in data.old_edges[old]:
            if ncode in data.registered and old in data.registered[ncode]:
                x = int(data.registered[ncode][old])
                overlaps[(ncode, old)] = (total, x, "retrieved")
            else:
                free_edges.append((ncode, old, total))
                overlaps[(ncode, old)] = (total, 0, "solved")

    forced = {n: 0 for n in new_codes}
    for (ncode, old), (_t, x, src) in overlaps.items():
        if src == "retrieved":
            forced[ncode] += x
    residual = {
        old: int(data.old_completed[old])
        - sum(reg.get(old, 0) for reg in data.registered.values())
        for old in old_codes
    }
    t_free = sum(residual.values())

    # 3) 建流网络：S -> 新区 -> 旧区 -> T
    #    节点编号：新区 [0, n_new)，旧区 [n_new, n_new+n_old)，S、T 在后
    S = n_new + n_old
    T = S + 1
    assert T + 1 == (n_new + n_old) + 2
    dinic = Dinic(n_new + n_old + 2)
    # 拥有至少一条自由边的新区（无自由边的新区无法再吸收自由流）
    free_new = {n for n, _o, _c in free_edges}
    # g_N = d_N − f_N；可吸收余量 max(0, g_N) 分两类：
    #   有自由边 -> 受限源边容量，计入 H_free，可被 M_A 吸收；
    #   无自由边 -> 最终数锁定 f_N，草稿高出部分成为固定调减 H_trapped。
    # g_N < 0（查回已超草稿）时 max(0,g)=0，其调减在最终 delta 中自然体现。
    h_free: dict[str, int] = {}
    h_trapped = 0
    for ncode in new_codes:
        g = draft[ncode] - forced[ncode]
        if g <= 0:
            continue
        if ncode in free_new:
            h_free[ncode] = g
            dinic.add_edge(S, new_idx[ncode], g, name=f"cap-{ncode}")
        else:
            h_trapped += g
    for ncode, old, cap in free_edges:
        dinic.add_edge(
            new_idx[ncode], n_new + old_idx[old], cap, name=f"{ncode}|{old}"
        )
    for old in old_codes:
        if residual[old] > 0:
            dinic.add_edge(n_new + old_idx[old], T, residual[old], name=f"need-{old}")

    # 4) 阶段一：仅有自由边且 g>0 的新区接源，求受限最大流 M_A
    m_a = dinic.max_flow(S, T)

    # 5) 阶段二：给每个有自由边的新区补一条无限容量源边，让自由流可超出
    #    可吸收余量，把旧区余数填满。cap 边保持不动（M_A 仅统计其增广量）；
    #    开放边在阶段二 BFS 重建分层后才参与增广，不影响 M_A。
    for ncode in free_new:
        dinic.add_edge(S, new_idx[ncode], 10**18, name=f"open-{ncode}")
    second = dinic.max_flow(S, T)
    total_flow = m_a + second
    if total_flow != t_free:
        # 理论上不会发生：建档与登记校验已保证底账可行
        return ChangeResult(
            status="infeasible",
            total_reduction=None,
            total_increase=None,
            adjustments=None,
            overlaps=None,
            ledger_total=ledger_total,
            final_total=None,
            message=(
                f"内部错误：自由流 {total_flow} 未填满旧区余数 {t_free}，"
                "请检查底账与档案相容性"
            ),
        )

    # 6) 重建自由边流量（业务边按 新区->旧区 节点对唯一匹配）
    free_flow: dict[tuple[str, str], int] = {}
    for ncode, old, _cap in free_edges:
        f = dinic.edge_flow_to(
            new_idx[ncode], n_new + old_idx[old]
        )
        free_flow[(ncode, old)] = f
        total_t, _x, _src = overlaps[(ncode, old)]
        overlaps[(ncode, old)] = (total_t, f, "solved")

    # 7) 各新区最终完工数与增减数
    free_by_new = {n: 0 for n in new_codes}
    for (ncode, old), f in free_flow.items():
        free_by_new[ncode] += f
    adjustments: dict[str, tuple[int, int, int]] = {}
    total_reduction = 0
    total_increase = 0
    final_total = 0
    for ncode in new_codes:
        final = forced[ncode] + free_by_new[ncode]
        delta = final - draft[ncode]
        adjustments[ncode] = (draft[ncode], final, delta)
        if delta < 0:
            total_reduction += -delta
        elif delta > 0:
            total_increase += delta
        final_total += final

    # 8) 核对（汇总级断言，不逐栋展开）
    #    R = H_free − M_A + H_trapped
    expected_r = sum(h_free.values()) - m_a + h_trapped
    assert total_reduction == expected_r, (total_reduction, expected_r)
    assert total_reduction == total_increase, (total_reduction, total_increase)
    assert final_total == ledger_total, (final_total, ledger_total)
    for old in old_codes:
        got = sum(overlaps[(n, old)][1] for n, _t in data.old_edges[old])
        assert got == data.old_completed[old], (old, got)
    for (ncode, old), (total, x, _src) in overlaps.items():
        assert 0 <= x <= total, (ncode, old, x, total)

    return ChangeResult(
        status="ok",
        total_reduction=total_reduction,
        total_increase=total_increase,
        adjustments=adjustments,
        overlaps=overlaps,
        ledger_total=ledger_total,
        final_total=final_total,
        message=(
            f"最小调减总数 {total_reduction} 栋"
            if total_reduction
            else "草稿无需调减即可对上底账（最小调减总数 0 栋）"
        ),
    )

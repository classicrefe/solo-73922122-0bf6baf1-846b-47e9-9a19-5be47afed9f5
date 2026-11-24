# 建设年报改造完工楼栋补算 API

按**现行区界**补算历年改造完工楼栋数的后端服务，包含两组能力：

1. **调档规划**：给定各期旧区完工底账、新旧区重叠范围楼栋总数、各新区调档费用，
   在调档结果与底账相容的前提下，给出**总费用最低的待调名单与金额**；
   照单调完后可**唯一**算出各新区完工数。
2. **完工草稿与改数单**：年报员按期提交新区完工草稿（合计对底账），
   在档案可能尚未查全的情况下，按楼栋汇总数求出**最小调减总数**，
   并给出一套对应的各新区增减数与各重叠范围完工数。

技术栈：FastAPI + SQLite（WAL）+ Pydantic，Docker Compose 启动。

## 1. 问题建模与关键结论

对每个期次：

- 旧区 `O` 有底账完工数 `c_O`；
- 新区 `N` 与旧区 `O` 的重叠范围有楼栋总数 `t_{N,O}`；
- 未知量 `x_{N,O}` = 现行新区 N 在原旧区 O 范围内的完工楼栋数，满足
  `0 ≤ x_{N,O} ≤ t_{N,O}`，且每个旧区 `Σ_N x_{N,O} = c_O`；
- 调一份新区 N 的档案，即得到该新区所有边上的 `x_{N,O}`，费用 `f_N`
  （正整数，每期最多 18 个新区）。

**何时能唯一确定全部未知量？**
底账相容时，只要每个旧区至多剩一条「未查」边即可：0 条未查边时余数必须为 0；
1 条时其值等于旧区完工余数。因此同一旧区下不允许同时存在两个未查新区——
这些新区两两构成**冲突**。待查集合必须覆盖冲突图的所有边，即冲突图的
**顶点覆盖**，目标是**费用最小的顶点覆盖**（等价于最大权独立集的补集）。
新区数 ≤ 18，服务用位掩码 DP 在 `O(2^k)`（实测 k=18 约 0.22 秒）内精确求解，
tie-break 规则：费用低 → 待调个数少 → 名单编码字典序小。

**底账相容性（登记校验，逐旧区独立）**

1. 各范围查回数介于 0 与该范围楼栋总数之间；
2. 已查总数 ≤ 旧区底账（完工余数非负）；
3. 完工余数 ≤ 未查楼栋总数（余数能由未查楼栋凑足）。

整数边容量 `0 ≤ x ≤ t` 下，条件 2、3 同时成立即保证存在可行分配。

### 1.1 完工草稿与最小调减改数单

年报员按现行新区界提交草稿 `d_N`（每个新区一个非负整数，**合计等于底账合计**
`T = Σ c_O`）。档案可能没查全：

- 已查回的重叠范围 `(N,O)`，完工数 `x_{N,O}` **照录**，不可更改；
- 其余未查范围可填 `0 ≤ x_{N,O} ≤ t_{N,O}` 的整数；
- 各旧区合计须对上底账 `Σ_N x_{N,O} = c_O`；
- 新区最终完工数 `y_N = Σ_O x_{N,O}`，增减数 `Δ_N = y_N − d_N`；
- 目标：最小化调减总数 `R = Σ_N max(0, d_N − y_N)`。

**按楼栋汇总数求解（Dinic 最大流，不逐栋展开）。** 剥离已查档案：

- `f_N` = 新区 N 已查回完工数；自由边容量仍为该范围楼栋总数 `t_{N,O}`；
- 旧区 O 尚需自由边凑足的余数 `r_O = c_O − Σ已查 x`；
- 新区自由量 `w_N = y_N − f_N`（无自由边则为 0）；令 `g_N = d_N − f_N`。

由恒等式 `max(0, g−w) = max(0, g) − min(max(0, g), w)` 得

```
R = H_free − M_A + H_trapped
```

- `H_free = Σ_{N 有自由边} max(0, g_N)`；
- `H_trapped = Σ_{N 无自由边} max(0, g_N)`：已查全、最终数锁定为 `f_N`，
  草稿高出部分必然调减；
- `M_A` 是受限网络最大流：源 → 有自由边且 `g_N>0` 的新区（容量 `g_N`），
  新区 → 旧区自由边（容量 `t_{N,O}`），旧区 → 汇（容量 `r_O`）。

先求 `M_A`，再给每个有自由边的新区补一条无限容量源边把旧区余数填满，
即可重建一套达到最小调减的完整边流量方案。所有容量都是**汇总楼栋数**，
Dinic 复杂度只取决于节点/边数（每期新区 ≤ 18），与单栋规模无关。
建边按编码排序、DFS 按邻接表顺序增广，最小调减总数唯一，等值最优解按此
顺序确定一套改数单。

## 2. 数据模型（SQLite）

| 表 | 关键字段 |
|---|---|
| `period` | 期次编码、名称 |
| `old_district` | 期次、编码、名称、底账完工数 |
| `new_district` | 期次、编码、名称、调档费用（>0） |
| `overlap` | 期次、新区、旧区、重叠楼栋总数（>0），三元组唯一 |
| `retrieval` | 期次、新区、旧区、查回完工数，`(期次,新区,旧区)` 唯一 |
| `draft` | 期次、新区、草稿完工数（≥0）、提交时间，`(期次,新区)` 唯一 |

并发与事务：

- 开启 **WAL**：核算/改数单走只读快照（`BEGIN`），登记/草稿提交走写事务，二者不互斥；
- 所有写操作使用 **`BEGIN IMMEDIATE`** 立即取写锁，并发写被数据库串行化，
  配合 `busy_timeout=5000` 等待锁；
- 任一校验失败即 `ROLLBACK`，**原记录/原稿保留**；
- 同一新区相同档案重送幂等返回 201，异值返回 409 且不覆盖；
- 草稿整稿替换在**单个写事务**内先删后插，失败回滚，并发提交只留一稿；
- 计划/核算/改数单端点在单个只读事务内读**同一已提交状态**（底账+档案+草稿），
  核对期间登记照常进行，登记或清空后再查即按新记录出单。

## 3. API 一览（启动后见 `/docs` 交互文档）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/periods/` | 建立期次底账（旧区完工数、重叠楼栋总数、调档费用） |
| GET | `/periods/` | 期次列表 |
| GET | `/periods/{code}` | 期次底账详情 |
| GET | `/periods/{code}/plan` | **总费用最低的待调名单与金额**（只读快照） |
| GET | `/periods/{code}/accounting` | 核算；无需再调档时返回各新区完工数 |
| POST | `/periods/{code}/retrievals` | 登记一份新区档案（同值幂等、异值退回） |
| GET | `/periods/{code}/retrievals` | 已登记档案 |
| DELETE | `/periods/{code}/retrievals` | 清空该期登记（便于在 /docs 重复试用） |
| POST | `/periods/{code}/draft` | **提交/替换完工草稿**（非负、合计对底账；失败保留原稿） |
| GET | `/periods/{code}/draft` | 查询当前草稿（无草稿时 `items` 为空） |
| GET | `/periods/{code}/change-order` | **最小调减改数单**：调减总数、各新区增减数、各重叠范围完工数 |

## 4. 用 Docker Compose 启动

```bash
docker compose up --build
# 打开交互文档
# http://localhost:8000/docs
```

数据持久化在命名卷 `retrocalc-data`（容器内 `/data/app.db`）。
首次启动自动播种演示期次 `DEMO-2023`（库中已有期次时不重复播种），
并随附一份演示完工草稿 `N1=32,N2=18,N3=14,N4=18`（合计 82 对底账）。

本地无 Docker 时的等价运行方式：

```bash
python3 -m venv --without-pip .venv          # 按环境引导 pip 后
.venv/bin/pip install -r requirements.txt
APP_DB_PATH=./data/app.db .venv/bin/uvicorn app.main:app --port 8000
```

## 5. /docs 试用脚本（调档前 → 调档后核算）

演示期底账：旧区 O1=30、O2=24、O3=18、O4=10；
新区费用 N1=12、N2=7、N3=3、N4=10。
冲突图的最大权独立集为 {N1, N4}（可省 22），故**最低费用待调名单 = {N2, N3}，金额 10**。

```bash
B=http://localhost:8000/periods/DEMO-2023

# 调档前：拿到最低费用名单
curl -s $B/plan
# status=retrieval_required, plan=["N2","N3"], total_fee=10
curl -s $B/accounting        # determined=false，尚不返回完工数

# 登记 N2 档案（查回数必须覆盖该新区涉及的全部旧区 O1/O3）
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":10,"O3":6}}'
# 名单收缩为 ["N3"]，金额 3

# 登记 N3 档案
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N3","counts":{"O2":14,"O3":2}}'
# status=completed，plan=[]

# 无需再调档，返回各新区完工数
curl -s $B/accounting
# N1=30, N2=16, N3=16, N4=20，合计 82 = 底账合计 30+24+18+10
```

校验类试用：

```bash
# 查回数超楼栋总数 -> 422
curl -i -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":16,"O3":0}}'
# 旧区集合不符 -> 422
curl -i -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":5}}'
# 同值重送 -> 201 幂等；异值重送 -> 409 原记录保留
# 完工余数无法由未查楼栋凑足 -> 409
```

### 5.1 草稿与改数单：档案变动前后各出一单

```bash
B=http://localhost:8000/periods/DEMO-2023
curl -s -X DELETE $B/retrievals >/dev/null   # 先清空，便于重复试用

# 查看随启动播种的草稿（合计 82 = 底账合计）
curl -s $B/draft
# {"total":82,"ledger_total":82, items: N1=32,N2=18,N3=14,N4=18}

# 提交一稿（键必须恰好覆盖全部新区，值非负，合计对底账，否则 422 且原稿保留）
curl -s -X POST $B/draft -H 'Content-Type: application/json' \
  -d '{"counts":{"N1":32,"N2":18,"N3":14,"N4":18}}'

# 档案变动前（无档案也能出单）：N1 受重叠容量限制至少调减 2 栋
curl -s $B/change-order
# total_reduction=2, total_increase=2
# adjustments: N1 32->30(-2), N4 18->20(+2)，N2/N3 不变

# 登记 N2 档案后再查：查回数照录，N2 锁定为 16（草稿 18，再减 2）
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":10,"O3":6}}' >/dev/null
curl -s $B/change-order
# total_reduction=4；N2/O1=10、N2/O3=6 两条 source=retrieved 照录

# 再登记 N3，档案查全：各新区数被唯一确定，与 /accounting 一致
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N3","counts":{"O2":14,"O3":2}}' >/dev/null
curl -s $B/change-order
# N1=30(-2), N2=16(-2), N3=16(+2), N4=20(+2)，final_total=82 对底账

# 核对期间可随时登记/清空；清空后再查即回到无档案结果
curl -s -X DELETE $B/retrievals >/dev/null
curl -s $B/change-order        # total_reduction 回到 2
```

改数单响应里 `overlap_completed` 逐条给出每个重叠范围的楼栋总数、完工数与
来源（`retrieved` 档案照录 / `solved` 未查范围求解）；未提交草稿时
`status=no_draft`，档案与底账不相容时 `status=infeasible`，均不抛 500。

草稿校验类试用：

```bash
# 合计不等于底账合计 82 -> 422，原稿仍可 GET 查回
curl -i -X POST $B/draft -H 'Content-Type: application/json' \
  -d '{"counts":{"N1":99,"N2":0,"N3":0,"N4":0}}'
# 含负数 -> 422；新区键集合不符（缺/多编码）-> 422
```

## 6. 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

- `tests/test_api.py`：建档校验、最低费用计划、四类登记校验、幂等/异值、
  计划推进、唯一核算、**真实 uvicorn 下的并发登记**、快照读不阻塞写；
- `tests/test_solver.py`：18 新区完全图性能与正确性、链状图选便宜覆盖、
  脏数据触发 `infeasible` 防御、单未查边唯一求解；
- `tests/test_revision.py`：草稿三类校验与失败留稿、整稿替换、
  无档案/部分档案/查全三态改数单（手工核对最小调减与边值）、档案照录、
  旧区对底账与增减守恒、**百万级汇总容量毫秒求解**、登记/清空后重新出单、
  脏档案 `infeasible` 防御、**真实 uvicorn 下并发提交草稿与读写并发**。

## 7. 目录结构

```
app/
  db.py        # SQLite 连接、WAL、IMMEDIATE 写事务、只读快照、建表（含 draft）
  schemas.py   # Pydantic 请求/响应模型（含草稿、改数单）
  services.py  # 相容性校验、最小权顶点覆盖 DP、唯一核算
  revision.py  # 草稿提交/查询、Dinic 最大流、最小调减改数单
  seed.py      # 演示期次种子数据与演示草稿
  main.py      # FastAPI 路由
tests/         # pytest 套件
Dockerfile  docker-compose.yml  requirements.txt  pytest.ini
```

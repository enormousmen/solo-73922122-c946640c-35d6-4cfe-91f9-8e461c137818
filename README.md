# FIB Compressor

边缘路由器换型配套工具：把现有 IPv4 转发表压缩成**条目最少**的等效表，装进新设备的容量预算；换机后还可用诊断接口核查哪些路由漏装。

- 输入：最多 128 条前缀互异的 IPv4 CIDR 路由（每条指定一个下一跳或 `drop` 丢弃）、新设备条目上限 `capacity`、一个要演练失效的下一跳 `failed_nexthop`。
- 输出：按最长前缀匹配（LPM）与原表**逐地址等价**的最小转发表——可换用新前缀，动作全部取自原表；`drop`（丢弃）与未匹配严格区分。
- 故障演练：把新旧两张表中指向 `failed_nexthop` 的条目全部撤掉后，两表仍须逐地址等价。压缩结果天然满足这一约束。
- 超限时只回报最低所需条数（`status=capacity_exceeded`，`table=null`）。
- 漏装诊断：`POST /diagnose` 接收配置路由（最多 16 条标为待核查，其余已确认）与设备快照（实装条数 + 最多 64 个目的地址的观测），逐条判定待核查路由**必定存在 / 必定缺失 / 无法确定**；证据自相矛盾时只回报冲突。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

`/docs`（Swagger UI）的 `POST /compress` 内置三个示例，下拉即可切换：

| 示例 | 说明 |
|---|---|
| `capacity_ok` | 容量足够：5 条压到 4 条，撤掉 NH2 后两表仍等价 |
| `capacity_exceeded` | 容量不够：只回报 `min_entries=4`，不下发转发表 |
| `failover_ladder` | 故障回落链：/8→NH1、/16→NH2、/24→NH3，演练失效 NH2 |

`POST /diagnose` 同样内置三个示例：

| 示例 | 说明 |
|---|---|
| `all_decided` | 3 条待核查路由全部被判定：2 条必装、1 条必漏，定向补发即可 |
| `undetermined` | 只凭实装条数二选一，两条待核查路由均无法确定 |
| `evidence_conflict` | 实装条数与观测互相矛盾，只回报证据冲突 |

命令行演示（服务启动后）：

```bash
./scripts/demo.sh                # 依次演示容量够 / 不够两种情况 + 漏装诊断
```

## API

### `POST /compress`

```json
{
  "routes": [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.2.0.0/16", "action": "NH1"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"}
  ],
  "capacity": 4,
  "failed_nexthop": "NH2"
}
```

容量足够时返回最小等效表（`10.2.0.0/16` 与 `10.0.0.0/8` 同为 NH1，被合并吸收）：

```json
{
  "status": "ok", "fits": true, "min_entries": 4, "verified": true,
  "table": [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"}
  ]
}
```

容量不够（如 `capacity=3`）时只报最低所需条数：

```json
{"status": "capacity_exceeded", "fits": false, "min_entries": 4, "table": null}
```

约束与约定：

- `routes` 最多 128 条，前缀互异；非规范写法（如 `10.1.2.3/8`）会归一化为网络地址。
- `action` 为下一跳名称，或保留字 `drop`；不匹配任何条目的地址是“未匹配”，与 `drop` 不同。
- 响应前服务端会对结果做逐区间等价复核（正常 + 故障两种场景），`verified=true` 才返回。

### `POST /verify`

独立校验两张表是否在正常与故障两种场景下逐地址等价，返回首个失配点。

### `POST /diagnose`

换机后的漏装诊断。前提假设：设备实装表 = 全部确认路由 + 待核查路由的某个子集（不会多装）。

```json
{
  "routes": [
    {"prefix": "10.0.0.0/8", "action": "NH1", "status": "confirmed"},
    {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"},
    {"prefix": "10.2.0.0/16", "action": "NH2", "status": "pending"},
    {"prefix": "10.3.0.0/16", "action": "drop", "status": "pending"},
    {"prefix": "192.168.0.0/16", "action": "NH3", "status": "confirmed"}
  ],
  "snapshot": {
    "installed_count": 4,
    "observations": [
      {"address": "10.1.2.3", "result": "NH2"},
      {"address": "10.3.2.1", "result": "NH1"},
      {"address": "192.168.9.9", "result": "NH3"},
      {"address": "203.0.113.5", "result": "no-match"}
    ]
  }
}
```

响应：逐条给出待核查路由的判定，并报告有多少张表与全部证据相符：

```json
{
  "status": "ok",
  "consistent_tables": 1,
  "verdicts": [
    {"prefix": "10.1.0.0/16", "action": "NH2", "verdict": "must_be_present"},
    {"prefix": "10.2.0.0/16", "action": "NH2", "verdict": "must_be_present"},
    {"prefix": "10.3.0.0/16", "action": "drop", "verdict": "must_be_missing"}
  ],
  "detail": null
}
```

- `status`：`confirmed`（默认，已确认装入）或 `pending`（待核查，最多 16 条）；`status` 字段可省略。
- 观测 `result`：下一跳名称、`drop` 或保留字 `no-match`（未匹配）；路由 `action` 不允许使用 `no-match`。
- 判定语义：`must_be_present`（所有相符的表里都装了）、`must_be_missing`（所有相符的表里都没装，需定向补发）、`undetermined`（证据不足）。
- 没有任何相符的表时只回报证据冲突：`{"status": "conflict", "consistent_tables": 0, "verdicts": null, "detail": "..."}`。
- 限制：路由 ≤ 128 条且前缀互异、待核查 ≤ 16 条、观测 ≤ 64 条、`installed_count` ∈ [0, 128]；实装条数与确认/待核查条数矛盾（如小于确认条数）按证据冲突处理，不是 422。

诊断方法：待核查路由至多 16 条，候选表即“大小 = 实装条数 − 确认条数”的待核查子集（最多 C(16,8)=12870 个）；逐个子集按最长前缀匹配核对全部观测，再按各待核查路由在幸存子集中的出现情况给出判定。

### `GET /`

服务信息。

## 算法（为什么是最少条目）

每个目的地址带一个二元需求 `(正常结果, 撤掉故障下一跳后的结果)`，由原表推出；两个函数在所有前缀边界切出的基本区间上都是常数（≤ 2n+1 段）。

1. 把地址空间建成“常数需求”二分前缀 trie（叶 = 极大常数需求 dyadic 区间）。
2. 在 trie 上做精确动态规划：状态是“上方最长匹配条目动作 + 上方最长非故障条目动作”的继承对（可达状态只有 `2k+2` 种，k 为非故障动作数）；叶节点用闭式代价（含“正常层=F、回落层=b”的双层结构，无法从祖先继承回落时最多需 3 条）；内部节点在“不放置 / 放置故障下一跳 / 放置某非故障动作”三种选择中取最优。
3. 回溯选择得到最小条目表。DP 是精确的，返回的条目数是可证明的最小值。

复杂度：`O(节点数 × 动作数)`，128 条路由实测 < 0.5s。

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest            # 110 项测试：压缩、故障切换、最小性、漏装诊断、API
uvicorn app.main:app --reload
```

## 目录结构

```
app/compressor.py   # 压缩核心：需求区间 → 常数 trie → 双场景 DP → 回溯
app/diagnosis.py    # 漏装诊断：候选子集枚举 → LPM 证据过滤 → 逐条判定
app/main.py         # FastAPI：/compress、/verify、/diagnose、/docs 示例
tests/              # 单元、随机性质、暴力最小性、诊断交叉验证、API 测试
scripts/demo.sh     # 两种容量场景 + 漏装诊断的 curl 演示
Dockerfile / docker-compose.yml
```

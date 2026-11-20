"""FIB compressor service.

Edge-router hardware migration helper: squeeze the current IPv4 forwarding
table into a new device's entry budget.  Given up to 128 routes with
distinct prefixes, a device capacity, and a next-hop to drill-fail, the
service returns the smallest equivalent table (new prefixes may be used,
actions are reused from the original table).  Equivalence means identical
longest-prefix-match results for every destination address — with "drop"
and "no-match" kept distinct — both before and after withdrawing the
failed next-hop from old and new tables.  When the minimum does not fit
the capacity, only the minimum required entry count is reported.

A second tool audits the swapped-in device: given the configured routes
(a few of them marked "pending") plus a snapshot of the installed entry
count and probed destination addresses, it reports for every pending route
whether it is certainly installed, certainly missing, or undecidable —
or flags an evidence conflict when no table fits all the facts.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import List, Literal, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .compressor import DROP, compress, verify
from .diagnosis import diagnose

APP_VERSION = "1.1.0"
MAX_ROUTES = 128
MAX_PENDING = 16
MAX_OBSERVATIONS = 64
NO_MATCH = "no-match"

# ---------------------------------------------------------------------------
# demo payloads (shown in /docs)
# ---------------------------------------------------------------------------

DEMO_ROUTES = [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.2.0.0/16", "action": "NH1"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"},
]

EXAMPLE_CAPACITY_OK = {
    "summary": "容量足够：5 条压到 4 条，故障切换保持等价",
    "description": (
        "10.2.0.0/16 与默认的 10.0.0.0/8 同为 NH1，被合并吸收；"
        "撤掉 NH2 后 10.1.0.0/16 回落到 NH1、192.168.0.0/16 变为未匹配，"
        "新旧表两种场景下逐地址一致。最少需要 4 条，容量 4 刚好装下。"
    ),
    "value": {
        "routes": DEMO_ROUTES,
        "capacity": 4,
        "failed_nexthop": "NH2",
    },
}

EXAMPLE_CAPACITY_EXCEEDED = {
    "summary": "容量不够：只回报最低所需条数",
    "description": (
        "同一张表演练失效 NH2，但新设备只能装 3 条。等效表最少需要 4 条，"
        "超过容量，因此只返回 min_entries=4，不下发转发表。"
    ),
    "value": {
        "routes": DEMO_ROUTES,
        "capacity": 3,
        "failed_nexthop": "NH2",
    },
}

EXAMPLE_FAILOVER_LADDER = {
    "summary": "故障回落链：被撤下一跳后回落到更短前缀",
    "description": (
        "10.0.0.0/8→NH1、10.1.0.0/16→NH2、10.1.1.0/24→NH3，演练失效 NH2。"
        "撤掉 NH2 后 10.1.0.0/16 内（除 10.1.1.0/24）须回落到 NH1。"
        "压缩结果保持正常与故障两种场景逐地址等价。"
    ),
    "value": {
        "routes": [
            {"prefix": "10.0.0.0/8", "action": "NH1"},
            {"prefix": "10.1.0.0/16", "action": "NH2"},
            {"prefix": "10.1.1.0/24", "action": "NH3"},
        ],
        "capacity": 8,
        "failed_nexthop": "NH2",
    },
}

# ---------------------------------------------------------------------------
# /diagnose demo payloads
# ---------------------------------------------------------------------------

DIAG_DEMO_ROUTES = [
    {"prefix": "10.0.0.0/8", "action": "NH1", "status": "confirmed"},
    {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"},
    {"prefix": "10.2.0.0/16", "action": "NH2", "status": "pending"},
    {"prefix": "10.3.0.0/16", "action": "drop", "status": "pending"},
    {"prefix": "192.168.0.0/16", "action": "NH3", "status": "confirmed"},
]

EXAMPLE_DIAG_ALL_DECIDED = {
    "summary": "换机核查：三条待核查路由全部被判定",
    "description": (
        "换机后 5 条配置路由中 3 条待核查，快照显示实装 4 条（即待核查中恰好装了 2 条）。"
        "观测 10.1.2.3→NH2 说明 10.1.0.0/16 必定在（否则走 /8 回落到 NH1）；"
        "观测 10.3.2.1→NH1 而非 drop 说明 10.3.0.0/16 必定漏装；"
        "于是条数约束迫使 10.2.0.0/16 必定在。漏装的只有 10.3.0.0/16，定向补发即可。"
    ),
    "value": {
        "routes": DIAG_DEMO_ROUTES,
        "snapshot": {
            "installed_count": 4,
            "observations": [
                {"address": "10.1.2.3", "result": "NH2"},
                {"address": "10.3.2.1", "result": "NH1"},
                {"address": "192.168.9.9", "result": "NH3"},
                {"address": "203.0.113.5", "result": "no-match"},
            ],
        },
    },
}

EXAMPLE_DIAG_UNDETERMINED = {
    "summary": "只凭条数无法确定：两条待核查路由二选一",
    "description": (
        "两条待核查路由去向同为 NH2，快照实装 2 条 = 1 条确认 + 恰好 1 条待核查，"
        "观测地址只命中确认路由，无法区分装了哪一条：两条都是 undetermined，"
        "相符的表共 2 张。"
    ),
    "value": {
        "routes": [
            {"prefix": "10.0.0.0/8", "action": "NH1", "status": "confirmed"},
            {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"},
            {"prefix": "10.2.0.0/16", "action": "NH2", "status": "pending"},
        ],
        "snapshot": {
            "installed_count": 2,
            "observations": [
                {"address": "10.9.9.9", "result": "NH1"},
            ],
        },
    },
}

EXAMPLE_DIAG_CONFLICT = {
    "summary": "证据冲突：实装条数与观测不可能同时成立",
    "description": (
        "快照称实装 2 条（确认 1 条 + 待核查 1 条全装），"
        "但观测 10.1.2.3→NH1：若 10.1.0.0/16 已装，最长匹配应得 NH2。"
        "没有任何符合模型的表能同时满足全部证据，只回报 conflict，不给逐条结论。"
    ),
    "value": {
        "routes": [
            {"prefix": "10.0.0.0/8", "action": "NH1", "status": "confirmed"},
            {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"},
        ],
        "snapshot": {
            "installed_count": 2,
            "observations": [
                {"address": "10.1.2.3", "result": "NH1"},
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

class Route(BaseModel):
    """One forwarding entry: a CIDR prefix and its action.

    ``action`` is a next-hop name, or the reserved word ``drop`` to discard
    matching traffic.  Addresses that match no entry are "no-match", which
    is distinct from ``drop``.
    """

    prefix: str = Field(examples=["10.0.0.0/8"])
    action: str = Field(min_length=1, max_length=64, examples=["NH1"])

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        try:
            net = ip_network(v.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 CIDR prefix: {v!r}") from exc
        if not isinstance(net, IPv4Network):
            raise ValueError(f"not an IPv4 prefix: {v!r}")
        return str(net)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        a = v.strip()
        if not a:
            raise ValueError("action must be a non-empty next-hop name or 'drop'")
        return a


class CompressRequest(BaseModel):
    routes: List[Route] = Field(
        max_length=MAX_ROUTES,
        description=f"Up to {MAX_ROUTES} routes with distinct prefixes.",
    )
    capacity: int = Field(ge=0, description="Entry budget of the new device.")
    failed_nexthop: str = Field(
        min_length=1,
        max_length=64,
        description="Next-hop withdrawn in the failure drill.",
    )

    @field_validator("failed_nexthop")
    @classmethod
    def _check_failed(cls, v: str) -> str:
        f = v.strip()
        if not f:
            raise ValueError("failed_nexthop must be non-empty")
        return f

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        prefixes = [r.prefix for r in self.routes]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("route prefixes must be distinct")
        return self


class CompressResponse(BaseModel):
    status: str = Field(description="'ok' or 'capacity_exceeded'.")
    fits: bool = Field(description="Whether the minimal table fits the capacity.")
    min_entries: int = Field(description="Minimal number of entries of any equivalent table.")
    capacity: int
    failed_nexthop: str
    table: Optional[List[Route]] = Field(
        description="The minimal equivalent table; null when over capacity."
    )
    verified: Optional[bool] = Field(
        description=(
            "Server-side re-check that old and new tables agree on every "
            "address in both the normal and the failure scenario."
        )
    )


class VerifyRequest(BaseModel):
    old_routes: List[Route] = Field(max_length=4096)
    new_routes: List[Route] = Field(max_length=4096)
    failed_nexthop: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        for name, rs in (("old_routes", self.old_routes), ("new_routes", self.new_routes)):
            prefixes = [r.prefix for r in rs]
            if len(set(prefixes)) != len(prefixes):
                raise ValueError(f"{name}: prefixes must be distinct")
        return self


class VerifyResponse(BaseModel):
    equivalent: bool
    normal_equivalent: bool
    failover_equivalent: bool
    checked_points: int = Field(
        description="Number of elementary address intervals compared."
    )
    first_mismatch: Optional[dict] = None


# ---------------------------------------------------------------------------
# /diagnose schemas
# ---------------------------------------------------------------------------

class DiagRoute(Route):
    """A configured route whose installation state is confirmed or pending."""

    status: Literal["confirmed", "pending"] = Field(
        default="confirmed",
        description=(
            "'confirmed' entries are certainly installed; 'pending' entries "
            f"(at most {MAX_PENDING}) are the only ones the actual table may lack."
        ),
    )

    @field_validator("action")
    @classmethod
    def _action_not_reserved(cls, v: str) -> str:
        if v == NO_MATCH:
            raise ValueError(f"action {NO_MATCH!r} is reserved for observations")
        return v


class Observation(BaseModel):
    """One probe: the forwarding result observed for a destination address."""

    address: str = Field(examples=["10.1.2.3"])
    result: str = Field(
        min_length=1,
        max_length=64,
        examples=["NH2"],
        description=(
            "Observed next-hop name, 'drop', or 'no-match' when no installed "
            "route covers the address."
        ),
    )

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        try:
            addr = ip_address(v.strip())
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 address: {v!r}") from exc
        if not isinstance(addr, IPv4Address):
            raise ValueError(f"not an IPv4 address: {v!r}")
        return str(addr)

    @field_validator("result")
    @classmethod
    def _check_result(cls, v: str) -> str:
        r = v.strip()
        if not r:
            raise ValueError("result must be a next-hop name, 'drop', or 'no-match'")
        return r


class Snapshot(BaseModel):
    """Evidence collected from the device being diagnosed."""

    installed_count: int = Field(
        ge=0,
        le=MAX_ROUTES,
        description="Number of entries actually installed on the device.",
    )
    observations: List[Observation] = Field(
        default=[],
        max_length=MAX_OBSERVATIONS,
        description=f"Up to {MAX_OBSERVATIONS} probed destination addresses.",
    )


class DiagnoseRequest(BaseModel):
    routes: List[DiagRoute] = Field(
        max_length=MAX_ROUTES,
        description=(
            f"Up to {MAX_ROUTES} configured routes with distinct prefixes; "
            f"at most {MAX_PENDING} may be marked 'pending'."
        ),
    )
    snapshot: Snapshot

    @model_validator(mode="after")
    def _check_routes(self):
        prefixes = [r.prefix for r in self.routes]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("route prefixes must be distinct")
        pending = sum(1 for r in self.routes if r.status == "pending")
        if pending > MAX_PENDING:
            raise ValueError(f"at most {MAX_PENDING} routes may be pending")
        return self


class PendingVerdict(BaseModel):
    prefix: str
    action: str
    verdict: Literal["must_be_present", "must_be_missing", "undetermined"] = Field(
        description=(
            "must_be_present: installed in every consistent table; "
            "must_be_missing: absent from every consistent table "
            "(re-issue this route); undetermined: the evidence cannot decide."
        )
    )


class DiagnoseResponse(BaseModel):
    status: str = Field(description="'ok' or 'conflict'.")
    consistent_tables: int = Field(
        description="How many actual tables agree with all the evidence (0 on conflict)."
    )
    verdicts: Optional[List[PendingVerdict]] = Field(
        description="Per pending-route verdicts; null on evidence conflict."
    )
    detail: Optional[str] = Field(
        description="Human-readable explanation, set on evidence conflict."
    )


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FIB Compressor",
    version=APP_VERSION,
    description=(
        "Compress an IPv4 forwarding table for a smaller-capacity edge router. "
        "The compressed table is entry-minimal and equivalent under "
        "longest-prefix matching — distinguishing drop from no-match — both "
        "before and after a next-hop failure drill. "
        "Try the ready-made examples below: one where the capacity suffices "
        "and one where only the minimum required count is reported.\n\n"
        "POST /diagnose audits a freshly swapped device: given the configured "
        "routes (a few marked pending) plus a snapshot of the installed entry "
        "count and probed destinations, it reports which pending routes are "
        "certainly installed, certainly missing, or undecidable — or flags an "
        "evidence conflict."
    ),
)


@app.get("/", summary="Service info")
def root():
    return {
        "service": "fib-compressor",
        "version": APP_VERSION,
        "endpoints": {
            "POST /compress": "minimal equivalent table (or minimum count when over capacity)",
            "POST /verify": "check two tables for normal + failover equivalence",
            "POST /diagnose": "audit which pending routes the device actually installed",
            "GET /docs": "interactive demo",
        },
        "limits": {
            "max_routes": MAX_ROUTES,
            "max_pending_routes": MAX_PENDING,
            "max_observations": MAX_OBSERVATIONS,
        },
        "reserved_action": DROP,
        "reserved_observation_result": NO_MATCH,
    }


@app.post("/compress", response_model=CompressResponse, summary="Compress a table")
def compress_endpoint(
    req: CompressRequest = Body(
        openapi_examples={
            "capacity_ok": EXAMPLE_CAPACITY_OK,
            "capacity_exceeded": EXAMPLE_CAPACITY_EXCEEDED,
            "failover_ladder": EXAMPLE_FAILOVER_LADDER,
        }
    )
):
    routes = [(IPv4Network(r.prefix), r.action) for r in req.routes]
    min_entries, table = compress(routes, req.failed_nexthop, req.capacity)

    if table is None:
        return CompressResponse(
            status="capacity_exceeded",
            fits=False,
            min_entries=min_entries,
            capacity=req.capacity,
            failed_nexthop=req.failed_nexthop,
            table=None,
            verified=None,
        )

    new_routes = [(IPv4Network(p), a) for p, a in table]
    ok, detail = verify(routes, new_routes, req.failed_nexthop)
    if not ok:  # pragma: no cover - indicates an algorithmic bug
        raise HTTPException(
            status_code=500,
            detail=f"internal verification failed: {detail['first_mismatch']}",
        )

    return CompressResponse(
        status="ok",
        fits=True,
        min_entries=min_entries,
        capacity=req.capacity,
        failed_nexthop=req.failed_nexthop,
        table=[Route(prefix=p, action=a) for p, a in table],
        verified=True,
    )


@app.post("/verify", response_model=VerifyResponse, summary="Check equivalence")
def verify_endpoint(req: VerifyRequest):
    old = [(IPv4Network(r.prefix), r.action) for r in req.old_routes]
    new = [(IPv4Network(r.prefix), r.action) for r in req.new_routes]
    ok, detail = verify(old, new, req.failed_nexthop)
    return VerifyResponse(equivalent=ok, **detail)


@app.post("/diagnose", response_model=DiagnoseResponse, summary="Diagnose missing routes")
def diagnose_endpoint(
    req: DiagnoseRequest = Body(
        openapi_examples={
            "all_decided": EXAMPLE_DIAG_ALL_DECIDED,
            "undetermined": EXAMPLE_DIAG_UNDETERMINED,
            "evidence_conflict": EXAMPLE_DIAG_CONFLICT,
        }
    )
):
    routes = [
        (IPv4Network(r.prefix), r.action, r.status == "pending") for r in req.routes
    ]
    observations = [
        (IPv4Address(o.address), None if o.result == NO_MATCH else o.result)
        for o in req.snapshot.observations
    ]
    verdicts, consistent = diagnose(
        routes, req.snapshot.installed_count, observations
    )

    if verdicts is None:
        return DiagnoseResponse(
            status="conflict",
            consistent_tables=0,
            verdicts=None,
            detail=(
                "证据冲突：实装条数与全部观测无法同时成立——"
                "不存在一张与证据相符的转发表（确认路由全装、"
                "待核查路由任选、条目总数与每次观测的最长前缀匹配结果一致）。"
            ),
        )

    pending_routes = [r for r in req.routes if r.status == "pending"]
    return DiagnoseResponse(
        status="ok",
        consistent_tables=consistent,
        verdicts=[
            PendingVerdict(prefix=r.prefix, action=r.action, verdict=v)
            for r, v in zip(pending_routes, verdicts)
        ],
        detail=None,
    )

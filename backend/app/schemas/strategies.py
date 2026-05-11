# PATH: backend/app/schemas/strategies.py
"""
Trading Strategies — Pydantic v2 Schemas
==========================================
Request validation, response serialisation, and recursive type definitions
for the Trading Strategies rule engine.

Schema hierarchy:

  Strategy Definition (the recursive condition tree)
    ConditionOperator      (enum)
    ConditionLeaf          (single field/operator/value predicate)
    ConditionGroup         (AND / OR / NOT wrapper containing children)
    ConditionNode          (union discriminated by "type" field)

  Configuration sub-schemas
    StopLossConfig         (fixed_pct | atr_multiple | price_level)
    TakeProfitLevel        (single TP level with pct + exit_fraction)
    TakeProfitConfig       (list of up to 3 TP levels)
    PositionSizingConfig   (fixed_qty | risk_pct | kelly)
    GuardrailsConfig       (session, max_positions, min_confidence, etc.)
    SignalGatesConfig      (allowed symbols, timeframes, directions, regimes)
    StrategyDefinition     (root — assembles all sub-schemas)

  Enums
    EnforcementMode, SizingMode, StrategyStatus, StrategyScope,
    ActivationState, BacktestMode, BacktestStatus, ActivationExitReason

  Requests  (inbound — strict validation)
    CreateStrategyRequest
    UpdateStrategyRequest
    UpdateUserPreferencesRequest
    UpdateUserProfileRequest
    SubscribeStrategyRequest
    ReorderSubscriptionsRequest
    TriggerBacktestRequest

  Responses  (outbound — ORM → JSON)
    StrategyResponse
    StrategySummaryResponse
    UserPreferencesResponse
    UserProfileResponse
    UserStrategySubscriptionResponse
    StrategyActivationResponse
    StrategyTradeResponse
    StrategyBacktestRunResponse

  List / paginated responses
    StrategiesListResponse
    SubscriptionsListResponse
    ActivationsListResponse
    TradesListResponse
    BacktestRunsListResponse

  Performance
    StrategyPerformanceResponse

Design rules:
  - All Decimal DB columns surface as float in JSON responses.
  - Request schemas validate exhaustively — fail loudly with actionable messages.
  - ConditionNode is a discriminated union; Pydantic resolves it via the "type"
    literal field on ConditionLeaf ("leaf") vs ConditionGroup ("group").
  - StrategyDefinition.model_rebuild() is called at module load to resolve the
    forward reference in the recursive ConditionGroup.children field.
  - Response schemas use ConfigDict(from_attributes=True) for ORM pass-through.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# Type aliases
# ──────────────────────────────────────────────────────────────────────────────

MoneyFloat = Annotated[float, Field(ge=0.0)]
PctFloat = Annotated[float, Field(ge=0.0, le=100.0)]


# ──────────────────────────────────────────────────────────────────────────────
# Shared Decimal → float coercion helper
# ──────────────────────────────────────────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _to_float_required(v: Any) -> float:
    if isinstance(v, Decimal):
        return float(v)
    return v


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class EnforcementMode(str, Enum):
    hard_gate = "hard_gate"
    soft_filter = "soft_filter"
    portfolio_opt_in = "portfolio_opt_in"


class SizingMode(str, Enum):
    fixed_qty = "fixed_qty"
    risk_pct = "risk_pct"
    kelly = "kelly"


class StrategyStatus(str, Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    archived = "archived"


class StrategyScope(str, Enum):
    private = "private"
    shared = "shared"


class ActivationState(str, Enum):
    WATCHING = "WATCHING"
    IN_SETUP = "IN_SETUP"
    IN_TRADE = "IN_TRADE"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    EXITED = "EXITED"
    CANCELLED = "CANCELLED"


class ActivationExitReason(str, Enum):
    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    FULL_TP = "FULL_TP"
    SL = "SL"
    MANUAL = "MANUAL"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    STRATEGY_PAUSED = "STRATEGY_PAUSED"
    RULE_VIOLATED = "RULE_VIOLATED"


class BacktestMode(str, Enum):
    signal_replay = "signal_replay"
    model_replay = "model_replay"
    hybrid = "hybrid"


class BacktestStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ConditionOperator(str, Enum):
    """Comparison operators supported in ConditionLeaf predicates."""
    gt = ">"
    gte = ">="
    lt = "<"
    lte = "<="
    eq = "=="
    neq = "!="
    between = "between"
    crosses_above = "crosses_above"
    crosses_below = "crosses_below"


class LogicalOperator(str, Enum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"


class StopLossType(str, Enum):
    fixed_pct = "fixed_pct"
    atr_multiple = "atr_multiple"
    price_level = "price_level"


# ──────────────────────────────────────────────────────────────────────────────
# Recursive Condition Tree
# ──────────────────────────────────────────────────────────────────────────────

class ConditionLeaf(BaseModel):
    """
    A single atomic predicate on a market context field.

    Examples:
      {"type": "leaf", "field": "rsi_14", "op": "<=", "value": 40}
      {"type": "leaf", "field": "ema_9", "op": "crosses_above", "value": "ema_21"}
      {"type": "leaf", "field": "volume_ratio", "op": "between", "value": [1.5, 3.0]}

    field: dot-path to a MarketContext attribute (e.g. "rsi_14", "signal.confidence")
    op: one of ConditionOperator
    value: scalar, string (field reference), or [low, high] for "between"
    """

    type: Literal["leaf"] = "leaf"
    field: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="MarketContext field path (e.g. 'rsi_14', 'signal.confidence')",
    )
    op: ConditionOperator
    value: Union[float, int, str, list[float]] = Field(
        ...,
        description=(
            "Comparison value: scalar, field-path string, "
            "or [low, high] pair for 'between'"
        ),
    )

    @model_validator(mode="after")
    def validate_between_value(self) -> "ConditionLeaf":
        if self.op == ConditionOperator.between:
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError(
                    "Operator 'between' requires value to be a two-element list [low, high]"
                )
            if self.value[0] >= self.value[1]:
                raise ValueError(
                    "between value[0] must be strictly less than value[1]"
                )
        return self


class ConditionGroup(BaseModel):
    """
    A logical group (AND / OR / NOT) wrapping child condition nodes.

    NOT groups must contain exactly one child.
    AND / OR groups must contain at least two children.
    Children may themselves be ConditionLeaf or nested ConditionGroup nodes,
    enabling arbitrary recursion depth.
    """

    type: Literal["group"] = "group"
    operator: LogicalOperator
    children: list["ConditionNode"] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_not_arity(self) -> "ConditionGroup":
        if self.operator == LogicalOperator.NOT and len(self.children) != 1:
            raise ValueError("NOT group must contain exactly one child")
        if self.operator in (LogicalOperator.AND, LogicalOperator.OR) and len(self.children) < 2:
            raise ValueError(f"{self.operator.value} group requires at least two children")
        return self


# Discriminated union — resolved by the "type" literal field.
# Forward-reference to ConditionGroup (self-referential) is resolved by
# model_rebuild() at the bottom of this module.
ConditionNode = Annotated[
    Union[ConditionLeaf, ConditionGroup],
    Field(discriminator="type"),
]

# Trigger model_rebuild so the forward-referenced ConditionGroup.children
# field is resolved correctly by Pydantic's type system.
ConditionGroup.model_rebuild()


# ──────────────────────────────────────────────────────────────────────────────
# Configuration Sub-Schemas
# ──────────────────────────────────────────────────────────────────────────────

class StopLossConfig(BaseModel):
    """
    Stop-loss specification for a strategy.

    type       : how the stop-loss distance is computed
    value      : percentage (fixed_pct), ATR multiple (atr_multiple),
                 or absolute price (price_level)
    atr_period : only relevant when type = atr_multiple (default 14)
    trail      : if true, stop-loss trails the best price seen since entry
    """

    type: StopLossType
    value: float = Field(..., gt=0, description="Stop distance — pct, ATR multiple, or price")
    atr_period: int = Field(default=14, ge=5, le=50)
    trail: bool = Field(default=False)

    @model_validator(mode="after")
    def validate_fixed_pct_range(self) -> "StopLossConfig":
        if self.type == StopLossType.fixed_pct and self.value >= 20:
            raise ValueError("fixed_pct stop-loss cannot exceed 20% — use atr_multiple for wider stops")
        return self


class TakeProfitLevel(BaseModel):
    """
    A single take-profit target level.

    pct            : target gain percentage from entry (e.g. 3.0 = +3%)
    exit_fraction  : fraction of the position to exit at this level (0.0–1.0)
    """

    pct: float = Field(..., gt=0, le=100, description="Target gain % from entry")
    exit_fraction: float = Field(
        ..., gt=0, le=1.0,
        description="Fraction of position to exit (e.g. 0.5 = exit 50%)",
    )


class TakeProfitConfig(BaseModel):
    """
    Up to three ordered take-profit levels.

    Total exit_fraction across all levels must not exceed 1.0 (100% of position).
    Levels must be ordered by ascending pct (TP1 < TP2 < TP3).
    """

    levels: list[TakeProfitLevel] = Field(..., min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_levels(self) -> "TakeProfitConfig":
        total_fraction = sum(lvl.exit_fraction for lvl in self.levels)
        if total_fraction > 1.0:
            raise ValueError(
                f"Sum of exit_fraction values ({total_fraction:.2f}) cannot exceed 1.0"
            )
        pcts = [lvl.pct for lvl in self.levels]
        if pcts != sorted(pcts):
            raise ValueError("Take-profit levels must be ordered by ascending pct")
        if len(pcts) != len(set(pcts)):
            raise ValueError("Duplicate take-profit pct values are not allowed")
        return self


class PositionSizingConfig(BaseModel):
    """
    Position sizing specification.

    mode        : fixed_qty | risk_pct | kelly
    fixed_qty   : exact number of shares (used when mode = fixed_qty)
    risk_pct    : percentage of portfolio to risk per trade (0 < x ≤ 5)
    kelly_fraction : fraction of the theoretical Kelly bet (0 < x ≤ 1)
    max_position_pct : hard cap — position cannot exceed this % of portfolio
    """

    mode: SizingMode
    fixed_qty: int | None = Field(default=None, ge=1)
    risk_pct: float | None = Field(default=None, gt=0, le=5.0)
    kelly_fraction: float | None = Field(default=None, gt=0, le=1.0)
    max_position_pct: float = Field(
        default=10.0, gt=0, le=25.0,
        description="Hard cap: position cannot exceed this % of portfolio value",
    )

    @model_validator(mode="after")
    def validate_mode_params(self) -> "PositionSizingConfig":
        if self.mode == SizingMode.fixed_qty and self.fixed_qty is None:
            raise ValueError("fixed_qty is required when mode = 'fixed_qty'")
        if self.mode == SizingMode.risk_pct and self.risk_pct is None:
            raise ValueError("risk_pct is required when mode = 'risk_pct'")
        if self.mode == SizingMode.kelly and self.kelly_fraction is None:
            raise ValueError("kelly_fraction is required when mode = 'kelly'")
        return self


class GuardrailsConfig(BaseModel):
    """
    Hard limits and session guards that gate strategy activation.

    max_concurrent_activations : cap on simultaneous IN_TRADE states for this
                                  strategy (independent of global user preference)
    min_confidence_score       : minimum signal consensus score (0–100)
    allowed_sessions           : e.g. ["morning", "afternoon"] — empty = all
    require_volume_confirm     : activation gated on above-average volume
    max_daily_loss_pct         : strategy auto-pauses if daily P&L < -this%
    """

    max_concurrent_activations: int = Field(default=3, ge=1, le=20)
    min_confidence_score: float = Field(default=60.0, ge=0.0, le=100.0)
    allowed_sessions: list[str] = Field(default_factory=list)
    require_volume_confirm: bool = Field(default=False)
    max_daily_loss_pct: float | None = Field(default=None, gt=0, le=10.0)


class SignalGatesConfig(BaseModel):
    """
    Pre-condition filters applied before the condition tree is evaluated.

    These are fast O(1) exclusions — the condition tree (O(n) recursive eval)
    only runs if all gates pass.

    allowed_symbols     : whitelist — empty means all symbols pass
    blocked_symbols     : explicit blocklist (takes precedence over allowed)
    allowed_directions  : ["BUY"] | ["SELL"] | ["BUY", "SELL"] (default both)
    allowed_timeframes  : e.g. ["15m", "1h", "1d"] — empty = all
    min_market_cap_cr   : minimum market cap in crores (optional)
    allowed_regimes     : e.g. ["trending_up", "ranging"] — empty = all
    """

    allowed_symbols: list[str] = Field(default_factory=list, max_length=500)
    blocked_symbols: list[str] = Field(default_factory=list, max_length=500)
    allowed_directions: list[Literal["BUY", "SELL"]] = Field(
        default_factory=lambda: ["BUY", "SELL"]
    )
    allowed_timeframes: list[str] = Field(default_factory=list)
    min_market_cap_cr: float | None = Field(default=None, ge=0)
    allowed_regimes: list[str] = Field(default_factory=list)

    @field_validator("allowed_directions")
    @classmethod
    def validate_directions_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("allowed_directions must contain at least one value")
        return v


class StrategyDefinition(BaseModel):
    """
    Root strategy definition stored as JSONB in the strategies table.

    signal_gates     : O(1) pre-filters evaluated before the condition trees
    signal_condition : condition tree evaluated against signal-level fields
    indicator_condition : condition tree evaluated against indicator cache fields
    stop_loss        : stop-loss specification (required when auto_execute=true)
    take_profit      : take-profit specification (optional — manual exit if absent)
    position_sizing  : how to size the position
    guardrails       : hard limits and session guards

    Both signal_condition and indicator_condition are optional — a strategy may
    rely purely on signal_gates + stop_loss/take_profit for execution rules.
    """

    signal_gates: SignalGatesConfig = Field(default_factory=SignalGatesConfig)
    signal_condition: ConditionGroup | None = Field(
        default=None,
        description="Condition tree evaluated against signal-level fields",
    )
    indicator_condition: ConditionGroup | None = Field(
        default=None,
        description="Condition tree evaluated against indicator cache fields",
    )
    stop_loss: StopLossConfig | None = None
    take_profit: TakeProfitConfig | None = None
    position_sizing: PositionSizingConfig = Field(
        default_factory=lambda: PositionSizingConfig(mode=SizingMode.risk_pct, risk_pct=2.0)
    )
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)


# ──────────────────────────────────────────────────────────────────────────────
# Request Schemas
# ──────────────────────────────────────────────────────────────────────────────

class CreateStrategyRequest(BaseModel):
    """Create a new trading strategy (starts in 'draft' status)."""

    name: str = Field(..., min_length=3, max_length=100, description="Unique display name")
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Searchable tags (max 10)",
    )
    definition: StrategyDefinition

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if len(tag) > 50:
                raise ValueError(f"Tag '{tag}' exceeds 50 characters")
            if not tag.strip():
                raise ValueError("Tags cannot be empty or whitespace-only")
        return [t.lower().strip() for t in v]


class UpdateStrategyRequest(BaseModel):
    """
    Update an existing strategy.

    All fields are optional — only provided fields are patched.
    Updating definition on an 'active' strategy increments version and
    freezes a new snapshot for subsequent activations.
    """

    name: str | None = Field(default=None, min_length=3, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=10)
    status: StrategyStatus | None = None
    definition: StrategyDefinition | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for tag in v:
            if len(tag) > 50:
                raise ValueError(f"Tag '{tag}' exceeds 50 characters")
        return [t.lower().strip() for t in v]


class UpdateUserPreferencesRequest(BaseModel):
    """Update the authenticated user's trading strategy preferences."""

    enforcement_mode: EnforcementMode | None = None
    hard_gate_notify: bool | None = None
    default_position_sizing_mode: SizingMode | None = None
    default_risk_per_trade_pct: float | None = Field(
        default=None,
        gt=0,
        le=10.0,
        description="Default risk % per trade (overridden by per-strategy config)",
    )
    max_concurrent_activations: int | None = Field(
        default=None, ge=1, le=50
    )


class UpdateUserProfileRequest(BaseModel):
    """Update the authenticated user's profile fields."""

    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=5, max_length=255)
    current_password: str | None = Field(
        default=None, min_length=1,
        description="Required when changing password",
    )
    new_password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="New password (requires current_password)",
    )

    @model_validator(mode="after")
    def validate_password_change(self) -> "UpdateUserProfileRequest":
        if self.new_password and not self.current_password:
            raise ValueError("current_password is required to set a new password")
        if self.current_password and not self.new_password:
            raise ValueError("new_password is required when current_password is provided")
        return self

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str | None) -> str | None:
        if v is not None and "@" not in v:
            raise ValueError("Invalid email address")
        return v


class SubscribeStrategyRequest(BaseModel):
    """Subscribe the authenticated user to a strategy."""

    priority: int = Field(
        ..., ge=1,
        description="Evaluation priority (1 = highest). Must be unique among active subscriptions.",
    )
    notes: str | None = Field(default=None, max_length=500)


class PatchSubscriptionRequest(BaseModel):
    """Pause or resume a subscription without fully unsubscribing."""

    is_active: bool = Field(
        ...,
        description="True = resume (strategy will process signals); False = pause (subscription kept, signals skipped).",
    )


class ReorderSubscriptionsRequest(BaseModel):
    """
    Atomically re-assign priorities across the user's active subscriptions.

    subscription_ids must contain every active subscription ID (no partial
    updates) — the service assigns priorities 1…N in the given order.
    """

    subscription_ids: list[int] = Field(
        ..., min_length=1,
        description="Ordered list of subscription IDs (first = priority 1)",
    )


class TriggerBacktestRequest(BaseModel):
    """Enqueue an async backtest run against a strategy."""

    symbols: list[str] = Field(
        ..., min_length=1, max_length=20,
        description="NSE symbols to backtest (e.g. ['RELIANCE', 'INFY'])",
    )
    start_dt: datetime = Field(..., description="Backtest start (UTC)")
    end_dt: datetime = Field(..., description="Backtest end (UTC)")
    mode: BacktestMode = Field(
        default=BacktestMode.hybrid,
        description="signal_replay | model_replay | hybrid",
    )

    @model_validator(mode="after")
    def validate_date_range(self) -> "TriggerBacktestRequest":
        if self.start_dt >= self.end_dt:
            raise ValueError("start_dt must be strictly before end_dt")
        return self

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip().upper() for s in v]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Duplicate symbols are not allowed")
        return cleaned


# ──────────────────────────────────────────────────────────────────────────────
# Response Schemas
# ──────────────────────────────────────────────────────────────────────────────

class UserPreferencesResponse(BaseModel):
    """Full user preferences — returned on GET/PUT /users/me/preferences."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    enforcement_mode: str
    hard_gate_notify: bool
    default_position_sizing_mode: str
    default_risk_per_trade_pct: float | None
    max_concurrent_activations: int
    created_at: datetime
    updated_at: datetime

    @field_validator("default_risk_per_trade_pct", mode="before")
    @classmethod
    def coerce_risk_pct(cls, v: Any) -> float | None:
        return _to_float(v)


class UserProfileResponse(BaseModel):
    """User profile fields — returned on GET/PUT /users/me/profile."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class StrategyResponse(BaseModel):
    """Full strategy detail — returned on POST/GET/PUT /strategies/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_by: int
    name: str
    description: str | None
    tags: list[str] | None
    scope: str
    status: str
    version: int
    definition: dict[str, Any]
    total_subscribers: int
    avg_daily_return_pct: float | None
    win_rate_pct: float | None
    total_trades: int
    created_at: datetime
    updated_at: datetime

    @field_validator("avg_daily_return_pct", "win_rate_pct", mode="before")
    @classmethod
    def coerce_metrics(cls, v: Any) -> float | None:
        return _to_float(v)


class StrategySummaryResponse(BaseModel):
    """Lightweight strategy card — used in library lists and subscription panels."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    tags: list[str] | None
    scope: str
    status: str
    version: int
    total_subscribers: int
    avg_daily_return_pct: float | None
    win_rate_pct: float | None
    total_trades: int
    created_at: datetime

    @field_validator("avg_daily_return_pct", "win_rate_pct", mode="before")
    @classmethod
    def coerce_metrics(cls, v: Any) -> float | None:
        return _to_float(v)


class UserStrategySubscriptionResponse(BaseModel):
    """A single subscription row — returned on list and mutate endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    strategy_id: UUID
    priority: int
    is_active: bool
    notes: str | None
    subscribed_at: datetime
    unsubscribed_at: datetime | None
    strategy: StrategySummaryResponse | None = None


class StrategyActivationResponse(BaseModel):
    """Full activation state — returned on GET /activations and websocket pushes."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: int
    strategy_id: UUID
    strategy_snapshot: dict[str, Any]
    signal_id: int | None
    symbol: str
    state: str
    state_history: list[dict[str, Any]]
    auto_execute: bool
    entry_order_id: UUID | None
    exit_order_id: UUID | None
    exit_reason: str | None
    entry_at: datetime | None
    exit_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StrategyTradeResponse(BaseModel):
    """Immutable completed-trade record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    strategy_id: UUID
    user_id: int
    activation_id: UUID
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    total_charges: float
    net_pnl: float
    pnl_pct: float
    exit_reason: str
    strategy_version: int
    hold_duration_seconds: int
    entry_at: datetime
    exit_at: datetime
    created_at: datetime

    @field_validator(
        "entry_price", "exit_price", "gross_pnl",
        "total_charges", "net_pnl", "pnl_pct",
        mode="before",
    )
    @classmethod
    def coerce_decimals(cls, v: Any) -> float:
        return _to_float_required(v)


class StrategyBacktestRunResponse(BaseModel):
    """Backtest task status and result."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: int
    strategy_id: UUID
    strategy_snapshot: dict[str, Any]
    mode: str
    symbols: list[str]
    start_dt: datetime
    end_dt: datetime
    status: str
    progress_pct: int
    result_metrics: dict[str, Any] | None
    error_detail: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class StrategyPerformanceResponse(BaseModel):
    """Aggregated performance metrics for the strategy detail page."""

    strategy_id: UUID
    total_trades: int
    win_rate_pct: float | None
    avg_daily_return_pct: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    max_drawdown_pct: float | None
    total_net_pnl: float
    avg_hold_duration_seconds: float | None
    best_trade_pnl: float | None
    worst_trade_pnl: float | None


# ──────────────────────────────────────────────────────────────────────────────
# List / Paginated Responses
# ──────────────────────────────────────────────────────────────────────────────

class StrategiesListResponse(BaseModel):
    items: list[StrategySummaryResponse]
    total: int
    page: int
    page_size: int


class SubscriptionsListResponse(BaseModel):
    items: list[UserStrategySubscriptionResponse]
    total: int


class ActivationsListResponse(BaseModel):
    items: list[StrategyActivationResponse]
    total: int
    page: int
    page_size: int


class TradesListResponse(BaseModel):
    items: list[StrategyTradeResponse]
    total: int
    page: int
    page_size: int
    cursor: str | None = None


class BacktestRunsListResponse(BaseModel):
    items: list[StrategyBacktestRunResponse]
    total: int


class ManualExitRequest(BaseModel):
    """
    Manually exit an IN_TRADE or PARTIAL_EXIT activation.

    exit_price is required when auto_execute=False (no paper position to
    derive a fill price from).  When auto_execute=True the current LTP is
    used as the exit price and this field is ignored.
    """

    exit_price: float | None = Field(
        default=None,
        gt=0,
        description="Exit price for the trade record (required for manual activations)",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Admin — Strategy Governance
# ──────────────────────────────────────────────────────────────────────────────

class AdminStrategyItemResponse(BaseModel):
    """
    Strategy row as seen by admins — includes creator info and full metrics.

    Returned in the admin governance panel.  creator_username is resolved via
    a JOIN to the users table in the service layer so it survives caching.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_by: int
    creator_username: str | None = None
    name: str
    description: str | None
    tags: list[str] | None
    scope: str
    status: str
    version: int
    total_subscribers: int
    avg_daily_return_pct: float | None
    win_rate_pct: float | None
    total_trades: int
    created_at: datetime
    updated_at: datetime

    @field_validator("avg_daily_return_pct", "win_rate_pct", mode="before")
    @classmethod
    def coerce_metrics(cls, v: Any) -> float | None:
        return _to_float(v)


class AdminStrategiesListResponse(BaseModel):
    items: list[AdminStrategyItemResponse]
    total: int
    page: int
    page_size: int


class PromoteStrategyRequest(BaseModel):
    """Body for admin promote / demote."""

    reason: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Human-readable justification recorded in the audit log",
    )


class StrategyAuditLogResponse(BaseModel):
    """Single audit log entry."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: UUID | None
    admin_user_id: int | None
    admin_username: str | None = None
    action: str
    reason: str
    scope_before: str
    scope_after: str
    strategy_name: str
    created_at: datetime


class StrategyAuditLogListResponse(BaseModel):
    items: list[StrategyAuditLogResponse]
    total: int

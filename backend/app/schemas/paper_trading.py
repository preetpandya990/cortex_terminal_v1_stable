"""
Paper Trading & Audit — Pydantic v2 Schemas
=============================================
Request validation, response serialisation, and WebSocket payload types
for the Paper Trading subsystem.

Schema hierarchy:

  Enums
    PortfolioType, TransactionType, ProductType, OrderType,
    OrderValidity, OrderStatus, PositionSide, PositionStatus, ExitReason

  Requests  (inbound — strict validation)
    CreatePortfolioRequest
    UpdatePortfolioSettingsRequest
    PlaceOrderRequest
    ClosePositionRequest

  Responses  (outbound — ORM → JSON serialisation)
    PortfolioResponse
    PortfolioSummaryResponse      (GET /portfolios/me — enriched with live stats)
    PaperOrderResponse
    PaperFillResponse
    PaperPositionResponse
    PaperPositionDetailResponse   (GET /positions/{id} — includes fills + outcome)
    PaperTradeOutcomeResponse
    PaperPnlSnapshotResponse

  List / paginated responses
    OrdersListResponse            (cursor-paginated)
    PositionsListResponse
    OutcomesListResponse          (cursor-paginated)
    PnlSnapshotsListResponse

  Utility responses
    QtySuggestionResponse         (GET /qty-suggestion)
    OutcomeStatsResponse          (GET /outcomes/stats — ML feedback dashboard)

  Real-time
    LivePositionPnL               (single position in WebSocket frame)
    LivePnLUpdate                 (full portfolio WebSocket push payload)

Design rules:
  - All Decimal DB columns surface as float in JSON (pydantic coerces via
    field_validator mode="before") — this is intentional and matches the rest
    of the API surface.
  - Request schemas use strict field constraints with actionable error messages.
  - Response schemas set from_attributes=True so FastAPI can pass ORM objects
    directly without manual mapping.
  - No Optional fields on required request body fields — fail loudly, fail early.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# Type aliases
# ──────────────────────────────────────────────────────────────────────────────

# Non-negative monetary float (used across many response fields)
MoneyFloat = Annotated[float, Field(ge=0.0)]


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class PortfolioType(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    CNC = "CNC"       # Delivery (T+1 settlement)
    MIS = "MIS"       # Intraday (auto-squared off at 15:15)
    NRML = "NRML"     # F&O carry-forward


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"         # Stop-loss limit
    SL_M = "SL-M"     # Stop-loss market


class OrderValidity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"       # Immediate or Cancel


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    SL = "SL"
    MANUAL = "MANUAL"
    EXPIRED = "EXPIRED"


# ──────────────────────────────────────────────────────────────────────────────
# Shared Decimal → float converter
# Used as a reusable validator across all response schemas
# ──────────────────────────────────────────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    """Coerce Decimal (or anything numeric) to float for JSON serialisation."""
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
# Request Schemas
# ──────────────────────────────────────────────────────────────────────────────

class CreatePortfolioRequest(BaseModel):
    """
    Create a paper trading portfolio.

    initial_capital is immutable after creation — it anchors the baseline
    for return-on-capital calculations throughout the portfolio's lifetime.
    """

    name: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Human-readable portfolio name",
        examples=["My Paper Portfolio"],
    )
    initial_capital: float = Field(
        ...,
        gt=0,
        le=100_000_000,
        description="Starting capital in INR (immutable after creation)",
        examples=[500_000.0],
    )
    risk_per_trade_pct: float = Field(
        default=2.0,
        gt=0,
        le=10,
        description=(
            "Percentage of current cash risked per trade (0–10). "
            "Drives the system-suggested quantity calculation: "
            "qty = floor(cash × risk_pct / 100 / (entry − stop_loss))"
        ),
        examples=[2.0],
    )
    max_open_positions: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of concurrent open positions",
        examples=[10],
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class UpdatePortfolioSettingsRequest(BaseModel):
    """
    Update mutable portfolio settings (risk parameters only).

    initial_capital and name are intentionally excluded — capital is
    immutable, and name changes would break external references.
    """

    risk_per_trade_pct: float | None = Field(
        default=None,
        gt=0,
        le=10,
        description="Updated risk percentage per trade (0–10)",
    )
    max_open_positions: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Updated maximum open positions cap",
    )

    @model_validator(mode="after")
    def at_least_one_field(self) -> "UpdatePortfolioSettingsRequest":
        if self.risk_per_trade_pct is None and self.max_open_positions is None:
            raise ValueError(
                "At least one of risk_per_trade_pct or max_open_positions must be provided."
            )
        return self


class PlaceOrderRequest(BaseModel):
    """
    Place a paper order derived from a TradeSuggestion.

    The suggestion_id links the order back to the originating signal so
    the audit trail remains complete from ML prediction → human decision →
    simulated execution.

    Validation rules:
      - LIMIT and SL orders require price
      - SL and SL-M orders require trigger_price
      - price and trigger_price must be positive when provided
    """

    suggestion_id: UUID = Field(
        ...,
        description="ID of the TradeSuggestion this order originates from",
    )
    transaction_type: TransactionType = Field(
        ...,
        description="BUY or SELL",
    )
    product_type: ProductType = Field(
        ...,
        description="CNC (delivery) | MIS (intraday) | NRML (F&O carry)",
    )
    order_type: OrderType = Field(
        ...,
        description="MARKET | LIMIT | SL | SL-M",
    )
    validity: OrderValidity = Field(
        default=OrderValidity.DAY,
        description="DAY (valid for the full session) | IOC (immediate or cancel)",
    )
    quantity: int = Field(
        ...,
        gt=0,
        le=1_000_000,
        description="Number of shares (user-confirmed, may differ from suggested qty)",
    )
    price: float | None = Field(
        default=None,
        gt=0,
        description="Limit price (required for LIMIT and SL orders)",
    )
    trigger_price: float | None = Field(
        default=None,
        gt=0,
        description="Stop-loss trigger price (required for SL and SL-M orders)",
    )

    @model_validator(mode="after")
    def validate_price_requirements(self) -> "PlaceOrderRequest":
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.price is None:
            raise ValueError(
                f"{self.order_type.value} orders require a price."
            )
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger_price is None:
            raise ValueError(
                f"{self.order_type.value} orders require a trigger_price."
            )
        return self


class ClosePositionRequest(BaseModel):
    """
    Close a position (fully or partially).

    quantity must not exceed the currently open quantity — the service
    layer enforces this with a read-then-validate pattern inside a
    serialisable transaction.
    """

    quantity: int = Field(
        ...,
        gt=0,
        description=(
            "Number of shares to close. Must be ≤ current open quantity. "
            "Pass the full open quantity for a complete close."
        ),
    )
    order_type: OrderType = Field(
        default=OrderType.MARKET,
        description="MARKET (fill at next tick) | LIMIT (fill when price is hit)",
    )
    price: float | None = Field(
        default=None,
        gt=0,
        description="Limit price for LIMIT close orders",
    )
    exit_reason: ExitReason = Field(
        default=ExitReason.MANUAL,
        description="Reason for closing this position",
    )

    @model_validator(mode="after")
    def validate_limit_price(self) -> "ClosePositionRequest":
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT close orders require a price.")
        return self


# ──────────────────────────────────────────────────────────────────────────────
# Response Schemas
# ──────────────────────────────────────────────────────────────────────────────

class PortfolioResponse(BaseModel):
    """Portfolio record serialised from ORM."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    user_id: int
    portfolio_type: PortfolioType
    name: str
    initial_capital: float
    current_cash: float
    currency: str
    risk_per_trade_pct: float
    max_open_positions: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "initial_capital", "current_cash", "risk_per_trade_pct",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: Any) -> float:
        return _to_float_required(v)


class PortfolioSummaryResponse(BaseModel):
    """
    Enriched portfolio response for GET /portfolios/me.

    Extends the base portfolio record with live aggregate stats computed
    at request time (open position count, aggregate unrealized P&L, etc.).
    These fields are read from the DB / Redis cache, not the portfolios table.
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    # Core portfolio record
    id: UUID
    user_id: int
    portfolio_type: PortfolioType
    name: str
    initial_capital: float
    current_cash: float
    currency: str
    risk_per_trade_pct: float
    max_open_positions: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Live aggregate stats (not on the ORM model — populated by the service)
    open_position_count: int = Field(
        default=0, description="Number of currently open positions"
    )
    total_unrealized_pnl: float = Field(
        default=0.0, description="Sum of unrealized P&L across all open positions"
    )
    total_realized_pnl: float = Field(
        default=0.0, description="Cumulative realised P&L from all closed trades"
    )
    portfolio_value: float = Field(
        default=0.0,
        description="current_cash + sum of open position market values",
    )
    total_return_pct: float = Field(
        default=0.0,
        description="(portfolio_value − initial_capital) / initial_capital × 100",
    )
    total_trades: int = Field(
        default=0, description="Total closed trades (all time)"
    )
    win_rate: float | None = Field(
        default=None, description="Percentage of closed trades with net_pnl > 0"
    )

    @field_validator(
        "initial_capital", "current_cash", "risk_per_trade_pct",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: Any) -> float:
        return _to_float_required(v)


class PaperOrderResponse(BaseModel):
    """Paper order record serialised from ORM."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    portfolio_id: UUID
    suggestion_id: UUID | None
    symbol: str
    instrument_key: str
    transaction_type: TransactionType
    product_type: ProductType
    order_type: OrderType
    validity: OrderValidity
    quantity: int
    price: float | None
    trigger_price: float | None
    status: OrderStatus
    rejection_reason: str | None
    placed_at: datetime
    updated_at: datetime

    @field_validator("price", "trigger_price", mode="before")
    @classmethod
    def coerce_optional_decimal(cls, v: Any) -> float | None:
        return _to_float(v)


class PaperFillResponse(BaseModel):
    """Immutable fill record serialised from ORM."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    order_id: UUID
    portfolio_id: UUID
    symbol: str
    fill_quantity: int
    fill_price: float
    slippage_bps: float

    # NSE charge breakdown
    brokerage: float
    stt: float
    exchange_charges: float
    sebi_charges: float
    gst: float
    stamp_duty: float
    total_charges: float
    net_amount: float

    settlement_date: date
    executed_at: datetime

    @field_validator(
        "fill_price", "slippage_bps",
        "brokerage", "stt", "exchange_charges", "sebi_charges",
        "gst", "stamp_duty", "total_charges", "net_amount",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: Any) -> float:
        return _to_float_required(v)


class PaperPositionResponse(BaseModel):
    """Live position state serialised from ORM."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    portfolio_id: UUID
    suggestion_id: UUID | None
    symbol: str
    instrument_key: str
    quantity: int
    avg_cost_price: float
    last_price: float | None
    unrealized_pnl: float | None
    realized_pnl: float
    total_charges: float
    side: PositionSide
    target_price_1: float | None
    target_price_2: float | None
    target_price_3: float | None
    stop_loss: float | None
    status: PositionStatus
    opened_at: datetime
    closed_at: datetime | None
    updated_at: datetime

    @field_validator(
        "avg_cost_price", "realized_pnl", "total_charges",
        mode="before",
    )
    @classmethod
    def coerce_decimal_required(cls, v: Any) -> float:
        return _to_float_required(v)

    @field_validator(
        "last_price", "unrealized_pnl",
        "target_price_1", "target_price_2", "target_price_3", "stop_loss",
        mode="before",
    )
    @classmethod
    def coerce_decimal_optional(cls, v: Any) -> float | None:
        return _to_float(v)


class PaperPositionDetailResponse(BaseModel):
    """
    Detailed position view for GET /positions/{id}.

    Includes the fill history so the client can reconstruct the exact
    WAC calculation and display per-fill charge attribution.
    """

    model_config = ConfigDict(protected_namespaces=())

    position: PaperPositionResponse
    fills: list[PaperFillResponse] = Field(
        default_factory=list,
        description="All fills contributing to this position (chronological)",
    )
    outcome: "PaperTradeOutcomeResponse | None" = Field(
        default=None,
        description="Audit outcome record (populated only after position is CLOSED)",
    )
    fill_count: int = Field(ge=0)


class PaperTradeOutcomeResponse(BaseModel):
    """Append-only audit/ML-feedback record serialised from ORM."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    portfolio_id: UUID
    position_id: UUID
    suggestion_id: UUID | None
    user_id: int
    symbol: str
    signal_direction: TransactionType
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    total_charges: float
    net_pnl: float
    pnl_pct: float
    hold_duration_seconds: int
    exit_reason: ExitReason

    # Suggestion snapshot (may be None if order was placed without a suggestion)
    suggested_entry_price: float | None
    suggested_stop_loss: float | None
    suggested_tp1: float | None
    suggested_tp2: float | None
    suggested_tp3: float | None
    suggestion_consensus_score: float | None
    suggestion_confidence_level: str | None

    entry_slippage_pct: float | None

    # ML feedback
    ml_direction_correct: bool | None
    hit_tp1: bool
    hit_tp2: bool
    hit_tp3: bool
    hit_sl: bool
    market_regime_at_entry: str | None

    created_at: datetime

    @field_validator(
        "entry_price", "exit_price", "gross_pnl",
        "total_charges", "net_pnl", "pnl_pct",
        mode="before",
    )
    @classmethod
    def coerce_decimal_required(cls, v: Any) -> float:
        return _to_float_required(v)

    @field_validator(
        "suggested_entry_price", "suggested_stop_loss",
        "suggested_tp1", "suggested_tp2", "suggested_tp3",
        "suggestion_consensus_score", "entry_slippage_pct",
        mode="before",
    )
    @classmethod
    def coerce_decimal_optional(cls, v: Any) -> float | None:
        return _to_float(v)


class PaperPnlSnapshotResponse(BaseModel):
    """Daily EOD P&L snapshot serialised from ORM."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    portfolio_id: UUID
    snapshot_date: date
    total_realized_pnl: float
    total_unrealized_pnl: float
    portfolio_value: float
    cash_balance: float
    num_open_positions: int
    num_trades_today: int
    win_rate: float | None
    nifty_close: float | None
    captured_at: datetime

    @field_validator(
        "total_realized_pnl", "total_unrealized_pnl",
        "portfolio_value", "cash_balance",
        mode="before",
    )
    @classmethod
    def coerce_decimal_required(cls, v: Any) -> float:
        return _to_float_required(v)

    @field_validator("win_rate", "nifty_close", mode="before")
    @classmethod
    def coerce_decimal_optional(cls, v: Any) -> float | None:
        return _to_float(v)


# Resolve forward reference in PaperPositionDetailResponse
PaperPositionDetailResponse.model_rebuild()


# ──────────────────────────────────────────────────────────────────────────────
# List / Paginated Responses
# ──────────────────────────────────────────────────────────────────────────────

class OrdersListResponse(BaseModel):
    """
    Cursor-paginated list of paper orders.

    Ordered by placed_at DESC. Supports the same cursor mechanism as
    the trade_suggestions endpoint.
    """

    model_config = ConfigDict(protected_namespaces=())

    orders: list[PaperOrderResponse]
    total: int | None = Field(
        default=None,
        ge=0,
        description="Exact total (offset mode only)",
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for next page",
    )
    has_more: bool
    limit: int = Field(ge=1, le=100)


class PositionsListResponse(BaseModel):
    """
    List of positions filtered by status.

    Not paginated — a portfolio will rarely exceed max_open_positions (≤100)
    simultaneously, so a full list response is always safe.
    """

    model_config = ConfigDict(protected_namespaces=())

    positions: list[PaperPositionResponse]
    open_count: int = Field(ge=0)
    closed_count: int = Field(ge=0)
    total_unrealized_pnl: float = Field(
        default=0.0,
        description="Sum of unrealized P&L across all OPEN positions in this response",
    )


class OutcomesListResponse(BaseModel):
    """
    Cursor-paginated list of trade outcomes (audit log).

    Admin / Dev role required. Ordered by created_at DESC.
    """

    model_config = ConfigDict(protected_namespaces=())

    outcomes: list[PaperTradeOutcomeResponse]
    total: int | None = Field(default=None, ge=0)
    next_cursor: str | None = None
    has_more: bool
    limit: int = Field(ge=1, le=100)


class PnlSnapshotsListResponse(BaseModel):
    """Historical daily P&L snapshots for a portfolio."""

    model_config = ConfigDict(protected_namespaces=())

    snapshots: list[PaperPnlSnapshotResponse]
    count: int = Field(ge=0)
    from_date: date | None = None
    to_date: date | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Utility Responses
# ──────────────────────────────────────────────────────────────────────────────

class QtySuggestionResponse(BaseModel):
    """
    System-suggested quantity for a given suggestion and portfolio.

    Formula:
        raw_qty  = (current_cash × risk_per_trade_pct / 100)
                   / (entry_price − stop_loss)
        suggested_qty = max(1, min(floor(raw_qty), max_affordable_qty))

    Returns all intermediate values so the UI can display a clear rationale
    (e.g. "Risking ₹10,000 on a ₹50 stop = 200 shares").
    """

    model_config = ConfigDict(protected_namespaces=())

    suggested_qty: int = Field(ge=1, description="System-recommended quantity")
    max_affordable_qty: int = Field(
        ge=1, description="Maximum shares affordable with current cash"
    )
    capital_at_risk: float = Field(
        ge=0, description="INR amount at risk based on suggested_qty and stop distance"
    )
    risk_pct_of_cash: float = Field(
        ge=0,
        le=100,
        description="Percentage of current cash at risk with suggested_qty",
    )
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    stop_distance: float = Field(
        gt=0, description="Absolute INR distance between entry and stop"
    )
    current_cash: float = Field(ge=0)
    risk_per_trade_pct: float = Field(gt=0, le=10)
    note: str | None = Field(
        default=None,
        description="Warning if suggestion lacks stop_loss (qty calc falls back to 1%)",
    )


class OutcomeStatsBreakdown(BaseModel):
    """Statistics for a single segment (confidence level, regime, exit reason)."""

    model_config = ConfigDict(protected_namespaces=())

    count: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0.0, le=100.0)
    avg_net_pnl: float | None = None
    avg_pnl_pct: float | None = None
    ml_direction_accuracy: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Fraction of trades where ML predicted the correct direction",
    )


class OutcomeStatsResponse(BaseModel):
    """
    Aggregated ML feedback statistics for the Admin / Dev dashboard.

    Designed to answer questions like:
      - "Are HIGH-confidence signals actually more profitable?"
      - "Which market regime performs best?"
      - "How often does the ML direction prediction prove correct?"
      - "What % of signals reach TP1 vs getting stopped out?"

    All stats are scoped to the requesting user's portfolio(s) unless the
    caller has admin role, in which case platform-wide stats are returned.
    """

    model_config = ConfigDict(protected_namespaces=())

    # Totals
    total_trades: int = Field(ge=0)
    total_winners: int = Field(ge=0)
    total_losers: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0.0, le=100.0)

    # P&L
    total_gross_pnl: float = 0.0
    total_charges: float = 0.0
    total_net_pnl: float = 0.0
    avg_net_pnl_per_trade: float | None = None
    avg_pnl_pct: float | None = None
    avg_hold_duration_hours: float | None = Field(
        default=None, description="Average position hold time in hours"
    )

    # ML accuracy
    ml_direction_accuracy: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Fraction of trades where ML direction was correct (excludes NULL)",
    )

    # Target / stop outcomes
    tp1_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    tp2_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    tp3_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    sl_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    # Exit reason distribution
    by_exit_reason: dict[str, int] = Field(
        default_factory=dict,
        description="Count of trades per exit reason (TP1/TP2/TP3/SL/MANUAL/EXPIRED)",
    )

    # Segmented breakdowns (keyed by segment value)
    by_confidence_level: dict[str, OutcomeStatsBreakdown] = Field(
        default_factory=dict,
        description="Stats broken down by suggestion confidence level (HIGH/MEDIUM/LOW)",
    )
    by_market_regime: dict[str, OutcomeStatsBreakdown] = Field(
        default_factory=dict,
        description="Stats broken down by market regime at trade entry",
    )
    by_signal_direction: dict[str, OutcomeStatsBreakdown] = Field(
        default_factory=dict,
        description="Stats broken down by signal direction (BUY/SELL)",
    )

    # Date range of the underlying data
    from_date: datetime | None = None
    to_date: datetime | None = None
    generated_at: datetime


# ──────────────────────────────────────────────────────────────────────────────
# Real-time WebSocket Payloads
# ──────────────────────────────────────────────────────────────────────────────

class LivePositionPnL(BaseModel):
    """
    Single position snapshot in a WebSocket P&L push frame.

    Kept deliberately compact — only what the frontend needs to update
    a live P&L row without re-fetching the full position.
    """

    model_config = ConfigDict(protected_namespaces=())

    position_id: UUID
    symbol: str
    quantity: int
    side: PositionSide
    avg_cost_price: float
    last_price: float
    unrealized_pnl: float
    pnl_pct: float = Field(description="(last_price − avg_cost) / avg_cost × 100")
    stop_loss: float | None = None
    target_price_1: float | None = None


class LivePnLUpdate(BaseModel):
    """
    Full portfolio P&L push — emitted by the P&L recompute worker
    at ~500 ms intervals to cai:paper:pnl:{portfolio_id}.

    Frontend WebSocket clients receive this frame and update every
    open position row simultaneously from a single payload.
    """

    model_config = ConfigDict(protected_namespaces=())

    portfolio_id: UUID
    positions: list[LivePositionPnL]

    # Aggregate portfolio stats
    total_unrealized_pnl: float
    total_realized_pnl: float
    current_cash: float
    portfolio_value: float = Field(
        description="current_cash + sum of open position market values"
    )
    total_return_pct: float = Field(
        description="(portfolio_value − initial_capital) / initial_capital × 100"
    )

    # ISO 8601 timestamp of this frame (UTC)
    ts: datetime

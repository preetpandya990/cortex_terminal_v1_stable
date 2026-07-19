"""
Portfolio Insight & Advise — API schemas
=========================================
Response models for the read-only advisory layer.  ``PortfolioInsightStats`` is
the ``GET /portfolio-insight/stats`` payload (workstream B4); ``PortfolioAdvice``
/ ``PerPositionNote`` (added in B5) are the on-demand AI-advice payload.

Every figure is a plain float rounded for JSON.  The service reports honest gaps
explicitly (positions without a stop, names excluded from correlation, etc.)
rather than silently guessing — see the per-stat fields and the top-level
``notes`` list.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ── Capital at risk ─────────────────────────────────────────────────────────────

class CapitalAtRiskStat(BaseModel):
    """
    Aggregate defined risk: Σ |avg_cost − stop_loss| × qty across positions that
    have a stop, expressed in INR and as a % of portfolio value.  Positions with
    no stop are excluded (their risk is undefined) and counted separately.
    """
    capital_at_risk: float = Field(description="Σ |avg_cost − stop| × qty over stopped positions (INR)")
    capital_at_risk_pct: float = Field(description="capital_at_risk ÷ portfolio_value × 100")
    positions_with_stop: int = Field(ge=0)
    positions_without_stop: int = Field(ge=0, description="Open positions with no stop — excluded from CaR (honest gap)")


# ── Single-name concentration ───────────────────────────────────────────────────

class HoldingWeight(BaseModel):
    symbol: str
    weight_pct: float = Field(description="Position market value ÷ portfolio_value × 100")


class SingleNameConcentration(BaseModel):
    """
    Single-name concentration by market-value weight, plus the industry-standard
    Herfindahl-Hirschman Index and its inverse (effective number of positions).
    """
    max_weight_pct: float = Field(description="Largest single-name weight (%)")
    max_weight_symbol: str | None = None
    hhi: float = Field(description="Herfindahl-Hirschman Index Σ wᵢ² (1/n … 1); higher ⇒ more concentrated")
    effective_positions: float = Field(description="1 / HHI — how many equal-weight names the book behaves like")
    top_holdings: list[HoldingWeight] = Field(default_factory=list, description="Largest holdings by weight (desc)")


# ── Sector concentration ────────────────────────────────────────────────────────

class SectorWeight(BaseModel):
    sector: str
    weight_pct: float


class SectorConcentration(BaseModel):
    """
    Weight by sector (resolved via fundamentals, then a symbol map, else
    'Unclassified').  Sector coverage is best-effort where fundamentals exist.
    """
    max_sector: str | None = None
    max_sector_weight_pct: float = Field(description="Weight of the most-concentrated sector (%)")
    unclassified_weight_pct: float = Field(description="Weight of positions with no resolvable sector (%)")
    breakdown: list[SectorWeight] = Field(default_factory=list, description="Weight per sector (desc)")


# ── Correlation ─────────────────────────────────────────────────────────────────

class CorrelationStat(BaseModel):
    """
    Pairwise correlation of daily log returns across open-position instruments.
    Names with insufficient overlapping history are excluded (honest gap).
    """
    max_pair_correlation: float | None = Field(default=None, description="Highest pairwise correlation in [-1, 1]")
    max_pair: list[str] | None = Field(default=None, description="The two symbols achieving max_pair_correlation")
    avg_pairwise_correlation: float | None = Field(default=None, description="Mean of all pairwise correlations")
    covered_positions: int = Field(ge=0, description="Instruments with enough history to correlate")
    excluded_positions: int = Field(ge=0, description="Instruments excluded for short history")
    window_days: int = Field(description="Trailing daily-return window used")


# ── Stress scan ─────────────────────────────────────────────────────────────────

class StressScenario(BaseModel):
    key: str = Field(description="Stable scenario id (index_down, sector_down, vol_double)")
    label: str
    delta_pct: float = Field(description="Portfolio value change under the scenario (%)")
    detail: str | None = Field(default=None, description="Scenario context, e.g. the sector shocked")


class StressScan(BaseModel):
    scenarios: list[StressScenario] = Field(default_factory=list)


# ── Top-level ───────────────────────────────────────────────────────────────────

class PortfolioInsightStats(BaseModel):
    """``GET /portfolio-insight/stats`` response — the portfolio-level risk panel."""

    model_config = ConfigDict(protected_namespaces=())

    portfolio_id: UUID
    portfolio_value: float
    open_position_count: int = Field(ge=0)

    capital_at_risk: CapitalAtRiskStat
    single_name: SingleNameConcentration
    sector: SectorConcentration
    correlation: CorrelationStat
    stress: StressScan

    notes: list[str] = Field(
        default_factory=list,
        description="Human-readable honest-gap notes (unstopped positions, excluded names, missing sectors).",
    )
    computed_at: datetime


# ── AI advice (POST /advice) ────────────────────────────────────────────────────

class PerPositionNote(BaseModel):
    """A short, actionable note about one holding."""
    symbol: str
    note: str


class PortfolioAdviceGeneration(BaseModel):
    """
    The LLM-authored content of the advice — the exact JSON schema handed to
    Gemini's structured-output mode.  Deliberately holds *only* generated fields;
    server-set fields (disclaimer, staleness, timestamps) live on
    ``PortfolioAdvice`` so the model is never asked to invent them.
    """
    assessment: str = Field(description="2–4 sentence overall risk-posture assessment of the book")
    key_risks: list[str] = Field(default_factory=list, description="The most important portfolio risks, most severe first")
    considerations: list[str] = Field(default_factory=list, description="Actionable considerations for the user to weigh (they decide)")
    per_position: list[PerPositionNote] = Field(default_factory=list, description="Short actionable note per notable holding")


class PortfolioAdvice(BaseModel):
    """
    ``POST /portfolio-insight/advice`` response — LLM-authored assessment plus
    server-controlled disclaimer and provenance.  Never 500s: on quota/rate-limit
    exhaustion the last-cached advice is returned with ``stale=True``.
    """

    model_config = ConfigDict(protected_namespaces=())

    assessment: str
    key_risks: list[str] = Field(default_factory=list)
    considerations: list[str] = Field(default_factory=list)
    per_position: list[PerPositionNote] = Field(default_factory=list)

    disclaimer: str = Field(description="Fixed regulatory notice (server-set, never LLM-generated)")
    stale: bool = Field(
        default=False,
        description="True when served from cache during a quota/rate-limit degrade (not freshly generated)",
    )
    generated_at: datetime
    model_id: str | None = Field(default=None, description="LLM model id that produced the advice, for provenance")

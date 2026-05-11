"""
Seed Strategy_001: 9/21 EMA Pullback into EMA Zone

Source: Sample_Stratagy.docx
Scope:  shared (global library, visible to all users)
Owner:  admin@cortex.local (user_id=1)

Original rules → system mapping
────────────────────────────────
9 EMA above 21 EMA      → ema_20 > ema_50  (closest available EMAs)
Price pulls into zone   → ltp between ema_50 and ema_20 (inclusive)
Bullish reversal signal → action == "BUY" AND confidence_score >= 65
SL: 2% below entry      → StopLossConfig fixed_pct=2.0
TP1: 50% at 1:2 R/R     → 4% gain, exit_fraction=0.5
TP2: remaining 50%      → 8% gain, exit_fraction=0.5  (trailing-stop approximation;
                           original: exit on close below 9 EMA — unsupported by FSM)

Run:
    cd backend
    python scripts/seed_strategy_001.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow imports from the backend package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.strategies import Strategy

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

STRATEGY_NAME = "Strategy_001"

DEFINITION: dict = {
    "signal_gates": {
        "allowed_symbols": [],
        "blocked_symbols": [],
        "allowed_directions": ["BUY"],
        "allowed_timeframes": [],
        "min_market_cap_cr": None,
        "allowed_regimes": ["trending_up"],
    },
    # ── Signal-level filter ────────────────────────────────────────────────
    # Requires a BUY signal with at least 65% confidence before the more
    # expensive indicator tree is evaluated.
    "signal_condition": {
        "type": "group",
        "operator": "AND",
        "children": [
            {
                "type": "leaf",
                "field": "action",
                "op": "==",
                "value": "BUY",
            },
            {
                "type": "leaf",
                "field": "confidence_score",
                "op": ">=",
                "value": 65.0,
            },
        ],
    },
    # ── Indicator-level conditions ─────────────────────────────────────────
    # Three-part entry rule:
    #   1. ema_20 > ema_50  — uptrend (9 EMA above 21 EMA)
    #   2. ltp >= ema_50    — price has not broken below 21 EMA (still in zone)
    #   3. ltp <= ema_20    — price pulled back to/below 9 EMA (in the EMA zone)
    "indicator_condition": {
        "type": "group",
        "operator": "AND",
        "children": [
            {
                "type": "leaf",
                "field": "ema_20",
                "op": ">",
                "value": "ema_50",
            },
            {
                "type": "leaf",
                "field": "ltp",
                "op": ">=",
                "value": "ema_50",
            },
            {
                "type": "leaf",
                "field": "ltp",
                "op": "<=",
                "value": "ema_20",
            },
        ],
    },
    # ── Risk management ────────────────────────────────────────────────────
    "stop_loss": {
        "type": "fixed_pct",
        "value": 2.0,
        "atr_period": 14,
        "trail": False,
    },
    "take_profit": {
        "levels": [
            # TP1 — 50% of position at 1:2 R/R (4% gain vs 2% stop)
            {"pct": 4.0, "exit_fraction": 0.5},
            # TP2 — remaining 50% at 1:4 R/R (approximates the original
            # trailing-stop rule: exit remaining when price closes below 9 EMA)
            {"pct": 8.0, "exit_fraction": 0.5},
        ]
    },
    # ── Position sizing ────────────────────────────────────────────────────
    "position_sizing": {
        "mode": "risk_pct",
        "risk_pct": 2.0,
        "max_position_pct": 10.0,
    },
    # ── Guardrails ─────────────────────────────────────────────────────────
    "guardrails": {
        "max_concurrent_activations": 3,
        "min_confidence_score": 65.0,
        "allowed_sessions": [],
        "require_volume_confirm": False,
        "max_daily_loss_pct": 4.0,
    },
}


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # Idempotent — skip if already seeded.
        existing = await db.scalar(
            select(Strategy).where(Strategy.name == STRATEGY_NAME)
        )
        if existing is not None:
            log.info("Strategy_001 already exists (id=%s) — nothing to do.", existing.id)
            return

        strategy = Strategy(
            created_by=1,           # admin@cortex.local
            name=STRATEGY_NAME,
            description=(
                "9/21 EMA Pullback into EMA Zone\n\n"
                "Trend-following entry that waits for price to pull back into the "
                "9/21 EMA zone during an established uptrend, then enters on a "
                "confirmed BUY signal.\n\n"
                "Entry: ema_20 > ema_50 (uptrend) + price between both EMAs + "
                "BUY signal ≥ 65% confidence.\n"
                "Exit: SL 2% below entry | TP1 4% (50% position) | "
                "TP2 8% (remaining 50%)."
            ),
            tags=["ema", "pullback", "trend-following", "equity"],
            scope="shared",
            status="active",
            version=1,
            definition=DEFINITION,
            total_subscribers=0,
            total_trades=0,
        )
        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)
        log.info("✓ Strategy_001 seeded (id=%s, scope=shared, status=active).", strategy.id)


if __name__ == "__main__":
    asyncio.run(main())

# PATH: backend/scripts/calibrate_rag_relevance_floor.py
"""
RAG relevance-floor calibration harness.
=========================================
Derives an empirical value for RAG_MIN_GENERIC_COSINE_SIMILARITY (see
core/config.py and app.ai.rag.retriever's relevance gate) from real corpus
data instead of guessing a number — following the golden-set-plus-margin
methodology this fix requires (see NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md).

Method
------
For a small hand-picked golden set:
  - NEGATIVE anchors: symbols with zero exact-tier news coverage that we
    already know, from the real corpus, pull in topically unrelated
    "generic" (symbol IS NULL) filler — e.g. COMSYN, the confirmed incident
    symbol (SK Hynix Nasdaq-debut noise).
  - POSITIVE anchors: symbols with a KNOWN_GOOD_HEADLINE_NEEDLE — a
    substring confirmed (by manually reading the matched headline, not
    assumed) to identify an article genuinely, specifically about that
    company. The needle is what makes a positive anchor's score trustworthy:
    the script reports the score of the ACTUAL confirmed-genuine article,
    not just whatever ranks highest by cosine — a lesson learned the hard
    way during the first calibration pass, where several "top-1" hits
    turned out to be generic sector chatter (e.g. "Private banks poised for
    a rally") that merely outscored the real, genuine article for that
    company on pure cosine similarity. Trusting raw top-1 without this
    manual verification silently recommends the wrong number.

For each anchor we build the exact query explanation_worker.py now builds
(symbol + company name + sector + boilerplate), embed it via the real
embed_query() path (same cache, same model), load the FULL "generic"-tier
candidate pool via retriever._load_candidates (limit=_CALIBRATION_LOAD_LIMIT,
deliberately larger than production's _MAX_CANDIDATES=500 — the production
cap exists to bound a single 24h-window request's latency, but calibration
over a widened 168h window must not silently truncate older-but-in-window
genuine matches out of the sample), and score every candidate with
retriever._cosine_similarities — the SAME function the relevance gate uses
at request time, so calibration measures exactly what production will
enforce.

Output: full similarity distribution per anchor, the confirmed-genuine
match's score (positive anchors) or top candidates (negative anchors), so a
human can visually re-verify. Recommends a threshold only when negative and
positive distributions cleanly separate; otherwise exits 1 rather than
rubber-stamping a number.

Usage
-----
  cd backend
  .venv/bin/python -m scripts.calibrate_rag_relevance_floor
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

from app.ai.rag.embedder import embed_query
from app.ai.rag.retriever import _cosine_similarities, _load_candidates
from app.ai.rag.sector_resolver import resolve_sector_and_peers
from app.core.database import AsyncSessionLocal
from app.services.symbol_validator import symbol_validator

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("calibrate_rag_relevance_floor")

# ── Golden set ──────────────────────────────────────────────────────────────
# NEGATIVE: reproduces the real, confirmed bug (COMSYN <- SK Hynix noise).
NEGATIVE_ANCHORS: list[str] = ["COMSYN"]

# POSITIVE: symbol -> a lowercase headline substring, manually confirmed to
# identify an article genuinely, specifically about that company (verified
# 2026-07-11 against the real corpus — re-verify if the corpus changes
# materially). ITC was dropped from an earlier draft of this set: its only
# headline match was "ITC Hotels" — a separately-listed demerged entity, too
# ambiguous to count as a genuine ITC match.
POSITIVE_ANCHORS: dict[str, str] = {
    "TCS": "tcs",
    "SBIN": "sbi",
    "VEDL": "vedanta",
    "HDFCBANK": "hdfc bank",
    "RELIANCE": "reliance",
    "INDIANB": "indian bank",
    "TITAN": "titan",
    "LT": "l&t",
    "BHARTIARTL": "bharti airtel",
    "YESBANK": "yes bank",
    "INDUSINDBK": "indusind bank",
    "KOTAKBANK": "kotak",
    "INFY": "infosys",
}

_WINDOW_HOURS = 168  # widen beyond the 24h default to maximize corpus coverage for calibration
_CALIBRATION_LOAD_LIMIT = 2000  # see module docstring — avoid truncating the widened window
_TOP_N_SHOWN = 5


async def _load_generic_pool(db, symbol: str) -> tuple[str, list, np.ndarray]:
    company_name = await symbol_validator.get_company_name(symbol, db)
    sector, sector_peers = await resolve_sector_and_peers(db, symbol, company_name, None)
    query = " ".join(p for p in (symbol, company_name, sector, "market analysis news") if p)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_WINDOW_HOURS)
    candidates = await _load_candidates(
        db, symbol, cutoff, sector_peers=sector_peers, limit=_CALIBRATION_LOAD_LIMIT,
    )
    generic = [c for c in candidates if c.tier == "generic"]
    if not generic:
        return query, [], np.array([])

    query_vector = await embed_query(query)
    sims = _cosine_similarities(generic, query_vector)
    return query, generic, sims


async def _run() -> int:
    async with AsyncSessionLocal() as db:
        print("=" * 100)
        print("NEGATIVE ANCHORS (expect: low similarity — no genuine relevance to this pool)")
        print("=" * 100)
        neg_maxes: list[float] = []
        for symbol in NEGATIVE_ANCHORS:
            query, generic, sims = await _load_generic_pool(db, symbol)
            if sims.size == 0:
                print(f"\n[{symbol}] query={query!r} — no generic-tier candidates in window, skipping")
                continue
            neg_maxes.append(float(sims.max()))
            print(
                f"\n[{symbol}] query={query!r} n={sims.size} "
                f"min={sims.min():.4f} mean={sims.mean():.4f} "
                f"p95={np.percentile(sims, 95):.4f} max={sims.max():.4f}"
            )
            ranked = sorted(zip(sims.tolist(), generic), key=lambda t: -t[0])[:_TOP_N_SHOWN]
            for s, c in ranked:
                headline = c.content.split("\n", 1)[0][:100]
                print(f"    {s:.4f}  [{c.source_name}] {headline}")

        print("\n" + "=" * 100)
        print("POSITIVE ANCHORS (score of the manually-confirmed genuine article, not raw top-1)")
        print("=" * 100)
        pos_confirmed: list[float] = []
        for symbol, needle in POSITIVE_ANCHORS.items():
            query, generic, sims = await _load_generic_pool(db, symbol)
            if sims.size == 0:
                print(f"\n[{symbol}] query={query!r} — no generic-tier candidates in window, skipping")
                continue

            matches = [
                (s, c) for s, c in zip(sims.tolist(), generic)
                if needle in c.content.split("\n", 1)[0].lower()
            ]
            if not matches:
                print(f"\n[{symbol}] query={query!r} — NO headline match for {needle!r} in the "
                      f"candidate pool; drop this anchor or widen the window/limit")
                continue
            matches.sort(key=lambda t: -t[0])
            best_score, best_candidate = matches[0]
            pos_confirmed.append(best_score)
            print(f"\n[{symbol}] query={query!r} confirmed-genuine score={best_score:.4f}")
            headline = best_candidate.content.split("\n", 1)[0][:100]
            print(f"    {best_score:.4f}  [{best_candidate.source_name}] {headline}")

        print("\n" + "=" * 100)
        if not neg_maxes or not pos_confirmed:
            print("INCONCLUSIVE: one or both anchor groups produced no candidates — "
                  "widen the corpus window or golden set before calibrating.")
            return 1

        noise_ceiling = max(neg_maxes)
        signal_floor = min(pos_confirmed)
        print(f"Noise ceiling (max sim across negative anchors): {noise_ceiling:.4f}")
        print(f"Signal floor (min confirmed-genuine sim across positive anchors): {signal_floor:.4f}")

        if signal_floor <= noise_ceiling:
            print(
                "\nNO CLEAN SEPARATION — noise ceiling meets or exceeds signal floor. "
                "Do not hardcode a threshold from this run. Widen the golden set, "
                "revisit query construction, or investigate embedding behavior "
                "before shipping."
            )
            return 1

        # Err toward precision: sit above the noise ceiling with a margin,
        # rather than exactly splitting the gap — an admitted noise doc
        # reaches real users; a missed borderline doc merely degrades to the
        # already-correct "no news" state (asymmetric cost).
        margin = (signal_floor - noise_ceiling) * 0.5
        recommended = round(noise_ceiling + margin, 4)
        print(f"\nCLEAN SEPARATION. Recommended RAG_MIN_GENERIC_COSINE_SIMILARITY = {recommended}")
        print("This is a starting point, not an auto-ship value — review the full")
        print("distributions above (and any anchors that scored close to either bound)")
        print("before hardcoding into config.py.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

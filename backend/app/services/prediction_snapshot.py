from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ml.inference.feature_loader import FeatureLoader

_inflight_lock = asyncio.Lock()
_inflight_tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}

# Ceiling on the shared coalesced computation, independent of any individual
# caller's own timeout budget. Callers may cancel their *wait* on the shared
# task (see get_prediction_snapshot's asyncio.shield usage) without affecting
# the task itself, so the task needs its own bound to avoid leaking a DB
# connection forever if inference hangs. Set above the largest known caller
# budget (ai_stream.py's _OPERATION_TIMEOUT_SECS = 25s).
_TASK_COMPUTE_TIMEOUT_SECS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_prediction_card(raw: dict[str, Any], timeframe: str = "1d") -> dict[str, Any]:
    """
    Serialize raw EnsemblePredictor.predict() output into the ML Analysis Card
    payload format. Pure function — no I/O.
    """
    meta = raw.get("metadata", {})
    models_raw = raw.get("models", {})
    probs = raw.get("probabilities", {})

    def _model_view(m: dict[str, Any]) -> dict[str, Any]:
        mp = m.get("probabilities", {})
        return {
            "direction": m.get("direction", "HOLD"),
            "confidence": round(float(m.get("confidence", 0.0)), 4),
            "conviction_scale": round(float(m.get("conviction_scale", 0.0)), 4),
            "threshold": round(float(m.get("threshold", 0.6)), 4),
            "probabilities": {
                "buy": round(float(mp.get("buy", 0.0)), 4),
                "sell": round(float(mp.get("sell", 0.0)), 4),
                "hold": round(float(mp.get("hold", 0.0)), 4),
            },
            "weight": round(float(m.get("weight", 0.0)), 4),
            "version": m.get("version", ""),
        }

    models: dict[str, Any] = {
        "xgboost": _model_view(models_raw["xgboost"]) if "xgboost" in models_raw else None,
        "gru": _model_view(models_raw["gru"]) if "gru" in models_raw else None,
    }

    effective_timeframe = meta.get("timeframe", timeframe)

    return {
        "available": True,
        "unavailable_reason": None,
        "direction": raw.get("direction_label", "HOLD"),
        "confidence": round(float(raw.get("confidence", 0.0)), 4),
        "conviction_scale": round(float(raw.get("conviction_scale", 0.0)), 4),
        "threshold": round(float(raw.get("threshold", 0.6)), 4),
        "probabilities": {
            "buy": round(float(probs.get("buy", 0.0)), 4),
            "sell": round(float(probs.get("sell", 0.0)), 4),
            "hold": round(float(probs.get("hold", 0.0)), 4),
        },
        "entry_price": round(float(raw.get("entry_price", 0.0)), 2),
        "stop_loss": round(float(raw.get("stop_loss", 0.0)), 2),
        "tp1": round(float(raw.get("tp1", 0.0)), 2),
        "tp2": round(float(raw.get("tp2", 0.0)), 2),
        "tp3": round(float(raw.get("tp3", 0.0)), 2),
        "volatility": round(float(raw.get("volatility", 0.0)), 4),
        "models": models,
        "xgboost_version": meta.get("xgboost_version", ""),
        "gru_version": meta.get("gru_version"),
        "xgboost_weight": round(float(meta.get("xgboost_weight", 0.75)), 4),
        "gru_weight": round(float(meta.get("gru_weight", 0.25)), 4),
        "timeframe": effective_timeframe,
        "predicted_at": meta.get("predicted_at", ""),
    }


def unavailable_prediction_snapshot(reason: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "available": False,
        "unavailable_reason": reason,
        "prediction_generated_at": now,
        "updated_at": now,
    }


async def _compute_prediction_snapshot(
    *,
    instrument_key: str,
    timeframe: str,
    predictor: Any,
    session_factory: async_sessionmaker,
    redis: Any,
) -> dict[str, Any]:
    if predictor is None:
        return unavailable_prediction_snapshot("no_model")

    try:
        return await asyncio.wait_for(
            _compute_prediction_snapshot_inner(
                instrument_key=instrument_key,
                timeframe=timeframe,
                predictor=predictor,
                session_factory=session_factory,
                redis=redis,
            ),
            timeout=_TASK_COMPUTE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        return unavailable_prediction_snapshot("timeout")


async def _compute_prediction_snapshot_inner(
    *,
    instrument_key: str,
    timeframe: str,
    predictor: Any,
    session_factory: async_sessionmaker,
    redis: Any,
) -> dict[str, Any]:
    # This task is shared across all concurrent callers coalesced onto it (see
    # get_prediction_snapshot). It must own a DB session with a lifetime
    # independent of any individual caller's request scope — borrowing a
    # caller-supplied session would let that caller's own cancellation/timeout
    # close the session out from under every other awaiter still using it.
    async with session_factory() as db:
        feature_loader = FeatureLoader(
            db=db,
            redis=redis,
            sequence_length=predictor.sequence_length,
            n_features=predictor.n_features,
            feature_names=predictor.feature_names,
            feature_version=getattr(predictor, "feature_version", "1.0.0"),
        )

        try:
            tabular, sequence, current_price, volatility = await feature_loader.load_features(
                symbol=instrument_key,
                timeframe=timeframe,
            )
        except ValueError:
            return unavailable_prediction_snapshot("insufficient_data")

        raw = await predictor.predict(
            features_tabular=tabular,
            features_sequence=sequence,
            symbol=instrument_key,
            current_price=current_price,
            volatility=volatility,
            timeframe=timeframe,
            use_cache=True,
        )

    now = _now_iso()
    return {
        **serialize_prediction_card(raw, timeframe=timeframe),
        "prediction_generated_at": now,
        "updated_at": now,
    }


async def _load_features_safe(
    *,
    instrument_key: str,
    timeframe: str,
    predictor: Any,
    session_factory: async_sessionmaker,
    redis: Any,
    sem: asyncio.Semaphore,
) -> dict[str, Any] | None:
    """
    Load features for one instrument in its own short-lived DB session,
    gated by `sem`. Returns None on any load failure (including
    insufficient-data ValueError) so the caller can degrade that single
    instrument without aborting the whole batch. Mirrors the phased
    load-then-batch-infer pattern in signal_scheduler.py.
    """
    async with sem:
        try:
            async with session_factory() as db:
                feature_loader = FeatureLoader(
                    db=db,
                    redis=redis,
                    sequence_length=predictor.sequence_length,
                    n_features=predictor.n_features,
                    feature_names=predictor.feature_names,
                    feature_version=getattr(predictor, "feature_version", "1.0.0"),
                )
                tabular, sequence, current_price, volatility = await feature_loader.load_features(
                    symbol=instrument_key,
                    timeframe=timeframe,
                )
            return {
                "symbol": instrument_key,
                "tabular": tabular,
                "sequence": sequence,
                "current_price": current_price,
                "volatility": volatility,
                "timeframe": timeframe,
            }
        except ValueError:
            return None
        except Exception:
            return None


async def get_prediction_snapshots_batch(
    *,
    instrument_keys: list[str],
    timeframe: str,
    predictor: Any,
    session_factory: async_sessionmaker,
    redis: Any,
    feature_concurrency: int,
) -> dict[str, dict[str, Any]]:
    """
    Compute prediction snapshots for many instruments with a single batched
    GPU inference call, instead of N sequential single-instrument predict()
    calls (which risks VRAM fragmentation on a 4GB card — see
    EnsemblePredictor.predict_batch's docstring).

    Feature loading is I/O-bound and runs concurrently (semaphore-gated, one
    short-lived DB session per instrument — never one session held across the
    whole batch). Inference itself is exactly one predict_batch() call for
    every instrument that loaded successfully.

    Returns a dict covering every key in `instrument_keys`, including ones
    that failed to load or infer — those map to an unavailable snapshot
    rather than being omitted, so callers never have to guess why an entry
    is missing.
    """
    if not instrument_keys:
        return {}

    if predictor is None:
        return {k: unavailable_prediction_snapshot("no_model") for k in instrument_keys}

    # ── Phase A: concurrent feature loading ─────────────────────────────────
    sem = asyncio.Semaphore(feature_concurrency)
    load_tasks = [
        asyncio.create_task(
            _load_features_safe(
                instrument_key=k,
                timeframe=timeframe,
                predictor=predictor,
                session_factory=session_factory,
                redis=redis,
                sem=sem,
            )
        )
        for k in instrument_keys
    ]
    load_results = await asyncio.gather(*load_tasks)

    results: dict[str, dict[str, Any]] = {}
    loaded: list[dict[str, Any]] = []
    for key, item in zip(instrument_keys, load_results):
        if item is None:
            results[key] = unavailable_prediction_snapshot("insufficient_data")
        else:
            loaded.append(item)

    if not loaded:
        return results

    # ── Phase B: single batched inference ───────────────────────────────────
    raw_predictions = await predictor.predict_batch(loaded)

    now = _now_iso()
    for item, raw in zip(loaded, raw_predictions):
        symbol = item["symbol"]
        if raw is None:
            results[symbol] = unavailable_prediction_snapshot("insufficient_data")
            continue
        results[symbol] = {
            **serialize_prediction_card(raw, timeframe=item.get("timeframe", timeframe)),
            "prediction_generated_at": now,
            "updated_at": now,
        }

    return results


async def get_prediction_snapshot(
    *,
    instrument_key: str,
    timeframe: str,
    predictor: Any,
    session_factory: async_sessionmaker,
    redis: Any,
    timeout: float | None = None,
) -> dict[str, Any]:
    """
    Compute the latest prediction snapshot, coalescing concurrent callers for
    the same instrument/timeframe within the current process.

    The underlying computation runs as a single shared task independent of
    any individual caller: each caller's optional `timeout` only bounds its
    own wait (via asyncio.shield) and never cancels the shared task, so one
    caller giving up does not affect any other caller waiting on the same
    instrument/timeframe.
    """
    key = (instrument_key, timeframe)

    async with _inflight_lock:
        task = _inflight_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                _compute_prediction_snapshot(
                    instrument_key=instrument_key,
                    timeframe=timeframe,
                    predictor=predictor,
                    session_factory=session_factory,
                    redis=redis,
                ),
                name=f"prediction_snapshot:{instrument_key}:{timeframe}",
            )
            _inflight_tasks[key] = task
            # Cleanup is tied to the task's own completion, not to any single
            # caller's wait finishing — a shielded caller can time out while
            # the task keeps running, and it must remain the registered
            # in-flight task for that key until it actually finishes so late
            # arrivals coalesce onto it instead of starting a duplicate.
            task.add_done_callback(lambda t, k=key: _inflight_tasks.pop(k, None) if _inflight_tasks.get(k) is t else None)

    if timeout is None:
        result = await asyncio.shield(task)
    else:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    return dict(result)

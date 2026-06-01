"""
Cortex AI — ML Training Pipeline Exceptions
============================================
Typed, fail-loud errors for the production training pipeline.

These are deliberately **plain `Exception` subclasses**, not
``app.exceptions.CortexBaseError``: the training orchestrator is a CLI/batch
process, not an API surface, so HTTP status codes / response shapes are
irrelevant here. This mirrors the existing in-module convention (e.g.
``QualityGateError`` in ``app.ml.model_registry``).

Design contract — *fail loud, never silently degrade*
-----------------------------------------------------
Model selection on noisy financial data is only trustworthy if every input is
exactly what it claims to be. A stale checkpoint or a missing forward-returns
array must **halt the run with a clear, actionable error** — never fall back to
a different optimisation metric or fabricate ``0.0`` financial metrics. A
silent substitution corrupts model selection invisibly, which is the most
expensive failure mode for a trading system.
"""
from __future__ import annotations


class StaleCheckpointError(RuntimeError):
    """
    Raised when an on-disk checkpoint is incompatible with the current code
    or configuration and therefore cannot be safely resumed.

    Triggers:
      * the checkpoint predates checkpoint schema versioning, or its
        ``schema_version`` does not match the current ``SCHEMA_VERSION``;
      * a *model-affecting* config key (e.g. ``n_features``,
        ``sequence_length``, ``include_fundamentals``, ``model_version``)
        differs from the value saved in the checkpoint.

    Resolution: start a clean run with ``--fresh`` (a model-affecting change
    invalidates every downstream artifact, so resuming would silently mix
    incompatible data — exactly the class of bug this guard prevents).
    """


class MissingReturnsError(RuntimeError):
    """
    Raised when a financial metric (Sharpe, Sortino, drawdown, win-rate, …)
    is required but the aligned forward-returns array is unavailable.

    The pipeline must **not** substitute an accuracy objective or emit ``0.0``
    financial metrics in this case: either would make an unvalidated model
    look validated. The correct response is to fail loudly so the run is
    re-executed with returns present (typically via ``--fresh``).
    """


class DataCoverageError(RuntimeError):
    """
    Raised when the training data universe is insufficient to proceed safely.

    Two triggers share this type because both represent the same class of
    failure — *not enough data to train a trustworthy model*:

    1. **Symbol coverage below threshold (A7 gate)**: the fraction of
       requested symbols that survived the data-quality audit is below
       ``TrainingConfig.min_symbol_coverage`` (default 0.85).  Training on a
       severely degraded symbol universe silently biases every downstream
       metric — this is the exact failure mode that produced the 2551→1198
       silent drop that the A7 gate is designed to catch.

    2. **Zero training samples after checkpoint resume**: the step-2
       feature-checkpoint metadata records zero rows, meaning the persisted
       data is corrupt or empty.  Proceeding would fit models on nothing and
       emit fabricated metrics.

    Resolution: investigate data pipeline health for trigger 1; run with
    ``--fresh`` to rebuild for trigger 2.
    """

"""
GRU Trainer Module

Production-grade GRU training with Keras Tuner hyperparameter optimization.
Optimized for sequential stock prediction with robust error handling and validation.

Author: Cortex AI Team
Date: 2026-04-13
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import keras
import keras_tuner as kt
import numpy as np
import tensorflow as tf
from keras import callbacks, layers, regularizers

from app.ml.inference.calibrator import ConfidenceCalibrator

logger = logging.getLogger(__name__)

# ── GPU configuration ──────────────────────────────────────────────────────────
# We deliberately avoid set_logical_device_configuration (hard memory cap):
#   • TF bug #53822 (unfixed as of TF 2.21): the cap pre-allocates
#     memory_limit + ~252 MiB of CUDA context overhead from physical VRAM
#     upfront.  On a 4 GB RTX 3050 with memory_limit=3584, this totals ~3836
#     MiB, leaving only ~140 MiB for cuDNN workspace — below the minimum needed
#     for cuDNN handle creation, causing CUDNN_STATUS_NOT_INITIALIZED.
#   • set_logical_device_configuration targets shared-GPU workstations;
#     for a dedicated training process it adds cost with no benefit.
#
# Correct approach: set_memory_growth=True (grow on demand, no upfront
# pre-allocation) + TF_DEVICE_MIN_SYS_MEMORY_IN_MB=512 (reserves fixed
# headroom for cuDNN workspace and CUDA driver overhead before TF computes
# its usable pool).  Genuine OOM raises ResourceExhaustedError, which the
# caller's batch-halving retry loop catches — same resilience as a hard cap.
_gpu_configured: bool = False


def configure_gpu() -> None:
    """
    Configure GPU memory policy before any TensorFlow GPU operation.

    Must be called before the first GPU kernel launch (before any tf.Variable,
    tf.constant, or model.fit() on GPU). Idempotent — safe to call multiple
    times; only the first call has effect.

    Memory strategy
    ---------------
    ``set_memory_growth(True)`` grows TF's GPU allocation on demand instead of
    pre-allocating a fixed pool.  Combined with ``TF_DEVICE_MIN_SYS_MEMORY_IN_MB``
    (reserves a fixed block for cuDNN workspace and CUDA driver overhead before
    TF computes its usable pool), this avoids the pre-allocation race that starves
    cuDNN on 4 GB GPUs (TF issue #53822, unfixed as of TF 2.21).

    OOM safety
    ----------
    Genuine VRAM exhaustion during training raises
    ``tf.errors.ResourceExhaustedError``, which the caller's OOM retry loop
    catches and handles by halving the batch size — identical resilience to a
    hard cap without the pre-allocation side-effects.

    XLA JIT
    -------
    Global XLA JIT must NOT be enabled in this codebase.  cuDNN's optimised
    GRU/LSTM kernels (CudnnRNNV3) have no XLA_GPU_JIT OpKernel (TF issue
    #100872, open as of TF 2.21) — enabling global JIT causes
    FAILED_PRECONDITION on the first XLA-compiled step.  cuDNN's native path
    already provides near-optimal Ampere throughput without JIT.
    """
    global _gpu_configured
    if _gpu_configured:
        return

    import os

    # Reserve GPU headroom for cuDNN workspace + CUDA driver overhead before TF
    # computes its usable BFC pool size.  512 MiB is empirically sufficient on
    # the RTX 3050 (4096 MiB) and conservative enough to be safe on larger GPUs.
    os.environ.setdefault("TF_DEVICE_MIN_SYS_MEMORY_IN_MB", "512")

    gpus = tf.config.list_physical_devices("GPU")
    if not gpus:
        logger.info("configure_gpu: no GPU detected — training will use CPU")
        _gpu_configured = True
        return

    failed: list[str] = []
    for gpu in gpus:
        try:
            tf.config.experimental.set_memory_growth(gpu, True)
        except RuntimeError as exc:
            failed.append(f"{gpu.name}: {exc}")

    if failed:
        logger.warning(
            "configure_gpu: set_memory_growth unavailable for %d GPU(s) "
            "(GPU context already initialised before this call).  VRAM will "
            "grow freely up to the physical limit; OOM retries remain active.  "
            "Details: %s",
            len(failed),
            "; ".join(failed),
        )
    else:
        logger.info(
            "configure_gpu: %d GPU(s) configured  memory_growth=True  "
            "TF_DEVICE_MIN_SYS_MEMORY_IN_MB=%s",
            len(gpus),
            os.environ["TF_DEVICE_MIN_SYS_MEMORY_IN_MB"],
        )
    _gpu_configured = True

    # Mixed precision: tensor-core acceleration on Ampere/Turing (RTX 30/20 series).
    # Variables remain float32 for numerical stability; compute paths use float16.
    try:
        keras.mixed_precision.set_global_policy(
            keras.mixed_precision.Policy("mixed_float16")
        )
        logger.info("configure_gpu: mixed_float16 precision active")
    except Exception as mp_err:
        logger.warning(
            "configure_gpu: mixed_float16 unavailable (%s) — using float32",
            mp_err,
        )


@keras.saving.register_keras_serializable(package="cortex")
class _BinaryAUCPR(keras.metrics.AUC):
    """
    AUC-PR metric that works correctly with a 2-class softmax output.

    Keras 3's optimised confusion-matrix path (``_update_confusion_matrix_variables_optimized``)
    flattens ``y_pred`` of shape ``[batch, 2]`` into ``[batch×2]`` when building
    ``segment_ids``, while ``y_true`` stays at ``[batch]`` — always a 2× shape mismatch
    that raises ``InvalidArgument: data.shape=[N] does not start with segment_ids.shape=[2N]``.

    This subclass intercepts ``update_state`` and slices out ``P(class=1)``
    (the positive-class probability column) before the base AUC implementation
    sees the predictions, reducing the shape to ``[batch]`` and eliminating the
    mismatch.  All AUC semantics (thresholds, curve, interpolation) are unchanged.
    """

    def update_state(
        self,
        y_true: tf.Tensor,
        y_pred: tf.Tensor,
        sample_weight: Optional[tf.Tensor] = None,
    ) -> None:
        if isinstance(y_pred, tf.Tensor) and len(y_pred.shape) == 2 and y_pred.shape[-1] == 2:
            y_pred = y_pred[:, 1]
        return super().update_state(y_true, y_pred, sample_weight)



class CrossSectionalSequenceGenerator(keras.utils.Sequence):
    """
    Streams cross-sectional training batches from per-symbol sequence arrays.

    The naive approach (np.vstack over all symbols) would materialize ~41GB for
    2,200 NSE instruments × 1,660 bars × (60 timesteps, 37 features).  This
    generator instead keeps each symbol's array in place and assembles batches
    on-the-fly, so peak additional memory per batch is:
        batch_size × seq_len × n_features × 4 bytes  (≈ 0.7 MB at batch=64)

    Args:
        features_data:  Dict[symbol → {'X': ndarray(n, seq_len, n_feat), ...}]
        targets_data:   Dict[symbol → {'target': ndarray(n,) int32}]
        symbols:        Ordered list of symbols to include (caller controls train/val split).
        batch_size:     Samples per batch.
        shuffle:        Whether to reshuffle the flat index after each epoch.
        row_end_frac:   Fraction of each symbol's rows to include (0.0–1.0).
                        Pass 0.8 for training (first 80%), 1.0 for all.
        row_start_frac: Start fraction (default 0.0).  Pair with row_end_frac to
                        select a slice without materialising the whole array.
    """

    def __init__(
        self,
        features_data: Dict[str, Any],
        targets_data: Dict[str, Any],
        symbols: List[str],
        batch_size: int = 64,
        shuffle: bool = True,
        row_start_frac: float = 0.0,
        row_end_frac: float = 1.0,
    ) -> None:
        self.features_data = features_data
        self.targets_data = targets_data
        self.batch_size = batch_size
        self.shuffle = shuffle

        # Build flat index: parallel arrays of (symbol, row_idx) for every
        # valid sample in the requested fraction of each symbol's history.
        sym_list: List[str] = []
        row_list: List[int] = []

        for symbol in symbols:
            if symbol not in features_data or symbol not in targets_data:
                continue
            n = min(
                len(features_data[symbol]['X']),
                len(targets_data[symbol]['target']),
            )
            start = int(n * row_start_frac)
            end   = int(n * row_end_frac)
            if end <= start:
                continue
            sym_list.extend([symbol] * (end - start))
            row_list.extend(range(start, end))

        # numpy object array for symbols (dict-key lookups stay O(1));
        # int32 row array for memory efficiency.
        self._symbols: np.ndarray = np.array(sym_list)          # dtype=object
        self._rows: np.ndarray    = np.array(row_list, dtype=np.int32)
        self._n: int              = len(self._symbols)

        # Permutation index — shuffled each epoch
        self._perm: np.ndarray = np.arange(self._n, dtype=np.int64)
        self.on_epoch_end()

        logger.info(
            "CrossSectionalSequenceGenerator: %d samples across %d symbols "
            "| batch_size=%d | batches_per_epoch=%d",
            self._n, len({s for s in sym_list}), batch_size, len(self),
        )

    # ── Keras Sequence protocol ───────────────────────────────────────────────

    def __len__(self) -> int:
        return math.ceil(self._n / self.batch_size)

    def __getitem__(self, idx: int) -> Tuple[np.ndarray, np.ndarray]:
        start = idx * self.batch_size
        end   = min(start + self.batch_size, self._n)
        perm_slice = self._perm[start:end]

        syms = self._symbols[perm_slice]
        rows = self._rows[perm_slice]

        # Each features_data[s]['X'][r] is a numpy view (no copy until stack).
        X_batch = np.stack([
            self.features_data[s]['X'][r] for s, r in zip(syms, rows)
        ]).astype(np.float32)
        y_batch = np.array([
            self.targets_data[s]['target'][r] for s, r in zip(syms, rows)
        ], dtype=np.int32)

        return X_batch, y_batch

    def on_epoch_end(self) -> None:
        if self.shuffle:
            np.random.shuffle(self._perm)

    # ── Utility ───────────────────────────────────────────────────────────────

    @property
    def total_samples(self) -> int:
        return self._n


class GRUTrainer:
    """
    Production-grade GRU trainer with comprehensive validation and optimization.
    
    Features:
    - Robust data validation
    - Gradient clipping and stability measures
    - Hyperparameter optimization with Keras Tuner
    - Early stopping and learning rate scheduling
    - Model checkpointing and recovery
    """
    
    def __init__(
        self,
        input_shape: Tuple[int, int] = (60, 40),
        num_classes: int = 2,  # Binary classification
        random_state: int = 42
    ):
        """
        Initialize GRU trainer with GPU optimization.

        Args:
            input_shape: (sequence_length, n_features)
            num_classes: Number of output classes (2 for binary)
            random_state: Random seed for reproducibility
        """
        self.input_shape = input_shape
        self.num_classes = num_classes
        self.random_state = random_state
        self.model: keras.Model | None = None
        self.history = None
        self.calibrator: ConfidenceCalibrator | None = None

        # Seeds — set before any TF op so HPO trials are reproducible.
        tf.random.set_seed(random_state)
        np.random.seed(random_state)

        # Configure GPU memory cap.  Idempotent — a no-op when the orchestrator
        # has already called configure_gpu() earlier in the process.  This call
        # ensures standalone use (e.g., scripts that instantiate GRUTrainer
        # directly) also gets the hard VRAM cap before the first GPU kernel.
        configure_gpu()

        # enable_op_determinism() intentionally omitted: it forces TF to
        # pre-allocate large scratch buffers for deterministic GPU algorithms,
        # consuming hundreds of MB that are needed for training data.
    
    def _validate_data(self, X: np.ndarray, y: np.ndarray, name: str) -> None:
        """
        Comprehensive data validation for production safety.
        
        Args:
            X: Input sequences
            y: Target labels
            name: Data split name for error messages
            
        Raises:
            ValueError: If data contains invalid values or wrong shapes
        """
        # Check for NaN/inf values
        if np.any(np.isnan(X)):
            raise ValueError(f"{name} features contain NaN values")
        if np.any(np.isinf(X)):
            raise ValueError(f"{name} features contain infinite values")
        if np.any(np.isnan(y)):
            raise ValueError(f"{name} labels contain NaN values")
        if np.any(np.isinf(y)):
            raise ValueError(f"{name} labels contain infinite values")
        
        # Check shapes
        if len(X.shape) != 3:
            raise ValueError(f"{name} features must be 3D (samples, timesteps, features)")
        if X.shape[1:] != self.input_shape:
            raise ValueError(f"{name} features shape {X.shape[1:]} != expected {self.input_shape}")
        
        # Check label range (binary: 0, 1)
        unique_labels = np.unique(y)
        expected_labels = np.array([0, 1])
        if not np.array_equal(np.sort(unique_labels), expected_labels):
            raise ValueError(f"{name} labels must be binary [0, 1], got {unique_labels}")
        
        logger.info(f"✓ {name} data validation passed: {X.shape} features, {len(y)} labels")
    
    def build_model(self, params: Optional[Dict] = None) -> keras.Model:
        """
        Build production-grade GRU model with stability optimizations.
        
        Args:
            params: Model hyperparameters
            
        Returns:
            Compiled Keras model
        """
        if params is None:
            params = self._get_default_params()
        
        # Input layer with explicit shape validation
        inputs = keras.Input(shape=self.input_shape, name="sequence_input")
        
        # First GRU layer with return sequences
        x = layers.GRU(
            units=params['gru_units_1'],
            return_sequences=True,
            dropout=params['dropout'],
            recurrent_dropout=params['recurrent_dropout'],
            kernel_regularizer=regularizers.l2(params['l2_reg']),
            name="gru_layer_1"
        )(inputs)
        
        # Second GRU layer
        x = layers.GRU(
            units=params['gru_units_2'],
            dropout=params['dropout'],
            recurrent_dropout=params['recurrent_dropout'],
            kernel_regularizer=regularizers.l2(params['l2_reg']),
            name="gru_layer_2"
        )(x)
        
        # Dense layers with batch normalization
        x = layers.Dense(
            params['dense_units'],
            activation='relu',
            kernel_regularizer=regularizers.l2(params['l2_reg']),
            name="dense_layer"
        )(x)
        x = layers.BatchNormalization(name="batch_norm")(x)
        x = layers.Dropout(params['dropout'], name="dropout_layer")(x)
        
        # Output layer — must be float32 regardless of global mixed-precision policy.
        # Softmax over float16 activations can saturate to 1.0/0.0 and produce NaN
        # gradients; casting the final layer back to float32 prevents this.
        outputs = layers.Dense(
            self.num_classes,
            activation='softmax',
            dtype='float32',
            name="output_layer",
        )(x)
        
        # Create model
        model = keras.Model(inputs=inputs, outputs=outputs, name="gru_stock_predictor")
        
        # Compile with production-grade optimizer settings
        # TensorFlow 2.21+ only allows one clipping method
        clip_kwargs = {}
        if params.get('clipnorm'):
            clip_kwargs['clipnorm'] = params['clipnorm']
        elif params.get('clipvalue'):
            clip_kwargs['clipvalue'] = params['clipvalue']
        
        optimizer = keras.optimizers.Adam(
            learning_rate=params['learning_rate'],
            **clip_kwargs
        )
        
        model.compile(
            optimizer=optimizer,
            loss='sparse_categorical_crossentropy',
            metrics=[
                'accuracy',
                # AUC-PR via a subclass that extracts P(class=1) before the metric
                # sees the predictions.  Keras 3's optimised confusion-matrix path
                # flattens a [batch, 2] softmax to [batch×2] for segment_ids, while
                # y_true stays at [batch] — a guaranteed 2× mismatch.  Slicing to
                # the positive-class column ([batch]) keeps both shapes equal.
                _BinaryAUCPR(curve='PR', name='auc_pr'),
            ],
        )
        
        logger.info("✓ GRU model built and compiled successfully")
        logger.info(f"Model parameters: {model.count_params():,}")
        
        return model
    
    def train(
        self,
        X_train: Optional[np.ndarray] = None,
        y_train: Optional[np.ndarray] = None,
        X_val: Optional[np.ndarray] = None,
        y_val: Optional[np.ndarray] = None,
        params: Optional[Dict] = None,
        epochs: int = 100,
        batch_size: int = 64,
        class_weight: Optional[Dict] = None,
        *,
        train_generator: Optional[CrossSectionalSequenceGenerator] = None,
        val_data: Optional[Tuple[np.ndarray, np.ndarray]] = None,
        initial_epoch: int = 0,
        extra_callbacks: Optional[List] = None,
    ) -> keras.Model:
        """
        Train GRU model with production-grade monitoring and stability.

        Two calling modes
        -----------------
        Numpy mode (original):
            train(X_train, y_train, X_val, y_val, ...)

        Generator mode (memory-efficient — avoids 41 GB np.vstack):
            train(train_generator=gen, val_data=(X_val_np, y_val_np), ...)

        Args:
            X_train:         Training sequences (n, seq_len, n_features).  Unused in generator mode.
            y_train:         Training labels (int32).  Unused in generator mode.
            X_val:           Validation sequences.  Unused in generator mode.
            y_val:           Validation labels.  Unused in generator mode.
            params:          Model hyperparameters.
            epochs:          Maximum training epochs.
            batch_size:      Batch size for numpy mode (generator carries its own).
            class_weight:    Class weights for imbalanced data.
            train_generator: CrossSectionalSequenceGenerator — enables generator mode.
            val_data:        (X_val_np, y_val_np) tuple required in generator mode.

        Returns:
            Trained Keras model.
        """
        use_generator = train_generator is not None

        is_tf_dataset = isinstance(train_generator, tf.data.Dataset) if train_generator is not None else False

        # H-1: split the held-out val into two disjoint subsets:
        #   • val_early — drives EarlyStopping / restore_best_weights (model selection)
        #   • val_cal   — held out from model selection, used ONLY for the post-hoc
        #                 temperature-scaling calibrator
        # Reason: fitting the calibrator on the same val EarlyStopping monitors
        # introduces selection leakage (best-restored weights are by construction
        # the ones scoring highest on that val).  The split restores the A4
        # leakage-safe principle to the sequence model.  Full per-symbol CPCV for
        # the GRU is deferred to Workstream B; this is the minimal correct interim.
        # Both subsets remain leakage-safe in time because the parent set is the
        # per-symbol purged val from orchestrator sub-A.  Seed=42 is deterministic.
        if use_generator:
            if val_data is None:
                raise ValueError("val_data=(X_val, y_val) is required when using train_generator")
            X_val_full, y_val_full = val_data
            self._validate_data(X_val_full, y_val_full, "Validation")
            X_val_np, y_val_np, X_val_cal, y_val_cal = self._split_val_for_calibration(
                X_val_full, y_val_full
            )
            y_val_cat = self._to_categorical_indices(y_val_np)
            n_train = getattr(train_generator, 'total_samples', '?') if not is_tf_dataset else '(tf.data)'
            n_val   = len(X_val_np)
        else:
            if X_train is None or y_train is None or X_val is None or y_val is None:
                raise ValueError("X_train, y_train, X_val, y_val are required in numpy mode")
            self._validate_data(X_train, y_train, "Training")
            self._validate_data(X_val, y_val, "Validation")
            X_val, y_val, X_val_cal, y_val_cal = self._split_val_for_calibration(
                X_val, y_val
            )
            y_train_cat = self._to_categorical_indices(y_train)
            y_val_cat   = self._to_categorical_indices(y_val)
            n_train = len(X_train)
            n_val   = len(X_val)

        # Build model if not already built
        if self.model is None:
            if params is None:
                params = self._get_default_params()
                logger.warning("No params provided and no pre-built model, using defaults")
            model = self.build_model(params)
        else:
            model = self.model
            logger.info("Using pre-built model with custom hyperparameters")
            if params is None:
                lr = float(model.optimizer.learning_rate.numpy())
                params = {'patience': 15, 'reduce_lr_patience': 5, 'learning_rate': lr}
                logger.info(f"Extracted params from model: learning_rate={lr}")

        callbacks_list = self._get_callbacks(params)
        if extra_callbacks:
            callbacks_list.extend(extra_callbacks)

        logger.info("Starting GRU model training...")
        logger.info(f"Training samples: {n_train} | Validation samples: {n_val:,}")
        logger.info(
            f"Mode: {'generator' if use_generator else 'numpy'} | "
            f"Batch: {batch_size} | Max epochs: {epochs} | Initial epoch: {initial_epoch}"
        )

        try:
            # XLA JIT is intentionally NOT enabled.
            # CudnnRNNV3 (the cuDNN GRU/LSTM kernel) has no XLA_GPU_JIT OpKernel
            # (TF issue #100872, open as of TF 2.21) — set_jit(True) causes
            # FAILED_PRECONDITION on the first XLA-compiled step.

            if use_generator:
                train_input = train_generator
                fit_val     = (X_val_np, y_val_cat)
                # tf.data.Dataset handles its own prefetch; Sequence needs worker threads.
                extra_kw: Dict[str, Any] = {} if is_tf_dataset else {'workers': 4, 'use_multiprocessing': False}
            else:
                # tf.data pipeline — prefetch runs in a C++ background thread that
                # bypasses the Python GIL, so the GPU stays busy continuously.
                # shuffle buffer of 50K gives good randomness without full-dataset copy.
                train_ds = (
                    tf.data.Dataset
                    .from_tensor_slices((X_train, y_train_cat))
                    .shuffle(buffer_size=min(n_train, 5_000),
                             reshuffle_each_iteration=True)
                    .batch(batch_size)
                    .prefetch(2)
                )
                val_ds = (
                    tf.data.Dataset
                    .from_tensor_slices((X_val, y_val_cat))
                    .batch(batch_size)
                    .prefetch(2)
                )
                train_input = train_ds
                fit_val     = val_ds
                extra_kw    = {}  # batch_size implicit in dataset

            history = model.fit(
                train_input,
                validation_data=fit_val,
                epochs=epochs,
                initial_epoch=initial_epoch,
                callbacks=callbacks_list,
                class_weight=class_weight,
                verbose=1,
                **extra_kw,
            )

            self.model = model
            self.history = history
            logger.info("✓ GRU training completed successfully")

            # H-1: fit calibrator on the held-out cal subset (never seen by
            # EarlyStopping) — leakage-safe Beta calibration discipline (A4
            # applied to the sequence model).
            self.calibrator = self._fit_calibrator(model, X_val_cal, y_val_cal)

            return model

        except Exception as e:
            logger.error(f"Training failed: {e}")
            raise
    
    def save_calibrator(self, path: Path) -> None:
        """
        Persist the GRU temperature-scaling calibrator to disk.

        Typically called by the training orchestrator after train() completes:
            trainer.save_calibrator(Path("models/1d/calibrator_gru.pkl"))

        Raises RuntimeError if train() has not been called.
        """
        if self.calibrator is None:
            raise RuntimeError(
                "No calibrator to save — call train() first to fit the calibrator."
            )
        self.calibrator.save(Path(path))

    @staticmethod
    def _split_val_for_calibration(
        X_val: np.ndarray,
        y_val: np.ndarray,
        *,
        cal_fraction: float = 0.20,
        min_cal_samples: int = 500,
        seed: int = 42,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Deterministically split val into (X_early, y_early, X_cal, y_cal).

        ``cal_fraction`` of the rows (minimum ``min_cal_samples``) are reserved
        for the post-hoc calibrator; the rest drive EarlyStopping monitoring.
        ``seed=42`` makes the split reproducible across runs and resumes.

        Raises ``ValueError`` if the val set is too small to give both subsets
        at least ``min_cal_samples`` rows.
        """
        n = len(X_val)
        cal_size = max(min_cal_samples, int(n * cal_fraction))
        if cal_size >= n - min_cal_samples:
            raise ValueError(
                f"Val set too small for early/cal split: n={n}, "
                f"cal_size={cal_size}.  Need at least {2 * min_cal_samples} rows."
            )
        rng = np.random.default_rng(seed)
        indices = rng.permutation(n)
        cal_idx   = np.sort(indices[:cal_size])
        early_idx = np.sort(indices[cal_size:])
        logger.info(
            "H-1 val split: total=%d  early_stopping=%d  calibration=%d  (seed=%d)",
            n, len(early_idx), len(cal_idx), seed,
        )
        return (
            X_val[early_idx], y_val[early_idx],
            X_val[cal_idx],   y_val[cal_idx],
        )

    def _fit_calibrator(
        self,
        model: keras.Model,
        X_val: np.ndarray,
        y_val: np.ndarray,
        batch_size: int = 512,
    ) -> ConfidenceCalibrator:
        """
        Run the trained model on the validation set and fit temperature scaling.

        Uses batch_size=512 to stay within VRAM limits while still being fast —
        a single full-dataset predict() call on 16 GB GPU would work but this
        gives predictable memory headroom regardless of val set size.
        """
        logger.info(
            "Fitting GRU temperature-scaling calibrator on validation set (%d samples)...",
            len(X_val),
        )
        # Probabilities shape: (n, 2) — [P(DOWN), P(UP)]
        val_proba = model.predict(X_val, batch_size=batch_size, verbose=0)

        calibrator = ConfidenceCalibrator("gru")
        calibrator.fit(y_val.astype(int), val_proba)

        if calibrator.ece_after is not None and calibrator.ece_after >= 0.05:
            logger.warning(
                "GRU calibration ECE %.4f ≥ 0.05 — model may benefit from retraining.",
                calibrator.ece_after,
            )
        return calibrator

    def _to_categorical_indices(self, y: np.ndarray) -> np.ndarray:
        """Convert binary labels [0, 1] to categorical indices (already correct format)."""
        return y.astype(np.int32)
    
    def _get_default_params(self) -> Dict[str, Any]:
        """
        Get production-optimized default parameters.
        
        Returns:
            Dictionary of hyperparameters
        """
        return {
            'gru_units_1': 128,
            'gru_units_2': 64,
            'dense_units': 32,
            'dropout': 0.3,
            'recurrent_dropout': 0.2,
            'l2_reg': 0.001,
            'learning_rate': 1e-4,
            'clipnorm': 1.0,   # gradient norm clipping; clipvalue intentionally omitted
            'patience': 15,
            'reduce_lr_patience': 5,
        }
    
    def _get_callbacks(self, params: Dict) -> list:
        """
        Get production-grade training callbacks.
        
        Args:
            params: Model parameters
            
        Returns:
            List of Keras callbacks
        """
        callbacks_list = [
            # Monitor AUC-PR (not loss) — we want the model that maximises
            # precision-recall trade-off, not the one with minimal cross-entropy.
            callbacks.EarlyStopping(
                monitor='val_auc_pr',
                patience=params['patience'],
                mode='max',
                restore_best_weights=True,
                verbose=1,
            ),
            callbacks.ReduceLROnPlateau(
                monitor='val_auc_pr',
                factor=0.5,
                patience=params['reduce_lr_patience'],
                mode='max',
                min_lr=1e-7,
                verbose=1,
            ),
            callbacks.TerminateOnNaN(),
        ]
        
        return callbacks_list
    
    def tune_hyperparameters(
        self,
        X_train: Optional[np.ndarray] = None,
        y_train: Optional[np.ndarray] = None,
        X_val: Optional[np.ndarray] = None,
        y_val: Optional[np.ndarray] = None,
        max_trials: int = 20,
        *,
        train_generator: Optional[CrossSectionalSequenceGenerator] = None,
        val_data: Optional[Tuple[np.ndarray, np.ndarray]] = None,
        tuner_directory: str = "hyperparameter_tuning",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        """
        Hyperparameter optimization with Keras Tuner (BayesianOptimization).

        Two calling modes — mirrors GRUTrainer.train():
            Numpy:     tune_hyperparameters(X_train, y_train, X_val, y_val, ...)
            Generator: tune_hyperparameters(train_generator=gen, val_data=(X_v, y_v), ...)

        Args:
            X_train, y_train:  Training data (numpy mode only).
            X_val, y_val:      Validation data (numpy mode only).
            max_trials:        Maximum Bayesian optimization trials.
            train_generator:   CrossSectionalSequenceGenerator (generator mode).
            val_data:          (X_val_np, y_val_np) required in generator mode.

        Returns:
            Best hyperparameters dictionary.
        """
        logger.info(f"Starting hyperparameter tuning: {max_trials} trials...")

        use_generator = train_generator is not None

        TUNE_BATCH = 512  # reduced from 1024 to prevent RAM exhaustion under memory pressure

        is_tf_dataset = isinstance(train_generator, tf.data.Dataset) if train_generator is not None else False

        if use_generator:
            if val_data is None:
                raise ValueError("val_data=(X_val, y_val) is required when using train_generator")
            X_val_np, y_val_np = val_data
            self._validate_data(X_val_np, y_val_np, "Validation")
            y_val_cat   = self._to_categorical_indices(y_val_np)
            train_input = train_generator
            fit_kwargs: Dict[str, Any] = {} if is_tf_dataset else {'workers': 4, 'use_multiprocessing': False}
            search_val  = (X_val_np, y_val_cat)
        else:
            if X_train is None or y_train is None or X_val is None or y_val is None:
                raise ValueError("X_train, y_train, X_val, y_val are required in numpy mode")
            self._validate_data(X_train, y_train, "Training")
            self._validate_data(X_val, y_val, "Validation")
            y_train_cat = self._to_categorical_indices(y_train)
            y_val_cat   = self._to_categorical_indices(y_val)
            # tf.data pipeline: prefetch in a GIL-free C++ thread keeps GPU saturated.
            train_input = (
                tf.data.Dataset
                .from_tensor_slices((X_train, y_train_cat))
                .shuffle(buffer_size=min(len(X_train), 5_000),
                         reshuffle_each_iteration=True)
                .batch(TUNE_BATCH)
                .prefetch(2)
            )
            val_ds = (
                tf.data.Dataset
                .from_tensor_slices((X_val, y_val_cat))
                .batch(TUNE_BATCH)
                .prefetch(2)
            )
            fit_kwargs  = {}        # batch_size is inside the dataset
            search_val  = val_ds

        # BayesianOptimization converges faster than RandomSearch for continuous
        # hyperparameter spaces.  Objective: maximise val_auc_pr (AUC-PR).
        # overwrite=False lets the tuner resume from previously-completed trials
        # when the orchestrator restarts after a crash (sub-checkpoint B logic).
        tuner = kt.BayesianOptimization(
            self._build_tuner_model,
            objective=kt.Objective('val_auc_pr', direction='max'),
            max_trials=max_trials,
            directory=tuner_directory,
            project_name='gru_stock_prediction',
            overwrite=overwrite,
        )

        search_args: Dict[str, Any] = dict(
            validation_data=search_val,
            epochs=30,
            callbacks=[
                callbacks.EarlyStopping(
                    monitor='val_auc_pr',
                    patience=5,
                    mode='max',
                    restore_best_weights=True,
                )
            ],
            **fit_kwargs,
        )

        if use_generator:
            # Sequence mode: y is embedded in the generator
            tuner.search(train_input, **search_args)
        else:
            # tf.data mode: dataset already contains (X, y) pairs
            tuner.search(train_input, **search_args)
        
        # Get best parameters
        best_hps = tuner.get_best_hyperparameters(num_trials=1)[0]
        best_params = {
            'gru_units_1': best_hps.get('gru_units_1'),
            'gru_units_2': best_hps.get('gru_units_2'),
            'dense_units': best_hps.get('dense_units'),
            'dropout': best_hps.get('dropout'),
            'recurrent_dropout': best_hps.get('recurrent_dropout'),
            'l2_reg': best_hps.get('l2_reg'),
            'learning_rate': best_hps.get('learning_rate'),
            'clipnorm': 1.0,
            'patience': 15,
            'reduce_lr_patience': 5,
        }
        
        logger.info(f"✓ Hyperparameter tuning completed. Best params: {best_params}")
        return best_params
    
    def _build_tuner_model(self, hp) -> keras.Model:
        """Build model for Keras Tuner — same architecture as build_model()."""
        params = {
            'gru_units_1': hp.Int('gru_units_1', 64, 256, step=32),
            'gru_units_2': hp.Int('gru_units_2', 32, 128, step=16),
            'dense_units': hp.Int('dense_units', 16, 64, step=8),
            'dropout': hp.Float('dropout', 0.1, 0.5, step=0.1),
            'recurrent_dropout': hp.Float('recurrent_dropout', 0.1, 0.3, step=0.1),
            'l2_reg': hp.Float('l2_reg', 1e-4, 1e-2, sampling='log'),
            'learning_rate': hp.Float('learning_rate', 1e-5, 1e-3, sampling='log'),
            'clipnorm': 1.0,
        }
        return self.build_model(params)


# Convenience functions for external use
def train_gru_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    params: Optional[Dict] = None,
    tune: bool = False,
    max_trials: int = 20
) -> Tuple[keras.Model, Dict]:
    """
    Production-grade convenience function to train GRU model.
    
    Args:
        X_train: Training sequences
        y_train: Training labels
        X_val: Validation sequences
        y_val: Validation labels
        params: Model parameters (None = defaults)
        tune: Whether to tune hyperparameters
        max_trials: Number of tuning trials
        
    Returns:
        Tuple of (trained_model, best_parameters)
    """
    trainer = GRUTrainer()
    
    if tune:
        logger.info("Performing hyperparameter tuning...")
        best_params = trainer.tune_hyperparameters(
            X_train, y_train, X_val, y_val, max_trials=max_trials
        )
        params = best_params
    
    model = trainer.train(X_train, y_train, X_val, y_val, params)
    
    return model, params or trainer._get_default_params()


def tune_gru_hyperparameters(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    max_trials: int = 20
) -> Dict[str, Any]:
    """
    Production-grade convenience function for hyperparameter tuning.
    
    Args:
        X_train: Training sequences
        y_train: Training labels
        X_val: Validation sequences
        y_val: Validation labels
        max_trials: Number of trials
        
    Returns:
        Best parameters dictionary
    """
    trainer = GRUTrainer()
    return trainer.tune_hyperparameters(
        X_train, y_train, X_val, y_val, max_trials
    )

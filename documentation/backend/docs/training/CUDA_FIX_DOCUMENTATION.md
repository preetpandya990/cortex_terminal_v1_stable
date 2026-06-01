# CUDA Error Fix - Production Documentation

## Issue Summary

**Error:** `cudaGetDevice() failed. Status: device kernel image is invalid`

**Root Cause:** Binary incompatibility between system CUDA runtime and TensorFlow's pre-compiled CUDA kernels.

**Environment:**
- System: WSL2 on Windows
- GPU: NVIDIA GeForce RTX 3050 Laptop (Ampere, compute capability 8.6)
- System CUDA: 12.2.2
- TensorFlow 2.21.0 built with: CUDA 12.5.1 + cuDNN 9

## Technical Analysis

### Why the Error Occurred

TensorFlow 2.21.0 was compiled with CUDA 12.5.1, but the system had CUDA 12.2.2 installed. CUDA kernels are compiled for specific CUDA versions and are **not forward/backward compatible** across minor versions.

When TensorFlow tried to initialize GPU operations:
1. It loaded pre-compiled CUDA kernels (built for CUDA 12.5.1)
2. The system's CUDA runtime (12.2.2) couldn't execute these kernels
3. Result: "device kernel image is invalid" error

### Why This is Critical for Production

This is **not** a minor compatibility issue. It represents:
- **Binary incompatibility** at the CUDA driver level
- **Complete GPU failure** - all GPU operations fail
- **Silent degradation risk** - could fall back to CPU without warning
- **Deployment fragility** - different CUDA versions across environments cause failures

## Solution: TensorFlow with Bundled CUDA

### Implementation

**Before (Broken):**
```bash
pip install tensorflow==2.21.0  # Uses system CUDA 12.2.2
```

**After (Fixed):**
```bash
pip install tensorflow[and-cuda]==2.21.0  # Bundles CUDA 12.5.1 + cuDNN 9
```

### What This Does

The `tensorflow[and-cuda]` package includes:
- **CUDA 12.5.1** runtime libraries (bundled)
- **cuDNN 9** deep learning primitives (bundled)
- **All NVIDIA libraries** (cuBLAS, cuFFT, cuRAND, cuSOLVER, cuSPARSE, NCCL)
- **Pre-compiled kernels** matching the bundled CUDA version

**Key Benefit:** Eliminates dependency on system CUDA version. TensorFlow uses its own bundled libraries.

## Verification

### 1. Check Bundled CUDA Version

```bash
python -c "
import tensorflow.python.platform.build_info as build
print('Bundled CUDA:', build.build_info.get('cuda_version'))
print('Bundled cuDNN:', build.build_info.get('cudnn_version'))
"
```

**Expected Output:**
```
Bundled CUDA: 12.5.1
Bundled cuDNN: 9
```

### 2. Test GPU Functionality

```bash
python -c "
import tensorflow as tf
print('GPU devices:', tf.config.list_physical_devices('GPU'))

# Test GPU computation
with tf.device('/GPU:0'):
    a = tf.constant([[1.0, 2.0], [3.0, 4.0]])
    b = tf.constant([[5.0, 6.0], [7.0, 8.0]])
    c = tf.matmul(a, b)
    print('GPU computation successful:', c.numpy())
"
```

**Expected Output:**
```
GPU devices: [PhysicalDevice(name='/physical_device:GPU:0', device_type='GPU')]
GPU computation successful: [[19. 22.]
 [43. 50.]]
```

### 3. Test Model Training

```bash
python -c "
import tensorflow as tf
from keras import layers, models
import numpy as np

model = models.Sequential([
    layers.Dense(10, activation='relu', input_shape=(5,)),
    layers.Dense(1)
])
model.compile(optimizer='adam', loss='mse')

X = np.random.randn(100, 5).astype(np.float32)
y = np.random.randn(100, 1).astype(np.float32)
model.fit(X, y, epochs=1, verbose=0)
print('✓ Model training successful')
"
```

## Updated Dependencies

### requirements.txt Changes

```diff
# ── ML & AI ────────────────────────────────────────────────────────────────────
-torch==2.3.0                    # PyTorch (numpy 2.x compatible)
-transformers==4.36.2            # Hugging Face transformers (FinBERT)
-sentencepiece==0.1.99           # Tokenizer for transformers
-onnxruntime==1.17.1             # ONNX inference engine
-evidently==0.4.33               # ML monitoring and drift detection
+# TensorFlow with bundled CUDA/cuDNN (eliminates version mismatch issues)
+tensorflow[and-cuda]==2.21.0   # Includes CUDA 12.5 + cuDNN 9 (WSL2/Linux only)
+keras==3.14.0                   # Deep learning API (included with TensorFlow)
+keras-tuner==1.4.7              # Hyperparameter optimization
+
+torch==2.3.0                    # PyTorch (numpy 2.x compatible)
+transformers==4.36.2            # Hugging Face transformers (FinBERT)
+sentencepiece==0.1.99           # Tokenizer for transformers
+onnxruntime==1.17.1             # ONNX inference engine
+evidently==0.4.33               # ML monitoring and drift detection
+
+# XGBoost for gradient boosting
+xgboost==2.0.3                  # Gradient boosting framework
+
+# ONNX conversion libraries
+onnxmltools==1.12.0             # XGBoost to ONNX converter
+tf2onnx==1.17.0                 # TensorFlow/Keras to ONNX converter (compatible with TF 2.21)
+skl2onnx==1.17.0                # Scikit-learn to ONNX (fallback)
+
+# Hyperparameter optimization
+optuna==3.5.0                   # Bayesian optimization framework
+
+# Scientific computing
+scikit-learn==1.4.0             # ML utilities and metrics
```

## Installation Instructions

### Fresh Installation

```bash
cd backend
source .venv/bin/activate

# Install TensorFlow with bundled CUDA
pip install tensorflow[and-cuda]==2.21.0 keras==3.14.0

# Install remaining ML packages
pip install keras-tuner==1.4.7 xgboost==2.0.3 \
    onnxmltools==1.12.0 tf2onnx==1.17.0 skl2onnx==1.17.0 \
    optuna==3.5.0 scikit-learn==1.4.0

# Verify installation
python -c "import tensorflow as tf; print('GPU:', tf.config.list_physical_devices('GPU'))"
```

### Upgrading Existing Installation

```bash
cd backend
source .venv/bin/activate

# Uninstall old TensorFlow
pip uninstall -y tensorflow tensorflow-estimator tensorflow-io-gcs-filesystem keras

# Install TensorFlow with bundled CUDA
pip install tensorflow[and-cuda]==2.21.0 keras==3.14.0

# Install remaining packages
pip install keras-tuner==1.4.7 xgboost==2.0.3 \
    onnxmltools==1.12.0 tf2onnx==1.17.0 skl2onnx==1.17.0 \
    optuna==3.5.0 scikit-learn==1.4.0
```

## Platform Compatibility

### Supported Platforms

✅ **Linux (including WSL2):** Full support with bundled CUDA  
✅ **Windows Native:** CPU-only (no GPU support since TensorFlow 2.10)  
✅ **macOS:** CPU-only (use `tensorflow-metal` for Apple Silicon)

### GPU Requirements

- **NVIDIA GPU:** Compute capability 3.5 or higher
- **Driver:** NVIDIA driver 450.80.02 or higher
- **Memory:** Minimum 2GB VRAM (4GB+ recommended)

**Supported GPUs:**
- RTX 30/40/50 series (Ampere/Ada Lovelace/Blackwell)
- GTX 16 series (Turing)
- GTX 10 series (Pascal)
- Tesla V100/A100/H100 (Volta/Ampere/Hopper)

## Performance Optimizations

### Mixed Precision Training (FP16)

Already enabled in `gru_trainer.py`:

```python
import tensorflow as tf
tf.keras.mixed_precision.set_global_policy('mixed_float16')
```

**Benefits:**
- 2x faster training on modern GPUs
- 50% memory reduction
- Maintained accuracy with loss scaling

### GPU Memory Growth

Already enabled in `gru_trainer.py`:

```python
gpus = tf.config.list_physical_devices('GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
```

**Benefits:**
- Prevents TensorFlow from allocating all GPU memory
- Allows multiple processes to share GPU
- Reduces OOM errors

## Troubleshooting

### Issue: "No module named 'tensorflow'"

**Solution:**
```bash
pip install tensorflow[and-cuda]==2.21.0
```

### Issue: "GPU not detected"

**Check:**
```bash
nvidia-smi  # Verify GPU is visible
python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"
```

**Solution:** Ensure NVIDIA driver is installed (WSL2 uses Windows driver)

### Issue: "Out of memory" during training

**Solution 1:** Reduce batch size in training config
```python
config.batch_size = 128  # Reduce from 256
```

**Solution 2:** Enable memory growth (already enabled)

**Solution 3:** Use gradient accumulation
```python
# In training loop
for i in range(accumulation_steps):
    with tf.GradientTape() as tape:
        loss = model(X_batch[i])
    gradients = tape.gradient(loss, model.trainable_variables)
    accumulated_gradients += gradients
optimizer.apply_gradients(zip(accumulated_gradients, model.trainable_variables))
```

### Issue: "CUDA out of memory" with multiple models

**Solution:** Clear GPU memory between models
```python
import tensorflow as tf
from keras import backend as K

# After training XGBoost
K.clear_session()
tf.keras.backend.clear_session()

# Before training GRU
gpus = tf.config.list_physical_devices('GPU')
if gpus:
    tf.config.experimental.reset_memory_stats(gpus[0])
```

## Best Practices

### 1. Always Use Bundled CUDA in Production

❌ **Don't:**
```bash
pip install tensorflow==2.21.0  # System CUDA dependency
```

✅ **Do:**
```bash
pip install tensorflow[and-cuda]==2.21.0  # Bundled CUDA
```

### 2. Pin Exact Versions

```txt
tensorflow[and-cuda]==2.21.0  # Not >=2.21.0
keras==3.14.0                 # Not >=3.14.0
```

### 3. Verify GPU in CI/CD

```bash
# In deployment script
python -c "
import tensorflow as tf
gpus = tf.config.list_physical_devices('GPU')
if not gpus:
    raise RuntimeError('No GPU detected')
print(f'✓ GPU detected: {gpus}')
"
```

### 4. Monitor GPU Utilization

```bash
# During training
watch -n 1 nvidia-smi
```

**Expected:** 80-100% GPU utilization during training

## References

- [TensorFlow GPU Installation Guide](https://www.tensorflow.org/install/pip)
- [TensorFlow 2.21 Release Notes](https://github.com/tensorflow/tensorflow/releases/tag/v2.21.0)
- [NVIDIA CUDA Compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/)
- [TensorFlow Mixed Precision](https://www.tensorflow.org/guide/mixed_precision)

## Changelog

### 2026-04-18 - Initial Fix

- **Issue:** `cudaGetDevice() failed. Status: device kernel image is invalid`
- **Root Cause:** CUDA version mismatch (system 12.2.2 vs TensorFlow 12.5.1)
- **Solution:** Switched to `tensorflow[and-cuda]` with bundled CUDA 12.5.1
- **Impact:** Complete resolution, all GPU operations now functional
- **Verification:** Tested with matrix multiplication, model creation, and training

---

**Status:** ✅ **RESOLVED**  
**Production Ready:** ✅ **YES**  
**Performance Impact:** ✅ **IMPROVED** (native CUDA 12.5.1 optimizations)

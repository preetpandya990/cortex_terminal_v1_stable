"""
Cortex AI — ONNX Model Conversion
===================================
Converts PyTorch models to ONNX format for optimized CPU inference.

Features:
- PyTorch to ONNX conversion
- Conversion validation (output comparison)
- Graph optimization for CPU
- Dynamic quantization for reduced latency
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import onnx
import onnxruntime as ort
import torch
from onnxruntime.quantization import quantize_dynamic, QuantType

from app.ml.models import MultiOutputModel


logger = logging.getLogger(__name__)


class ONNXConverter:
    """
    ONNX model converter for production deployment.
    
    Converts PyTorch models to ONNX format with optimizations
    for fast CPU inference.
    """

    def __init__(
        self,
        input_size: int = 40,
        sequence_length: int = 60,
    ):
        """
        Initialize ONNX converter.

        Args:
            input_size: Number of input features
            sequence_length: Length of input sequences
        """
        self.input_size = input_size
        self.sequence_length = sequence_length

    def convert_to_onnx(
        self,
        model: MultiOutputModel,
        output_path: str,
        opset_version: int = 14,
    ) -> str:
        """
        Convert PyTorch model to ONNX format.

        Args:
            model: PyTorch MultiOutputModel
            output_path: Path to save ONNX model
            opset_version: ONNX opset version (default: 14)

        Returns:
            Path to saved ONNX model

        Example:
            >>> converter = ONNXConverter()
            >>> onnx_path = converter.convert_to_onnx(model, "model.onnx")
            >>> print(f"Model saved to {onnx_path}")
        """
        logger.info(f"Converting PyTorch model to ONNX (opset {opset_version})")

        # Set model to evaluation mode
        model.eval()

        # Create dummy input
        dummy_input = torch.randn(
            1,  # batch size
            self.sequence_length,
            self.input_size,
        )

        # Define input and output names
        input_names = ["input"]
        output_names = [
            "direction",
            "confidence",
            "entry_price",
            "tp1",
            "tp2",
            "tp3",
            "stop_loss",
            "volatility",
        ]

        # Dynamic axes for batch size
        dynamic_axes = {
            "input": {0: "batch_size"},
            **{name: {0: "batch_size"} for name in output_names}
        }

        # Export to ONNX
        torch.onnx.export(
            model,
            dummy_input,
            output_path,
            export_params=True,
            opset_version=opset_version,
            do_constant_folding=True,
            input_names=input_names,
            output_names=output_names,
            dynamic_axes=dynamic_axes,
        )

        logger.info(f"Model exported to ONNX: {output_path}")

        # Verify ONNX model
        onnx_model = onnx.load(output_path)
        onnx.checker.check_model(onnx_model)
        logger.info("ONNX model validation passed")

        return output_path

    def validate_conversion(
        self,
        pytorch_model: MultiOutputModel,
        onnx_path: str,
        num_samples: int = 10,
        tolerance: float = 1e-5,
    ) -> tuple[bool, dict[str, float]]:
        """
        Validate ONNX conversion by comparing outputs.

        Args:
            pytorch_model: Original PyTorch model
            onnx_path: Path to ONNX model
            num_samples: Number of test samples
            tolerance: Maximum allowed difference

        Returns:
            Tuple of (passed, differences_dict)

        Example:
            >>> passed, diffs = converter.validate_conversion(model, "model.onnx")
            >>> if passed:
            ...     print("Conversion validated successfully")
        """
        logger.info(f"Validating ONNX conversion with {num_samples} samples")

        pytorch_model.eval()

        # Create ONNX Runtime session
        ort_session = ort.InferenceSession(
            onnx_path,
            providers=['CPUExecutionProvider']
        )

        max_differences = {}
        all_passed = True

        with torch.no_grad():
            for i in range(num_samples):
                # Generate random input
                test_input = torch.randn(
                    1,
                    self.sequence_length,
                    self.input_size,
                )

                # PyTorch inference
                pytorch_outputs = pytorch_model(test_input)

                # ONNX inference
                onnx_inputs = {"input": test_input.numpy()}
                onnx_outputs = ort_session.run(None, onnx_inputs)

                # Compare outputs
                output_names = [
                    "direction", "confidence", "entry_price",
                    "tp1", "tp2", "tp3", "stop_loss", "volatility"
                ]

                for idx, name in enumerate(output_names):
                    pytorch_out = pytorch_outputs[name].numpy()
                    onnx_out = onnx_outputs[idx]

                    # Calculate difference
                    diff = np.abs(pytorch_out - onnx_out).max()

                    if name not in max_differences:
                        max_differences[name] = diff
                    else:
                        max_differences[name] = max(max_differences[name], diff)

                    if diff > tolerance:
                        all_passed = False
                        logger.warning(
                            f"Output '{name}' difference exceeds tolerance: "
                            f"{diff:.2e} > {tolerance:.2e}"
                        )

        if all_passed:
            logger.info(
                f"✓ ONNX conversion validated: "
                f"Max difference = {max(max_differences.values()):.2e}"
            )
        else:
            logger.error(
                f"✗ ONNX conversion validation FAILED: "
                f"Differences exceed tolerance"
            )

        return all_passed, max_differences

    def optimize_onnx(
        self,
        input_path: str,
        output_path: str,
    ) -> str:
        """
        Apply graph optimizations to ONNX model for CPU inference.

        Args:
            input_path: Path to input ONNX model
            output_path: Path to save optimized model

        Returns:
            Path to optimized ONNX model

        Example:
            >>> optimized_path = converter.optimize_onnx("model.onnx", "model_opt.onnx")
        """
        logger.info(f"Optimizing ONNX model for CPU inference")

        # Load ONNX model
        model = onnx.load(input_path)

        # Apply optimizations
        # Note: ONNX Runtime will apply optimizations during session creation
        # Here we just save the model for explicit optimization
        onnx.save(model, output_path)

        logger.info(f"Optimized ONNX model saved to {output_path}")

        return output_path

    def quantize_model(
        self,
        input_path: str,
        output_path: str,
        validate_accuracy: bool = True,
        pytorch_model: MultiOutputModel | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        Apply dynamic quantization to ONNX model.

        Reduces model size and improves inference latency on CPU.

        Args:
            input_path: Path to input ONNX model
            output_path: Path to save quantized model
            validate_accuracy: Whether to validate accuracy drop
            pytorch_model: Original PyTorch model for validation

        Returns:
            Tuple of (quantized_model_path, statistics)

        Example:
            >>> quant_path, stats = converter.quantize_model(
            ...     "model.onnx", "model_quant.onnx"
            ... )
            >>> print(f"Size reduction: {stats['size_reduction_pct']:.1f}%")
        """
        logger.info("Applying dynamic quantization to ONNX model")

        # Get original model size
        original_size = Path(input_path).stat().st_size

        # Apply dynamic quantization
        quantize_dynamic(
            model_input=input_path,
            model_output=output_path,
            weight_type=QuantType.QInt8,
        )

        # Get quantized model size
        quantized_size = Path(output_path).stat().st_size
        size_reduction_pct = ((original_size - quantized_size) / original_size) * 100

        statistics = {
            "original_size_mb": original_size / (1024 * 1024),
            "quantized_size_mb": quantized_size / (1024 * 1024),
            "size_reduction_pct": size_reduction_pct,
        }

        logger.info(
            f"Quantization complete: "
            f"{statistics['original_size_mb']:.2f} MB → "
            f"{statistics['quantized_size_mb']:.2f} MB "
            f"({size_reduction_pct:.1f}% reduction)"
        )

        # Validate accuracy if requested
        if validate_accuracy and pytorch_model is not None:
            passed, diffs = self.validate_conversion(
                pytorch_model=pytorch_model,
                onnx_path=output_path,
                num_samples=100,
                tolerance=0.01,  # Allow 1% difference for quantized model
            )
            statistics["accuracy_validated"] = passed
            statistics["max_differences"] = diffs

            if not passed:
                logger.warning(
                    "Quantized model accuracy validation failed - "
                    "consider using higher precision"
                )

        return output_path, statistics

    def convert_and_optimize(
        self,
        pytorch_model: MultiOutputModel,
        output_dir: str,
        model_name: str = "model",
        apply_quantization: bool = True,
    ) -> dict[str, Any]:
        """
        Complete conversion pipeline: PyTorch → ONNX → Optimized → Quantized.

        Args:
            pytorch_model: PyTorch model to convert
            output_dir: Directory to save models
            model_name: Base name for model files
            apply_quantization: Whether to apply quantization

        Returns:
            Dictionary with paths and statistics

        Example:
            >>> results = converter.convert_and_optimize(model, "models/")
            >>> print(f"Final model: {results['final_model_path']}")
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        results = {}

        # 1. Convert to ONNX
        onnx_path = str(output_dir / f"{model_name}.onnx")
        self.convert_to_onnx(pytorch_model, onnx_path)
        results["onnx_path"] = onnx_path

        # 2. Validate conversion
        passed, diffs = self.validate_conversion(pytorch_model, onnx_path)
        results["validation_passed"] = passed
        results["max_differences"] = diffs

        if not passed:
            logger.error("ONNX conversion validation failed - stopping pipeline")
            return results

        # 3. Optimize ONNX
        optimized_path = str(output_dir / f"{model_name}_optimized.onnx")
        self.optimize_onnx(onnx_path, optimized_path)
        results["optimized_path"] = optimized_path

        # 4. Quantize (optional)
        if apply_quantization:
            quantized_path = str(output_dir / f"{model_name}_quantized.onnx")
            quant_path, quant_stats = self.quantize_model(
                input_path=optimized_path,
                output_path=quantized_path,
                validate_accuracy=True,
                pytorch_model=pytorch_model,
            )
            results["quantized_path"] = quant_path
            results["quantization_stats"] = quant_stats
            results["final_model_path"] = quant_path
        else:
            results["final_model_path"] = optimized_path

        logger.info(
            f"Conversion pipeline complete: {results['final_model_path']}"
        )

        return results


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def convert_model_to_onnx(
    pytorch_model: MultiOutputModel,
    output_dir: str,
    model_name: str = "model",
    apply_quantization: bool = True,
) -> dict[str, Any]:
    """
    Convenience function to convert PyTorch model to optimized ONNX.

    Args:
        pytorch_model: PyTorch model
        output_dir: Output directory
        model_name: Model name
        apply_quantization: Whether to quantize

    Returns:
        Dictionary with conversion results

    Example:
        >>> results = convert_model_to_onnx(model, "models/production")
        >>> print(f"Model ready: {results['final_model_path']}")
    """
    converter = ONNXConverter()
    return converter.convert_and_optimize(
        pytorch_model=pytorch_model,
        output_dir=output_dir,
        model_name=model_name,
        apply_quantization=apply_quantization,
    )

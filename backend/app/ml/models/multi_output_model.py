"""
Cortex AI — Multi-Output LSTM + Transformer Model
===================================================
Hybrid architecture combining LSTM for temporal patterns and Transformer
for attention mechanism, with 7 output heads for comprehensive predictions.

Architecture:
- LSTM Encoder: 2 layers, 128 hidden units, bidirectional
- Transformer Encoder: 8 attention heads, 2 layers, 128 dim
- Multi-Output Heads: 7 outputs (direction, confidence, entry, TP1-3, SL, volatility)

Outputs:
1. direction: Classification (BUY=2, HOLD=1, SELL=0)
2. confidence: Regression [0, 1]
3. entry_price: Regression (current price)
4. tp1, tp2, tp3: Regression (target profit levels)
5. stop_loss: Regression (stop loss level)
6. volatility: Regression (predicted volatility)
"""
from __future__ import annotations

import logging
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F


logger = logging.getLogger(__name__)


class MultiOutputModel(nn.Module):
    """
    LSTM + Transformer hybrid model with multiple output heads.
    
    This architecture combines:
    - LSTM for capturing temporal dependencies in sequential data
    - Transformer for learning attention patterns across time steps
    - Multiple output heads for simultaneous prediction of 7 targets
    """

    def __init__(
        self,
        input_size: int = 40,
        sequence_length: int = 60,
        lstm_hidden_size: int = 128,
        lstm_num_layers: int = 2,
        lstm_dropout: float = 0.2,
        transformer_d_model: int = 128,
        transformer_nhead: int = 8,
        transformer_num_layers: int = 2,
        transformer_dropout: float = 0.1,
    ):
        """
        Initialize multi-output model.

        Args:
            input_size: Number of input features (default: 40)
            sequence_length: Length of input sequences (default: 60)
            lstm_hidden_size: LSTM hidden dimension (default: 128)
            lstm_num_layers: Number of LSTM layers (default: 2)
            lstm_dropout: LSTM dropout rate (default: 0.2)
            transformer_d_model: Transformer model dimension (default: 128)
            transformer_nhead: Number of attention heads (default: 8)
            transformer_num_layers: Number of transformer layers (default: 2)
            transformer_dropout: Transformer dropout rate (default: 0.1)
        """
        super().__init__()
        
        self.input_size = input_size
        self.sequence_length = sequence_length
        self.lstm_hidden_size = lstm_hidden_size
        self.transformer_d_model = transformer_d_model

        # ── LSTM Encoder ───────────────────────────────────────────────────────
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=lstm_hidden_size,
            num_layers=lstm_num_layers,
            dropout=lstm_dropout if lstm_num_layers > 1 else 0,
            bidirectional=True,
            batch_first=True,
        )

        # Bidirectional LSTM outputs 2 * hidden_size
        lstm_output_size = lstm_hidden_size * 2

        # ── Projection Layer (LSTM → Transformer) ──────────────────────────────
        self.lstm_to_transformer = nn.Linear(lstm_output_size, transformer_d_model)

        # ── Transformer Encoder ────────────────────────────────────────────────
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=transformer_d_model,
            nhead=transformer_nhead,
            dim_feedforward=transformer_d_model * 4,
            dropout=transformer_dropout,
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(
            encoder_layer=encoder_layer,
            num_layers=transformer_num_layers,
        )

        # ── Global Pooling ─────────────────────────────────────────────────────
        # Combine sequence outputs into single vector
        self.global_pool = nn.AdaptiveAvgPool1d(1)

        # ── Multi-Output Heads ─────────────────────────────────────────────────
        
        # Direction head (classification: BUY=2, HOLD=1, SELL=0)
        self.direction_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 3),  # 3 classes
        )

        # Confidence head (regression: [0, 1])
        self.confidence_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
            nn.Sigmoid(),  # Output in [0, 1]
        )

        # Price regression heads (entry, TP1-3, SL)
        self.entry_price_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
        )

        self.tp1_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
        )

        self.tp2_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
        )

        self.tp3_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
        )

        self.stop_loss_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
        )

        # Volatility head (regression: positive values)
        self.volatility_head = nn.Sequential(
            nn.Linear(transformer_d_model, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
            nn.ReLU(),  # Ensure positive volatility
        )

        logger.info(
            f"Initialized MultiOutputModel: "
            f"input_size={input_size}, sequence_length={sequence_length}, "
            f"lstm_hidden={lstm_hidden_size}, transformer_d_model={transformer_d_model}"
        )

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        """
        Forward pass through the model.

        Args:
            x: Input tensor of shape (batch_size, sequence_length, input_size)

        Returns:
            Dictionary with output tensors:
            {
                "direction": (batch_size, 3),  # Logits for 3 classes
                "confidence": (batch_size, 1),
                "entry_price": (batch_size, 1),
                "tp1": (batch_size, 1),
                "tp2": (batch_size, 1),
                "tp3": (batch_size, 1),
                "stop_loss": (batch_size, 1),
                "volatility": (batch_size, 1),
            }
        """
        batch_size = x.size(0)

        # 1. LSTM Encoder
        lstm_out, _ = self.lstm(x)  # (batch, seq_len, lstm_hidden * 2)

        # 2. Project to Transformer dimension
        transformer_input = self.lstm_to_transformer(lstm_out)  # (batch, seq_len, d_model)

        # 3. Transformer Encoder
        transformer_out = self.transformer(transformer_input)  # (batch, seq_len, d_model)

        # 4. Global pooling (average across sequence)
        # Transpose for pooling: (batch, d_model, seq_len)
        pooled = self.global_pool(transformer_out.transpose(1, 2))  # (batch, d_model, 1)
        pooled = pooled.squeeze(-1)  # (batch, d_model)

        # 5. Multi-output heads
        outputs = {
            "direction": self.direction_head(pooled),  # (batch, 3)
            "confidence": self.confidence_head(pooled),  # (batch, 1)
            "entry_price": self.entry_price_head(pooled),  # (batch, 1)
            "tp1": self.tp1_head(pooled),  # (batch, 1)
            "tp2": self.tp2_head(pooled),  # (batch, 1)
            "tp3": self.tp3_head(pooled),  # (batch, 1)
            "stop_loss": self.stop_loss_head(pooled),  # (batch, 1)
            "volatility": self.volatility_head(pooled),  # (batch, 1)
        }

        return outputs

    def get_num_parameters(self) -> int:
        """Get total number of trainable parameters."""
        return sum(p.numel() for p in self.parameters() if p.requires_grad)


class MultiOutputLoss(nn.Module):
    """
    Custom loss function combining classification and regression losses.
    
    Loss components:
    1. Direction loss: CrossEntropyLoss (classification)
    2. Regression losses: MSELoss for continuous outputs
    3. Consistency penalty: Enforce TP ordering and entry/SL/TP relationships
    
    Loss weights are configurable to balance different objectives.
    """

    def __init__(
        self,
        direction_weight: float = 2.0,
        confidence_weight: float = 1.0,
        price_weight: float = 1.5,
        volatility_weight: float = 1.0,
        consistency_weight: float = 0.5,
    ):
        """
        Initialize multi-output loss.

        Args:
            direction_weight: Weight for direction classification loss
            confidence_weight: Weight for confidence regression loss
            price_weight: Weight for price regression losses
            volatility_weight: Weight for volatility regression loss
            consistency_weight: Weight for consistency penalty
        """
        super().__init__()
        
        self.direction_weight = direction_weight
        self.confidence_weight = confidence_weight
        self.price_weight = price_weight
        self.volatility_weight = volatility_weight
        self.consistency_weight = consistency_weight

        # Loss functions
        self.ce_loss = nn.CrossEntropyLoss()
        self.mse_loss = nn.MSELoss()

    def forward(
        self,
        predictions: dict[str, torch.Tensor],
        targets: dict[str, torch.Tensor],
    ) -> tuple[torch.Tensor, dict[str, float]]:
        """
        Compute total loss and individual loss components.

        Args:
            predictions: Dictionary of model predictions
            targets: Dictionary of ground truth targets

        Returns:
            Tuple of (total_loss, loss_dict)
        """
        # 1. Direction loss (classification)
        direction_loss = self.ce_loss(
            predictions["direction"],
            targets["direction"].long(),
        )

        # 2. Confidence loss (regression)
        confidence_loss = self.mse_loss(
            predictions["confidence"],
            targets["confidence"].unsqueeze(1),
        )

        # 3. Price regression losses
        entry_loss = self.mse_loss(
            predictions["entry_price"],
            targets["entry_price"].unsqueeze(1),
        )

        tp1_loss = self.mse_loss(
            predictions["tp1"],
            targets["tp1"].unsqueeze(1),
        )

        tp2_loss = self.mse_loss(
            predictions["tp2"],
            targets["tp2"].unsqueeze(1),
        )

        tp3_loss = self.mse_loss(
            predictions["tp3"],
            targets["tp3"].unsqueeze(1),
        )

        stop_loss_loss = self.mse_loss(
            predictions["stop_loss"],
            targets["stop_loss"].unsqueeze(1),
        )

        # 4. Volatility loss (regression)
        volatility_loss = self.mse_loss(
            predictions["volatility"],
            targets["volatility"].unsqueeze(1),
        )

        # 5. Consistency penalty
        consistency_penalty = self._compute_consistency_penalty(predictions)

        # Combine losses with weights
        total_loss = (
            self.direction_weight * direction_loss
            + self.confidence_weight * confidence_loss
            + self.price_weight * (entry_loss + tp1_loss + tp2_loss + tp3_loss + stop_loss_loss)
            + self.volatility_weight * volatility_loss
            + self.consistency_weight * consistency_penalty
        )

        # Loss breakdown for logging
        loss_dict = {
            "total": total_loss.item(),
            "direction": direction_loss.item(),
            "confidence": confidence_loss.item(),
            "entry_price": entry_loss.item(),
            "tp1": tp1_loss.item(),
            "tp2": tp2_loss.item(),
            "tp3": tp3_loss.item(),
            "stop_loss": stop_loss_loss.item(),
            "volatility": volatility_loss.item(),
            "consistency": consistency_penalty.item(),
        }

        return total_loss, loss_dict

    def _compute_consistency_penalty(
        self,
        predictions: dict[str, torch.Tensor],
    ) -> torch.Tensor:
        """
        Compute consistency penalty to enforce logical constraints.

        Constraints:
        1. TP ordering: TP1 < TP2 < TP3 (for BUY direction)
        2. Entry/SL/TP relationships: SL < Entry < TP1 (for BUY)
        
        Args:
            predictions: Dictionary of model predictions

        Returns:
            Consistency penalty (scalar tensor)
        """
        penalty = torch.tensor(0.0, device=predictions["entry_price"].device)

        # Extract predictions
        entry = predictions["entry_price"].squeeze()
        tp1 = predictions["tp1"].squeeze()
        tp2 = predictions["tp2"].squeeze()
        tp3 = predictions["tp3"].squeeze()
        sl = predictions["stop_loss"].squeeze()

        # 1. TP ordering penalty: TP1 < TP2 < TP3
        tp_order_penalty = (
            F.relu(tp1 - tp2) + F.relu(tp2 - tp3)
        ).mean()

        # 2. Entry/SL/TP relationship penalty
        # For BUY: SL < Entry < TP1
        # Penalize violations
        sl_entry_penalty = F.relu(sl - entry).mean()
        entry_tp1_penalty = F.relu(entry - tp1).mean()

        penalty = tp_order_penalty + sl_entry_penalty + entry_tp1_penalty

        return penalty


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def create_model(
    input_size: int = 40,
    sequence_length: int = 60,
    device: str = "cpu",
) -> MultiOutputModel:
    """
    Factory function to create and initialize model.

    Args:
        input_size: Number of input features
        sequence_length: Length of input sequences
        device: Device to place model on ("cpu" or "cuda")

    Returns:
        Initialized MultiOutputModel
    """
    model = MultiOutputModel(
        input_size=input_size,
        sequence_length=sequence_length,
    )
    
    model = model.to(device)
    
    num_params = model.get_num_parameters()
    logger.info(
        f"Created model with {num_params:,} trainable parameters on {device}"
    )
    
    return model

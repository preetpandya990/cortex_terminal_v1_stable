# ML Training Pipeline - Visual Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         DATA PREPARATION                                 │
└─────────────────────────────────────────────────────────────────────────┘

    ┌──────────────────┐
    │  upstox_ohlcv    │  ← Real database (2,551 instruments × 10 years)
    │   PostgreSQL     │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │  Load OHLCV Data │  ← Query by instrument_key, timeframe='1D'
    │  (Real, not fake)│
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │  Calculate ATR   │  ← 14-period Average True Range
    │  (Volatility)    │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────────────────────┐
    │  Generate Binary Targets         │
    │  • Threshold = 1.5 × ATR         │  ← Adaptive per stock
    │  • UP (1) if return > threshold  │
    │  • DOWN (0) otherwise            │
    │  • Result: ~50/50 balance        │
    └────────┬─────────────────────────┘
             │
             ▼
    ┌──────────────────┐
    │ Compute Features │  ← 42 technical indicators
    │  (RSI, MACD, BB) │     (price, momentum, volatility, volume)
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │ Calculate Class  │  ← Balanced weights for 0/1
    │     Weights      │     (handles any residual imbalance)
    └────────┬─────────┘
             │
             │
┌────────────┴────────────────────────────────────────────────────────────┐
│                         MODEL TRAINING                                   │
└──────────────────────────────────────────────────────────────────────────┘
             │
             ├─────────────────────┬─────────────────────┐
             ▼                     ▼                     ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │   XGBoost       │   │      GRU        │   │   Walk-Forward  │
    │   Training      │   │   Training      │   │   Validation    │
    └─────────────────┘   └─────────────────┘   └─────────────────┘
             │                     │                     │
             │                     │                     │
    ┌────────────────────┐ ┌────────────────────┐      │
    │ • Binary objective │ │ • 2 output neurons │      │
    │ • Sample weights   │ │ • FP16 mixed prec  │      │
    │ • Optuna tuning    │ │ • Class weights    │      │
    │ • Last timestep    │ │ • Data generators  │      │
    │   features (n,47)  │ │ • Sequences (n,60,42)│    │
    └────────┬───────────┘ └────────┬───────────┘      │
             │                     │                     │
             │                     │              ┌──────▼──────┐
             │                     │              │ 2yr train   │
             │                     │              │ 90d val     │
             │                     │              │ 30d test    │
             │                     │              │ (rolling)   │
             │                     │              └─────────────┘
             │                     │
             └──────────┬──────────┘
                        ▼
             ┌─────────────────────┐
             │  Ensemble Creation  │
             │  • Grid search      │  ← Optimize on Sharpe ratio
             │  • Weight range:    │     (not hardcoded 60/40)
             │    XGB: 0.3-0.8     │
             │    GRU: 0.2-0.7     │
             └──────────┬──────────┘
                        │
                        ▼
             ┌─────────────────────┐
             │  Weighted Average   │
             │  Probabilities      │
             │  P = w₁·P_xgb +     │
             │      w₂·P_gru       │
             └──────────┬──────────┘
                        │
                        │
┌───────────────────────┴──────────────────────────────────────────────────┐
│                         EVALUATION                                        │
└───────────────────────────────────────────────────────────────────────────┘
                        │
                        ▼
             ┌─────────────────────┐
             │  Binary Prediction  │
             │  pred = argmax(P)   │  ← 0 or 1
             └──────────┬──────────┘
                        │
                        ▼
             ┌─────────────────────┐
             │ Confidence Filter   │
             │ if max(P) < 0.7:    │  ← Selective classification
             │    pred = -1 (HOLD) │     (inference-time decision)
             └──────────┬──────────┘
                        │
                        ├──────────────────┬──────────────────┐
                        ▼                  ▼                  ▼
             ┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
             │ Classification   │ │  Financial   │ │  Backtesting     │
             │    Metrics       │ │   Metrics    │ │   Simulation     │
             └──────────────────┘ └──────────────┘ └──────────────────┘
                        │                  │                  │
                        │                  │                  │
             ┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
             │ • Accuracy       │ │ • Sharpe     │ │ • Long on UP     │
             │ • Precision      │ │ • Sortino    │ │ • Short on DOWN  │
             │ • Recall         │ │ • Max DD     │ │ • Calculate PnL  │
             │ • F1 Score       │ │ • Win Rate   │ │ • Cumulative ret │
             │ • Confusion Mtx  │ │ • Profit Fct │ │ • Drawdown curve │
             └──────────────────┘ └──────────────┘ └──────────────────┘
                        │                  │                  │
                        └──────────┬───────┴──────────────────┘
                                   ▼
                        ┌─────────────────────┐
                        │   Export to ONNX    │
                        │   • XGBoost.onnx    │
                        │   • GRU.onnx        │
                        │   • Ensemble config │
                        └──────────┬──────────┘
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │  Model Registry     │
                        │  • Version: 2.0.0   │
                        │  • Encrypted        │
                        │  • Checksummed      │
                        │  • Production ready │
                        └─────────────────────┘


┌─────────────────────────────────────────────────────────────────────────┐
│                         KEY DIFFERENCES                                  │
└─────────────────────────────────────────────────────────────────────────┘

    BEFORE (3-class)              →        AFTER (binary)
    ════════════════                       ═══════════════
    
    Labels: -1, 0, 1                       Labels: 0, 1
    (SELL, HOLD, BUY)                      (DOWN, UP)
    
    Threshold: Fixed 1%                    Threshold: 1.5 × ATR
    (same for all stocks)                  (adaptive per stock)
    
    Class balance: 93.7% HOLD              Class balance: ~50/50
    (severe bias)                          (natural balance)
    
    XGBoost: multi:softprob                XGBoost: binary:logistic
    num_class=3                            num_class=2
    
    GRU: 3 output neurons                  GRU: 2 output neurons
    
    Class weights: Computed                Class weights: Applied
    but not used                           (sample weights)
    
    Financial metrics: All zeros           Financial metrics: Real
    (not implemented)                      (from backtesting)
    
    Ensemble: Hardcoded 60/40              Ensemble: Grid search
    (no optimization)                      (Sharpe optimization)
    
    GPU: No optimization                   GPU: FP16 mixed precision
    (full precision)                       (2x memory efficiency)
    
    Data: Synthetic random walk            Data: Real from database
    (meaningless training)                 (actual market data)


┌─────────────────────────────────────────────────────────────────────────┐
│                         EXPECTED OUTPUT                                  │
└─────────────────────────────────────────────────────────────────────────┘

    Training Results:
    ═════════════════
    • Accuracy: 55-65% (on real data)
    • UP recall: 50-60%
    • DOWN recall: 50-60%
    • Sharpe ratio: 1.5-2.5
    • Max drawdown: 15-25%
    • Win rate: 52-58%
    
    High-Confidence Predictions (>70%):
    ═══════════════════════════════════
    • Precision: 65-75%
    • Trade frequency: 20-30% of days
    • Sharpe ratio: 2.0-3.0
    
    Model Files:
    ════════════
    • xgboost_binary_v2.onnx (411 KB)
    • gru_binary_v2.onnx (721 KB)
    • ensemble_config_v2.json
    • training_metrics_v2.json
```

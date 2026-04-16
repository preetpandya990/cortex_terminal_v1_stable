"""API V1 routes."""

import app.api.v1.auth as auth
import app.api.v1.hawk_eye as hawk_eye
import app.api.v1.market_data as market_data
import app.api.v1.scanner as scanner
import app.api.v1.health as health
import app.api.v1.upstox as upstox
import app.api.v1.ml_predictions as ml_predictions

__all__ = ["auth", "hawk_eye", "health", "market_data", "scanner", "upstox", "ml_predictions"]

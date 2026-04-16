#!/usr/bin/env python3
"""
Script to add audit logging and Request parameter to ml_predictions.py
"""

import re

# Read the file
with open('backend/app/api/v1/ml_predictions.py', 'r') as f:
    content = f.read()

# Add Request import if not present
if 'from fastapi import APIRouter, Depends, HTTPException, status' in content:
    content = content.replace(
        'from fastapi import APIRouter, Depends, HTTPException, status',
        'from fastapi import APIRouter, Depends, HTTPException, Request, status'
    )

# Add request parameter to predict_single function
content = re.sub(
    r'(async def predict_single\(\s+request: PredictionRequest,)',
    r'\1\n    fastapi_request: Request,',
    content
)

# Add request parameter to predict_batch function
content = re.sub(
    r'(async def predict_batch\(\s+request: BatchPredictionRequest,)',
    r'\1\n    fastapi_request: Request,',
    content
)

# Add request parameter to get_cached_prediction function
content = re.sub(
    r'(async def get_cached_prediction\(\s+symbol: str,)',
    r'\1\n    fastapi_request: Request,',
    content
)

# Add request parameter to predict_ensemble function
content = re.sub(
    r'(async def predict_ensemble\(\s+request: EnsemblePredictionRequest,)',
    r'\1\n    fastapi_request: Request,',
    content
)

# Write back
with open('backend/app/api/v1/ml_predictions.py', 'w') as f:
    f.write(content)

print("Added Request parameter and audit logging setup")

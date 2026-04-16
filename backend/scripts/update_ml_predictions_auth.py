#!/usr/bin/env python3
"""
Script to update ml_predictions.py with JWT authentication
"""

import re

# Read the file
with open('backend/app/api/v1/ml_predictions.py', 'r') as f:
    content = f.read()

# Replace current_user with user_id in function signatures
content = re.sub(
    r'current_user: dict = Depends\(get_current_user\)',
    r'user_id: str = Depends(get_current_user_id)',
    content
)

# Replace current_user references in docstrings
content = re.sub(
    r'current_user: Authenticated user',
    r'user_id: Authenticated user ID',
    content
)

# Add user_id to MLPrediction creation
content = re.sub(
    r'(db_prediction = MLPrediction\(\s+symbol=request\.symbol,)',
    r'\1\n                user_id=user_id,',
    content
)

# Update audit log creation to use user_id parameter instead of request.user_id
content = re.sub(
    r'user_id=request\.user_id,',
    r'user_id=user_id,',
    content
)

# Write back
with open('backend/app/api/v1/ml_predictions.py', 'w') as f:
    f.write(content)

print("Updated ml_predictions.py with JWT authentication")

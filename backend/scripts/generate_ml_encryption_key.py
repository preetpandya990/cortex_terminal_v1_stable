#!/usr/bin/env python3
"""
Generate Fernet encryption key for ML model artifacts.

Usage:
    python generate_ml_encryption_key.py

Output:
    Prints a base64-encoded 32-byte Fernet key to stdout.
    Add this to your .env file as ML_MODEL_ENCRYPTION_KEY.
"""

from cryptography.fernet import Fernet

if __name__ == "__main__":
    key = Fernet.generate_key()
    print("Generated ML Model Encryption Key:")
    print(key.decode())
    print("\nAdd this to your .env file:")
    print(f"ML_MODEL_ENCRYPTION_KEY={key.decode()}")

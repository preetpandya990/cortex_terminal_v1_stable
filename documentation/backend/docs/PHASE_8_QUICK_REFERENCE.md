# Phase 8 Quick Reference: Security Features

## Model Encryption

### Setup
```bash
# 1. Generate encryption key
python backend/scripts/generate_ml_encryption_key.py

# 2. Add to backend/.env
ML_MODEL_ENCRYPTION_KEY=your_generated_key_here

# 3. Apply database migration
cd backend
alembic upgrade head
```

### Usage
```python
# Models are automatically encrypted on registration
from app.ml.model_registry import get_model_registry

registry = get_model_registry(session)

# Register (encrypts automatically)
model = await registry.register_model(
    version="1.0.0",
    model_type="lstm_transformer",
    artifact_path="models/model.onnx",
    metrics={"accuracy": 0.87},
    metadata={},
    feature_version="v1.0.0",
)

# Load (decrypts and validates checksum)
plaintext = await registry.load_model_artifact(model)
```

### Security Properties
- **Algorithm**: Fernet (AES-128-CBC + HMAC-SHA256)
- **Checksum**: SHA256 (computed before encryption)
- **Key Storage**: Environment variable only
- **File Extension**: `.onnx.enc` (encrypted models)

---

## Rate Limiting

### Configuration
```bash
# backend/.env
ML_RATE_LIMIT_PREDICT=10/minute      # Single prediction
ML_RATE_LIMIT_BATCH=5/minute         # Batch prediction
ML_RATE_LIMIT_ENSEMBLE=5/minute      # Ensemble prediction
```

### How It Works
- **Hybrid Approach**: Both user_id AND IP must pass
- **User Limit**: 10 requests/minute (configurable)
- **IP Limit**: 20 requests/minute (2x user limit)
- **Algorithm**: Sliding window with Redis sorted sets
- **Response**: HTTP 429 with `Retry-After` header

### Monitoring
```python
# Check violations for user
violations = await redis.get(f"ml:ratelimit:violations:{user_id}")

# Log output
# WARNING: Rate limit exceeded: user=user123 ip=192.168.1.1 endpoint=/api/v1/ml/predict type=user limit=10/60s
```

---

## Error Handling

### Encryption Errors
```python
# Invalid encryption key
ModelEncryptionError: "Failed to decrypt model: invalid encryption key or corrupted data"

# Checksum mismatch (tampering detected)
ModelEncryptionError: "Model integrity check failed: expected checksum abc123..., got def456..."

# Missing artifact
FileNotFoundError: "Model artifact not found: /path/to/model.onnx.enc"
```

### Rate Limit Errors
```json
{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded: 10/minute per user",
  "limit": 10,
  "window": "minute",
  "retry_after": 60
}
```

---

## Best Practices

### Encryption
✅ **DO**:
- Generate unique keys per environment
- Store keys in environment variables only
- Rotate keys periodically
- Back up keys securely

❌ **DON'T**:
- Commit keys to source control
- Hard-code keys in application
- Share keys via insecure channels
- Use same key across environments

### Rate Limiting
✅ **DO**:
- Monitor violation rates
- Alert on repeated violations
- Adjust limits based on usage
- Use exponential backoff for retries

❌ **DON'T**:
- Set limits too low (impacts users)
- Set limits too high (allows abuse)
- Ignore violation logs
- Use only IP or only user limiting

---

## Testing

### Test Encryption
```bash
# Unit tests
pytest tests/unit/test_model_registry.py::test_model_encryption -v

# Manual test
python -c "
from cryptography.fernet import Fernet
key = Fernet.generate_key()
cipher = Fernet(key)
encrypted = cipher.encrypt(b'test data')
decrypted = cipher.decrypt(encrypted)
assert decrypted == b'test data'
print('Encryption test passed')
"
```

### Test Rate Limiting
```bash
# Make 15 requests (should fail after 10)
for i in {1..15}; do
  curl -X POST http://localhost:8000/api/v1/ml/predict \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"symbol": "AAPL", "timeframe": "1d"}'
  echo "Request $i"
  sleep 1
done
```

---

## Troubleshooting

### "Invalid encryption key" Error
**Cause**: Wrong key or corrupted data  
**Solution**: 
1. Verify `ML_MODEL_ENCRYPTION_KEY` in `.env`
2. Check key format (base64-encoded, 44 characters)
3. Re-encrypt models if key was rotated

### "Rate limit exceeded" for Legitimate User
**Cause**: Limits too low or multiple clients  
**Solution**:
1. Increase limits in `.env`
2. Check for multiple API clients using same credentials
3. Implement request queuing on client side

### "Model integrity check failed"
**Cause**: Model tampering or corruption  
**Solution**:
1. Re-register model from source
2. Check disk integrity
3. Verify no manual file modifications

---

## Production Checklist

Before deploying to production:

- [ ] Generate production encryption key
- [ ] Store key in secure secrets manager (Vault, AWS Secrets Manager)
- [ ] Set appropriate rate limits for production load
- [ ] Enable rate limit violation alerting
- [ ] Configure log aggregation for security events
- [ ] Test key rotation procedure
- [ ] Document incident response procedures
- [ ] Set up monitoring dashboards
- [ ] Configure automated backups of encrypted models
- [ ] Test disaster recovery procedures

---

**Phase 8 Status**: Tasks 31-32 Complete ✅  
**Next Phase**: Phase 9 (Testing & Validation)

# Upstox Credentials Configuration Guide

**Last Updated**: April 16, 2026

---

## 📍 Current Configuration Location

The Upstox credentials are configured in **environment variables** stored in `.env` files:

### Primary .env File Locations

1. **Backend**: `/home/preet/code/Cortex_Merge_AI-ML/backend/.env`
2. **Root** (for Docker Compose): `/home/preet/code/Cortex_Merge_AI-ML/.env`

---

## 🔑 Required Upstox Credentials

### 1. UPSTOX_API_KEY
- **Purpose**: Your Upstox app API key
- **Where to get**: https://developer.upstox.com/apps
- **Current value** (backend/.env): `c4f707e6-b923-4f4e-814a-0beecd1d7585`

### 2. UPSTOX_API_SECRET
- **Purpose**: Your Upstox app API secret
- **Where to get**: https://developer.upstox.com/apps
- **Current value** (backend/.env): `oh7jig2zql`

### 3. UPSTOX_ACCESS_TOKEN ⚠️ **MOST IMPORTANT**
- **Purpose**: OAuth access token for API authentication
- **Where to get**: Generated via OAuth flow or manually from Upstox dashboard
- **Current value** (backend/.env): 
  ```
  eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1NENRWU4iLCJqdGkiOiI2OWQ3OGVhYWFlMjM5ZTMxM2E2ZWU4MTAiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3NTczNDQ0MiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzc1NzcyMDAwfQ.Gp10jUd-3H7hyOD8mThGMUpW6QRqlXiDK_e26CJfjFI
  ```
- **Expiry**: This token expires daily (check `exp` claim in JWT)
- **Status**: ⚠️ **EXPIRED** (exp: 1775772000 = Feb 7, 2026)

### 4. UPSTOX_REDIRECT_URI
- **Purpose**: OAuth callback URL
- **Current value**: `http://localhost:8000/api/v1/auth/callback`

### 5. UPSTOX_BASE_URL
- **Purpose**: Upstox API base URL
- **Current value** (backend/.env): `https://api.upstox.com/v3`
- **Standard value**: `https://api.upstox.com/v2`

### 6. UPSTOX_WS_URL
- **Purpose**: WebSocket URL for real-time market data
- **Current value**: `wss://api.upstox.com/v2/feed/market-data-feed`

---

## 📂 Configuration Files

### Backend .env (`/backend/.env`)
```bash
# ── Upstox ─────────────────────────────────────────────────────────────────────
UPSTOX_API_KEY=c4f707e6-b923-4f4e-814a-0beecd1d7585
UPSTOX_API_SECRET=oh7jig2zql
UPSTOX_REDIRECT_URI=http://localhost:8000/api/v1/auth/callback
UPSTOX_ACCESS_TOKEN=eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1NENRWU4iLCJqdGkiOiI2OWQ3OGVhYWFlMjM5ZTMxM2E2ZWU4MTAiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3NTczNDQ0MiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzc1NzcyMDAwfQ.Gp10jUd-3H7hyOD8mThGMUpW6QRqlXiDK_e26CJfjFI
UPSTOX_BASE_URL=https://api.upstox.com/v3
UPSTOX_WS_URL=wss://api.upstox.com/v2/feed/market-data-feed
```

### Root .env (`/.env`)
```bash
# ── Upstox ─────────────────────────────────────────────────────────────────────
UPSTOX_API_KEY=your_upstox_api_key_here
UPSTOX_API_SECRET=your_upstox_api_secret_here
UPSTOX_REDIRECT_URI=http://localhost:8000/api/v1/auth/callback
UPSTOX_BASE_URL=https://api.upstox.com/v2
UPSTOX_WS_URL=wss://api.upstox.com/v2/feed/market-data-feed
```

**Note**: Root `.env` has placeholder values. Backend `.env` has actual credentials.

---

## 🔄 How Credentials Are Used

### 1. Application Startup (`backend/app/main.py`)
```python
# Line 76-77
if settings.UPSTOX_ACCESS_TOKEN:
    upstox_client.set_access_token(settings.UPSTOX_ACCESS_TOKEN)
    logger.info("Loaded Upstox access token")
```

### 2. Upstox Client (`backend/app/services/upstox_client.py`)
```python
# Line 54-55
if settings.UPSTOX_ACCESS_TOKEN:
    self._access_token = settings.UPSTOX_ACCESS_TOKEN
```

### 3. Configuration (`backend/app/core/config.py`)
```python
# Line 63
UPSTOX_ACCESS_TOKEN: str | None = None
```

---

## ⚠️ Current Issues

### 1. **Access Token Expired**
- **Token expiry**: Feb 7, 2026 (expired ~2 months ago)
- **Impact**: All Upstox API calls will fail with 401 Unauthorized
- **Solution**: Generate new access token (see below)

### 2. **API Version Mismatch**
- **Backend .env**: Uses `v3` (`https://api.upstox.com/v3`)
- **Standard**: Should be `v2` (`https://api.upstox.com/v2`)
- **Impact**: May cause API endpoint errors
- **Solution**: Change to `v2` unless you specifically need v3

---

## 🔧 How to Get a New Access Token

### Method 1: OAuth Flow (Recommended)
1. Go to https://developer.upstox.com/apps
2. Select your app
3. Click "Generate Token" or "Authorize"
4. Complete OAuth flow
5. Copy the access token
6. Update `UPSTOX_ACCESS_TOKEN` in `backend/.env`
7. Restart the application

### Method 2: Manual Token Generation
1. Visit: https://api.upstox.com/v2/login/authorization/dialog?client_id=YOUR_API_KEY&redirect_uri=YOUR_REDIRECT_URI&response_type=code
2. Login with your Upstox credentials
3. Authorize the app
4. Copy the authorization code from redirect URL
5. Exchange code for access token:
   ```bash
   curl -X POST https://api.upstox.com/v2/login/authorization/token \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "code=YOUR_AUTH_CODE" \
     -d "client_id=YOUR_API_KEY" \
     -d "client_secret=YOUR_API_SECRET" \
     -d "redirect_uri=YOUR_REDIRECT_URI" \
     -d "grant_type=authorization_code"
   ```
6. Extract `access_token` from response
7. Update `UPSTOX_ACCESS_TOKEN` in `backend/.env`

### Method 3: Using the Application (if implemented)
1. Start the backend: `docker-compose up backend`
2. Visit: http://localhost:8000/api/v1/upstox/auth
3. Follow OAuth flow
4. Token will be automatically stored

---

## 🔄 Token Refresh Strategy

### Current Implementation
- **No automatic refresh** implemented
- **Manual refresh required** when token expires
- **Token expiry**: Typically 24 hours (check JWT `exp` claim)

### Recommended Implementation (TODO)
1. Store refresh token in database
2. Implement automatic token refresh before expiry
3. Add token expiry monitoring
4. Send alerts when token is about to expire

---

## 📝 Configuration Checklist

Before running the application, ensure:

- [ ] `UPSTOX_API_KEY` is set (from Upstox developer portal)
- [ ] `UPSTOX_API_SECRET` is set (from Upstox developer portal)
- [ ] `UPSTOX_ACCESS_TOKEN` is set and **not expired**
- [ ] `UPSTOX_BASE_URL` is correct (`v2` or `v3`)
- [ ] `UPSTOX_REDIRECT_URI` matches your app configuration
- [ ] Backend `.env` file exists and is readable
- [ ] Application restarted after updating credentials

---

## 🧪 Testing Credentials

### Test 1: Check Token Validity
```bash
curl -X GET "https://api.upstox.com/v2/user/profile" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Expected**: User profile data  
**If 401**: Token expired or invalid

### Test 2: Check via Application
```bash
# Start backend
docker-compose up backend

# Check health
curl http://localhost:8000/health

# Check Upstox status
curl http://localhost:8000/api/v1/upstox/status
```

### Test 3: Decode JWT Token
```bash
# Install jwt-cli: cargo install jwt-cli
jwt decode YOUR_ACCESS_TOKEN

# Or use online: https://jwt.io
```

Check `exp` (expiry) claim to see when token expires.

---

## 🚨 Security Best Practices

1. **Never commit `.env` files** to Git
   - Already in `.gitignore`
   - Use `.env.example` as template

2. **Rotate tokens regularly**
   - Generate new token every 24 hours
   - Implement automatic refresh

3. **Use environment-specific tokens**
   - Development: Separate token
   - Staging: Separate token
   - Production: Separate token

4. **Monitor token usage**
   - Log token refresh events
   - Alert on authentication failures
   - Track API rate limits

5. **Encrypt tokens at rest**
   - Use secrets management (Vault, AWS Secrets Manager)
   - Encrypt `.env` files in production

---

## 📊 Token Expiry Monitoring

### Current Token Status
```
Token: eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ...
Issued At (iat): 1775734442 (Feb 7, 2026 00:00:42 UTC)
Expires At (exp): 1775772000 (Feb 7, 2026 10:26:40 UTC)
Status: ⚠️ EXPIRED (expired ~68 days ago)
```

### Action Required
**Generate a new access token immediately** to restore Upstox API functionality.

---

## 🔗 Useful Links

- **Upstox Developer Portal**: https://developer.upstox.com
- **API Documentation**: https://upstox.com/developer/api-documentation
- **OAuth Guide**: https://upstox.com/developer/api-documentation/authorization
- **Support**: https://upstox.com/support

---

## 📞 Support

If you encounter issues:
1. Check token expiry (decode JWT)
2. Verify API key/secret are correct
3. Check Upstox API status: https://status.upstox.com
4. Review application logs: `docker-compose logs backend`
5. Contact Upstox support: support@upstox.com

---

**Last Token Update**: Feb 7, 2026 (EXPIRED)  
**Next Action**: Generate new access token  
**Priority**: 🔴 HIGH (API calls currently failing)

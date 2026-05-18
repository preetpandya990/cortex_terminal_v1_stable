# TA-Lib Installation Guide — Production Environment

## Overview
TA-Lib requires both the C library and Python wrapper.

## Installation Steps

### 1. Install C Library

#### Ubuntu/Debian (WSL/Linux)
```bash
# Download and install TA-Lib C library
wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz
tar -xzf ta-lib-0.4.0-src.tar.gz
cd ta-lib/
./configure --prefix=/usr
make
sudo make install
cd ..
rm -rf ta-lib ta-lib-0.4.0-src.tar.gz
```

#### macOS
```bash
brew install ta-lib
```

#### Docker (Add to Dockerfile)
```dockerfile
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib/ && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && \
    rm -rf ta-lib ta-lib-0.4.0-src.tar.gz
```

### 2. Install Python Wrapper
```bash
cd backend
pip install TA-Lib
```

### 3. Verify Installation
```bash
python -c "import talib; print(f'TA-Lib version: {talib.__version__}')"
```

Expected output:
```
TA-Lib version: 0.4.28
```

## Troubleshooting

### Error: "talib/_ta_lib.c:... fatal error: ta-lib/ta_defs.h: No such file or directory"
**Solution**: C library not installed. Follow step 1 above.

### Error: "ModuleNotFoundError: No module named 'talib'"
**Solution**: Python wrapper not installed. Run `pip install TA-Lib`

### Error: "ImportError: libta_lib.so.0: cannot open shared object file"
**Solution**: Library path not configured. Run:
```bash
sudo ldconfig
```

## Production Deployment

### Docker Multi-Stage Build
```dockerfile
# Stage 1: Build TA-Lib
FROM python:3.11-slim as talib-builder
RUN apt-get update && apt-get install -y wget build-essential
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib/ && \
    ./configure --prefix=/usr && \
    make && \
    make install

# Stage 2: Runtime
FROM python:3.11-slim
COPY --from=talib-builder /usr/lib/libta_lib.* /usr/lib/
COPY --from=talib-builder /usr/include/ta-lib /usr/include/ta-lib
RUN ldconfig
RUN pip install TA-Lib
```

### Performance Validation
After installation, run:
```bash
python backend/poc/pattern_detection_poc.py
```

Expected performance:
- Latency: <10ms for 365 candles
- Memory: <50MB
- Throughput: >30,000 candles/second

## References
- Official docs: https://ta-lib.github.io/ta-lib-python/
- GitHub: https://github.com/TA-Lib/ta-lib-python
- C library: https://ta-lib.org/

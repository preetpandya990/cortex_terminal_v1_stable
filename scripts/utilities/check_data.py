from app.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    # Check stock_ohlcv table
    result = db.execute(text('SELECT COUNT(*) FROM stock_ohlcv')).scalar()
    print(f'stock_ohlcv records: {result:,}')
    
    # Check stock_master table
    result = db.execute(text('SELECT COUNT(*) FROM stock_master')).scalar()
    print(f'stock_master records: {result:,}')
    
    # Check technical_indicators table
    result = db.execute(text('SELECT COUNT(*) FROM technical_indicators')).scalar()
    print(f'technical_indicators records: {result:,}')
    
    # Check market_scans table
    result = db.execute(text('SELECT COUNT(*) FROM market_scans')).scalar()
    print(f'market_scans records: {result:,}')
    
    # Get sample stock symbols
    result = db.execute(text('SELECT symbol FROM stock_master LIMIT 10')).fetchall()
    print(f'\nSample stocks: {[r[0] for r in result]}')
    
    # Check latest OHLCV data
    result = db.execute(text('SELECT symbol, MAX(timestamp) as latest FROM stock_ohlcv GROUP BY symbol LIMIT 5')).fetchall()
    print(f'\nLatest OHLCV data:')
    for r in result:
        print(f'  {r[0]}: {r[1]}')
    
except Exception as e:
    print(f'Error: {e}')
finally:
    db.close()

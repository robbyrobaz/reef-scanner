import sys
sys.path.insert(0, '.')
from db import get_db
import duckdb

con = get_db()
rows = con.execute('SELECT * FROM wallets ORDER BY score DESC LIMIT 15').fetchall()
headers = [d[0] for d in con.execute('SELECT * FROM wallets LIMIT 0').description]
for r in rows:
    print(dict(zip(headers, r)))

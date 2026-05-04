import sys
sys.path.insert(0, ".")
from harvest.config import get_db_conn

conn = get_db_conn()
cur = conn.cursor()

cur.execute("SELECT COUNT(*), MIN(sold_date), MAX(sold_date) FROM redfin_sold")
print("counts/dates:", dict(cur.fetchone()))

cur.execute("SELECT source_county, COUNT(*) AS n FROM redfin_sold GROUP BY source_county")
print("by county:", [dict(r) for r in cur.fetchall()])

cur.execute("SELECT address, sold_price, sold_date, listing_url, source_county FROM redfin_sold LIMIT 5")
for r in cur.fetchall():
    print(dict(r))

conn.close()

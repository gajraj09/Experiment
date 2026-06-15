import sqlite3

conn = sqlite3.connect('data/tradingbot.db')
rows = conn.execute('SELECT tradeNumber, price, netPnl FROM trade_history WHERE interval="5m" ORDER BY tradeNumber').fetchall()
for r in rows:
    print(f"Trade {r[0]}: price={r[1]}, netPnl={r[2]}")
conn.close()

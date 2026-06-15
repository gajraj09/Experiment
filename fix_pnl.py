import sqlite3

conn = sqlite3.connect('data/tradingbot.db')
# Update all trades with netPnl = 0.0 to set them to NULL
conn.execute('UPDATE trade_history SET netPnl = NULL WHERE netPnl = 0.0')
conn.commit()
print("Updated trades with netPnl = 0.0 to NULL")
conn.close()

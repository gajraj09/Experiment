import sqlite3
from decimal import Decimal

price_tick_size = Decimal("0.0001")

conn = sqlite3.connect('data/tradingbot.db')
conn.row_factory = sqlite3.Row

for interval in ['5m', '15m']:
    trades = conn.execute(
        'SELECT id, tradeNumber, price, size, netPnl FROM trade_history WHERE interval = ? AND signal = "Entry" ORDER BY tradeNumber',
        (interval,)
    ).fetchall()
    
    print(f"\n{interval} interval trades:")
    for i, trade in enumerate(trades):
        print(f"  Trade {trade['tradeNumber']}: price={trade['price']}, size={trade['size']}, pnl={trade['netPnl']}")
        
        # Calculate P&L if there's a next trade
        if i + 1 < len(trades):
            next_trade = trades[i + 1]
            if trade['netPnl'] is None:
                # P&L = (current_price - next_price) * size
                pnl = float((Decimal(str(trade['price'])) - Decimal(str(next_trade['price']))) * Decimal(str(trade['size'])))
                print(f"    -> Calculating P&L: ({trade['price']} - {next_trade['price']}) * {trade['size']} = {pnl}")
                conn.execute('UPDATE trade_history SET netPnl = ? WHERE id = ?', (pnl, trade['id']))

conn.commit()
print("\nP&L calculation complete!")
conn.close()

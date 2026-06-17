import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
import os
import json

import orjson
import websockets
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import httpx


SYMBOL = "XRPUSDC"
ATR_LENGTH = 1
ATR_MULT = Decimal("0.1")
CANDLE_LIMIT = ATR_LENGTH + 2
DEFAULT_PRICE_TICK_SIZE = Decimal("0.0001")
INTERVALS = {"5m", "15m"}
BINANCE_FAPI = "https://fapi.binance.com"
STREAM_SYMBOL = SYMBOL.lower()
WS_URL = f"wss://fstream.binance.com/ws/{STREAM_SYMBOL}@trade"
RECONNECT_DELAY = 1
DATA_DIR = Path("data")
DATABASE_FILE = DATA_DIR / "tradingbot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("uvicorn.access").disabled = True
logging.getLogger("uvicorn.access").propagate = False
logger = logging.getLogger("tradingbot")

state = {
    "last_trade_price": None,
    "best_bid": None,
    "best_ask": None,
    "mid_price": None,
    "spread": None,
    "updated_at": None,
}
price_tick_size = DEFAULT_PRICE_TICK_SIZE
candle_cache: dict[str, list[dict[str, Any]]] = {}
trade_level_cache: dict[str, dict[str, Any]] = {}
current_position_by_interval: dict[str, dict[str, Any] | None] = {
    "5m": {"type": "long", "entryPrice": None, "size": 1},
    "15m": {"type": "long", "entryPrice": None, "size": 1},
}
trade_execution_state_by_interval: dict[str, dict[str, Any]] = {
    "5m": {
        "tradePlacedInCurrentCandle": False,
        "executedTradeTime": None,
        "executedTradeNumber": None,
        "executedCandleOpenTime": None,
    },
    "15m": {
        "tradePlacedInCurrentCandle": False,
        "executedTradeTime": None,
        "executedTradeNumber": None,
        "executedCandleOpenTime": None,
    },
}
candle_lock = asyncio.Lock()
trade_history_lock = asyncio.Lock()
trade_execution_lock = asyncio.Lock()


class TradeRow(BaseModel):
    tradeNumber: int
    type: Literal["long", "short"]
    signal: Literal["Open", "Entry", "Exit"]
    dateTime: str
    price: float
    size: float
    netPnl: float | None = None
    favorableExcursion: float | None = None
    adverseExcursion: float | None = None
    cumulativePnl: float | None = None
    openCandleTime: str | None = None


def empty_trade_history() -> dict[str, list[dict[str, Any]]]:
    return {interval: [] for interval in INTERVALS}


def database_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATABASE_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interval TEXT NOT NULL,
                tradeNumber INTEGER NOT NULL,
                type TEXT NOT NULL,
                dateTime TEXT NOT NULL,
                signal TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                netPnl REAL,
                favorableExcursion REAL,
                adverseExcursion REAL,
                cumulativePnl REAL,
                executedCandleOpenTime INTEGER,
                openCandleTime TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_one_execution_per_candle
            ON trade_history(interval, executedCandleOpenTime)
            WHERE executedCandleOpenTime IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_unique_trade_number
            ON trade_history(interval, tradeNumber)
            """
        )
        # Ensure we have columns for fill status and fill time for unfilled/fill tracking
        cols = {r[1] for r in connection.execute("PRAGMA table_info(trade_history)").fetchall()}
        if "fill_status" not in cols:
            connection.execute("ALTER TABLE trade_history ADD COLUMN fill_status TEXT")
        if "fill_time" not in cols:
            connection.execute("ALTER TABLE trade_history ADD COLUMN fill_time TEXT")
        # track the ticker at time of placement and whether price has moved since placement
        if "openCandleTime" not in cols:
            connection.execute("ALTER TABLE trade_history ADD COLUMN openCandleTime TEXT")
        if "placed_ticker" not in cols:
            connection.execute("ALTER TABLE trade_history ADD COLUMN placed_ticker REAL")
        if "moved_since_placement" not in cols:
            connection.execute("ALTER TABLE trade_history ADD COLUMN moved_since_placement INTEGER DEFAULT 0")


def format_timestamp_ist(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    ist = timezone(timedelta(hours=5, minutes=30))
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(ist)
    return dt.strftime("%d-%m-%Y %H:%M:%S IST")


def trade_row_from_db(row: sqlite3.Row) -> dict[str, Any]:
    open_candle_time = None
    if "openCandleTime" in row.keys():
        open_candle_time = row["openCandleTime"]
    elif "executedCandleOpenTime" in row.keys() and row["executedCandleOpenTime"] is not None:
        open_candle_time = format_timestamp_ist(int(row["executedCandleOpenTime"]))

    return {
        "id": row["id"],
        "tradeNumber": row["tradeNumber"],
        "type": row["type"],
        "dateTime": row["dateTime"],
        "signal": row["signal"],
        "price": row["price"],
        "size": row["size"],
        "netPnl": row["netPnl"],
        "favorableExcursion": row["favorableExcursion"],
        "adverseExcursion": row["adverseExcursion"],
        "cumulativePnl": row["cumulativePnl"],
        "fill_status": row["fill_status"] if "fill_status" in row.keys() else None,
        "fill_time": row["fill_time"] if "fill_time" in row.keys() else None,
        "openCandleTime": open_candle_time,
        "placed_ticker": row["placed_ticker"] if "placed_ticker" in row.keys() else None,
        "moved_since_placement": bool(row["moved_since_placement"]) if "moved_since_placement" in row.keys() else False,
    }


def get_trade_history(interval: str) -> list[dict[str, Any]]:
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, tradeNumber, type, dateTime, signal, price, size, netPnl,
                   favorableExcursion, adverseExcursion, cumulativePnl,
                   fill_status, fill_time, openCandleTime, placed_ticker, moved_since_placement
            FROM trade_history
            WHERE interval = ?
            ORDER BY id ASC
            """,
            (interval,),
        ).fetchall()
    return [trade_row_from_db(row) for row in rows]


def get_last_trade_number(interval: str) -> int | None:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT tradeNumber FROM trade_history WHERE interval = ? ORDER BY tradeNumber DESC LIMIT 1",
            (interval,),
        ).fetchone()
    return int(row["tradeNumber"]) if row else None


def get_last_trade_row(interval: str) -> sqlite3.Row | None:
    with database_connection() as connection:
        return connection.execute(
            "SELECT * FROM trade_history WHERE interval = ? ORDER BY id DESC LIMIT 1",
            (interval,),
        ).fetchone()


def get_last_executed_trade_row(interval: str) -> sqlite3.Row | None:
    with database_connection() as connection:
        return connection.execute(
            "SELECT * FROM trade_history WHERE interval = ? AND executedCandleOpenTime IS NOT NULL ORDER BY id DESC LIMIT 1",
            (interval,),
        ).fetchone()


def get_execution_trade_by_candle(interval: str, executed_candle_open_time: int) -> dict[str, Any] | None:
    with database_connection() as connection:
        row = connection.execute(
            """
            SELECT id, tradeNumber, type, dateTime, signal, price, size, netPnl,
                   favorableExcursion, adverseExcursion, cumulativePnl, fill_status, fill_time,
                   openCandleTime, placed_ticker, moved_since_placement
            FROM trade_history
            WHERE interval = ? AND executedCandleOpenTime = ?
            ORDER BY id DESC LIMIT 1
            """,
            (interval, executed_candle_open_time),
        ).fetchone()
    return trade_row_from_db(row) if row else None


def restore_trade_state_from_db(interval: str, current_open_time: int | None = None) -> None:
    row = get_last_executed_trade_row(interval)
    if not row:
        return

    current_position_by_interval[interval] = {
        "type": row["type"],
        "entryPrice": float(Decimal(str(row["price"])).quantize(price_tick_size)),
        "size": row["size"],        "tradeId": row["id"],    }

    execution_state = trade_execution_state_by_interval[interval]
    execution_state["executedTradeNumber"] = row["tradeNumber"]
    execution_state["executedTradeTime"] = row["dateTime"]
    execution_state["executedCandleOpenTime"] = row["executedCandleOpenTime"]
    execution_state["tradePlacedInCurrentCandle"] = (
        row["executedCandleOpenTime"] is not None
        and current_open_time is not None
        and row["executedCandleOpenTime"] == current_open_time
    )


def next_trade_number(interval: str, connection: sqlite3.Connection | None = None) -> int:
    owns_connection = connection is None
    connection = connection or database_connection()
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(tradeNumber), 0) + 1 AS nextTradeNumber FROM trade_history WHERE interval = ?",
            (interval,),
        ).fetchone()
        return int(row["nextTradeNumber"])
    finally:
        if owns_connection:
            connection.close()


def get_trade_by_open_candle_time(interval: str, open_candle_time: str) -> sqlite3.Row | None:
    """Get a trade from history by interval and openCandleTime"""
    with database_connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM trade_history
            WHERE interval = ? AND openCandleTime = ?
            ORDER BY id DESC LIMIT 1
            """,
            (interval, open_candle_time),
        ).fetchone()
    return row


def get_last_trade_by_interval(interval: str) -> sqlite3.Row | None:
    """Get the very last trade for an interval"""
    with database_connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM trade_history
            WHERE interval = ?
            ORDER BY id DESC LIMIT 1
            """,
            (interval,),
        ).fetchone()
    return row


def verify_active_trade(interval: str, verify_trade_data: dict[str, Any]) -> bool:
    """
    Verify trade data against trade history.
    If matched candle has different type:
    - If matched trade is LAST: insert with payload type
    - If matched trade is NOT last: insert with FLIPPED type
    Preserves all other columns from matched trade.
    """
    logger.debug(f"[VERIFY] Starting verification for interval={interval}")

    open_candle_time = verify_trade_data.get("candle_open_time")
    payload_type = str(verify_trade_data.get("type", "")).lower()
    entry_price = Decimal(str(verify_trade_data.get("entry_price", 0)))

    if not open_candle_time or not payload_type:
        logger.warning(f"[VERIFY] Invalid verify_trade_data for {interval}: missing candle_open_time or type")
        return False

    logger.debug(f"[VERIFY] Looking for trade with openCandleTime={open_candle_time}, payload_type={payload_type}")

    # Find the trade with matching openCandleTime
    history_row = get_trade_by_open_candle_time(interval, open_candle_time)
    if not history_row:
        # logger.info(f"[VERIFY] No trade found in history with openCandleTime={open_candle_time} for {interval}")
        return False

    history_type = str(history_row["type"]).lower()
    # logger.debug(f"[VERIFY] Found matching trade: id={history_row['id']}, tradeNumber={history_row['tradeNumber']}, type={history_type}")

    # If types match, do nothing (silently)
    if history_type == payload_type:
        return False

    # logger.warning(f"[VERIFY] TYPE MISMATCH for {interval}: history_type={history_type} vs payload_type={payload_type}")

    # Types differ - check if matched trade is the last trade
    last_trade = get_last_trade_by_interval(interval)
    is_last = last_trade and last_trade["id"] == history_row["id"]
    
    # logger.info(f"[VERIFY] Matched trade is_last={is_last}, last_trade_id={last_trade['id'] if last_trade else None}")

    # Determine the type to insert
    if is_last:
        insert_type = payload_type  # Use payload type as-is
        # logger.info(f"[VERIFY] Matched trade IS LAST -> INSERT with PAYLOAD type: {insert_type}")
    else:
        insert_type = "short" if payload_type == "long" else "long"  # Flip the type
        # logger.info(f"[VERIFY] Matched trade is NOT LAST -> INSERT with FLIPPED type: {insert_type} (payload was {payload_type})")

    logger.info(
        # f"[VERIFY] Inserting new trade: interval={interval}, candle_time={open_candle_time}, "
        f"history_type={history_type}, payload_type={payload_type}, is_last={is_last}, insert_type={insert_type}"
    )

    # Insert new trade with preserved columns and determined type
    try:
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            new_trade_number = next_trade_number(interval, connection)
            event_time = int(datetime.now(timezone.utc).timestamp() * 1000)

            # logger.info(f"[VERIFY] Generating new tradeNumber={new_trade_number}, event_time={event_time}")

            cursor = connection.execute(
                """
                INSERT INTO trade_history (
                    interval, tradeNumber, type, dateTime, signal, price, size,
                    netPnl, favorableExcursion, adverseExcursion, cumulativePnl,
                    executedCandleOpenTime, openCandleTime, fill_status, fill_time,
                    placed_ticker, moved_since_placement
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interval,
                    new_trade_number,
                    insert_type,
                    event_time_to_iso(event_time),
                    "MISSED",
                    float(entry_price),
                    history_row["size"],
                    None,
                    None,
                    None,
                    None,
                    None,  # do not reuse executedCandleOpenTime for verify-derived insert
                    open_candle_time,
                    history_row["fill_status"] if "fill_status" in history_row.keys() else "Unfilled",
                    history_row["fill_time"] if "fill_time" in history_row.keys() else None,
                    float(entry_price),
                    False,
                ),
            )
            connection.commit()
            if cursor.rowcount != 1:
                logger.error(f"[VERIFY] Failed to insert verify trade for {interval} - rowcount={cursor.rowcount}")
                return False

            inserted_id = int(cursor.lastrowid or 0)
            current_position_by_interval[interval] = {
                "type": insert_type,
                "entryPrice": float(entry_price),
                "size": history_row["size"],
                "tradeId": inserted_id,
            }

            execution_state = trade_execution_state_by_interval[interval]
            execution_state["executedTradeNumber"] = new_trade_number
            execution_state["executedTradeTime"] = event_time_to_iso(event_time)
            execution_state["executedCandleOpenTime"] = None
            execution_state["tradePlacedInCurrentCandle"] = False

            logger.info(
                # f"[VERIFY] ✓ SUCCESSFULLY INSERTED verification trade: "
                # f"interval={interval}, tradeNumber={new_trade_number}, type={insert_type}, "
                # f"price={entry_price}, candle_time={open_candle_time}, size={history_row['size']}, "
                # f"tradeId={inserted_id}"
            )
            return True

    except Exception as e:
        logger.exception(f"[VERIFY] ✗ ERROR in verify_active_trade for {interval}: {e}")
        return False


def check_and_update_last_unfilled_trade(interval: str, ticker_price: Decimal, event_time: int | None) -> bool:
    """Check the current open trade for this interval and mark it Filled
    only when the refined condition is met. Returns True if an update was performed."""
    current_position = current_position_by_interval.get(interval)
    if not current_position:
        return False

    trade_id = current_position.get("tradeId")
    if not trade_id:
        return False

    try:
        with database_connection() as connection:
            row = connection.execute(
                "SELECT * FROM trade_history WHERE id = ? AND interval = ? AND fill_status = ?",
                (trade_id, interval, "Unfilled"),
            ).fetchone()

            if not row:
                return False

            pos = row["type"]
            entry_price = Decimal(str(row["price"])).quantize(price_tick_size)
            placed_ticker = None
            moved_flag = bool(row["moved_since_placement"]) if "moved_since_placement" in row.keys() else False
            if "placed_ticker" in row.keys():
                try:
                    placed_ticker = Decimal(str(row["placed_ticker"])).quantize(price_tick_size)
                except Exception:
                    placed_ticker = None

            # If price is still at placement and hasn't moved, do not fill yet
            if placed_ticker is not None and ticker_price == placed_ticker and not moved_flag:
                return False

            should_fill = False
            if pos == "long" and ticker_price <= entry_price:
                # require that price has moved since placement before filling; allow older rows
                if moved_flag or placed_ticker is None:
                    should_fill = True
            elif pos == "short" and ticker_price >= entry_price:
                if moved_flag or placed_ticker is None:
                    should_fill = True

            # If we haven't yet seen price move since placement, but the current tick differs
            # from the placed ticker, mark moved_since_placement and continue checking for fill.
            if not moved_flag and placed_ticker is not None and ticker_price != placed_ticker:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE trade_history SET moved_since_placement = 1 WHERE id = ?",
                        (int(row["id"]),),
                    )
                    connection.commit()
                    moved_flag = True
                except Exception:
                    logger.exception("Failed to mark moved_since_placement for id=%s", row["id"])
                    return False

            if not should_fill:
                return False

            # Backup current row to undo log before updating
            try:
                backup_path = os.path.join("data", "undo_fill_log.txt")
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                with open(backup_path, "a", encoding="utf-8") as f:
                    backup = {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "interval": interval,
                        "id": int(row["id"]),
                        "row": {k: row[k] for k in row.keys()},
                    }
                    f.write(json.dumps(backup) + "\n")
            except Exception:
                logger.exception("Failed to write undo backup for fill update")

            # perform the update
            fill_time_iso = event_time_to_iso(event_time)
            connection.execute(
                "BEGIN IMMEDIATE",
            )
            connection.execute(
                "UPDATE trade_history SET fill_status = ?, fill_time = ? WHERE id = ?",
                ("Filled", fill_time_iso, int(row["id"])),
            )
            connection.commit()
            # logger.info("Marked trade id=%s interval=%s as Filled at %s", row["id"], interval, fill_time_iso)
            return True
    except Exception:
        logger.exception("Error updating last unfilled trade for %s", interval)
        return False


def insert_trade_history_row(
    interval: str,
    row: dict[str, Any],
    executed_candle_open_time: int | None = None,
) -> bool:
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO trade_history (
                interval, tradeNumber, type, dateTime, signal, price, size,
                netPnl, favorableExcursion, adverseExcursion, cumulativePnl,
                executedCandleOpenTime, openCandleTime, fill_status, fill_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interval,
                row["tradeNumber"],
                row["type"],
                row["dateTime"],
                row["signal"],
                row["price"],
                row["size"],
                row.get("netPnl"),
                row.get("favorableExcursion"),
                row.get("adverseExcursion"),
                row.get("cumulativePnl"),
                executed_candle_open_time,
                row.get("openCandleTime"),
                row.get("fill_status"),
                row.get("fill_time"),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1


def clear_trade_history(interval: str) -> None:
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM trade_history WHERE interval = ?", (interval,))
        connection.commit()


def insert_manual_trade_row(interval: str, row: dict[str, Any]) -> None:
    inserted = insert_trade_history_row(interval, row)
    if not inserted:
        logger.error(
            "Manual trade insert failed for interval %s tradeNumber=%s signal=%s price=%s",
            interval,
            row.get("tradeNumber"),
            row.get("signal"),
            row.get("price"),
        )
        raise HTTPException(status_code=409, detail="Trade row already exists for this candle")
    logger.info(
        "Manual trade inserted successfully for interval %s tradeNumber=%s signal=%s price=%s",
        interval,
        row.get("tradeNumber"),
        row.get("signal"),
        row.get("price"),
    )


def calculate_and_update_previous_trade_pnl(
    interval: str,
    new_entry_price: Decimal,
    new_trade_size: float,
    new_trade_number: int,
) -> None:
    """Calculate P&L for the previous trade when a new trade is executed.
    P&L = (previous_entry_price - new_entry_price) * quantity
    Only updates closed (previous) trades, not the current open trade."""
    try:
        with database_connection() as connection:
            # Get the previous trade (one with tradeNumber = new_trade_number - 1)
            row = connection.execute(
                """
                SELECT id, price, size, netPnl FROM trade_history
                WHERE interval = ? AND tradeNumber = ?
                """,
                (interval, new_trade_number - 1),
            ).fetchone()
            
            if not row:
                logger.info("No previous trade found for tradeNumber=%s", new_trade_number - 1)
                return
            
            if row["netPnl"] is not None:
                logger.info("P&L already calculated for trade id=%s, netPnl=%s", row["id"], row["netPnl"])
                return
            
            previous_entry_price = Decimal(str(row["price"])).quantize(price_tick_size)
            previous_size = row["size"]
            
            # Calculate P&L: (previous_entry_price - new_entry_price) * quantity
            pnl = float((previous_entry_price - new_entry_price) * Decimal(str(previous_size)))
            
            # Log to file for debugging
            with open("data/pnl_debug.log", "a") as f:
                f.write(f"P&L Calc: interval={interval}, tradeNum={new_trade_number}, prev_price={previous_entry_price}, new_price={new_entry_price}, size={previous_size}, pnl={pnl}\n")
            
            # Update the previous trade with P&L
            connection.execute(
                "UPDATE trade_history SET netPnl = ? WHERE id = ?",
                (pnl, int(row["id"])),
            )
            connection.commit()
            # logger.info(
            #     "Updated trade id=%s with P&L: %.6f",
            #     row["id"],
            #     pnl,
            # )
    except Exception as e:
        logger.exception("Error calculating P&L for previous trade in %s: %s", interval, str(e))
        with open("data/pnl_debug.log", "a") as f:
            f.write(f"Error: {str(e)}\n")


def create_execution_trade_row(
    interval: str,
    trade_type: Literal["long", "short"],
    execution_price: Decimal,
    event_time: int | None,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    current_position = current_position_by_interval.get(interval) or {}
    execution_price = execution_price.quantize(price_tick_size)
    return {
        "tradeNumber": next_trade_number(interval, connection),
        "type": trade_type,
        "dateTime": event_time_to_iso(event_time),
        "signal": "Entry",
        "price": float(execution_price),
        "size": float(current_position.get("size") or 1),
        "netPnl": None,
        "favorableExcursion": None,
        "adverseExcursion": None,
        "cumulativePnl": None,
        "fill_status": "Unfilled",
        "fill_time": None,
        "openCandleTime": None,
        "placed_ticker": None,
        "moved_since_placement": False,
    }


def insert_execution_trade(
    interval: str,
    trade_type: Literal["long", "short"],
    execution_price: Decimal,
    event_time: int | None,
    executed_candle_open_time: int,
    placement_ticker: Decimal | None = None,
) -> dict[str, Any] | None:
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = create_execution_trade_row(interval, trade_type, execution_price, event_time, connection)
        # store the ticker observed at the time of placement so we can require price movement before fill
        row["placed_ticker"] = float(placement_ticker) if placement_ticker is not None else None
        row["moved_since_placement"] = False
        row["openCandleTime"] = format_timestamp_ist(executed_candle_open_time)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO trade_history (
                interval, tradeNumber, type, dateTime, signal, price, size,
                netPnl, favorableExcursion, adverseExcursion, cumulativePnl,
                executedCandleOpenTime, openCandleTime, fill_status, fill_time, placed_ticker, moved_since_placement
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interval,
                row["tradeNumber"],
                row["type"],
                row["dateTime"],
                row["signal"],
                row["price"],
                row["size"],
                row["netPnl"],
                row["favorableExcursion"],
                row["adverseExcursion"],
                row["cumulativePnl"],
                executed_candle_open_time,
                row.get("openCandleTime"),
                row.get("fill_status"),
                row.get("fill_time"),
                row["placed_ticker"],
                row["moved_since_placement"],
            ),
        )
        connection.commit()
        if cursor.rowcount != 1:
            logger.error(
                "Execution trade insert ignored for interval %s candle=%s tradeNumber=%s",
                interval,
                executed_candle_open_time,
                row["tradeNumber"],
            )
            return get_execution_trade_by_candle(interval, executed_candle_open_time)
        # logger.info(
        #     "Execution trade placed successfully for interval %s tradeNumber=%s type=%s price=%s candle=%s",
        #     interval,
        #     row["tradeNumber"],
        #     row["type"],
        #     row["price"],
        #     executed_candle_open_time,
        # )
        print("Trade Executed")
        payload = {
            "symbol": SYMBOL,
            "side": "BUY" if trade_type == "long" else "SELL",
            "quantity": row["size"],
            "price": row["price"],
            "tradeNumber": row["tradeNumber"],
            "interval": interval,
            "secret": "my_secret_key",
        }
        try:
            response = httpx.post("http://127.0.0.1:5000/webhook", json=payload, timeout=5.0)
            response.raise_for_status()
            print("Sent payload:", payload)
        except httpx.RequestError as exc:
            logger.exception("Webhook request error sending execution trade: %s", exc)
            # print("Webhook error:", exc)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Webhook response error for execution trade: status=%s body=%s",
                exc.response.status_code,
                exc.response.text,
            )
            # print("Webhook error:", exc)
        return get_execution_trade_by_candle(interval, executed_candle_open_time)


def trade_row_to_dict(trade: TradeRow) -> dict[str, Any]:
    if hasattr(trade, "model_dump"):
        return trade.model_dump()
    return trade.dict()


def event_time_to_iso(event_time: int | None) -> str:
    timestamp = (event_time or int(datetime.now(timezone.utc).timestamp() * 1000)) / 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def format_price(value: str | float | Decimal) -> str:
    price = Decimal(str(value)).quantize(price_tick_size)
    decimals = abs(price_tick_size.as_tuple().exponent)
    return f"{price:.{decimals}f}"


def candle_from_kline(kline: list[Any]) -> dict[str, Any]:
    return {
        "open_time": int(kline[0]),
        "open": format_price(kline[1]),
        "high": format_price(kline[2]),
        "low": format_price(kline[3]),
        "close": format_price(kline[4]),
        "volume": kline[5],
        "close_time": int(kline[6]),
    }


def true_range(current_candle: dict[str, Any], previous_candle: dict[str, Any]) -> Decimal:
    high = Decimal(current_candle["high"])
    low = Decimal(current_candle["low"])
    previous_close = Decimal(previous_candle["close"])
    return max(
        high - low,
        abs(high - previous_close),
        abs(low - previous_close),
    )


def trade_levels_from_candles(interval: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
    current_open_time = candles[-1]["open_time"]
    cached = trade_level_cache.get(interval)
    if cached and cached["current_open_time"] == current_open_time:
        return cached

    previous_candle = candles[-2]
    prior_candle = candles[-3]
    atr_value = true_range(previous_candle, prior_candle) * ATR_MULT
    previous_close = Decimal(previous_candle["close"])

    levels = {
        "current_open_time": current_open_time,
        "buy_trigger_price": format_price(previous_close + atr_value),
        "sell_trigger_price": format_price(previous_close - atr_value),
        "atr_value": format_price(atr_value),
        "length": ATR_LENGTH,
        "mult": f"{ATR_MULT:f}",
    }
    trade_level_cache[interval] = levels
    return levels


def update_trade_execution_state(interval: str, current_open_time: int) -> dict[str, Any]:
    execution_state = trade_execution_state_by_interval[interval]
    if execution_state["executedCandleOpenTime"] != current_open_time:
        execution_state["tradePlacedInCurrentCandle"] = False
        execution_state["executedCandleOpenTime"] = current_open_time
        execution_state["executedTradeNumber"] = None
        execution_state["executedTradeTime"] = None

    if not execution_state["tradePlacedInCurrentCandle"]:
        executed_trade = get_execution_trade_by_candle(interval, current_open_time)
        if executed_trade is not None:
            execution_state["tradePlacedInCurrentCandle"] = True
            execution_state["executedTradeNumber"] = executed_trade["tradeNumber"]
            execution_state["executedTradeTime"] = executed_trade["dateTime"]

    return execution_state


async def evaluate_trade_execution(ticker_price: float, event_time: int | None) -> None:
    price = Decimal(str(ticker_price)).quantize(price_tick_size)

    async with candle_lock:
        candle_snapshot = {
            interval: list(candles)
            for interval, candles in candle_cache.items()
            if len(candles) >= CANDLE_LIMIT
        }

    if not candle_snapshot:
        return

    async with trade_execution_lock:
        for interval, candles in candle_snapshot.items():
            # First: attempt to update the last unfilled trade for this interval
            try:
                check_and_update_last_unfilled_trade(interval, price, event_time)
            except Exception:
                logger.exception("Failed while checking last unfilled trade for %s", interval)

            trade_levels = trade_levels_from_candles(interval, candles)
            execution_state = update_trade_execution_state(
                interval,
                trade_levels["current_open_time"],
            )

            if execution_state["tradePlacedInCurrentCandle"]:
                continue

            current_position = current_position_by_interval.get(interval)
            if not current_position:
                continue

            current_candle = candles[-1]
            current_low = Decimal(current_candle["low"]).quantize(price_tick_size)
            current_high = Decimal(current_candle["high"]).quantize(price_tick_size)

            position_type = current_position.get("type")
            buy_trigger_price = Decimal(trade_levels["buy_trigger_price"]).quantize(price_tick_size)
            sell_trigger_price = Decimal(trade_levels["sell_trigger_price"]).quantize(price_tick_size)
            next_position_type = None
            execution_price = price

            if (
                position_type == "long"
                and price <= sell_trigger_price
                and current_low <= price <= current_high
            ):
                next_position_type = "short"
            elif (
                position_type == "short"
                and price >= buy_trigger_price
                and current_low <= price <= current_high
            ):
                next_position_type = "long"

            if next_position_type is None:
                continue

            row = insert_execution_trade(
                interval,
                next_position_type,
                execution_price,
                event_time,
                trade_levels["current_open_time"],
                placement_ticker=price,
            )
            if row is None:
                execution_state["tradePlacedInCurrentCandle"] = True
                continue

            # Calculate P&L for the previous trade using the new trade's entry price
            calculate_and_update_previous_trade_pnl(
                interval,
                execution_price,
                row["size"],
                row["tradeNumber"],
            )

            current_position_by_interval[interval] = {
                "type": next_position_type,
                "entryPrice": float(execution_price),
                "size": row["size"],
                "tradeId": row["id"],
            }
            execution_state["tradePlacedInCurrentCandle"] = True
            execution_state["executedTradeTime"] = row["dateTime"]
            execution_state["executedTradeNumber"] = row["tradeNumber"]
            execution_state["executedCandleOpenTime"] = trade_levels["current_open_time"]


async def ticker_stream() -> None:
    json_loads = orjson.loads
    state_ref = state

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
                max_queue=16,
                compression=None,
            ) as ws:
                logger.info("Connected to Binance Futures trade stream: %s", WS_URL)
                async for message in ws:
                    data = json_loads(message)
                    ticker_price = float(data["p"])
                    state_ref["last_trade_price"] = ticker_price
                    state_ref["updated_at"] = data.get("E") or data.get("T")
                    await evaluate_trade_execution(ticker_price, state_ref["updated_at"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Binance websocket disconnected; reconnecting in %s second", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


async def fetch_klines(interval: str) -> list[dict[str, Any]]:
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {
        "symbol": SYMBOL.upper(),
        "interval": interval,
        "limit": CANDLE_LIMIT,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
    except httpx.RequestError as exc:
        logger.warning(
            "Unable to fetch %s klines from Binance: %s",
            interval,
            str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Unable to fetch {interval} candles from Binance: {exc}",
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Binance returned {response.status_code}: {response.text}",
        )

    return [candle_from_kline(kline) for kline in response.json()]


async def load_symbol_tick_size() -> None:
    global price_tick_size

    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)

    if response.status_code != 200:
        logger.warning(
            "Unable to load exchangeInfo; using fallback tick size %s",
            DEFAULT_PRICE_TICK_SIZE,
        )
        return

    for symbol_info in response.json().get("symbols", []):
        if symbol_info.get("symbol") != SYMBOL:
            continue
        for symbol_filter in symbol_info.get("filters", []):
            if symbol_filter.get("filterType") == "PRICE_FILTER":
                price_tick_size = Decimal(symbol_filter["tickSize"])
                logger.info("Loaded %s tick size: %s", SYMBOL, price_tick_size)
                return

    logger.warning("PRICE_FILTER not found for %s; using fallback tick size %s", SYMBOL, DEFAULT_PRICE_TICK_SIZE)


async def refresh_candle_cache() -> None:
    while True:
        for interval in INTERVALS:
            try:
                candles = await fetch_klines(interval)
                async with candle_lock:
                    candle_cache[interval] = candles
            except Exception:
                logger.exception("Unable to refresh %s candle cache", interval)
        await asyncio.sleep(1)


async def get_cached_klines(interval: str) -> list[dict[str, Any]]:
    async with candle_lock:
        candles = candle_cache.get(interval)

    if candles:
        return candles

    try:
        candles = await fetch_klines(interval)
    except HTTPException:
        # If we cannot fetch fresh candles, preserve existing cache if possible.
        if candles:
            return candles
        raise

    async with candle_lock:
        candle_cache[interval] = candles
    return candles


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()

    try:
        await load_symbol_tick_size()
    except Exception:
        logger.exception("Unable to load symbol metadata; using fallback tick size %s", DEFAULT_PRICE_TICK_SIZE)

    for interval in INTERVALS:
        try:
            candles = await get_cached_klines(interval)
            restore_trade_state_from_db(interval, candles[-1]["open_time"])
        except Exception:
            logger.exception("Unable to restore trade execution state for %s", interval)

    ticker_task = asyncio.create_task(ticker_stream())
    candle_task = asyncio.create_task(refresh_candle_cache())
    yield
    for task in (ticker_task, candle_task):
        task.cancel()
    await asyncio.gather(ticker_task, candle_task, return_exceptions=True)


app = FastAPI(title="TradingBot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/api/market/{interval}")
async def market_snapshot(interval: str) -> dict[str, Any]:
    if interval not in INTERVALS:
        raise HTTPException(status_code=404, detail="Unsupported interval")

    candles = await get_cached_klines(interval)
    if len(candles) < 3:
        raise HTTPException(status_code=502, detail="Not enough candle data returned")

    trade_levels = trade_levels_from_candles(interval, candles)
    trade_execution_state = update_trade_execution_state(
        interval,
        trade_levels["current_open_time"],
    )

    executed_trade = get_execution_trade_by_candle(interval, trade_levels["current_open_time"])
    if executed_trade is not None:
        trade_execution_state["tradePlacedInCurrentCandle"] = True
        trade_execution_state["executedTradeNumber"] = executed_trade["tradeNumber"]
        trade_execution_state["executedTradeTime"] = executed_trade["dateTime"]

    async with trade_history_lock:
        trade_history = get_trade_history(interval)

    last_trade_number = trade_execution_state.get("executedTradeNumber")
    if last_trade_number is None:
        last_trade_number = get_last_trade_number(interval)
    if last_trade_number is None:
        last_trade_number = 0

    return {
        "last_trade_number": last_trade_number,
        "symbol": SYMBOL,
        "interval": interval,
        "ticker_price": format_price(state["last_trade_price"] or candles[-1]["close"]),
        "last_trade_price": format_price(state["last_trade_price"])
        if state["last_trade_price"] is not None
        else None,
        "best_bid": format_price(state["best_bid"]) if state["best_bid"] is not None else None,
        "best_ask": format_price(state["best_ask"]) if state["best_ask"] is not None else None,
        "mid_price": format_price(state["mid_price"]) if state["mid_price"] is not None else None,
        "spread": format_price(state["spread"]) if state["spread"] is not None else None,
        "updated_at": state["updated_at"],
        "open_candle": candles[-1],
        "trade_levels": trade_levels,
        "trade_execution_state": trade_execution_state,
        "closed_candles": candles[-3:-1],
        "current_position": current_position_by_interval.get(interval),
        "trade_history": trade_history,
    }


@app.get("/api/verify")
async def proxy_verify() -> dict[str, Any]:
    """Proxy verify endpoints running on localhost:5000 for each configured interval.
    Returns a mapping of interval -> { status, status_code, payload | error }.
    """
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for interval in sorted(INTERVALS):
            url = f"http://127.0.0.1:5000/verify/{interval}"
            try:
                resp = await client.get(url)
                try:
                    body = resp.json()
                except Exception:
                    text = resp.text
                    # Try to parse PowerShell hashtable-like string: @{k=v; a=b}
                    if isinstance(text, str) and text.strip().startswith("@{") and text.strip().endswith("}"):
                        inner = text.strip()[2:-1]
                        parsed: dict[str, Any] = {}
                        for part in inner.split(";"):
                            part = part.strip()
                            if not part:
                                continue
                            if "=" in part:
                                k, v = part.split("=", 1)
                                k = k.strip()
                                v = v.strip()
                                # strip quotes
                                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                                    v = v[1:-1]
                                # try numeric conversion
                                try:
                                    if "." in v:
                                        v_conv = float(v)
                                    else:
                                        v_conv = int(v)
                                    v = v_conv
                                except Exception:
                                    pass
                                parsed[k] = v
                        body = parsed
                    else:
                        body = text

                # Call verify_active_trade to check and insert if types differ
                verify_result = False
                if isinstance(body, dict):
                    # Extract the actual payload if it's wrapped in a 'payload' key
                    verify_payload = body.get("payload", body)
                    verify_result = verify_active_trade(interval, verify_payload)

                results[interval] = {
                    "status": "success" if resp.status_code == 200 else "error",
                    "status_code": resp.status_code,
                    "payload": body,
                    "verify_trade_processed": verify_result,
                }
            except Exception as exc:
                results[interval] = {"status": "error", "error": str(exc)}

    return results




@app.get("/api/trades/{interval}")
async def get_trades(interval: str) -> dict[str, Any]:
    if interval not in INTERVALS:
        raise HTTPException(status_code=404, detail="Unsupported interval")

    async with trade_history_lock:
        rows = get_trade_history(interval)

    return {"interval": interval, "trade_history": rows}


@app.get("/api/trades/{interval}/download")
async def download_trades(interval: str):
    if interval not in INTERVALS:
        raise HTTPException(status_code=404, detail="Unsupported interval")

    async with trade_history_lock:
        rows = get_trade_history(interval)

    headers = [
        'tradeNumber','type','dateTime','signal','price','size','openCandleTime','fill_status','fill_time','netPnl','favorableExcursion','adverseExcursion','cumulativePnl'
    ]

    def iter_csv():
        yield ','.join(headers) + '\n'
        for r in rows:
            values = []
            for h in headers:
                v = r.get(h, '')
                if v is None:
                    values.append('')
                    continue
                if h in ('dateTime', 'fill_time'):
                    try:
                        values.append(f'"{datetime.fromisoformat(str(v)).isoformat()}"')
                    except Exception:
                        values.append(f'"{str(v)}"')
                    continue
                if isinstance(v, str):
                    values.append('"' + v.replace('"', '""') + '"')
                else:
                    values.append(str(v))
            yield ','.join(values) + '\n'

    filename = f"trades-{interval}-{datetime.utcnow().isoformat().replace(':','-')}.csv"
    return StreamingResponse(iter_csv(), media_type='text/csv', headers={
        'Content-Disposition': f'attachment; filename="{filename}"'
    })


@app.post("/api/trades/{interval}")
async def add_trade(interval: str, trade: TradeRow) -> dict[str, Any]:
    if interval not in INTERVALS:
        raise HTTPException(status_code=404, detail="Unsupported interval")

    async with trade_history_lock:
        row = trade_row_to_dict(trade)
        insert_manual_trade_row(interval, row)
        rows = get_trade_history(interval)

    return {"interval": interval, "trade_history": rows}


@app.delete("/api/trades/{interval}")
async def clear_trades(interval: str) -> dict[str, Any]:
    if interval not in INTERVALS:
        raise HTTPException(status_code=404, detail="Unsupported interval")

    async with trade_history_lock:
        clear_trade_history(interval)

    return {"interval": interval, "trade_history": []}

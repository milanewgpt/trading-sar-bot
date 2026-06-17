"""
Unified trading bot: two strategies, one Telegram bot.
  • SAR   — DOGE-USDT futures, 5m/15m, Parabolic SAR + SMA
  • EMA   — SOL-USDT  futures, 1h,     EMA 7/14/28/100 pullback
"""

import asyncio
import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bingx import BingXClient
from config import (
    BINGX_API_KEY,
    BINGX_SECRET_KEY,
    CANDLES_LIMIT,
    DATA_DIR,
    LEVERAGE,
    LOOP_INTERVAL,
    MARGIN,
    PAPER_MODE,
    SAR_PAPER_MODE,
    POSITION_SIZE,
    SYMBOL,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    TF_CONFIRM,
    TF_ENTRY,
    MAX_OPEN_POSITIONS,
)
from strategy import check_signal
from strategy_ema import check_ema_signal, signal_details

_log_handlers: list = [logging.StreamHandler()]
# On Railway (DATA_DIR set): also write to persistent volume log file
if os.getenv("DATA_DIR"):
    _log_handlers.append(logging.FileHandler(Path(DATA_DIR) / "bot.log"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SOL_SYMBOL = "SOL-USDT"
EMA_TF = "1h"
EMA_CANDLES = 220  # need 100+ for EMA100 warmup

_data = Path(DATA_DIR)
_data.mkdir(parents=True, exist_ok=True)
STATE_SAR          = _data / "state_sar.json"
STATE_EMA          = _data / "state_ema.json"
STATE_SAR_ETH_LIVE = _data / "state_sar_eth_live.json"
STATE_SAR_BTC_LIVE = _data / "state_sar_btc_live.json"
STATE_SAR_SOL_LIVE = _data / "state_sar_sol_live.json"
STATE_EMA_BTC_LIVE = _data / "state_ema_btc_live.json"
STATE_EMA_ETH_LIVE = _data / "state_ema_eth_live.json"

ETH_SAR_SYMBOL = "ETH-USDT"
BTC_SAR_SYMBOL = "BTC-USDT"
SOL_SAR_SYMBOL = "SOL-USDT"
BTC_EMA_SYMBOL = "BTC-USDT"
ETH_EMA_SYMBOL = "ETH-USDT"
TRADE_LOG  = _data / "trades.csv"

SL_COOLDOWN = 4 * 3600  # 4h cooldown after SL hit

MONITORING = "monitoring"
PENDING_APPROVAL = "pending_approval"
POSITION_OPEN = "position_open"

bingx = BingXClient(BINGX_API_KEY, BINGX_SECRET_KEY)

# ── Paper strategy configs (auto-approved, no Telegram keyboard) ──────────────

PAPER_SAR_CONFIGS = []
PAPER_EMA_CONFIGS = []

def paper_state_path(name: str) -> Path:
    return _data / f"state_{name.lower()}.json"

# ── State helpers ─────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "state": MONITORING,
        "signal": None,
        "position": None,
        "last_candle_ts": None,
    }


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return _empty_state()


def save_state(path: Path, s: dict) -> None:
    path.write_text(json.dumps(s, indent=2))


# ── Trade log ─────────────────────────────────────────────────────────────────

def append_trade_log(row: dict) -> None:
    fields = [
        "mode", "strategy", "timestamp", "symbol", "direction",
        "entry", "stop", "take", "quantity", "margin", "close_reason", "result",
    ]
    # Dedup guard: PAPER — full history scan (no time limit, catches restart re-logs)
    # LIVE — 2h window only
    if TRADE_LOG.exists():
        try:
            with open(TRADE_LOG, newline="") as f:
                existing = list(csv.DictReader(f))
            is_paper = row.get("mode") == "PAPER"
            cutoff = 0 if is_paper else time.time() - 7200
            scan = existing if is_paper else existing[-50:]
            for r in scan:
                if not is_paper:
                    try:
                        ts = datetime.fromisoformat(r.get("timestamp", "")).timestamp()
                    except Exception:
                        continue
                    if ts <= cutoff:
                        continue
                if (r.get("strategy") == row.get("strategy")
                        and r.get("symbol") == row.get("symbol")
                        and r.get("entry") == str(row.get("entry"))
                        and r.get("direction") == row.get("direction")):
                    log.warning("[dedup] skipping duplicate trade %s %s @ %s",
                                row.get("strategy"), row.get("direction"), row.get("entry"))
                    return
        except Exception as e:
            log.warning("[dedup] check failed: %s", e)

    exists = TRADE_LOG.exists()
    with open(TRADE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def read_trade_stats(mode_filter: str) -> dict:
    """Returns {strategy: {wins, losses, pnl}} filtered by mode (LIVE/PAPER)."""
    stats: dict = {}
    if not TRADE_LOG.exists():
        return stats
    with open(TRADE_LOG, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("mode", "").upper() != mode_filter.upper():
                continue
            strat = row.get("strategy", "?")
            if strat not in stats:
                stats[strat] = {"wins": 0, "losses": 0, "pnl": 0.0}
            result = row.get("result", "")
            try:
                pnl = float(result.split()[-1].replace("$", ""))
            except Exception:
                pnl = 0.0
            if "WIN" in result:
                stats[strat]["wins"] += 1
            else:
                stats[strat]["losses"] += 1
            stats[strat]["pnl"] += pnl
    for s in stats:
        stats[s]["pnl"] = round(stats[s]["pnl"], 2)
    return stats


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _sar_signal_text(sig: dict, symbol: str = None) -> str:
    if symbol is None:
        symbol = SYMBOL
    d = sig["direction"].upper()
    emoji = "🟢" if sig["direction"] == "long" else "🔴"
    sym_label = symbol.replace("-", "")
    fmt = ".6f" if "DOGE" in symbol else ".4f"
    return (
        f"{emoji} *{sym_label} — {d}*\n"
        f"_Strategy: SAR + SMA 5m/15m_\n\n"
        f"Entry: `{sig['entry']:{fmt}}`\n"
        f"Stop Loss: `{sig['stop']:{fmt}}`\n"
        f"Take Profit: `{sig['take']:{fmt}}`\n\n"
        f"Margin: ${MARGIN}  |  Leverage: x{LEVERAGE}\n"
        f"Position size: ${POSITION_SIZE}\n"
        f"RR: 1:2"
    )


def _ema_signal_text(sig: dict, symbol: str = None) -> str:
    if symbol is None:
        symbol = SOL_SYMBOL
    d = sig["direction"].upper()
    emoji = "🟢" if sig["direction"] == "long" else "🔴"
    sym_label = symbol.replace("-", "")
    det = sig.get("details", {})
    return (
        f"{emoji} *{sym_label} — {d}*\n"
        f"_Strategy: EMA Pullback 1h_\n\n"
        f"Entry: `{sig['entry']:.4f}`\n"
        f"Stop Loss: `{sig['stop']:.4f}`\n"
        f"Take Profit: `{sig['take']:.4f}`\n\n"
        f"EMA100 trend: *{det.get('trend', '?')}*\n"
        f"Pullback candles: *{det.get('pullback', '?')}*\n"
        f"Entry candle: *{det.get('candle', '?')}*\n"
        f"Vol ratio: *{det.get('vol_ratio', '?')}x*\n\n"
        f"Margin: ${MARGIN}  |  Leverage: x{LEVERAGE}\n"
        f"Position size: ${POSITION_SIZE}\n"
        f"RR: 1:2"
    )


def _approve_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"{prefix}:approve"),
        InlineKeyboardButton("❌ Skip",    callback_data=f"{prefix}:skip"),
    ]])


async def send_signal(app: Application, text: str, prefix: str) -> int:
    msg = await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=_approve_keyboard(prefix),
    )
    return msg.message_id


async def notify(app: Application, text: str) -> None:
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown"
    )


# ── Trade execution ───────────────────────────────────────────────────────────

async def execute_trade(sig: dict, symbol: str, paper: bool = True) -> dict:
    entry = sig["entry"]
    if paper:
        # Fractional qty → correct PnL for any price (BTC, ETH, DOGE, etc.)
        quantity = round(POSITION_SIZE / entry, 6)
        log.info("[PAPER] Virtual %s %s @ %.5f qty=%.6f", sig["direction"], symbol, entry, quantity)
    else:
        if entry >= 500:
            # BTC/ETH: fractional qty, 3 decimal places (min 0.001)
            quantity = max(0.001, round(POSITION_SIZE / entry, 3))
        else:
            # SOL/DOGE and other cheap assets: integer qty (floor to avoid over-exposure)
            quantity = max(1, int(POSITION_SIZE / entry))
        log.info("[LIVE] Opening %s %s @ %.5f qty=%s", sig["direction"], symbol, entry, quantity)
        await bingx.set_leverage(symbol, LEVERAGE)
        order = await bingx.open_position(
            symbol=symbol,
            direction=sig["direction"],
            quantity=quantity,
            sl_price=sig["stop"],
            tp_price=sig["take"],
        )
        if not await confirm_live_position(symbol):
            raise RuntimeError(f"{symbol}: order accepted but no open position found on BingX")
        # Use actual fill price from exchange instead of signal price
        try:
            avg_price = float(order.get("data", {}).get("order", {}).get("avgPrice", 0))
            if avg_price > 0:
                log.info("[LIVE] actual fill price: %.6f (signal was %.6f)", avg_price, entry)
                entry = avg_price
        except Exception:
            pass
        log.info("[LIVE] %s order confirmed on exchange: %s", symbol, order)

    return {
        "direction": sig["direction"],
        "entry": entry,
        "stop": sig["stop"],
        "take": sig["take"],
        "quantity": quantity,
        "open_time": datetime.now(timezone.utc).isoformat(),
    }


async def confirm_live_position(symbol: str, attempts: int = 5, delay: float = 1.0) -> bool:
    for attempt in range(attempts):
        positions = await bingx.get_positions(symbol)
        for pos in positions:
            if abs(float(pos.get("positionAmt", 0))) > 0:
                return True
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    return False


# ── Position monitoring ───────────────────────────────────────────────────────

async def is_position_closed(symbol: str) -> bool:
    positions = await bingx.get_positions(symbol)
    for pos in positions:
        if abs(float(pos.get("positionAmt", 0))) > 0:
            return False
    return True


def can_open_position(symbol: str, direction: str) -> bool:
    """Return False if opening would violate position rules:
    - max 1 open/pending position per symbol
    - max 1 open/pending position per direction (long/short)
    """
    # (state_path, symbol) for every live strategy
    all_strategies = [
        (STATE_SAR,          SYMBOL),
        (STATE_SAR_ETH_LIVE, ETH_SAR_SYMBOL),
        (STATE_SAR_BTC_LIVE, BTC_SAR_SYMBOL),
        (STATE_SAR_SOL_LIVE, SOL_SAR_SYMBOL),
        (STATE_EMA,          SOL_SYMBOL),
        (STATE_EMA_BTC_LIVE, BTC_EMA_SYMBOL),
        (STATE_EMA_ETH_LIVE, ETH_EMA_SYMBOL),
    ]
    direction_lc = direction.lower()
    for path, strat_symbol in all_strategies:
        try:
            s = load_state(path)
            if s.get("state") not in (POSITION_OPEN, PENDING_APPROVAL):
                continue
            # Same asset → block
            if strat_symbol == symbol:
                return False
            # Same direction → block
            pos = s.get("position") or s.get("signal") or {}
            if (pos.get("direction") or "").lower() == direction_lc:
                return False
        except Exception:
            pass
    return True


async def get_live_close_reason(symbol: str, open_time: str, direction: str = "short") -> tuple[str, float | None]:
    """Query BingX to determine close reason (TP/SL/CLOSED) and actual exit price.
    Returns (reason, exit_price). Uses historyOrders first, fillHistory as fallback."""
    try:
        start_ts = int(datetime.fromisoformat(open_time).timestamp() * 1000)
        await asyncio.sleep(3)
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(4)
            # Try historyOrders for explicit SL/TP order type
            orders = await bingx.get_history_orders(symbol, start_ts=start_ts, limit=20)
            log.info("[close_reason] attempt=%d historyOrders=%d", attempt + 1, len(orders))
            for order in orders:
                if order.get("status") != "FILLED":
                    continue
                otype = order.get("type", "")
                price = float(order.get("avgPrice") or order.get("price") or 0) or None
                log.info("[close_reason] type=%s price=%s", otype, price)
                if otype == "TAKE_PROFIT_MARKET":
                    return "TP", price
                if otype == "STOP_MARKET":
                    return "SL", price
            # Fallback: use fillHistory — determine reason from realizedPNL sign
            close_side = "BUY" if direction == "short" else "SELL"
            pos_side = "SHORT" if direction == "short" else "LONG"
            fills = await bingx.get_fill_history(symbol, start_ts=start_ts, limit=50)
            log.info("[close_reason] attempt=%d fillHistory=%d", attempt + 1, len(fills))
            for fill in fills:
                if fill.get("side") == close_side and fill.get("positionSide") == pos_side:
                    pnl = float(fill.get("realizedPNL", 0) or 0)
                    price = float(fill.get("price", 0) or 0) or None
                    if pnl != 0:
                        reason = "TP" if pnl > 0 else "SL"
                        log.info("[close_reason] fill fallback: %s price=%s pnl=%s", reason, price, pnl)
                        return reason, price
    except Exception as e:
        log.warning("get_live_close_reason failed: %s", e)
    return "CLOSED", None


def paper_check_closed(position: dict, candles: list) -> str | None:
    direction = position["direction"]
    sl = float(position["stop"])
    tp = float(position["take"])
    open_ts = int(datetime.fromisoformat(position["open_time"]).timestamp() * 1000)
    for c in candles:
        # skip candles that opened before the position was opened
        if int(c["time"]) < open_ts:
            continue
        high = float(c["high"])
        low = float(c["low"])
        if direction == "long":
            if low <= sl:
                return "SL"
            if high >= tp:
                return "TP"
        else:
            if high >= sl:
                return "SL"
            if low <= tp:
                return "TP"
    return None


# ── Position closed handler (shared) ─────────────────────────────────────────

async def on_position_closed(
    app: Application,
    state: dict,
    state_path: Path,
    close_reason: str,
    strategy_name: str,
    symbol: str,
    sl_cooldown: int = 0,
    paper: bool = True,
    actual_exit: float | None = None,
) -> None:
    pos = state.get("position", {})
    direction = pos.get("direction", "").upper()
    entry = float(pos.get("entry", 0))
    sl = float(pos.get("stop", 0))
    tp = float(pos.get("take", 0))

    qty = float(pos.get("quantity", 1))
    if actual_exit is not None:
        exit_price = actual_exit
        if direction == "LONG":
            pnl = round((exit_price - entry) * qty, 2)
        else:
            pnl = round((entry - exit_price) * qty, 2)
        result = "WIN" if pnl > 0 else "LOSS"
        if close_reason == "CLOSED":
            close_reason = "TP" if result == "WIN" else "SL"
    else:
        result = "WIN" if close_reason == "TP" else "LOSS"
        exit_price = tp if close_reason == "TP" else sl
        if direction == "LONG":
            pnl = round((exit_price - entry) * qty, 2)
        else:
            pnl = round((entry - exit_price) * qty, 2)

    log.info("[%s][%s] closed via %s | %s entry=%.5f pnl=%+.2f",
             "PAPER" if paper else "LIVE", strategy_name, close_reason, direction, entry, pnl)

    append_trade_log({
        "mode": "PAPER" if paper else "LIVE",
        "strategy": strategy_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": sl,
        "take": tp,
        "quantity": pos.get("quantity", ""),
        "margin": MARGIN,
        "close_reason": close_reason,
        "result": f"{result} {pnl:+.2f}$",
    })

    state["state"] = MONITORING
    state["position"] = None
    state["signal"] = None
    if close_reason == "SL" and sl_cooldown > 0:
        state["cooldown_until"] = time.time() + sl_cooldown
        log.info("[%s] SL cooldown active for %dh", strategy_name, sl_cooldown // 3600)
    save_state(state_path, state)

    mode_label = "📝 PAPER" if paper else "📋 LIVE"
    result_emoji = "✅" if result == "WIN" else "❌"
    sym_label = symbol.replace("-", "")
    safe_name = strategy_name.replace("_", " ")
    await notify(
        app,
        f"{mode_label} | {result_emoji} *{result}* — {direction} {sym_label}\n"
        f"_Strategy: {safe_name}_\n"
        f"Entry: `{entry:.5f}`  Close: *{close_reason}*  PnL: `{pnl:+.2f}$`",
    )


# ── Paper strategy loops (auto-approve, 6 instances: BTC/ETH/SOL × SAR/EMA) ──

async def sar_paper_tick(app: Application, state: dict, cfg: dict, state_path: Path) -> None:
    """SAR tick for paper — auto-executes signal without user confirmation."""
    name = cfg["name"]
    symbol = cfg["symbol"]
    tf_entry = cfg["tf_entry"]
    tf_confirm = cfg["tf_confirm"]
    s = state["state"]

    if s == MONITORING:
        candles_5m = await bingx.get_klines(symbol, tf_entry, CANDLES_LIMIT)
        if len(candles_5m) < 3:
            return
        closed_5m = candles_5m[:-1]
        last_ts = closed_5m[-1]["time"]
        if last_ts == state.get("last_candle_ts"):
            return
        state["last_candle_ts"] = last_ts
        save_state(state_path, state)

        candles_15m = await bingx.get_klines(symbol, tf_confirm, CANDLES_LIMIT)
        if len(candles_15m) < 3:
            return

        direction, entry, sar_val = check_signal(closed_5m, candles_15m[:-1])
        if direction is None:
            return

        risk = entry - sar_val if direction == "long" else sar_val - entry
        take = entry + 2 * risk if direction == "long" else entry - 2 * risk
        sig = {"direction": direction, "entry": entry, "stop": sar_val, "take": take}
        log.info("[%s] signal %s entry=%.6f sl=%.6f tp=%.6f", name, direction, entry, sar_val, take)

        position = await execute_trade(sig, symbol, paper=True)
        state["state"] = POSITION_OPEN
        state["position"] = position
        state["signal"] = None
        save_state(state_path, state)

        emoji = "🟢" if direction == "long" else "🔴"
        await notify(app,
            f"📝 PAPER | {emoji} *{name}* — {direction.upper()} {symbol.replace('-','')}\n"
            f"Entry: `{entry:.5f}`  SL: `{sar_val:.5f}`  TP: `{take:.5f}`"
        )

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        candles = await bingx.get_klines(symbol, tf_entry, 10)
        close_reason = paper_check_closed(pos, candles[:-1])
        if close_reason:
            await on_position_closed(app, state, state_path, close_reason, name, symbol, paper=True)


async def ema_paper_tick(app: Application, state: dict, cfg: dict, state_path: Path) -> None:
    """EMA tick for paper — auto-executes signal without user confirmation."""
    name = cfg["name"]
    symbol = cfg["symbol"]
    s = state["state"]

    if s == MONITORING:
        if time.time() < state.get("cooldown_until", 0):
            return
        candles = await bingx.get_klines(symbol, EMA_TF, EMA_CANDLES)
        if len(candles) < 110:
            return
        closed = candles[:-1]
        last_ts = closed[-1]["time"]
        if last_ts == state.get("last_candle_ts"):
            return
        state["last_candle_ts"] = last_ts
        save_state(state_path, state)

        direction, entry, stop = check_ema_signal(closed)
        if direction is None:
            return

        risk = entry - stop if direction == "long" else stop - entry
        take = entry + 2 * risk if direction == "long" else entry - 2 * risk
        sig = {"direction": direction, "entry": entry, "stop": stop, "take": take}
        log.info("[%s] signal %s entry=%.4f sl=%.4f tp=%.4f", name, direction, entry, stop, take)

        position = await execute_trade(sig, symbol, paper=True)
        state["state"] = POSITION_OPEN
        state["position"] = position
        state["signal"] = None
        save_state(state_path, state)

        emoji = "🟢" if direction == "long" else "🔴"
        await notify(app,
            f"📝 PAPER | {emoji} *{name}* — {direction.upper()} {symbol.replace('-','')}\n"
            f"Entry: `{entry:.4f}`  SL: `{stop:.4f}`  TP: `{take:.4f}`"
        )

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        candles = await bingx.get_klines(symbol, EMA_TF, 10)
        close_reason = paper_check_closed(pos, candles[:-1])
        if close_reason:
            await on_position_closed(app, state, state_path, close_reason, name, symbol,
                                     paper=True, sl_cooldown=SL_COOLDOWN)


async def paper_strategy_loop(app: Application, tick_fn, cfg: dict, interval: float) -> None:
    name = cfg["name"]
    state_path = paper_state_path(name)
    log.info("[%s] paper loop started.", name)
    while True:
        state = load_state(state_path)
        try:
            await tick_fn(app, state, cfg, state_path)
        except Exception as e:
            log.error("[%s] paper tick error: %s", name, e, exc_info=True)
        await asyncio.sleep(interval)


# ── SAR loop ──────────────────────────────────────────────────────────────────

async def sar_loop(app: Application) -> None:
    log.info("[SAR] loop started.")
    while True:
        state = load_state(STATE_SAR)
        try:
            await sar_tick(app, state)
        except Exception as e:
            log.error("[SAR] tick error: %s", e, exc_info=True)
        await asyncio.sleep(LOOP_INTERVAL)


async def sar_eth_loop(app: Application) -> None:
    log.info("[SAR_ETH] loop started.")
    while True:
        state = load_state(STATE_SAR_ETH_LIVE)
        try:
            await sar_eth_tick(app, state)
        except Exception as e:
            log.error("[SAR_ETH] tick error: %s", e, exc_info=True)
        await asyncio.sleep(LOOP_INTERVAL)


async def sar_btc_loop(app: Application) -> None:
    log.info("[SAR_BTC] loop started.")
    while True:
        state = load_state(STATE_SAR_BTC_LIVE)
        try:
            await sar_btc_tick(app, state)
        except Exception as e:
            log.error("[SAR_BTC] tick error: %s", e, exc_info=True)
        await asyncio.sleep(LOOP_INTERVAL)


async def sar_sol_loop(app: Application) -> None:
    log.info("[SAR_SOL] loop started.")
    while True:
        state = load_state(STATE_SAR_SOL_LIVE)
        try:
            await sar_sol_tick(app, state)
        except Exception as e:
            log.error("[SAR_SOL] tick error: %s", e, exc_info=True)
        await asyncio.sleep(LOOP_INTERVAL)


async def _sar_live_tick(
    app: Application, state: dict, *,
    symbol: str, state_path: Path, strategy_name: str, callback_prefix: str, paper: bool,
) -> None:
    if state.get("paused"):
        return
    s = state["state"]

    if s == MONITORING:
        candles_5m = await bingx.get_klines(symbol, TF_ENTRY, CANDLES_LIMIT)
        if len(candles_5m) < 3:
            return

        closed_5m = candles_5m[:-1]
        last_ts = closed_5m[-1]["time"]

        if last_ts == state.get("last_candle_ts"):
            return

        log.info("[%s] new candle ts=%s close=%.6f", strategy_name, last_ts, float(closed_5m[-1]["close"]))
        state["last_candle_ts"] = last_ts
        save_state(state_path, state)

        candles_15m = await bingx.get_klines(symbol, TF_CONFIRM, CANDLES_LIMIT)
        if len(candles_15m) < 3:
            return

        direction, entry, sar_val = check_signal(closed_5m, candles_15m[:-1])

        if direction is None:
            return

        risk = entry - sar_val if direction == "long" else sar_val - entry
        take = entry + 2 * risk if direction == "long" else entry - 2 * risk

        sig = {
            "direction": direction,
            "entry": entry,
            "stop": sar_val,
            "take": take,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not can_open_position(symbol, direction):
            log.info("[%s] signal skipped: position limit (1 per asset, 1 per direction)", strategy_name)
            return

        log.info("[%s] signal %s entry=%.6f sl=%.6f tp=%.6f", strategy_name, direction, entry, sar_val, take)
        msg_id = await send_signal(app, _sar_signal_text(sig, symbol), callback_prefix)

        state["state"] = PENDING_APPROVAL
        state["signal"] = sig
        state["pending_msg_id"] = msg_id
        save_state(state_path, state)

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        close_reason, actual_exit = None, None
        if paper:
            candles = await bingx.get_klines(symbol, TF_ENTRY, 10)
            close_reason = paper_check_closed(pos, candles[:-1])
        else:
            if await is_position_closed(symbol):
                close_reason, actual_exit = await get_live_close_reason(symbol, pos.get("open_time", ""), pos.get("direction", "short"))

        if close_reason:
            await on_position_closed(app, state, state_path, close_reason, strategy_name, symbol, paper=paper, actual_exit=actual_exit)


async def sar_tick(app: Application, state: dict) -> None:
    await _sar_live_tick(
        app, state,
        symbol=SYMBOL, state_path=STATE_SAR, strategy_name="SAR",
        callback_prefix="sar", paper=SAR_PAPER_MODE,
    )


async def sar_eth_tick(app: Application, state: dict) -> None:
    await _sar_live_tick(
        app, state,
        symbol=ETH_SAR_SYMBOL, state_path=STATE_SAR_ETH_LIVE, strategy_name="SAR_ETH",
        callback_prefix="sar_eth", paper=False,
    )


async def sar_btc_tick(app: Application, state: dict) -> None:
    await _sar_live_tick(
        app, state,
        symbol=BTC_SAR_SYMBOL, state_path=STATE_SAR_BTC_LIVE, strategy_name="SAR_BTC",
        callback_prefix="sar_btc", paper=False,
    )


async def sar_sol_tick(app: Application, state: dict) -> None:
    await _sar_live_tick(
        app, state,
        symbol=SOL_SAR_SYMBOL, state_path=STATE_SAR_SOL_LIVE, strategy_name="SAR_SOL",
        callback_prefix="sar_sol", paper=False,
    )



# ── EMA loop ──────────────────────────────────────────────────────────────────

async def ema_loop(app: Application) -> None:
    log.info("[EMA] loop started.")
    while True:
        state = load_state(STATE_EMA)
        try:
            await ema_tick(app, state)
        except Exception as e:
            log.error("[EMA] tick error: %s", e, exc_info=True)
        await asyncio.sleep(30)  # check every 30s, new 1h candle appears rarely


async def ema_tick(app: Application, state: dict) -> None:
    if state.get("paused"):
        return
    s = state["state"]

    if s == MONITORING:
        cooldown_until = state.get("cooldown_until", 0)
        if time.time() < cooldown_until:
            log.info("[EMA] cooldown active, %dm remaining", int(cooldown_until - time.time()) // 60)
            return

        candles = await bingx.get_klines(SOL_SYMBOL, EMA_TF, EMA_CANDLES)
        if len(candles) < 110:
            return

        closed = candles[:-1]
        last_ts = closed[-1]["time"]

        if last_ts == state.get("last_candle_ts"):
            return

        log.info("[EMA] new candle ts=%s close=%.4f", last_ts, float(closed[-1]["close"]))
        state["last_candle_ts"] = last_ts
        save_state(STATE_EMA, state)

        direction, entry, stop = check_ema_signal(closed)

        if direction is None:
            return

        risk = entry - stop if direction == "long" else stop - entry
        take = entry + 2 * risk if direction == "long" else entry - 2 * risk

        details = signal_details(closed)

        sig = {
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "take": take,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not can_open_position(SOL_SYMBOL, direction):
            log.info("[EMA] signal skipped: position limit (1 per asset, 1 per direction)")
            return

        log.info("[EMA] signal %s entry=%.4f sl=%.4f tp=%.4f", direction, entry, stop, take)
        msg_id = await send_signal(app, _ema_signal_text(sig), "ema")

        state["state"] = PENDING_APPROVAL
        state["signal"] = sig
        state["pending_msg_id"] = msg_id
        save_state(STATE_EMA, state)

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        close_reason, actual_exit = None, None
        if PAPER_MODE:
            candles = await bingx.get_klines(SOL_SYMBOL, EMA_TF, 10)
            close_reason = paper_check_closed(pos, candles[:-1])
        else:
            if await is_position_closed(SOL_SYMBOL):
                close_reason, actual_exit = await get_live_close_reason(SOL_SYMBOL, pos.get("open_time", ""), pos.get("direction", "short"))

        if close_reason:
            await on_position_closed(app, state, STATE_EMA, close_reason, "EMA", SOL_SYMBOL, paper=PAPER_MODE, sl_cooldown=SL_COOLDOWN, actual_exit=actual_exit)


async def _ema_live_tick(
    app: Application, state: dict, *,
    symbol: str, state_path: Path, strategy_name: str, callback_prefix: str,
) -> None:
    if state.get("paused"):
        return
    s = state["state"]

    if s == MONITORING:
        cooldown_until = state.get("cooldown_until", 0)
        if time.time() < cooldown_until:
            log.info("[%s] cooldown active, %dm remaining", strategy_name, int(cooldown_until - time.time()) // 60)
            return

        candles = await bingx.get_klines(symbol, EMA_TF, EMA_CANDLES)
        if len(candles) < 110:
            return

        closed = candles[:-1]
        last_ts = closed[-1]["time"]

        if last_ts == state.get("last_candle_ts"):
            return

        log.info("[%s] new candle ts=%s close=%.4f", strategy_name, last_ts, float(closed[-1]["close"]))
        state["last_candle_ts"] = last_ts
        save_state(state_path, state)

        direction, entry, stop = check_ema_signal(closed)
        if direction is None:
            return

        risk = entry - stop if direction == "long" else stop - entry
        take = entry + 2 * risk if direction == "long" else entry - 2 * risk
        details = signal_details(closed)

        sig = {
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "take": take,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not can_open_position(symbol, direction):
            log.info("[%s] signal skipped: position limit (1 per asset, 1 per direction)", strategy_name)
            return

        log.info("[%s] signal %s entry=%.4f sl=%.4f tp=%.4f", strategy_name, direction, entry, stop, take)
        msg_id = await send_signal(app, _ema_signal_text(sig, symbol), callback_prefix)

        state["state"] = PENDING_APPROVAL
        state["signal"] = sig
        state["pending_msg_id"] = msg_id
        save_state(state_path, state)

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        if await is_position_closed(symbol):
            close_reason, actual_exit = await get_live_close_reason(symbol, pos.get("open_time", ""), pos.get("direction", "short"))
        else:
            close_reason, actual_exit = None, None

        if close_reason:
            await on_position_closed(app, state, state_path, close_reason, strategy_name, symbol, paper=False, sl_cooldown=SL_COOLDOWN, actual_exit=actual_exit)


async def ema_btc_loop(app: Application) -> None:
    log.info("[EMA_BTC] loop started.")
    while True:
        state = load_state(STATE_EMA_BTC_LIVE)
        try:
            await ema_btc_tick(app, state)
        except Exception as e:
            log.error("[EMA_BTC] tick error: %s", e, exc_info=True)
        await asyncio.sleep(30)


async def ema_eth_loop(app: Application) -> None:
    log.info("[EMA_ETH] loop started.")
    while True:
        state = load_state(STATE_EMA_ETH_LIVE)
        try:
            await ema_eth_tick(app, state)
        except Exception as e:
            log.error("[EMA_ETH] tick error: %s", e, exc_info=True)
        await asyncio.sleep(30)


async def ema_btc_tick(app: Application, state: dict) -> None:
    await _ema_live_tick(app, state,
        symbol=BTC_EMA_SYMBOL, state_path=STATE_EMA_BTC_LIVE,
        strategy_name="EMA_BTC", callback_prefix="ema_btc")


async def ema_eth_tick(app: Application, state: dict) -> None:
    await _ema_live_tick(app, state,
        symbol=ETH_EMA_SYMBOL, state_path=STATE_EMA_ETH_LIVE,
        strategy_name="EMA_ETH", callback_prefix="ema_eth")


# ── Callback handler (routes by prefix) ──────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        prefix, action = query.data.split(":")
    except ValueError:
        return

    if prefix == "sar":
        await _handle_approval(query, context, action, STATE_SAR, SYMBOL, "SAR", paper=SAR_PAPER_MODE)
    elif prefix == "sar_eth":
        await _handle_approval(query, context, action, STATE_SAR_ETH_LIVE, ETH_SAR_SYMBOL, "SAR_ETH", paper=False)
    elif prefix == "sar_btc":
        await _handle_approval(query, context, action, STATE_SAR_BTC_LIVE, BTC_SAR_SYMBOL, "SAR_BTC", paper=False)
    elif prefix == "sar_sol":
        await _handle_approval(query, context, action, STATE_SAR_SOL_LIVE, SOL_SAR_SYMBOL, "SAR_SOL", paper=False)
    elif prefix == "ema":
        await _handle_approval(query, context, action, STATE_EMA, SOL_SYMBOL, "EMA", paper=PAPER_MODE)
    elif prefix == "ema_btc":
        await _handle_approval(query, context, action, STATE_EMA_BTC_LIVE, BTC_EMA_SYMBOL, "EMA_BTC", paper=False)
    elif prefix == "ema_eth":
        await _handle_approval(query, context, action, STATE_EMA_ETH_LIVE, ETH_EMA_SYMBOL, "EMA_ETH", paper=False)


async def _handle_approval(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    state_path: Path,
    symbol: str,
    strategy_name: str,
    paper: bool = True,
) -> None:
    state = load_state(state_path)

    if state["state"] != PENDING_APPROVAL:
        await query.edit_message_text("⚠️ Signal already expired or processed.")
        return

    if action == "approve":
        sig = state["signal"]
        await query.edit_message_text(
            f"⏳ Opening {sig['direction'].upper()} {symbol} ({strategy_name})…"
        )
        try:
            position = await execute_trade(sig, symbol, paper=paper)
        except Exception as e:
            log.error("[%s] trade execution failed: %s", strategy_name, e, exc_info=True)
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"❌ Failed to open {strategy_name} position: {e}",
            )
            state["state"] = MONITORING
            state["signal"] = None
            save_state(state_path, state)
            return

        state["state"] = POSITION_OPEN
        state["position"] = position
        state["signal"] = None
        save_state(state_path, state)

        mode_tag = "📝 PAPER" if paper else "✅ LIVE"
        sym_label = symbol.replace("-", "")
        safe_name = strategy_name.replace("_", " ")
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"{mode_tag} | *Position opened*\n"
                    f"_Strategy: {safe_name}_\n\n"
                    f"*{sig['direction'].upper()}* {sym_label}\n"
                    f"Entry: `{sig['entry']:.5f}`\n"
                    f"SL: `{sig['stop']:.5f}`\n"
                    f"TP: `{sig['take']:.5f}`\n"
                    f"Qty: `{position['quantity']}` | Margin: ${MARGIN} | x{LEVERAGE}"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("[%s] failed to send open notification: %s", strategy_name, e)

    elif action == "skip":
        await query.edit_message_text(f"⏭ {strategy_name} signal skipped.")
        state["state"] = MONITORING
        state["signal"] = None
        save_state(state_path, state)
        log.info("[%s] signal skipped by user.", strategy_name)



# ── /status command ───────────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📊 *Bot Status*\n"]

    # ── SAR + EMA ─────────────────────────────────────────────────────────────
    strategies = [
        ("SAR",     STATE_SAR,          SYMBOL,         TF_ENTRY),
        ("SAR ETH", STATE_SAR_ETH_LIVE, ETH_SAR_SYMBOL, TF_ENTRY),
        ("SAR BTC", STATE_SAR_BTC_LIVE, BTC_SAR_SYMBOL, TF_ENTRY),
        ("SAR SOL", STATE_SAR_SOL_LIVE, SOL_SAR_SYMBOL, TF_ENTRY),
        ("EMA",     STATE_EMA,          SOL_SYMBOL,     EMA_TF),
        ("EMA BTC", STATE_EMA_BTC_LIVE, BTC_EMA_SYMBOL, EMA_TF),
        ("EMA ETH", STATE_EMA_ETH_LIVE, ETH_EMA_SYMBOL, EMA_TF),
    ]

    for name, state_path, symbol, tf in strategies:
        state = load_state(state_path)
        s = state["state"]
        sym_label = symbol.replace("-", "")  # e.g. SOL-USDT → SOLUSDT
        paused = state.get("paused", False)
        mode_label = "🔴 Stop" if paused else "🟢 LIVE"

        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"*{name} {mode_label}* — {sym_label} ({tf})")

        if s == MONITORING:
            if paused:
                lines.append("Status: ⏸ Paused")
            else:
                lines.append("Status: 👀 Monitoring")

        elif s == PENDING_APPROVAL:
            sig = state.get("signal", {})
            d = sig.get("direction", "").upper()
            lines.append(f"Status: ⏳ Pending approval")
            lines.append(f"Signal: *{d}* @ `{sig.get('entry', 0):.5f}`")

        elif s == POSITION_OPEN:
            pos = state.get("position", {})
            direction = pos.get("direction", "")
            entry = float(pos.get("entry", 0))
            sl = float(pos.get("stop", 0))
            tp = float(pos.get("take", 0))
            qty = pos.get("quantity", 0)
            open_time = pos.get("open_time", "")

            # Fetch current price
            try:
                candles = await bingx.get_klines(symbol, tf, 2)
                current_price = float(candles[-1]["close"])
            except Exception:
                current_price = entry

            # Unrealized PnL (nominal)
            if direction == "long":
                price_diff = current_price - entry
            else:
                price_diff = entry - current_price
            pnl_pct = price_diff / entry
            pnl_usd = round(POSITION_SIZE * pnl_pct, 2)
            pnl_emoji = "✅" if pnl_usd >= 0 else "❌"

            # Progress to TP/SL
            if direction == "long":
                risk = entry - sl
                progress = (current_price - entry) / (tp - entry) * 100 if tp != entry else 0
            else:
                risk = sl - entry
                progress = (entry - current_price) / (entry - tp) * 100 if tp != entry else 0
            progress = max(-999, min(100, round(progress)))

            # Time open
            try:
                opened = datetime.fromisoformat(open_time)
                elapsed = datetime.now(timezone.utc) - opened
                h, m = divmod(int(elapsed.total_seconds()) // 60, 60)
                elapsed_str = f"{h}h {m}m" if h else f"{m}m"
            except Exception:
                elapsed_str = "?"

            d_label = "LONG 🟢" if direction == "long" else "SHORT 🔴"
            lines.append(f"Status: 📈 Position open ({elapsed_str})")
            lines.append(f"Direction: *{d_label}*")
            lines.append(f"Entry:  `{entry:.5f}`")
            lines.append(f"Price:  `{current_price:.5f}`")
            lines.append(f"SL:     `{sl:.5f}`")
            lines.append(f"TP:     `{tp:.5f}`")
            lines.append(f"PnL:    {pnl_emoji} `{pnl_usd:+.2f}$` ({pnl_pct*100:+.2f}%)")
            lines.append(f"TP progress: `{progress}%`")

    # ── Statistics ────────────────────────────────────────────────────────────
    lines.append("\n━━━━━━━━━━━━━━━")
    lines.append("📈 *LIVE stats*")
    live = read_trade_stats("LIVE")
    if live:
        for strat, s in live.items():
            total = s["wins"] + s["losses"]
            wr = round(s["wins"] / total * 100) if total else 0
            pnl_emoji = "✅" if s["pnl"] >= 0 else "❌"
            lines.append(f"{strat.replace('_', ' ')}: {s['wins']}W / {s['losses']}L  WR {wr}%  {pnl_emoji} {s['pnl']:+.2f}$")
    else:
        lines.append("No trades yet")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Strategy control commands ────────────────────────────────────────────────

async def _manual_close(
    update: Update,
    state: dict,
    state_path: Path,
    strategy_name: str,
    symbol: str,
    tf: str,
    paper: bool,
    is_rsi: bool = False,
) -> bool:
    """Close open position manually. Returns True if position was closed."""
    if state.get("state") != POSITION_OPEN:
        await update.message.reply_text(f"⚠️ {strategy_name}: no open position.")
        return False

    pos = state.get("position", {})
    direction = pos.get("direction", "")
    entry = float(pos.get("entry", 0))

    try:
        candles = await bingx.get_klines(symbol, tf, 2)
        current_price = float(candles[-1]["close"])
    except Exception:
        current_price = entry

    if not paper:
        try:
            close_side = "SELL" if direction == "long" else "BUY"
            pos_side = "LONG" if direction == "long" else "SHORT"
            await bingx.place_market_order(symbol, close_side, pos_side, pos.get("quantity", 1))
        except Exception as e:
            await update.message.reply_text(f"❌ BingX error: {e}")
            return False

    if direction == "long":
        pnl = round((current_price - entry) / entry * POSITION_SIZE, 2)
    else:
        pnl = round((entry - current_price) / entry * POSITION_SIZE, 2)

    result = "WIN" if pnl >= 0 else "LOSS"
    row = {
        "mode": "PAPER" if paper else "LIVE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "direction": direction.upper(),
        "entry": entry,
        "stop": pos.get("stop", ""),
        "take": pos.get("take", ""),
        "quantity": pos.get("quantity", ""),
        "margin": MARGIN,
        "close_reason": "MANUAL",
        "result": f"{result} {pnl:+.2f}$",
    }
    if is_rsi:
        append_rsi_log(row)
    else:
        append_trade_log({**row, "strategy": strategy_name})

    state["state"] = MONITORING
    state["position"] = None
    state["signal"] = None
    save_state(state_path, state)

    pnl_emoji = "✅" if pnl >= 0 else "❌"
    await update.message.reply_text(
        f"🛑 *{strategy_name.replace('_', ' ')}* closed manually\n"
        f"_{('PAPER' if paper else 'LIVE')}_ | {direction.upper()} {symbol.replace('-', '')}\n"
        f"Entry: `{entry:.5f}` → `{current_price:.5f}`\n"
        f"PnL: {pnl_emoji} `{pnl:+.2f}$`",
        parse_mode="Markdown",
    )
    return True


# ── SAR control ───────────────────────────────────────────────────────────────

async def stop_sar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR)
    state["paused"] = True
    save_state(STATE_SAR, state)
    await update.message.reply_text("⏸ SAR paused.")


async def start_sar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR)
    state["paused"] = False
    save_state(STATE_SAR, state)
    await update.message.reply_text("▶️ SAR resumed.")


async def close_sar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR)
    await _manual_close(update, state, STATE_SAR, "SAR", SYMBOL, TF_ENTRY, paper=SAR_PAPER_MODE)


# ── SAR ETH control (LIVE) ────────────────────────────────────────────────────

async def stop_sar_eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_ETH_LIVE)
    state["paused"] = True
    save_state(STATE_SAR_ETH_LIVE, state)
    await update.message.reply_text("⏸ SAR_ETH paused.")


async def start_sar_eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_ETH_LIVE)
    state["paused"] = False
    save_state(STATE_SAR_ETH_LIVE, state)
    await update.message.reply_text("▶️ SAR_ETH resumed.")


async def close_sar_eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_ETH_LIVE)
    await _manual_close(update, state, STATE_SAR_ETH_LIVE, "SAR_ETH", ETH_SAR_SYMBOL, TF_ENTRY, paper=False)


# ── EMA control ───────────────────────────────────────────────────────────────

async def stop_ema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA)
    state["paused"] = True
    save_state(STATE_EMA, state)
    await update.message.reply_text("⏸ EMA paused.")


async def start_ema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA)
    state["paused"] = False
    save_state(STATE_EMA, state)
    await update.message.reply_text("▶️ EMA resumed.")


async def close_ema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA)
    await _manual_close(update, state, STATE_EMA, "EMA", SOL_SYMBOL, EMA_TF, paper=PAPER_MODE)


# ── SAR BTC control (LIVE) ────────────────────────────────────────────────────

async def stop_sar_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_BTC_LIVE)
    state["paused"] = True
    save_state(STATE_SAR_BTC_LIVE, state)
    await update.message.reply_text("⏸ SAR_BTC paused.")


async def start_sar_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_BTC_LIVE)
    state["paused"] = False
    save_state(STATE_SAR_BTC_LIVE, state)
    await update.message.reply_text("▶️ SAR_BTC resumed.")


async def close_sar_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_BTC_LIVE)
    await _manual_close(update, state, STATE_SAR_BTC_LIVE, "SAR_BTC", BTC_SAR_SYMBOL, TF_ENTRY, paper=False)


# ── SAR SOL control (LIVE) ────────────────────────────────────────────────────

async def stop_sar_sol_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_SOL_LIVE)
    state["paused"] = True
    save_state(STATE_SAR_SOL_LIVE, state)
    await update.message.reply_text("⏸ SAR_SOL paused.")


async def start_sar_sol_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_SOL_LIVE)
    state["paused"] = False
    save_state(STATE_SAR_SOL_LIVE, state)
    await update.message.reply_text("▶️ SAR_SOL resumed.")


async def close_sar_sol_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR_SOL_LIVE)
    await _manual_close(update, state, STATE_SAR_SOL_LIVE, "SAR_SOL", SOL_SAR_SYMBOL, TF_ENTRY, paper=False)


# ── EMA BTC control (LIVE) ────────────────────────────────────────────────────

async def stop_ema_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA_BTC_LIVE)
    state["paused"] = True
    save_state(STATE_EMA_BTC_LIVE, state)
    await update.message.reply_text("⏸ EMA_BTC paused.")


async def start_ema_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA_BTC_LIVE)
    state["paused"] = False
    save_state(STATE_EMA_BTC_LIVE, state)
    await update.message.reply_text("▶️ EMA_BTC resumed.")


async def close_ema_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA_BTC_LIVE)
    await _manual_close(update, state, STATE_EMA_BTC_LIVE, "EMA_BTC", BTC_EMA_SYMBOL, EMA_TF, paper=False)


# ── EMA ETH control (LIVE) ────────────────────────────────────────────────────

async def stop_ema_eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA_ETH_LIVE)
    state["paused"] = True
    save_state(STATE_EMA_ETH_LIVE, state)
    await update.message.reply_text("⏸ EMA_ETH paused.")


async def start_ema_eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA_ETH_LIVE)
    state["paused"] = False
    save_state(STATE_EMA_ETH_LIVE, state)
    await update.message.reply_text("▶️ EMA_ETH resumed.")


async def close_ema_eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA_ETH_LIVE)
    await _manual_close(update, state, STATE_EMA_ETH_LIVE, "EMA_ETH", ETH_EMA_SYMBOL, EMA_TF, paper=False)


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not TRADE_LOG.exists():
        await update.message.reply_text(f"trades.csv not found\nPath: `{TRADE_LOG}`", parse_mode="Markdown")
        return
    text = TRADE_LOG.read_text().strip()
    if not text:
        await update.message.reply_text("trades.csv is empty.")
        return
    if len(text) > 3800:
        text = text[-3800:]
    await update.message.reply_text(f"`{TRADE_LOG}`\n```\n{text}\n```", parse_mode="Markdown")


# ── Entry point ───────────────────────────────────────────────────────────────

# ── Morning report (daily 06:00 UTC = 09:00 Israel) ──────────────────────────

def _build_morning_report() -> str:
    """Build SAR/EMA daily stats block from trades.csv."""
    live  = read_trade_stats("LIVE")
    paper = read_trade_stats("PAPER")
    has_active_paper = bool(PAPER_SAR_CONFIGS or PAPER_EMA_CONFIGS)

    lines = ["⚡ <b>SAR BOT — DAILY REPORT</b>"]

    # LIVE section
    lines.append("\n<b>LIVE trades</b>")
    if live:
        for strat, s in sorted(live.items()):
            total = s["wins"] + s["losses"]
            wr    = round(s["wins"] / total * 100) if total else 0
            wr_e  = "🟢" if wr >= 60 else ("🟡" if wr >= 40 else "🔴")
            pnl_e = "✅" if s["pnl"] >= 0 else "❌"
            lines.append(f"  {strat}: {s['wins']}W/{s['losses']}L  WR {wr_e} {wr}%  {pnl_e} {s['pnl']:+.2f}$")
    else:
        lines.append("  нет сделок")

    # PAPER section — only when paper mode is active
    if has_active_paper:
        lines.append("\n<b>PAPER trades</b>")
        if paper:
            for strat, s in sorted(paper.items()):
                total = s["wins"] + s["losses"]
                wr    = round(s["wins"] / total * 100) if total else 0
                wr_e  = "🟢" if wr >= 60 else ("🟡" if wr >= 40 else "🔴")
                pnl_e = "✅" if s["pnl"] >= 0 else "❌"
                lines.append(f"  {strat}: {s['wins']}W/{s['losses']}L  WR {wr_e} {wr}%  {pnl_e} {s['pnl']:+.2f}$")
        else:
            lines.append("  нет сделок")

    # Quick analysis — only LIVE stats (paper is historical archive)
    lines.append("\n<i>💡 Анализ:</i>")
    if not live:
        lines.append("  нет данных — стратегии ещё не совершили сделок")
    else:
        found = False
        for strat, s in live.items():
            total = s["wins"] + s["losses"]
            if total == 0:
                continue
            wr = s["wins"] / total * 100
            if s["pnl"] < -30:
                lines.append(f"  • {strat}: убыток {s['pnl']:+.2f}$ — пересмотри параметры SL/TP")
                found = True
            elif wr < 35 and total >= 10:
                lines.append(f"  • {strat}: WR {wr:.0f}% — проверь условия входа")
                found = True
            elif wr >= 55 and s["pnl"] > 0:
                lines.append(f"  • {strat}: ✅ рабочий результат (WR {wr:.0f}%, PnL {s['pnl']:+.2f}$)")
                found = True
        if not found:
            lines.append("  ничего критичного")

    # All 7 strategies status
    state_labels = {
        MONITORING: "👀 мониторинг",
        PENDING_APPROVAL: "⏳ апрув",
        POSITION_OPEN: "📈 позиция",
    }
    all_states = [
        (STATE_SAR,          "SAR"),
        (STATE_SAR_ETH_LIVE, "SAR_ETH"),
        (STATE_SAR_BTC_LIVE, "SAR_BTC"),
        (STATE_SAR_SOL_LIVE, "SAR_SOL"),
        (STATE_EMA,          "EMA"),
        (STATE_EMA_BTC_LIVE, "EMA_BTC"),
        (STATE_EMA_ETH_LIVE, "EMA_ETH"),
    ]
    sar_parts = []
    ema_parts = []
    for state_path, name in all_states:
        st = load_state(state_path).get("state", "?")
        label = f"{name}→{state_labels.get(st, st)}"
        if name.startswith("SAR"):
            sar_parts.append(label)
        else:
            ema_parts.append(label)
    lines.append(f"\nSAR: {' | '.join(sar_parts)}")
    lines.append(f"EMA: {' | '.join(ema_parts)}")

    return "\n".join(lines)


async def morning_report_loop(app: Application) -> None:
    """Wait until 06:00 UTC, send report, then repeat daily.

    Uses MORNING_REPORT_TOKEN + MORNING_REPORT_CHAT_ID if set,
    so the message arrives from the same bot as the VPS unified report.
    """
    _report_chat_id_str = os.getenv("MORNING_REPORT_CHAT_ID", "")
    report_chat_id = int(_report_chat_id_str) if _report_chat_id_str else TELEGRAM_CHAT_ID
    report_token = os.getenv("MORNING_REPORT_TOKEN", "")
    log.info("[morning_report] scheduler started. chat_id=%s own_token=%s",
             report_chat_id, not bool(report_token))
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        log.info("[morning_report] next report in %.0f min", wait_sec / 60)
        await asyncio.sleep(wait_sec)

        try:
            text = _build_morning_report()
            if report_token:
                # Send via a separate bot token so message lands in the unified report chat
                from telegram import Bot as _Bot
                async with _Bot(token=report_token) as _bot:
                    await _bot.send_message(chat_id=report_chat_id, text=text, parse_mode="HTML")
            else:
                await app.bot.send_message(chat_id=report_chat_id, text=text, parse_mode="HTML")
            log.info("[morning_report] sent at %s", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.error("[morning_report] failed to send: %s", e)

        await asyncio.sleep(60)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("status",    status_command))
    app.add_handler(CommandHandler("trades",    trades_command))
    app.add_handler(CommandHandler("stop_sar",      stop_sar_command))
    app.add_handler(CommandHandler("start_sar",     start_sar_command))
    app.add_handler(CommandHandler("close_sar",     close_sar_command))
    app.add_handler(CommandHandler("stop_sar_eth",  stop_sar_eth_command))
    app.add_handler(CommandHandler("start_sar_eth", start_sar_eth_command))
    app.add_handler(CommandHandler("close_sar_eth", close_sar_eth_command))
    app.add_handler(CommandHandler("stop_sar_btc",  stop_sar_btc_command))
    app.add_handler(CommandHandler("start_sar_btc", start_sar_btc_command))
    app.add_handler(CommandHandler("close_sar_btc", close_sar_btc_command))
    app.add_handler(CommandHandler("stop_sar_sol",  stop_sar_sol_command))
    app.add_handler(CommandHandler("start_sar_sol", start_sar_sol_command))
    app.add_handler(CommandHandler("close_sar_sol", close_sar_sol_command))
    app.add_handler(CommandHandler("stop_ema",      stop_ema_command))
    app.add_handler(CommandHandler("start_ema",     start_ema_command))
    app.add_handler(CommandHandler("close_ema",     close_ema_command))
    app.add_handler(CommandHandler("stop_ema_btc",  stop_ema_btc_command))
    app.add_handler(CommandHandler("start_ema_btc", start_ema_btc_command))
    app.add_handler(CommandHandler("close_ema_btc", close_ema_btc_command))
    app.add_handler(CommandHandler("stop_ema_eth",  stop_ema_eth_command))
    app.add_handler(CommandHandler("start_ema_eth", start_ema_eth_command))
    app.add_handler(CommandHandler("close_ema_eth", close_ema_eth_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    async def on_startup(application: Application) -> None:
        await application.bot.set_my_commands([
            ("status",    "📊 Show all strategies status"),
            ("trades",    "📋 Show trades.csv"),
            ("stop_sar",      "⏸ Pause SAR DOGE"),
            ("start_sar",     "▶️ Resume SAR DOGE"),
            ("close_sar",     "🛑 Close SAR DOGE manually"),
            ("stop_sar_eth",  "⏸ Pause SAR ETH"),
            ("start_sar_eth", "▶️ Resume SAR ETH"),
            ("close_sar_eth", "🛑 Close SAR ETH manually"),
            ("stop_sar_btc",  "⏸ Pause SAR BTC"),
            ("start_sar_btc", "▶️ Resume SAR BTC"),
            ("close_sar_btc", "🛑 Close SAR BTC manually"),
            ("stop_sar_sol",  "⏸ Pause SAR SOL"),
            ("start_sar_sol", "▶️ Resume SAR SOL"),
            ("close_sar_sol", "🛑 Close SAR SOL manually"),
            ("stop_ema",      "⏸ Pause EMA SOL"),
            ("start_ema",     "▶️ Resume EMA SOL"),
            ("close_ema",     "🛑 Close EMA SOL manually"),
            ("stop_ema_btc",  "⏸ Pause EMA BTC"),
            ("start_ema_btc", "▶️ Resume EMA BTC"),
            ("close_ema_btc", "🛑 Close EMA BTC manually"),
            ("stop_ema_eth",  "⏸ Pause EMA ETH"),
            ("start_ema_eth", "▶️ Resume EMA ETH"),
            ("close_ema_eth", "🛑 Close EMA ETH manually"),
        ])
        asyncio.create_task(sar_loop(application))
        asyncio.create_task(sar_eth_loop(application))
        asyncio.create_task(sar_btc_loop(application))
        asyncio.create_task(sar_sol_loop(application))
        asyncio.create_task(ema_loop(application))
        asyncio.create_task(ema_btc_loop(application))
        asyncio.create_task(ema_eth_loop(application))
        asyncio.create_task(morning_report_loop(application))
        log.info(
            "Bot started. LIVE: SAR→%s | SAR_ETH→%s | SAR_BTC→%s | SAR_SOL→%s"
            " | EMA→%s | EMA_BTC→%s | EMA_ETH→%s",
            SYMBOL, ETH_SAR_SYMBOL, BTC_SAR_SYMBOL, SOL_SAR_SYMBOL,
            SOL_SYMBOL, BTC_EMA_SYMBOL, ETH_EMA_SYMBOL,
        )

    app.post_init = on_startup
    app.run_polling(allowed_updates=["callback_query", "message"])


if __name__ == "__main__":
    main()

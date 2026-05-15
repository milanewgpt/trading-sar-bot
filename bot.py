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
from datetime import datetime, timezone
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
STATE_SAR = _data / "state_sar.json"
STATE_EMA = _data / "state_ema.json"
TRADE_LOG  = _data / "trades.csv"

SL_COOLDOWN = 4 * 3600  # 4h cooldown after SL hit

MONITORING = "monitoring"
PENDING_APPROVAL = "pending_approval"
POSITION_OPEN = "position_open"

bingx = BingXClient(BINGX_API_KEY, BINGX_SECRET_KEY)

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

def _sar_signal_text(sig: dict) -> str:
    d = sig["direction"].upper()
    emoji = "🟢" if sig["direction"] == "long" else "🔴"
    return (
        f"{emoji} *DOGEUSDT — {d}*\n"
        f"_Strategy: SAR + SMA 5m/15m_\n\n"
        f"Entry: `{sig['entry']:.6f}`\n"
        f"Stop Loss: `{sig['stop']:.6f}`\n"
        f"Take Profit: `{sig['take']:.6f}`\n\n"
        f"Margin: ${MARGIN}  |  Leverage: x{LEVERAGE}\n"
        f"Position size: ${POSITION_SIZE}\n"
        f"RR: 1:2"
    )


def _ema_signal_text(sig: dict) -> str:
    d = sig["direction"].upper()
    emoji = "🟢" if sig["direction"] == "long" else "🔴"
    det = sig.get("details", {})
    return (
        f"{emoji} *SOLUSDT — {d}*\n"
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
    quantity = max(1, round(POSITION_SIZE / entry))

    if paper:
        log.info("[PAPER] Virtual %s %s @ %.5f qty=%d", sig["direction"], symbol, entry, quantity)
    else:
        log.info("[LIVE] Opening %s %s @ %.5f qty=%d", sig["direction"], symbol, entry, quantity)
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
) -> None:
    pos = state.get("position", {})
    direction = pos.get("direction", "").upper()
    entry = float(pos.get("entry", 0))
    sl = float(pos.get("stop", 0))
    tp = float(pos.get("take", 0))

    result = "WIN" if close_reason == "TP" else "LOSS"
    qty = float(pos.get("quantity", 1))
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

    mode_label = "📝 PAPER" if paper else "📋 LIVE"
    result_emoji = "✅" if result == "WIN" else "❌"
    sym_label = symbol.replace("-", "")
    await notify(
        app,
        f"{mode_label} | {result_emoji} *{result}* — {direction} {sym_label}\n"
        f"_Strategy: {strategy_name}_\n"
        f"Entry: `{entry:.5f}`  Close: *{close_reason}*  PnL: `{pnl:+.2f}$`",
    )

    state["state"] = MONITORING
    state["position"] = None
    state["signal"] = None
    if close_reason == "SL" and sl_cooldown > 0:
        state["cooldown_until"] = time.time() + sl_cooldown
        log.info("[%s] SL cooldown active for %dh", strategy_name, sl_cooldown // 3600)
    save_state(state_path, state)


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


async def sar_tick(app: Application, state: dict) -> None:
    if state.get("paused"):
        return
    s = state["state"]

    if s == MONITORING:
        candles_5m = await bingx.get_klines(SYMBOL, TF_ENTRY, CANDLES_LIMIT)
        if len(candles_5m) < 3:
            return

        closed_5m = candles_5m[:-1]
        last_ts = closed_5m[-1]["time"]

        if last_ts == state.get("last_candle_ts"):
            return

        log.info("[SAR] new candle ts=%s close=%.6f", last_ts, float(closed_5m[-1]["close"]))
        state["last_candle_ts"] = last_ts
        save_state(STATE_SAR, state)  # persist so dedup works across ticks

        candles_15m = await bingx.get_klines(SYMBOL, TF_CONFIRM, CANDLES_LIMIT)
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

        log.info("[SAR] signal %s entry=%.6f sl=%.6f tp=%.6f", direction, entry, sar_val, take)
        msg_id = await send_signal(app, _sar_signal_text(sig), "sar")

        state["state"] = PENDING_APPROVAL
        state["signal"] = sig
        state["pending_msg_id"] = msg_id
        save_state(STATE_SAR, state)

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        if SAR_PAPER_MODE:
            candles = await bingx.get_klines(SYMBOL, TF_ENTRY, 10)
            close_reason = paper_check_closed(pos, candles[:-1])
        else:
            if await is_position_closed(SYMBOL):
                candles = await bingx.get_klines(SYMBOL, TF_ENTRY, 10)
                close_reason = paper_check_closed(pos, candles[:-1]) or "CLOSED"
            else:
                close_reason = None

        if close_reason:
            await on_position_closed(app, state, STATE_SAR, close_reason, "SAR", SYMBOL, paper=SAR_PAPER_MODE)


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

        log.info("[EMA] signal %s entry=%.4f sl=%.4f tp=%.4f", direction, entry, stop, take)
        msg_id = await send_signal(app, _ema_signal_text(sig), "ema")

        state["state"] = PENDING_APPROVAL
        state["signal"] = sig
        state["pending_msg_id"] = msg_id
        save_state(STATE_EMA, state)

    elif s == POSITION_OPEN:
        pos = state.get("position", {})
        if PAPER_MODE:
            candles = await bingx.get_klines(SOL_SYMBOL, EMA_TF, 10)
            close_reason = paper_check_closed(pos, candles[:-1])
        else:
            if await is_position_closed(SOL_SYMBOL):
                candles = await bingx.get_klines(SOL_SYMBOL, EMA_TF, 10)
                close_reason = paper_check_closed(pos, candles[:-1]) or "CLOSED"
            else:
                close_reason = None

        if close_reason:
            await on_position_closed(app, state, STATE_EMA, close_reason, "EMA", SOL_SYMBOL, paper=PAPER_MODE, sl_cooldown=SL_COOLDOWN)


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
    elif prefix == "ema":
        await _handle_approval(query, context, action, STATE_EMA, SOL_SYMBOL, "EMA", paper=PAPER_MODE)


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
            state["state"] = POSITION_OPEN
            state["position"] = position
            state["signal"] = None
            save_state(state_path, state)

            mode_tag = "📝 PAPER" if paper else "✅ LIVE"
            sym_label = symbol.replace("-", "")
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"{mode_tag} | *Position opened*\n"
                    f"_Strategy: {strategy_name}_\n\n"
                    f"*{sig['direction'].upper()}* {sym_label}\n"
                    f"Entry: `{sig['entry']:.5f}`\n"
                    f"SL: `{sig['stop']:.5f}`\n"
                    f"TP: `{sig['take']:.5f}`\n"
                    f"Qty: `{position['quantity']}` | Margin: ${MARGIN} | x{LEVERAGE}"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("[%s] trade execution failed: %s", strategy_name, e, exc_info=True)
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"❌ Failed to open {strategy_name} position: {e}",
            )
            state["state"] = MONITORING
            state["signal"] = None
            save_state(state_path, state)

    elif action == "skip":
        await query.edit_message_text(f"⏭ {strategy_name} signal skipped.")
        state["state"] = MONITORING
        state["signal"] = None
        save_state(state_path, state)
        log.info("[%s] signal skipped by user.", strategy_name)



# ── /status command ───────────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📊 *Bot Status*\n"]
    sar_mode = "📝 PAPER" if SAR_PAPER_MODE else "🔴 LIVE"
    ema_mode = "📝 PAPER" if PAPER_MODE else "🔴 LIVE"
    lines.append(f"SAR: {sar_mode}  |  EMA: {ema_mode}\n")

    # ── SAR + EMA ─────────────────────────────────────────────────────────────
    strategies = [
        ("SAR", STATE_SAR, SYMBOL, TF_ENTRY),
        ("EMA", STATE_EMA, SOL_SYMBOL, EMA_TF),
    ]

    for name, state_path, symbol, tf in strategies:
        state = load_state(state_path)
        s = state["state"]
        sym_label = symbol.replace("-", "")  # e.g. SOL-USDT → SOLUSDT

        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"*{name}* — {sym_label} ({tf})")

        if s == MONITORING:
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
    lines.append("📈 *LIVE статистика*")
    live = read_trade_stats("LIVE")
    if live:
        for strat, s in live.items():
            total = s["wins"] + s["losses"]
            wr = round(s["wins"] / total * 100) if total else 0
            pnl_emoji = "✅" if s["pnl"] >= 0 else "❌"
            lines.append(f"{strat}: {s['wins']}W / {s['losses']}L  WR {wr}%  {pnl_emoji} {s['pnl']:+.2f}$")
    else:
        lines.append("Нет сделок")

    lines.append("\n📋 *PAPER история*")
    paper = read_trade_stats("PAPER")
    if paper:
        for strat, s in paper.items():
            total = s["wins"] + s["losses"]
            wr = round(s["wins"] / total * 100) if total else 0
            pnl_emoji = "✅" if s["pnl"] >= 0 else "❌"
            lines.append(f"{strat}: {s['wins']}W / {s['losses']}L  WR {wr}%  {pnl_emoji} {s['pnl']:+.2f}$")
    else:
        lines.append("Нет сделок")

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
        await update.message.reply_text(f"⚠️ {strategy_name}: нет открытой позиции.")
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
            await update.message.reply_text(f"❌ BingX ошибка: {e}")
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
        f"🛑 *{strategy_name}* закрыта вручную\n"
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
    await update.message.reply_text("⏸ SAR приостановлена.")


async def start_sar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR)
    state["paused"] = False
    save_state(STATE_SAR, state)
    await update.message.reply_text("▶️ SAR запущена.")


async def close_sar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_SAR)
    await _manual_close(update, state, STATE_SAR, "SAR", SYMBOL, TF_ENTRY, paper=SAR_PAPER_MODE)


# ── EMA control ───────────────────────────────────────────────────────────────

async def stop_ema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA)
    state["paused"] = True
    save_state(STATE_EMA, state)
    await update.message.reply_text("⏸ EMA приостановлена.")


async def start_ema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA)
    state["paused"] = False
    save_state(STATE_EMA, state)
    await update.message.reply_text("▶️ EMA запущена.")


async def close_ema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state(STATE_EMA)
    await _manual_close(update, state, STATE_EMA, "EMA", SOL_SYMBOL, EMA_TF, paper=PAPER_MODE)


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not TRADE_LOG.exists():
        await update.message.reply_text("trades.csv не найден.")
        return
    text = TRADE_LOG.read_text()
    if len(text) > 4000:
        text = text[-4000:]
    await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("status",    status_command))
    app.add_handler(CommandHandler("trades",    trades_command))
    app.add_handler(CommandHandler("stop_sar",  stop_sar_command))
    app.add_handler(CommandHandler("start_sar", start_sar_command))
    app.add_handler(CommandHandler("close_sar", close_sar_command))
    app.add_handler(CommandHandler("stop_ema",  stop_ema_command))
    app.add_handler(CommandHandler("start_ema", start_ema_command))
    app.add_handler(CommandHandler("close_ema", close_ema_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    async def on_startup(application: Application) -> None:
        await application.bot.set_my_commands([
            ("status",    "📊 Статус всех стратегий"),
            ("stop_sar",  "⏸ Остановить SAR"),
            ("start_sar", "▶️ Запустить SAR"),
            ("close_sar", "🛑 Закрыть позицию SAR"),
            ("stop_ema",  "⏸ Остановить EMA"),
            ("start_ema", "▶️ Запустить EMA"),
            ("close_ema", "🛑 Закрыть позицию EMA"),
        ])
        asyncio.create_task(sar_loop(application))
        asyncio.create_task(ema_loop(application))
        log.info("Bot started. SAR→%s | EMA→%s | PAPER=%s", SYMBOL, SOL_SYMBOL, PAPER_MODE)

    app.post_init = on_startup
    app.run_polling(allowed_updates=["callback_query", "message"])


if __name__ == "__main__":
    main()

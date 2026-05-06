from indicators import calc_ema

EMA_TREND = 100
PULLBACK_MIN = 2      # minimum consecutive pullback candles required
PULLBACK_LOOKBACK = 6 # max candles to scan backwards for pullback
MAX_STOP_PCT = 0.05
MIN_STOP_PCT = 0.012
SL_BUFFER = 1.003     # 0.3% buffer behind local low/high


def _parse_candles(raw: list) -> tuple:
    opens   = [float(c["open"])          for c in raw]
    highs   = [float(c["high"])          for c in raw]
    lows    = [float(c["low"])           for c in raw]
    closes  = [float(c["close"])         for c in raw]
    volumes = [float(c.get("volume", 0)) for c in raw]
    return opens, highs, lows, closes, volumes


def _count_pullback(opens: list, closes: list, signal_idx: int, direction: str) -> int:
    """
    Count consecutive candles immediately before signal_idx going against the trend.
    direction="long"  → count red candles (close < open)
    direction="short" → count green candles (close > open)
    """
    count = 0
    for i in range(signal_idx - 1, max(signal_idx - PULLBACK_LOOKBACK - 1, -1), -1):
        is_pullback = closes[i] < opens[i] if direction == "long" else closes[i] > opens[i]
        if is_pullback:
            count += 1
        else:
            break
    return count


def _volume_ok(volumes: list, signal_idx: int, pullback_count: int) -> bool:
    """Entry candle volume >= 80% of average pullback candle volume."""
    if pullback_count == 0:
        return False
    pullback_vols = volumes[signal_idx - pullback_count : signal_idx]
    total = sum(pullback_vols)
    if total == 0:
        return True  # no volume data — skip check
    avg = total / len(pullback_vols)
    return volumes[signal_idx] >= avg * 0.8


def check_ema_signal(candles_1h: list) -> tuple[str | None, float | None, float | None]:
    """
    Returns (direction, entry, stop) or (None, None, None).
    direction: "long" | "short"
    stop: below/above local pullback low/high
    TP is calculated by caller as entry ± 2 * risk.
    """
    if len(candles_1h) < EMA_TREND + 10:
        return None, None, None

    o, h, l, c, v = _parse_candles(candles_1h)
    ema100 = calc_ema(c, EMA_TREND)

    idx = len(c) - 1
    e100 = ema100[idx]
    e100p = ema100[idx - 1]

    if e100 is None or e100p is None:
        return None, None, None

    price = c[idx]

    # ── LONG ─────────────────────────────────────────────────────────────────
    if price > e100 and e100 > e100p:
        is_green = c[idx] > o[idx]
        if not is_green:
            return None, None, None

        pullback = _count_pullback(o, c, idx, "long")
        if pullback < PULLBACK_MIN:
            return None, None, None

        if not _volume_ok(v, idx, pullback):
            return None, None, None

        stop = min(l[idx - pullback : idx]) / SL_BUFFER
        risk = price - stop
        if risk <= 0 or risk / price > MAX_STOP_PCT or risk / price < MIN_STOP_PCT:
            return None, None, None

        return "long", price, stop

    # ── SHORT ────────────────────────────────────────────────────────────────
    if price < e100 and e100 < e100p:
        is_red = c[idx] < o[idx]
        if not is_red:
            return None, None, None

        pullback = _count_pullback(o, c, idx, "short")
        if pullback < PULLBACK_MIN:
            return None, None, None

        if not _volume_ok(v, idx, pullback):
            return None, None, None

        stop = max(h[idx - pullback : idx]) * SL_BUFFER
        risk = stop - price
        if risk <= 0 or risk / price > MAX_STOP_PCT or risk / price < MIN_STOP_PCT:
            return None, None, None

        return "short", price, stop

    return None, None, None


def signal_details(candles_1h: list) -> dict:
    """Returns human-readable indicator state for the Telegram message."""
    o, h, l, c, v = _parse_candles(candles_1h)
    ema100 = calc_ema(c, EMA_TREND)
    idx = len(c) - 1
    e100 = ema100[idx]
    e100p = ema100[idx - 1]

    trend = "UP" if (e100 and e100p and e100 > e100p) else "DOWN"
    candle = "green" if c[idx] > o[idx] else "red"
    pullback_long = _count_pullback(o, c, idx, "long")
    pullback_short = _count_pullback(o, c, idx, "short")
    pullback_count = pullback_long if candle == "green" else pullback_short

    avg_pb_vol = 0.0
    if pullback_count > 0:
        pb_vols = v[idx - pullback_count : idx]
        avg_pb_vol = sum(pb_vols) / len(pb_vols) if pb_vols else 0

    vol_ratio = round(v[idx] / avg_pb_vol, 2) if avg_pb_vol > 0 else "n/a"

    return {
        "trend": trend,
        "candle": candle,
        "pullback": pullback_count,
        "vol_ratio": vol_ratio,
    }

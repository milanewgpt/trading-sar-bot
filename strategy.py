from indicators import calc_sma, calc_parabolic_sar
from config import SAR_STEP, SAR_MAX, SMA_FAST, SMA_SLOW

MIN_STOP_PCT = 0.008  # skip signals where SAR is < 0.8% from entry (pure noise)


def _parse_candles(raw: list) -> tuple[list, list, list, list]:
    """BingX kline format: dict with keys open/high/low/close/time"""
    highs = [float(c["high"]) for c in raw]
    lows = [float(c["low"]) for c in raw]
    closes = [float(c["close"]) for c in raw]
    opens = [float(c["open"]) for c in raw]
    return opens, highs, lows, closes


def check_signal(
    candles_5m: list,
    candles_15m: list,
) -> tuple[str | None, float | None, float | None]:
    """
    Both lists must contain only CLOSED candles (caller strips the forming one).
    Returns (direction, entry_price, sar_value) or (None, None, None).
    direction: "long" | "short"
    entry_price: close of the signal candle
    sar_value:  SAR value on the signal candle (used for SL)
    """
    if len(candles_5m) < SMA_SLOW + 3 or len(candles_15m) < SMA_SLOW + 2:
        return None, None, None

    _, h5, l5, c5 = _parse_candles(candles_5m)
    _, h15, l15, c15 = _parse_candles(candles_15m)

    sar5, bull5 = calc_parabolic_sar(h5, l5, SAR_STEP, SAR_MAX)
    sma50_5 = calc_sma(c5, SMA_FAST)
    sma100_5 = calc_sma(c5, SMA_SLOW)

    sma50_15 = calc_sma(c15, SMA_FAST)
    sma100_15 = calc_sma(c15, SMA_SLOW)

    # Signal candle = last element (-1); previous candle = -2
    prev_bull_5m = bull5[-2]
    curr_bull_5m = bull5[-1]
    curr_sar_5m = sar5[-1]

    s50_5 = sma50_5[-1]
    s100_5 = sma100_5[-1]
    s50_15 = sma50_15[-1]
    s100_15 = sma100_15[-1]

    if any(v is None for v in [s50_5, s100_5, s50_15, s100_15]):
        return None, None, None

    entry = c5[-1]

    # Long: SAR flipped bullish on 5m + SMA aligned on both timeframes
    if (
        not prev_bull_5m
        and curr_bull_5m
        and s50_5 > s100_5
        and s50_15 > s100_15
    ):
        if (entry - curr_sar_5m) / entry < MIN_STOP_PCT:
            return None, None, None
        return "long", entry, curr_sar_5m

    # Short: SAR flipped bearish on 5m + SMA aligned on both timeframes
    if (
        prev_bull_5m
        and not curr_bull_5m
        and s50_5 < s100_5
        and s50_15 < s100_15
    ):
        if (curr_sar_5m - entry) / entry < MIN_STOP_PCT:
            return None, None, None
        return "short", entry, curr_sar_5m

    return None, None, None

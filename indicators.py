def calc_rsi(closes: list[float], period: int = 14) -> list[float]:
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result

    def _rsi(ag: float, al: float) -> float:
        return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = _rsi(avg_gain, avg_loss)

    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        result[i] = _rsi(avg_gain, avg_loss)

    return result


def calc_sma_of(values: list, period: int) -> list:
    """SMA over a list that may contain leading Nones (e.g. RSI output)."""
    n = len(values)
    result = [None] * n
    for i in range(n):
        if values[i] is None:
            continue
        window = [v for v in values[max(0, i - period + 1):i + 1] if v is not None]
        if len(window) == period:
            result[i] = sum(window) / period
    return result


def calc_ema(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return [None] * len(closes)
    k = 2.0 / (period + 1)
    result = [None] * len(closes)
    result[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1 - k)
    return result


def calc_sma(closes: list[float], period: int) -> list[float]:
    result = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1 : i + 1]) / period
    return result


def calc_parabolic_sar(
    highs: list[float],
    lows: list[float],
    step: float = 0.02,
    max_step: float = 0.2,
) -> tuple[list[float], list[bool]]:
    """
    Returns (sar_values, is_bullish).
    is_bullish[i] == True  → SAR is below price (uptrend)
    is_bullish[i] == False → SAR is above price (downtrend)
    """
    n = len(highs)
    sar = [0.0] * n
    is_bull = [True] * n

    # Bootstrap using first two candles
    if n < 2:
        return sar, is_bull

    bull = highs[1] > highs[0]
    ep = highs[0] if bull else lows[0]
    af = step
    sar[0] = lows[0] if bull else highs[0]

    for i in range(1, n):
        if bull:
            new_sar = sar[i - 1] + af * (ep - sar[i - 1])
            # SAR cannot exceed the two previous lows
            new_sar = min(new_sar, lows[i - 1])
            if i >= 2:
                new_sar = min(new_sar, lows[i - 2])

            if lows[i] < new_sar:
                # Reversal to downtrend
                bull = False
                sar[i] = ep
                ep = lows[i]
                af = step
                is_bull[i] = False
            else:
                sar[i] = new_sar
                is_bull[i] = True
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_step)
        else:
            new_sar = sar[i - 1] + af * (ep - sar[i - 1])
            # SAR cannot go below the two previous highs
            new_sar = max(new_sar, highs[i - 1])
            if i >= 2:
                new_sar = max(new_sar, highs[i - 2])

            if highs[i] > new_sar:
                # Reversal to uptrend
                bull = True
                sar[i] = ep
                ep = highs[i]
                af = step
                is_bull[i] = True
            else:
                sar[i] = new_sar
                is_bull[i] = False
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_step)

    return sar, is_bull

import hashlib
import hmac
import time

import aiohttp

BASE_URL = "https://open-api.bingx.com"


class BingXAPIError(Exception):
    pass


class BingXClient:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key

    def _build_signed_url(self, path: str, params: dict) -> str:
        sorted_keys = sorted(params)
        qs = "&".join(f"{k}={params[k]}" for k in sorted_keys)
        qs += f"&timestamp={int(time.time() * 1000)}"
        sig = hmac.new(
            self.secret_key.encode(),
            qs.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{BASE_URL}{path}?{qs}&signature={sig}"

    def _headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = self._build_signed_url(path, params or {})
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers()) as resp:
                return await resp.json()

    async def _post(self, path: str, params: dict | None = None) -> dict:
        url = self._build_signed_url(path, params or {})
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._headers(), data={}) as resp:
                return await resp.json()

    @staticmethod
    def _ensure_success(response: dict, action: str) -> dict:
        code = response.get("code")
        if code not in (0, "0", None):
            msg = response.get("msg") or response.get("message") or "unknown BingX error"
            raise BingXAPIError(f"{action} failed: code={code} msg={msg}")
        return response

    # ── Market data ──────────────────────────────────────────────────────────

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        data = await self._get(
            "/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        return data.get("data", [])

    # ── Account / positions ───────────────────────────────────────────────────

    async def get_positions(self, symbol: str) -> list:
        data = await self._get(
            "/openApi/swap/v2/user/positions", {"symbol": symbol}
        )
        self._ensure_success(data, "get_positions")
        return data.get("data", [])

    async def get_open_orders(self, symbol: str) -> list:
        data = await self._get(
            "/openApi/swap/v2/trade/openOrders", {"symbol": symbol}
        )
        return data.get("data", {}).get("orders", [])

    async def get_history_orders(self, symbol: str, start_ts: int, limit: int = 10) -> list:
        data = await self._get(
            "/openApi/swap/v2/trade/historyOrders",
            {"symbol": symbol, "startTime": start_ts, "limit": limit},
        )
        return data.get("data", {}).get("orders", [])

    async def get_fill_history(self, symbol: str, start_ts: int, limit: int = 50) -> list:
        data = await self._get(
            "/openApi/swap/v3/trade/fillHistory",
            {"symbol": symbol, "startTime": start_ts, "limit": limit},
        )
        return data.get("data", []) or []

    # ── Trading ───────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        # Set leverage for both sides
        for side in ("LONG", "SHORT"):
            resp = await self._post(
                "/openApi/swap/v2/trade/leverage",
                {"symbol": symbol, "side": side, "leverage": leverage},
            )
            self._ensure_success(resp, f"set_leverage[{side}]")

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
    ) -> dict:
        response = await self._post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": symbol,
                "side": side,
                "positionSide": position_side,
                "type": "MARKET",
                "quantity": quantity,
            },
        )
        return self._ensure_success(response, "place_market_order")

    async def place_stop_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        stop_price: float,
        order_type: str,  # STOP_MARKET | TAKE_PROFIT_MARKET
    ) -> dict:
        response = await self._post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": symbol,
                "side": side,
                "positionSide": position_side,
                "type": order_type,
                "quantity": quantity,
                "stopPrice": round(stop_price, 6),
                "closePosition": "true",
            },
        )
        return self._ensure_success(response, f"place_stop_order[{order_type}]")

    async def open_position(
        self,
        symbol: str,
        direction: str,
        quantity: float,
        sl_price: float,
        tp_price: float,
    ) -> dict:
        """
        direction: "long" | "short"
        Opens market position + attaches SL and TP orders.
        Returns main order response.
        """
        side = "BUY" if direction == "long" else "SELL"
        close_side = "SELL" if direction == "long" else "BUY"
        pos_side = "LONG" if direction == "long" else "SHORT"

        order = await self.place_market_order(symbol, side, pos_side, quantity)

        # SL and TP failures must not cancel the trade — position is already open.
        # BingX rejects TP if price moved past it between fill and order placement.
        try:
            await self.place_stop_order(
                symbol, close_side, pos_side, quantity, sl_price, "STOP_MARKET"
            )
        except BingXAPIError as e:
            import logging
            logging.getLogger(__name__).warning("SL order failed (position open without SL): %s", e)

        try:
            await self.place_stop_order(
                symbol, close_side, pos_side, quantity, tp_price, "TAKE_PROFIT_MARKET"
            )
        except BingXAPIError as e:
            import logging
            logging.getLogger(__name__).warning("TP order failed (position open without TP): %s", e)

        return order

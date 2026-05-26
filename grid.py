import asyncio
import json
import os
import threading
import time
import traceback
import urllib.parse
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

import requests
import telegram
import websockets
from dotenv import load_dotenv
from eth_account import Account

try:
    from eth_account.messages import encode_typed_data
except ImportError:
    encode_typed_data = None

try:
    from eth_account.messages import encode_structured_data
except ImportError:
    encode_structured_data = None


load_dotenv()


# 交易参数统一放在代码中，.env 只保存 address/private_key/bot_token/chat_id 等密钥。
# 修改参数后需要重启进程或 systemd 服务才会生效。

# SPOT 使用现货余额；PERP 使用合约账户保证金。
MARKET_TYPE = "PERP"

# 默认交易对为 BASE_ASSET + QUOTE_ASSET，例如 LINKUSDT。
# 如果实际交易对符号不是这个拼接规则，再手动填写 SYMBOL。
BASE_ASSET = "LINK"
QUOTE_ASSET = "USDT"
SYMBOL = ""

# 距离参考价最近一档的买单/卖单基础数量。
INITIAL_BUY_QUANTITY = Decimal("2")
INITIAL_SELL_QUANTITY = Decimal("2")

# 更深网格的数量递增。例：NUM_ORDERS=3、INITIAL_BUY_QUANTITY=2、
# BUY_INCREMENT=0.1 时，三档买单数量为 2、2.1、2.2。
BUY_INCREMENT = Decimal("0.1")
SELL_INCREMENT = Decimal("0")

# 相邻网格价格间距。启动时会校验它不能小于交易所 tick size。
PRICE_STEP = Decimal("0.5")

# 每侧挂单数量。1 表示参考价下方 1 个买单，上方 1 个卖单。
NUM_ORDERS = 1

# True 只打印订单，不真实撤单/挂单；确认无误后再改为 False。
DRY_RUN = True

# REST API 最大重试次数，以及 WebSocket 断线后最大重连等待秒数。
API_MAX_RETRIES = 5
RECONNECT_BACKOFF_CAP = 600

# 仅 PERP 使用。LEVERAGE 只用于买单本地保证金估算，真实杠杆需在账户中提前设置。
# HEDGE_MODE=True 时买单和卖单都操作 LONG 仓位；卖单只减多，不开空。
LEVERAGE = Decimal("1")
HEDGE_MODE = False

SPOT_REST_BASE_URL = "https://sapi.asterdex.com"
SPOT_WS_BASE_URL = "wss://sstream.asterdex.com"
PERP_REST_BASE_URL = "https://fapi.asterdex.com"
PERP_WS_BASE_URL = "wss://fstream.asterdex.com"


def secret(name):
    return os.getenv(name) or os.getenv(name.upper(), "")


@dataclass(frozen=True)
class GridConfig:
    market_type: str = MARKET_TYPE.upper()
    base_asset: str = BASE_ASSET.upper()
    quote_asset: str = QUOTE_ASSET.upper()
    symbol: str = SYMBOL
    initial_buy_quantity: Decimal = INITIAL_BUY_QUANTITY
    buy_increment: Decimal = BUY_INCREMENT
    initial_sell_quantity: Decimal = INITIAL_SELL_QUANTITY
    sell_increment: Decimal = SELL_INCREMENT
    price_step: Decimal = PRICE_STEP
    num_orders: int = NUM_ORDERS
    dry_run: bool = DRY_RUN
    api_max_retries: int = API_MAX_RETRIES
    recv_backoff_cap: int = RECONNECT_BACKOFF_CAP
    leverage: Decimal = LEVERAGE
    hedge_mode: bool = HEDGE_MODE

    @property
    def trade_symbol(self):
        return self.symbol.upper() if self.symbol else f"{self.base_asset}{self.quote_asset}"


class ApiError(RuntimeError):
    pass


class ExchangeClient:
    def __init__(self, config):
        self.config = config
        self.user = secret("address")
        self.private_key = secret("private_key")
        self.signer = Account.from_key(self.private_key).address if self.private_key else ""
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "GridBot/1.0"}
        )
        self._nonce_lock = threading.Lock()
        self._last_ms = 0
        self._nonce_seq = 0

        if config.market_type == "SPOT":
            self.rest_base = SPOT_REST_BASE_URL
            self.ws_base = SPOT_WS_BASE_URL
            self.path_prefix = "/api/v3"
        elif config.market_type == "PERP":
            self.rest_base = PERP_REST_BASE_URL
            self.ws_base = PERP_WS_BASE_URL
            self.path_prefix = "/fapi/v3"
        else:
            raise ValueError("MARKET_TYPE must be SPOT or PERP.")

    def _nonce(self):
        with self._nonce_lock:
            now_ms = int(time.time() * 1000)
            if now_ms == self._last_ms:
                self._nonce_seq += 1
            else:
                self._last_ms = now_ms
                self._nonce_seq = 0
            return now_ms * 1000 + self._nonce_seq

    def _sign(self, params):
        if not self.private_key:
            raise ApiError("Missing private_key.")
        query = urllib.parse.urlencode(params)
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Message": [{"name": "msg", "type": "string"}],
            },
            "primaryType": "Message",
            "domain": {
                "name": "AsterSignTransaction",
                "version": "1",
                "chainId": 1666,
                "verifyingContract": "0x0000000000000000000000000000000000000000",
            },
            "message": {"msg": query},
        }
        if encode_typed_data:
            message = encode_typed_data(full_message=typed_data)
        elif encode_structured_data:
            message = encode_structured_data(primitive=typed_data)
        else:
            raise ApiError("eth-account does not provide EIP-712 typed-data signing.")
        return Account.sign_message(message, private_key=self.private_key).signature.hex()

    def _signed_params(self, params=None):
        if not self.user:
            raise ApiError("Missing address.")
        if not self.signer:
            raise ApiError("Unable to derive signer address from private_key.")
        signed = dict(params or {})
        signed["user"] = self.user
        signed["signer"] = self.signer
        signed["nonce"] = str(self._nonce())
        signed["signature"] = self._sign(signed)
        return signed

    def request(self, method, path, params=None, signed=False):
        method = method.upper()
        url = self.rest_base + self.path_prefix + path
        payload = self._signed_params(params) if signed else dict(params or {})

        for attempt in range(self.config.api_max_retries):
            try:
                if method == "GET":
                    response = self.session.get(url, params=payload, timeout=10)
                elif method == "POST":
                    response = self.session.post(url, data=payload, timeout=10)
                elif method == "PUT":
                    response = self.session.put(url, data=payload, timeout=10)
                elif method == "DELETE":
                    response = self.session.delete(url, data=payload, timeout=10)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                if response.status_code in {418, 429}:
                    retry_after = int(response.headers.get("Retry-After", "1"))
                    raise ApiError(f"Rate limited: HTTP {response.status_code}, retry after {retry_after}s")

                data = response.json() if response.text else {}
                if response.status_code >= 400 or (isinstance(data, dict) and "code" in data and int(data["code"]) < 0):
                    raise ApiError(f"{method} {path} failed: HTTP {response.status_code} {data}")
                return data
            except Exception:
                if attempt == self.config.api_max_retries - 1:
                    raise
                time.sleep(min(2**attempt, 30))

    def exchange_info(self):
        return self.request("GET", "/exchangeInfo")

    def latest_price(self, symbol):
        return Decimal(str(self.request("GET", "/ticker/price", {"symbol": symbol})["price"]))

    def recent_trades(self, symbol, limit=1):
        return self.request("GET", "/trades", {"symbol": symbol, "limit": limit})

    def account_trades(self, symbol, limit=1):
        return self.request("GET", "/userTrades", {"symbol": symbol, "limit": limit}, signed=True)

    def account(self):
        if self.config.market_type == "SPOT":
            return self.request("GET", "/account", signed=True)
        return self.request("GET", "/accountWithJoinMargin", signed=True)

    def open_orders(self, symbol):
        return self.request("GET", "/openOrders", {"symbol": symbol}, signed=True)

    def cancel_all_orders(self, symbol):
        return self.request("DELETE", "/allOpenOrders", {"symbol": symbol}, signed=True)

    def place_limit_order(self, symbol, side, price, quantity):
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price,
        }
        if self.config.market_type == "PERP":
            if self.config.hedge_mode:
                params["positionSide"] = "LONG"
            elif side == "SELL":
                params["reduceOnly"] = "true"
        return self.request("POST", "/order", params, signed=True)

    def start_listen_key(self):
        return self.request("POST", "/listenKey", signed=True)["listenKey"]

    def keepalive_listen_key(self, listen_key):
        params = {"listenKey": listen_key} if self.config.market_type == "SPOT" else None
        return self.request("PUT", "/listenKey", params, signed=True)

    def close_listen_key(self, listen_key):
        params = {"listenKey": listen_key} if self.config.market_type == "SPOT" else None
        return self.request("DELETE", "/listenKey", params, signed=True)


class GridStrategy:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.symbol = config.trade_symbol
        self.price_tick = None
        self.qty_step = None
        self.min_qty = None
        self.min_notional = Decimal("0")
        self.cancelled_order_ids = set()
        self.filled_order_ids = set()
        self.order_lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task_queue = asyncio.Queue()
        self.bot = self._make_bot()

    def _make_bot(self):
        token = secret("bot_token")
        if not token:
            return None
        return telegram.Bot(token)

    def send_message(self, message):
        print(message)
        chat_id = secret("chat_id")
        if self.config.dry_run or not self.bot or not chat_id:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.bot.send_message(chat_id=chat_id, text=message), self.loop)
        except Exception as exc:
            print(f"发送Telegram消息失败: {exc}")

    def load_symbol_rules(self):
        exchange_info = self.client.exchange_info()
        symbol_info = next((item for item in exchange_info["symbols"] if item["symbol"] == self.symbol), None)
        if not symbol_info or symbol_info.get("status") != "TRADING":
            raise ValueError(f"Invalid or non-trading symbol: {self.symbol}")

        filters = {item["filterType"]: item for item in symbol_info["filters"]}
        self.price_tick = Decimal(filters["PRICE_FILTER"]["tickSize"])
        self.qty_step = Decimal(filters["LOT_SIZE"]["stepSize"])
        self.min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
        if "MIN_NOTIONAL" in filters:
            self.min_notional = Decimal(filters["MIN_NOTIONAL"].get("notional") or filters["MIN_NOTIONAL"].get("minNotional"))
        if self.config.price_step < self.price_tick:
            raise ValueError(f"PRICE_STEP must be >= tickSize {self.price_tick}.")

        for value in [
            self.config.initial_buy_quantity,
            self.config.buy_increment,
            self.config.initial_sell_quantity,
            self.config.sell_increment,
        ]:
            if Decimal(value) > 0 and Decimal(value) < self.min_qty:
                raise ValueError(f"Quantity config {value} is smaller than minQty {self.min_qty}.")

    def floor_to_step(self, value, step):
        value = Decimal(str(value))
        step = Decimal(str(step))
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def format_decimal(self, value):
        return format(value.normalize(), "f")

    def format_price(self, price):
        grid_price = self.floor_to_step(price, self.config.price_step)
        return self.format_decimal(self.floor_to_step(grid_price, self.price_tick))

    def format_qty(self, quantity):
        return self.format_decimal(self.floor_to_step(quantity, self.qty_step))

    def get_balances(self):
        account = self.client.account()
        if self.config.market_type == "SPOT":
            balances = {item["asset"]: item for item in account.get("balances", [])}
            base = balances.get(self.config.base_asset, {"free": "0", "locked": "0"})
            quote = balances.get(self.config.quote_asset, {"free": "0", "locked": "0"})
            return {
                "base_total": Decimal(base["free"]) + Decimal(base["locked"]),
                "quote_total": Decimal(quote["free"]) + Decimal(quote["locked"]),
                "available_margin": Decimal("0"),
                "leverage": self.config.leverage,
            }

        leverage = self.config.leverage
        long_position = Decimal("0")
        for position in account.get("positions", []):
            if position.get("symbol") != self.symbol:
                continue
            if Decimal(position.get("leverage", "0")) > 0:
                leverage = Decimal(position["leverage"])
            position_side = position.get("positionSide", "BOTH")
            position_amt = Decimal(position.get("positionAmt", "0"))
            if position_side != "SHORT" and position_amt > 0:
                long_position += position_amt
        return {
            "base_total": long_position,
            "quote_total": Decimal("0"),
            "available_margin": Decimal(account.get("availableBalance", "0")),
            "leverage": leverage,
        }

    def get_last_trade(self):
        trades = self.client.account_trades(self.symbol, limit=1)
        if trades:
            trade = trades[-1]
            return trade["side"], Decimal(trade["qty"]), Decimal(trade["price"])

        recent = self.client.recent_trades(self.symbol, limit=1)
        fallback_price = Decimal(recent[-1]["price"]) if recent else self.client.latest_price(self.symbol)
        return "SELL", self.config.initial_sell_quantity, fallback_price

    def wait_no_open_orders(self, attempts=10, wait_time=1):
        for attempt in range(attempts):
            open_orders = self.client.open_orders(self.symbol)
            if not open_orders:
                return True
            if attempt < attempts - 1:
                time.sleep(wait_time)
        return False

    def place_grid_order(self, side, price, quantity):
        formatted_price = self.format_price(price)
        formatted_qty = self.format_qty(quantity)
        if Decimal(formatted_qty) < self.min_qty:
            self.send_message(f"{side} quantity {formatted_qty} is smaller than minQty {self.min_qty}")
            return None
        if self.min_notional and Decimal(formatted_price) * Decimal(formatted_qty) < self.min_notional:
            self.send_message(f"{side} notional is smaller than minNotional {self.min_notional}")
            return None
        if self.config.dry_run:
            print(f"DRY RUN {side} {formatted_qty} {self.config.base_asset} at {formatted_price}")
            return {"orderId": f"dry-{side}-{formatted_price}-{formatted_qty}"}
        return self.client.place_limit_order(self.symbol, side, formatted_price, formatted_qty)

    def update_orders(self, last_trade_side, last_trade_qty, last_trade_price):
        with self.order_lock:
            try:
                open_orders = self.client.open_orders(self.symbol)
                self.cancelled_order_ids.update(str(order["orderId"]) for order in open_orders)
                if open_orders and not self.config.dry_run:
                    self.client.cancel_all_orders(self.symbol)
                    if not self.wait_no_open_orders():
                        self.send_message("旧挂单未能全部撤销，暂停重建网格")
                        return

                balances = self.get_balances()
                last_trade_side = last_trade_side.upper()
                last_trade_qty = Decimal(str(last_trade_qty))
                last_trade_price = Decimal(str(last_trade_price))

                if last_trade_side == "BUY":
                    initial_buy_qty = max(last_trade_qty + self.config.buy_increment, self.config.initial_buy_quantity)
                    initial_sell_qty = self.config.initial_sell_quantity
                else:
                    initial_buy_qty = self.config.initial_buy_quantity
                    initial_sell_qty = max(last_trade_qty + self.config.sell_increment, self.config.initial_sell_quantity)

                quote_balance = balances["quote_total"]
                base_balance = balances["base_total"]
                free_margin = balances["available_margin"]
                leverage = balances["leverage"] if balances["leverage"] > 0 else Decimal("1")

                for i in range(self.config.num_orders):
                    buy_price = last_trade_price - (i + 1) * self.config.price_step
                    buy_qty = initial_buy_qty + i * self.config.buy_increment
                    if self.config.market_type == "SPOT":
                        required = buy_price * buy_qty
                        if quote_balance < required:
                            self.send_message(
                                f"{self.config.quote_asset}余额: {quote_balance}，无法在{self.format_price(buy_price)}"
                                f"买入{self.format_qty(buy_qty)}{self.config.base_asset}"
                            )
                            break
                        quote_balance -= required
                    else:
                        required = buy_price * buy_qty / leverage
                        if free_margin < required:
                            self.send_message(
                                f"保证金余额: {free_margin}，无法在{self.format_price(buy_price)}"
                                f"做多{self.format_qty(buy_qty)}{self.config.base_asset}"
                            )
                            break
                        free_margin -= required
                    order = self.place_grid_order("BUY", buy_price, buy_qty)
                    if order:
                        print(f"在{self.format_price(buy_price)}买入/做多{self.format_qty(buy_qty)}{self.config.base_asset}挂单成功")

                for i in range(self.config.num_orders):
                    sell_price = last_trade_price + (i + 1) * self.config.price_step
                    sell_qty = initial_sell_qty + i * self.config.sell_increment
                    if self.config.market_type == "SPOT":
                        if base_balance < sell_qty:
                            self.send_message(
                                f"{self.config.base_asset}余额: {base_balance}，无法在{self.format_price(sell_price)}"
                                f"卖出{self.format_qty(sell_qty)}{self.config.base_asset}"
                            )
                            break
                        base_balance -= sell_qty
                    else:
                        if base_balance < sell_qty:
                            self.send_message(
                                f"{self.config.base_asset}多单余额: {base_balance}，无法在{self.format_price(sell_price)}"
                                f"卖出{self.format_qty(sell_qty)}{self.config.base_asset}"
                            )
                            break
                        base_balance -= sell_qty
                    order = self.place_grid_order("SELL", sell_price, sell_qty)
                    if order:
                        print(f"在{self.format_price(sell_price)}卖出/减多{self.format_qty(sell_qty)}{self.config.base_asset}挂单成功")
            except Exception as exc:
                self.send_message(f"更新订单失败: {exc}")
                print(traceback.format_exc())

    async def task_consumer(self):
        while True:
            func, args = await self.task_queue.get()
            try:
                await self.loop.run_in_executor(None, func, *args)
            except Exception as exc:
                self.send_message(f"任务执行失败: {func.__name__} - {exc}")
                print(traceback.format_exc())
            finally:
                self.task_queue.task_done()

    def add_task(self, func, *args):
        self.task_queue.put_nowait((func, args))

    async def keepalive_loop(self, listen_key):
        while True:
            await asyncio.sleep(30 * 60)
            try:
                await self.loop.run_in_executor(None, self.client.keepalive_listen_key, listen_key)
            except Exception as exc:
                self.send_message(f"listenKey续期失败: {exc}")

    def parse_fill_event(self, message):
        def first_positive(*values):
            for value in values:
                if value is None:
                    continue
                decimal = Decimal(str(value))
                if decimal > 0:
                    return decimal
            return Decimal("0")

        if self.config.market_type == "SPOT":
            if message.get("e") != "executionReport" or message.get("X") != "FILLED":
                return None
            return {
                "order_id": str(message["i"]),
                "side": message["S"],
                "qty": Decimal(message.get("z") or message["q"]),
                "price": first_positive(message.get("L"), message.get("ap"), message.get("p")),
            }

        if message.get("e") != "ORDER_TRADE_UPDATE":
            return None
        order = message.get("o", {})
        if order.get("X") != "FILLED":
            return None
        return {
            "order_id": str(order["i"]),
            "side": order["S"],
            "qty": Decimal(order.get("z") or order["q"]),
            "price": first_positive(order.get("L"), order.get("ap"), order.get("p")),
        }

    def parse_cancel_event(self, message):
        if self.config.market_type == "SPOT":
            if message.get("e") == "executionReport" and message.get("X") == "CANCELED":
                return str(message["i"])
            return None

        if message.get("e") == "ORDER_TRADE_UPDATE":
            order = message.get("o", {})
            if order.get("X") == "CANCELED":
                return str(order["i"])
        return None

    async def listen(self):
        if not self.config.dry_run:
            self.client.cancel_all_orders(self.symbol)
        last_side, last_qty, last_price = await self.loop.run_in_executor(None, self.get_last_trade)
        self.add_task(self.update_orders, last_side, last_qty, last_price)

        listen_key = await self.loop.run_in_executor(None, self.client.start_listen_key)
        keepalive_task = self.loop.create_task(self.keepalive_loop(listen_key))
        ws_url = f"{self.client.ws_base}/ws/{listen_key}"

        try:
            async with websockets.connect(ws_url, ping_interval=None) as websocket:
                while True:
                    raw = await websocket.recv()
                    message = json.loads(raw)

                    fill = self.parse_fill_event(message)
                    if fill:
                        if fill["order_id"] in self.filled_order_ids:
                            continue
                        self.filled_order_ids.add(fill["order_id"])
                        fill_msg = f"{fill['side']} {fill['qty']}{self.config.base_asset} at {fill['price']}"
                        self.add_task(self.send_message, fill_msg)
                        self.add_task(self.update_orders, fill["side"], fill["qty"], fill["price"])
                        continue

                    canceled_id = self.parse_cancel_event(message)
                    if canceled_id:
                        if canceled_id in self.cancelled_order_ids:
                            self.cancelled_order_ids.discard(canceled_id)
                            continue
                        last_side, last_qty, last_price = await self.loop.run_in_executor(None, self.get_last_trade)
                        self.add_task(self.update_orders, last_side, last_qty, last_price)
        finally:
            keepalive_task.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive_task
            with suppress(Exception):
                await self.loop.run_in_executor(None, self.client.close_listen_key, listen_key)

    def run(self):
        self.load_symbol_rules()
        self.loop.create_task(self.task_consumer())
        retry_count = 0
        last_success_time = time.time()

        while True:
            try:
                now = time.time()
                if now - last_success_time > 3600:
                    retry_count = 0
                    last_success_time = now
                self.loop.run_until_complete(self.listen())
            except KeyboardInterrupt:
                self.send_message("程序被用户中断")
                break
            except Exception as exc:
                retry_count += 1
                delay = min(2**retry_count, self.config.recv_backoff_cap)
                self.send_message(f"程序错误，{delay}秒后重试 (第{retry_count}次): {exc}")
                print(traceback.format_exc())
                time.sleep(delay)


def main():
    config = GridConfig()
    client = ExchangeClient(config)
    strategy = GridStrategy(client, config)
    strategy.run()


if __name__ == "__main__":
    main()

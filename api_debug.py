import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal


USED_GROUP = "used"
UNUSED_GROUP = "unused"


@dataclass(frozen=True)
class TestCase:
    group: str
    name: str
    method: str
    path: str
    params: dict | None = None
    signed: bool = False
    market_types: tuple[str, ...] = ("SPOT", "PERP")
    unsafe: bool = False
    needs_order_id: bool = False
    note: str = ""


def make_config(args):
    from grid import GridConfig

    base = GridConfig()
    return GridConfig(
        market_type=args.market_type or base.market_type,
        base_asset=args.base_asset or base.base_asset,
        quote_asset=args.quote_asset or base.quote_asset,
        symbol=args.symbol if args.symbol is not None else base.symbol,
        initial_buy_quantity=base.initial_buy_quantity,
        buy_increment=base.buy_increment,
        initial_sell_quantity=base.initial_sell_quantity,
        sell_increment=base.sell_increment,
        price_step=base.price_step,
        num_orders=base.num_orders,
        dry_run=base.dry_run,
        api_max_retries=args.retries,
        recv_backoff_cap=base.recv_backoff_cap,
        leverage=base.leverage,
        hedge_mode=base.hedge_mode,
    )


def short_json(value, max_chars=1600):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... <truncated>"


def public_used_tests(symbol):
    return [
        TestCase(USED_GROUP, "exchange_info", "GET", "/exchangeInfo"),
        TestCase(USED_GROUP, "latest_price", "GET", "/ticker/price", {"symbol": symbol}),
        TestCase(USED_GROUP, "recent_trades", "GET", "/trades", {"symbol": symbol, "limit": 1}),
    ]


def private_used_tests(config, symbol):
    account_path = "/account" if config.market_type == "SPOT" else "/accountWithJoinMargin"
    return [
        TestCase(USED_GROUP, "account_trades", "GET", "/userTrades", {"symbol": symbol, "limit": 1}, signed=True),
        TestCase(USED_GROUP, "account", "GET", account_path, signed=True),
        TestCase(USED_GROUP, "open_orders", "GET", "/openOrders", {"symbol": symbol}, signed=True),
        TestCase(USED_GROUP, "start_listen_key", "POST", "/listenKey", signed=True),
        TestCase(USED_GROUP, "keepalive_listen_key", "PUT", "/listenKey", signed=True, note="requires listenKey from start_listen_key"),
        TestCase(USED_GROUP, "close_listen_key", "DELETE", "/listenKey", signed=True, note="requires listenKey from start_listen_key"),
        TestCase(USED_GROUP, "place_limit_order", "POST", "/order", signed=True, unsafe=True),
        TestCase(USED_GROUP, "cancel_all_open_orders", "DELETE", "/allOpenOrders", {"symbol": symbol}, signed=True, unsafe=True),
    ]


def unused_market_tests(symbol):
    return [
        TestCase(UNUSED_GROUP, "server_time", "GET", "/time"),
        TestCase(UNUSED_GROUP, "order_book", "GET", "/depth", {"symbol": symbol, "limit": 5}),
        TestCase(UNUSED_GROUP, "ticker_24hr", "GET", "/ticker/24hr", {"symbol": symbol}),
        TestCase(UNUSED_GROUP, "klines_1m", "GET", "/klines", {"symbol": symbol, "interval": "1m", "limit": 5}),
    ]


def unused_spot_tests(symbol, order_id):
    order_params = {"symbol": symbol, "orderId": order_id} if order_id else {"symbol": symbol}
    return [
        TestCase(UNUSED_GROUP, "query_order", "GET", "/order", order_params, signed=True, market_types=("SPOT",), needs_order_id=True),
        TestCase(UNUSED_GROUP, "query_current_open_order", "GET", "/openOrder", order_params, signed=True, market_types=("SPOT",), needs_order_id=True),
        TestCase(UNUSED_GROUP, "all_orders", "GET", "/allOrders", {"symbol": symbol, "limit": 5}, signed=True, market_types=("SPOT",)),
        TestCase(UNUSED_GROUP, "transaction_history", "GET", "/transactionHistory", {"limit": 5}, signed=True, market_types=("SPOT",)),
        TestCase(UNUSED_GROUP, "withdraw_fee_estimate", "GET", "/aster/withdraw/estimateFee", {"chainId": "56", "asset": "USDT"}, market_types=("SPOT",)),
    ]


def unused_perp_tests(symbol, order_id):
    order_params = {"symbol": symbol, "orderId": order_id} if order_id else {"symbol": symbol}
    return [
        TestCase(UNUSED_GROUP, "position_mode", "GET", "/positionSide/dual", signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "multi_assets_mode", "GET", "/multiAssetsMargin", signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "query_order", "GET", "/order", order_params, signed=True, market_types=("PERP",), needs_order_id=True),
        TestCase(UNUSED_GROUP, "query_current_open_order", "GET", "/openOrder", order_params, signed=True, market_types=("PERP",), needs_order_id=True),
        TestCase(UNUSED_GROUP, "all_orders", "GET", "/allOrders", {"symbol": symbol, "limit": 5}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "balance", "GET", "/balance", signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "position_information", "GET", "/positionRisk", {"symbol": symbol}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "income_history", "GET", "/income", {"symbol": symbol, "limit": 5}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "leverage_bracket", "GET", "/leverageBracket", {"symbol": symbol}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "adl_quantile", "GET", "/adlQuantile", {"symbol": symbol}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "force_orders", "GET", "/forceOrders", {"symbol": symbol, "limit": 5}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "commission_rate", "GET", "/commissionRate", {"symbol": symbol}, signed=True, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "premium_index", "GET", "/premiumIndex", {"symbol": symbol}, market_types=("PERP",)),
        TestCase(UNUSED_GROUP, "funding_rate", "GET", "/fundingRate", {"symbol": symbol, "limit": 5}, market_types=("PERP",)),
    ]


def make_order_params(args, config, symbol):
    if args.order_price is None or args.order_quantity is None:
        raise ValueError("--place-test-order requires --order-price and --order-quantity")
    params = {
        "symbol": symbol,
        "side": args.order_side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": args.order_quantity,
        "price": args.order_price,
    }
    if config.market_type == "PERP":
        if args.position_side and args.position_side != "LONG":
            raise ValueError("PERP test orders only support LONG positionSide in this project")
        if args.position_side == "LONG":
            params["positionSide"] = "LONG"
        elif args.order_side == "SELL":
            params["reduceOnly"] = "true"
    return params


def should_run(case, args, config):
    if config.market_type not in case.market_types:
        return False, f"market_type {config.market_type} not supported by this test"
    if case.needs_order_id and not args.order_id:
        return False, "requires --order-id"
    if case.unsafe and case.name == "place_limit_order" and not args.place_test_order:
        return False, "unsafe; add --place-test-order --order-price --order-quantity to run"
    if case.unsafe and case.name == "cancel_all_open_orders" and not args.cancel_all_open_orders:
        return False, "unsafe; add --cancel-all-open-orders to run"
    return True, ""


def run_case(client, config, case, args, listen_key_holder):
    params = dict(case.params or {})
    if case.name in {"keepalive_listen_key", "close_listen_key"}:
        listen_key = listen_key_holder.get("listenKey")
        if not listen_key:
            return {"status": "skipped", "reason": "start_listen_key did not return listenKey"}
        if config.market_type == "SPOT":
            params["listenKey"] = listen_key
        else:
            params = None
    if case.name == "place_limit_order":
        params = make_order_params(args, config, client.config.trade_symbol)
    started = time.time()
    data = client.request(case.method, case.path, params=params, signed=case.signed)
    elapsed_ms = round((time.time() - started) * 1000, 2)
    if case.name == "start_listen_key" and isinstance(data, dict) and "listenKey" in data:
        listen_key_holder["listenKey"] = data["listenKey"]
    return {"status": "ok", "elapsed_ms": elapsed_ms, "response": data}


async def run_ws_smoke(client, seconds):
    import websockets

    listen_key = client.start_listen_key()
    listen_key_value = listen_key["listenKey"]
    ws_url = f"{client.ws_base}/ws/{listen_key_value}"
    messages = []
    try:
        async with websockets.connect(ws_url, ping_interval=None) as websocket:
            end_at = time.time() + seconds
            while time.time() < end_at:
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.1, end_at - time.time()))
                except asyncio.TimeoutError:
                    break
                messages.append(json.loads(raw))
    finally:
        try:
            client.close_listen_key(listen_key_value)
        except Exception:
            pass
    return messages


def parse_args():
    parser = argparse.ArgumentParser(description="Test REST and user-stream endpoints used by this project.")
    parser.add_argument("--market-type", choices=["SPOT", "PERP"], help="Override MARKET_TYPE from grid.py")
    parser.add_argument("--base-asset", help="Override BASE_ASSET from grid.py")
    parser.add_argument("--quote-asset", help="Override QUOTE_ASSET from grid.py")
    parser.add_argument("--symbol", help="Override SYMBOL. Use empty string to fall back to base+quote.")
    parser.add_argument("--group", choices=["used", "unused", "all"], default="used", help="Which tests to run")
    parser.add_argument("--public-only", action="store_true", help="Only run public endpoints that do not require .env secrets")
    parser.add_argument("--order-id", help="Order ID for query_order/query_current_open_order tests")
    parser.add_argument("--place-test-order", action="store_true", help="Actually place a LIMIT order. Requires price and quantity.")
    parser.add_argument("--order-side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--order-price", help="Limit price for --place-test-order")
    parser.add_argument("--order-quantity", help="Quantity for --place-test-order")
    parser.add_argument("--position-side", choices=["LONG"], help="PERP hedge-mode positionSide for --place-test-order; only LONG is supported")
    parser.add_argument("--cancel-all-open-orders", action="store_true", help="Actually cancel all open orders for the symbol")
    parser.add_argument("--ws-smoke", type=int, default=0, help="Connect user WebSocket for N seconds after REST tests")
    parser.add_argument("--retries", type=int, default=1, help="Retries per endpoint during debugging")
    parser.add_argument("--max-response-chars", type=int, default=1600)
    return parser.parse_args()


def main():
    args = parse_args()
    from grid import ExchangeClient

    config = make_config(args)
    client = ExchangeClient(config)
    symbol = config.trade_symbol

    tests = []
    tests.extend(public_used_tests(symbol))
    if not args.public_only:
        tests.extend(private_used_tests(config, symbol))
    if args.group in {"unused", "all"}:
        tests.extend(unused_market_tests(symbol))
        if not args.public_only:
            tests.extend(unused_spot_tests(symbol, args.order_id))
            tests.extend(unused_perp_tests(symbol, args.order_id))

    if args.group != "all":
        tests = [case for case in tests if case.group == args.group]

    print(f"market_type={config.market_type} symbol={symbol} public_only={args.public_only}")
    print("本项目用到的接口在 used 组；当前策略没用到的接口在 unused 组。\n")

    listen_key_holder = {}
    ok = failed = skipped = 0
    for index, case in enumerate(tests, start=1):
        runnable, reason = should_run(case, args, config)
        label = f"[{index:02d}] {case.group}.{case.name} {case.method} {client.path_prefix}{case.path}"
        if not runnable:
            skipped += 1
            print(f"{label}\n  SKIP: {reason}\n")
            continue
        try:
            result = run_case(client, config, case, args, listen_key_holder)
            if result["status"] == "skipped":
                skipped += 1
                print(f"{label}\n  SKIP: {result['reason']}\n")
            else:
                ok += 1
                print(f"{label}\n  OK {result['elapsed_ms']} ms\n{short_json(result['response'], args.max_response_chars)}\n")
        except Exception as exc:
            failed += 1
            print(f"{label}\n  FAIL: {type(exc).__name__}: {exc}\n")

    if args.ws_smoke > 0 and not args.public_only:
        try:
            messages = asyncio.run(run_ws_smoke(client, args.ws_smoke))
            ok += 1
            print(f"[ws] user_stream {args.ws_smoke}s\n  OK messages={len(messages)}\n{short_json(messages, args.max_response_chars)}\n")
        except Exception as exc:
            failed += 1
            print(f"[ws] user_stream {args.ws_smoke}s\n  FAIL: {type(exc).__name__}: {exc}\n")

    print(f"summary: ok={ok} failed={failed} skipped={skipped}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()

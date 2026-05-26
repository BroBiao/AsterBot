# Grid Bot

程序运行一个成交驱动的网格策略：启动时读取最近一笔个人成交；如果没有成交记录，则使用最新市场成交价作为参考价。随后在参考价下方挂买单/做多，在参考价上方挂卖单；PERP 模式下卖单只用于减少多单，不会建立空单。任意网格单完全成交后，程序会撤销剩余挂单，并以最新成交价为中心重建网格。人工取消挂单时，程序也会按最近成交重新补网格。

## 安装依赖

建议在虚拟环境中安装依赖：

```bash
cd /opt/grid-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

如果不使用虚拟环境，也可以直接安装：

```bash
pip install -r requirements.txt
```

## 密钥配置

`.env` 只放需要保密的信息，不放交易参数：

```bash
address=0x...
private_key=0x...
bot_token=
chat_id=
```

参数说明：

- `address`：主账户钱包地址。
- `private_key`：API wallet / signer 的私钥。程序会从私钥自动推导 signer 地址。
- `bot_token`：Telegram Bot token，可留空。
- `chat_id`：Telegram 接收消息的 chat id，可留空。

## 交易参数

交易参数直接修改 `grid.py` 顶部的配置区。常用参数如下：

- `MARKET_TYPE`：`SPOT` 或 `PERP`。`SPOT` 使用现货余额；`PERP` 使用合约账户保证金。
- `BASE_ASSET` / `QUOTE_ASSET`：交易对基础资产和计价资产，例如 `LINK` / `USDT`。
- `SYMBOL`：交易所交易对符号。通常留空，程序会自动拼成 `BASE_ASSET + QUOTE_ASSET`，例如 `LINKUSDT`。如果交易所符号不符合这个规则，再手动填写。
- `INITIAL_BUY_QUANTITY`：最近一档买单/做多的基础数量。
- `INITIAL_SELL_QUANTITY`：最近一档卖单基础数量。PERP 模式下它表示减多数量，不表示开空数量。
- `BUY_INCREMENT`：更深买单的数量递增。例如 `INITIAL_BUY_QUANTITY = 2`、`BUY_INCREMENT = 0.1`、`NUM_ORDERS = 3` 时，三档买单数量为 `2`、`2.1`、`2.2`。
- `SELL_INCREMENT`：更深卖单的数量递增。设为 `0` 表示每档卖单数量相同。
- `PRICE_STEP`：相邻网格价格间距。程序会把挂单价规整到这个间距，并在启动时校验它不能小于交易所 tick size。
- `NUM_ORDERS`：每侧挂单数量。`1` 表示下方 1 个买单、上方 1 个卖单。
- `DRY_RUN`：`True` 只打印将要挂的订单，不真实下单；`False` 才会真实撤单和挂单。首次运行必须先保持 `True` 看日志。
- `API_MAX_RETRIES`：REST API 失败后的最大重试次数。
- `RECONNECT_BACKOFF_CAP`：WebSocket 断线后指数退避重连的最大等待秒数。
- `LEVERAGE`：仅用于 `PERP` 模式下买单本地保证金检查。实际杠杆倍数需要提前在账户里设置好。
- `HEDGE_MODE`：`PERP` 双向持仓时设为 `True`，程序会给买单和卖单都指定 `LONG` 仓位；卖单只减多，不开 `SHORT`。单向持仓保持 `False`，卖单会附带 `reduceOnly=true`。

### 配置示例

现货 `LINKUSDT`，上下各挂 1 档，每 0.5 USDT 一个网格：

```python
MARKET_TYPE = "SPOT"
BASE_ASSET = "LINK"
QUOTE_ASSET = "USDT"
SYMBOL = ""
PRICE_STEP = Decimal("0.5")
NUM_ORDERS = 1
DRY_RUN = True
```

合约 `LINKUSDT`，上下各挂 3 档，买单逐档加 0.1 LINK，卖单数量固定且只减多：

```python
MARKET_TYPE = "PERP"
BASE_ASSET = "LINK"
QUOTE_ASSET = "USDT"
INITIAL_BUY_QUANTITY = Decimal("2")
BUY_INCREMENT = Decimal("0.1")
INITIAL_SELL_QUANTITY = Decimal("2")
SELL_INCREMENT = Decimal("0")
PRICE_STEP = Decimal("0.5")
NUM_ORDERS = 3
LEVERAGE = Decimal("1")
DRY_RUN = True
```

## API 调试脚本

`api_debug.py` 用来单独测试接口签名、网络连通性和返回字段。接口按顺序分组：

- `used`：本项目运行策略时实际用到的接口，默认只跑这一组。
- `unused`：当前策略没有用到、但调试时常见的行情/订单/账户查询接口。
- `all`：先跑 `used`，再跑 `unused`。

只测试公开接口，不需要 `.env`：

```bash
python3 api_debug.py --public-only
```

测试本项目用到的接口：

```bash
python3 api_debug.py --group used
```

测试当前策略没用到的查询接口：

```bash
python3 api_debug.py --group unused
```

临时覆盖交易模式或交易对，不修改 `grid.py`：

```bash
python3 api_debug.py --market-type PERP --symbol LINKUSDT
python3 api_debug.py --market-type SPOT --base-asset LINK --quote-asset USDT
```

查询指定订单相关接口需要提供订单号：

```bash
python3 api_debug.py --group unused --order-id 123456789
```

测试用户 WebSocket 连接 15 秒：

```bash
python3 api_debug.py --ws-smoke 15
```

危险接口默认不会执行。只有显式传入下面参数时，脚本才会真实下单或撤单：

```bash
python3 api_debug.py --place-test-order --order-side BUY --order-price 1.23 --order-quantity 1
python3 api_debug.py --cancel-all-open-orders
```

运行 `python3 api_debug.py --help` 可以查看全部参数。

## 手动运行

先 dry-run：

```bash
cd /opt/grid-bot
. .venv/bin/activate
python3 grid.py
```

确认日志中的交易对、方向、价格和数量都符合预期后，再把 `grid.py` 中的 `DRY_RUN` 改为 `False`。

## systemd 服务

项目内提供了 `systemd/grid-bot.service`。文件里使用 `/opt/grid-bot` 作为示例部署目录、`gridbot` 作为示例运行用户；安装前请按你的服务器实际目录和用户修改。默认配置如下：

- `User=gridbot`
- `WorkingDirectory=/opt/grid-bot`
- `EnvironmentFile=/opt/grid-bot/.env`
- `ExecStart=/opt/grid-bot/.venv/bin/python /opt/grid-bot/grid.py`

如果你不使用 `.venv`，把服务文件里的 `ExecStart` 改成：

```ini
ExecStart=/usr/bin/python3 /opt/grid-bot/grid.py
```

安装并启动服务：

```bash
sudo cp /opt/grid-bot/systemd/grid-bot.service /etc/systemd/system/grid-bot.service
sudo systemctl daemon-reload
sudo systemctl enable grid-bot
sudo systemctl start grid-bot
```

查看状态和日志：

```bash
systemctl status grid-bot
journalctl -u grid-bot -f
```

修改 `grid.py` 或 `.env` 后重启：

```bash
sudo systemctl restart grid-bot
```

停止服务：

```bash
sudo systemctl stop grid-bot
```

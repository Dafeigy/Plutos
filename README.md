# Plutos

基于 [OpenCTP](https://github.com/openctp/openctp) + [FastAPI](https://fastapi.tiangolo.com/) 的期货交易 REST API 服务。将 CTP 的事件驱动回调模型封装为同步 HTTP 接口，提供账户查询、行情获取、委托下单功能。

## 环境要求

- **Python** ≥ 3.10
- **操作系统**：Linux x86_64（`openctp-ctp` 动态库仅支持此平台）
- **CTP 柜台**：SimNow 仿真环境或实盘环境

## 快速开始

```bash
# 1. 克隆项目
cd Plutos

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填入你的 CTP 账号信息

# 4. 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

服务启动后，访问 http://localhost:8000/docs 查看 Swagger API 文档。

## API 接口

### 1. 查询账户余额

```bash
GET /account/balance
```

**响应示例：**

```json
{
  "user_id": "123456",
  "balance": 1000000.00,
  "available": 950000.00,
  "frozen_margin": 50000.00,
  "curr_margin": 48000.00,
  "close_profit": 2000.00,
  "position_profit": -500.00,
  "deposit": 1000000.00,
  "withdraw": 0.00
}
```

| 字段 | 说明 |
|------|------|
| `balance` | 静态权益 |
| `available` | 可用资金 |
| `frozen_margin` | 冻结保证金 |
| `curr_margin` | 当前保证金 |
| `close_profit` | 平仓盈亏 |
| `position_profit` | 持仓盈亏 |

### 2. 查询合约最新价格

```bash
GET /market/{instrument_id}/price
```

**示例：** `GET /market/rb2510/price`

**响应示例：**

```json
{
  "instrument_id": "rb2510",
  "last_price": 3458.0,
  "bid_price1": 3457.0,
  "ask_price1": 3458.0,
  "bid_volume1": 25,
  "ask_volume1": 12,
  "volume": 158632,
  "open_interest": 1523400.0,
  "update_time": "14:59:58",
  "update_millisec": 500
}
```

> 首次请求某个合约时，服务会自动向 CTP 订阅该合约并等待首次行情推送（最多 8 秒）。后续请求直接命中内存缓存。

### 3. 委托下单

```bash
POST /order
Content-Type: application/json
```

**请求体：**

```json
{
  "instrument_id": "rb2510",
  "direction": "buy",
  "offset_flag": "open",
  "price": 3450.0,
  "volume": 1
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `instrument_id` | string | 是 | 合约代码 |
| `direction` | string | 是 | `buy` 买 / `sell` 卖 |
| `offset_flag` | string | 否 | `open` 开仓（默认）/ `close` 平仓 / `close_today` 平今 |
| `price` | float | 是 | 委托价格 (>0) |
| `volume` | int | 是 | 委托数量 (≥1) |

**响应示例：**

```json
{
  "order_ref": "1",
  "order_sys_id": "ORD20240622_001",
  "instrument_id": "rb2510",
  "direction": "buy",
  "price": 3450.0,
  "volume": 1,
  "order_status": "未成交队列中",
  "status_msg": "委托已提交"
}
```

报单状态说明：

| 状态值 | 说明 |
|--------|------|
| 全部成交 | 订单全部成交 |
| 部分成交 | 订单部分成交 |
| 未成交 | 订单未成交 |
| 未成交队列中 | 订单在队列中等待 |
| 已撤单 | 订单已撤销 |

### 4. 健康检查

```bash
GET /health
# → {"status": "ok"}
```

## 配置说明

所有配置通过 `.env` 文件设置：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MD_FRONT` | `tcp://180.168.146.187:10131` | 行情前端地址 |
| `TRADE_FRONT` | `tcp://180.168.146.187:10130` | 交易前端地址 |
| `BROKER_ID` | `9999` | 经纪商代码 |
| `USER_ID` | — | 用户代码 |
| `PASSWORD` | — | 密码 |
| `SUBSCRIBE_INSTRUMENTS` | — | 启动时自动订阅的合约（逗号分隔，如 `rb2510,ag2512`） |
| `DEFAULT_TIMEOUT` | `10` | CTP 操作超时秒数 |

### SimNow 仿真地址

SimNow 是上期技术提供的免费 CTP 仿真环境，适合开发测试。申请账号后使用：

```env
MD_FRONT=tcp://180.168.146.187:10131
TRADE_FRONT=tcp://180.168.146.187:10130
BROKER_ID=9999
USER_ID=<你的SimNow用户代码>
PASSWORD=<你的SimNow密码>
```

## 项目结构

```
Plutos/
├── app/
│   ├── main.py              # FastAPI 应用入口，生命周期管理
│   ├── config.py             # 配置管理（.env → pydantic-settings）
│   ├── models.py             # Pydantic 请求/响应模型
│   ├── ctp/
│   │   ├── bridge.py         # FutureStore 异步桥接层（核心）
│   │   ├── md_client.py      # 行情客户端（MdApi 封装）
│   │   └── trader_client.py  # 交易客户端（TraderApi 封装）
│   └── api/
│       ├── account.py        # GET  /account/balance
│       ├── market.py         # GET  /market/{id}/price
│       └── order.py          # POST /order
├── docs/
│   └── architecture.md       # 系统架构设计文档
├── requirements.txt
├── .env.example
├── CLAUDE.md
└── README.md
```

## 架构概览

核心挑战：CTP 是事件驱动的——操作结果通过回调在 CTP 内部线程中异步返回，而 FastAPI 是请求-响应模型。

**桥接方案**：使用 `concurrent.futures.Future`（线程安全）+ `asyncio.wrap_future()`（转协程）：

```
Endpoint → 创建 Future → 调用 CTP API → await Future
                                            ↑
                          CTP 回调触发 → resolve Future
```

`FutureStore` 管理所有待处理 Future，后台线程自动超时清理。详细设计见 [docs/architecture.md](docs/architecture.md)。

## 注意事项

- CTP 同账号不可重复登录，一个时刻只能启动一个 Plutos 实例连接同一账号
- 当前未实现自动断线重连，CTP 连接断开后需重启服务获取最新行情
- `openctp-ctp` 的 `.so` 文件要求 Linux x86_64 环境，开发时请确认平台兼容

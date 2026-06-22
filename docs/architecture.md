# Plutos 系统架构设计

## 1. 概述

Plutos 是一个基于 [OpenCTP](https://github.com/openctp/openctp) + [FastAPI](https://fastapi.tiangolo.com/) 的期货交易 REST API 服务。核心挑战在于：**CTP 是事件驱动模型**（结果通过回调在 CTP 内部线程中返回），而 **FastAPI 是 async 请求-响应模型**。本设计通过 `concurrent.futures.Future` + `asyncio.wrap_future()` 桥接这两个世界。

## 2. 系统架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      HTTP Clients                           │
│                 (curl, browser, scripts)                    │
└─────────────────────┬───────────────────────────────────────┘
                      │  REST (JSON)
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   FastAPI Application                        │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ /account     │  │ /market/{id} │  │  /order      │       │
│  │ balance      │  │ price        │  │              │       │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                 │                 │                │
│         │    asyncio.wrap_future(future)     │                │
│         └──────────┬──────┴─────────────────┘                │
│                    │                                         │
│              ┌─────▼────────────────────────┐               │
│              │        FutureStore            │               │
│              │  ┌──────────────────────┐     │               │
│              │  │ dict[int, Future]    │     │  query: 10s  │
│              │  │ dict[int, list]      │     │  order: 15s  │
│              │  │ dict[str, int]       │     │              │
│              │  └──────────────────────┘     │               │
│              │   background cleanup daemon    │               │
│              └──────────┬────────────────────┘               │
│                         │                                    │
│           ┌─────────────┴──────────────┐                     │
│           ▼                            ▼                     │
│  ┌─────────────────┐         ┌─────────────────┐            │
│  │   MdClient      │         │  TraderClient   │            │
│  │  (MdApi 封装)   │         │  (TraderApi 封装)│            │
│  │                 │         │                  │            │
│  │  price cache    │         │  auth / login    │            │
│  │  + lock         │         │  queries/orders  │            │
│  └────────┬────────┘         └────────┬─────────┘            │
└───────────┼───────────────────────────┼──────────────────────┘
            │                           │
      ┌─────▼──────┐            ┌───────▼───────┐
      │ CTP 行情前置 │            │ CTP 交易前置    │
      │ (tcp port) │            │ (tcp port)    │
      └────────────┘            └───────────────┘
```

## 3. 核心桥接机制：FutureStore

### 3.1 问题

CTP 的操作模式是：

1. 调用 `api.ReqQryTradingAccount(req, request_id)` 发起查询
2. 结果通过回调 `OnRspQryTradingAccount(pTradingAccount, pRspInfo, nRequestID, bIsLast)` 返回
3. 回调在 **CTP 内部线程**中执行，不在 FastAPI 事件循环线程中
4. 多记录查询（如持仓）会**多次触发回调**，`bIsLast=True` 标记最后一条

### 3.2 方案

```
                FastAPI Route (async coroutine)
                         │
                         │ ① future = store.create(request_id)
                         │ ② trader.api.ReqQryAccount(req, request_id)
                         │ ③ result = await asyncio.wrap_future(future)
                         │
              ┌──────────┴──────────┐
              │   asyncio event     │   CTP internal thread
              │   loop waits        │         │
              │                     │   ④ callback fires
              │                     │   ⑤ store.accumulate(request_id, record)
              │                     │   ⑥ if bIsLast: store.resolve_with_accumulator(request_id)
              │                     │         │
              │   ◄──────────────────────────┘
              │   ⑦ future.set_result([records])
              │
              ▼
          return response
```

### 3.3 FutureStore 设计

```python
class FutureStore:
    _lock: threading.Lock
    _store: dict[int, Future]          # request_id → Future
    _accumulators: dict[int, list]     # request_id → accumulated records
    _timestamps: dict[int, float]      # request_id → creation time
    _order_ref_map: dict[str, int]     # OrderRef → request_id (订单错误回调映射)
```

**关键方法：**

| 方法 | 用途 | 调用方 |
|------|------|--------|
| `create(rid)` | 注册 Future + 累计器 + 时间戳 | TraderClient 查询方法 |
| `accumulate(rid, item)` | 追加一条中间结果 | CTP 回调线程 |
| `resolve_with_accumulator(rid)` | 用累计列表填充 Future | CTP 回调线程 (`bIsLast=True`) |
| `resolve_direct(rid, result)` | 用单个值填充 Future | CTP 回调线程 (单记录操作) |
| `reject(rid, error_id, msg)` | 用 CTPError 拒绝 Future | CTP 回调线程 (出错) |
| `create_with_order_ref(rid, ref)` | 创建 Future + OrderRef 索引 | TraderClient.insert_order() |
| `reject_by_order_ref(ref, ...)` | 按 OrderRef 拒绝 | `OnErrRtnOrderInsert` 回调 |

**后台清理线程**：每 1 秒扫描过期 Future，设置 `TimeoutError`。超时时间由创建时的 Store 实例决定（查询 10s，下单 15s）。

### 3.4 为什么用两个 FutureStore 实例

- **查询 Store**：默认超时 10s（可通过 `DEFAULT_TIMEOUT` 配置）
- **下单 Store**：固定超时 15s

分开管理避免超时策略相互影响，且两个 Store 各自拥有独立的清理线程。

## 4. 客户端生命周期

### 4.1 启动流程

```
uvicorn app.main:app
  │
  ├─ ① 读取 .env 配置 (pydantic-settings)
  ├─ ② 创建 query_store (10s) + order_store (15s)
  ├─ ③ 创建 MdClient + TraderClient
  │
  ├─ ④ MdClient.init()         → 连接行情前置
  │     await_login()           → 等待登录完成(无需认证)
  │
  ├─ ⑤ TraderClient.init()     → 连接交易前置
  │     await_login()           → 等待认证→登录→结算确认完成
  │
  ├─ ⑥ 预订阅合约 (SUBSCRIBE_INSTRUMENTS)
  │     └─ run_in_executor(md_client.subscribe, instruments)
  │
  └─ ⑦ 注册路由，开始接受请求
```

### 4.2 登录序列

```
TraderClient                CTP 交易前置
    │                            │
    │── Init() ─────────────────►│
    │                            │
    │◄── OnFrontConnected ──────│
    │── ReqAuthenticate ────────►│
    │◄── OnRspAuthenticate ─────│  (失败 → 退出)
    │── ReqUserLogin ───────────►│
    │◄── OnRspUserLogin ────────│  (失败 → 退出; 成功→保存 FrontID/SessionID/OrderRef)
    │── ReqSettlementInfoConfirm►│
    │◄── OnRspSettlementInfo ───│  (成功 → resolve login_future)
    │                            │
    ▼ 就绪接受请求
```

**登录作为启动关卡**：任何一步失败都会阻止服务启动，避免在未就绪状态下接受请求。

### 4.3 关闭流程

```
服务关闭信号
  │
  ├─ ① query_store.stop() / order_store.stop()     → 停止清理线程
  ├─ ② trader_client.release()                     → 释放 CTP 交易连接
  └─ ③ md_client.release()                         → 释放 CTP 行情连接
```

## 5. 行情数据流

```
GET /market/au2506/price
  │
  ├─ ① cache 命中? → 直接返回 PriceResponse
  │
  └─ ② cache 未命中
       │
       ├─ 创建 pending Future (去重: 同一合约只订阅一次)
       ├─ run_in_executor → md_client.subscribe(["au2506"])
       │      └─ CTP 内部: 向行情前置发送订阅请求
       │
       ├─ await first tick (timeout=8s)
       │      │
       │      └─ OnRtnDepthMarketData → 更新 cache → resolve pending Future
       │
       └─ 从 cache 读取 → 返回 PriceResponse
```

**线程安全**：`_cache` 和 `_pending_subs` 分别受各自 `threading.Lock` 保护。CTP 回调在内部线程中更新 cache，FastAPI 路由在事件循环中读取 cache。

## 6. 下单流程

```
POST /order {instrument_id, direction, price, volume}
  │
  ├─ ① 模型验证 (Pydantic: price>0, volume≥1, direction∈{buy,sell})
  ├─ ② 推断交易所 (instrument prefix → SHFE/DCE/CZCE/...)
  │
  ├─ ③ trader.insert_order() ──── 创建 Future (order_store, 15s timeout)
  │      ├─ 原子递增 OrderRef
  │      ├─ ReqOrderInsert(req, request_id)
  │      └─ 返回 Future
  │
  ├─ ④ await asyncio.wrap_future(future)
  │      │
  │      ├─ OnRspOrderInsert → resolve_direct(request_id, pInputOrder)
  │      └─ OnErrRtnOrderInsert → reject_by_order_ref(OrderRef, ...)
  │
  └─ ⑤ 映射 CTP 响应 → OrderResponse
```

**订单错误回调的特殊处理**：`OnErrRtnOrderInsert` 不携带 `nRequestID`（CTP 的限制），只能通过 `OrderRef` 字段映射。`FutureStore` 维护一个 `OrderRef → request_id` 的二级索引，在 `reject_by_order_ref()` 时查找对应的 Future。

## 7. 错误处理映射

| 异常类型 | HTTP 状态码 | 触发条件 |
|---------|------------|---------|
| `TimeoutError` | 408 | Future 在超时时间内未被 resolve |
| `CTPError` | 500 | CTP 回调中 `pRspInfo.ErrorID != 0` |
| 空结果 | 404 | 查询返回空列表或行情数据不存在 |

## 8. 线程模型

```
┌──────────────────────────────────────┐
│  Main Thread (asyncio event loop)    │
│  - FastAPI route handlers            │
│  - lifespan startup/shutdown         │
│  - asyncio.wrap_future() 等待       │
└───────────┬──────────────────────────┘
            │ 线程安全边界 (Future)
            │
┌───────────▼──────────────────────────┐
│  CTP Internal Threads                │
│  - OnFrontConnected                  │
│  - OnRspUserLogin                    │
│  - OnRspQryTradingAccount            │
│  - OnRtnDepthMarketData              │
│  - OnRtnOrder / OnRtnTrade           │
│  (不透明 — 由 CTP SDK 管理)          │
└──────────────────────────────────────┘
            │
┌───────────▼──────────────────────────┐
│  FutureStore Cleanup Threads         │
│  - query_store: 每 1s 扫描超时       │
│  - order_store: 每 1s 扫描超时       │
│  (daemon threads)                    │
└──────────────────────────────────────┘
```

**关键约束**：CTP 回调线程**绝不能**直接接触 asyncio 对象。所有跨线程通信必须通过 `concurrent.futures.Future`（自身线程安全）+ `asyncio.wrap_future()`（安全地拆接两个世界）。

## 9. 配置管理

```env
# .env 文件
MD_FRONT=tcp://180.168.146.187:10131        # 行情前置
TRADE_FRONT=tcp://180.168.146.187:10130      # 交易前置
BROKER_ID=9999                                # 经纪商代码
USER_ID=                                      # 用户代码
PASSWORD=                                     # 密码
APP_ID=simnow_client_test                     # 产品代码
AUTH_CODE=0000000000000000                    # 认证码
SUBSCRIBE_INSTRUMENTS=rb2510,ag2512           # 预订阅合约 (逗号分隔)
DEFAULT_TIMEOUT=10                            # 查询超时 (秒)
```

使用 `pydantic-settings` (底层调用 `python-dotenv`) 读取，`Settings` 类提供 `subscribe_list` 属性自动解析逗号分隔列表。

## 10. 局限性与未来改进

- **无断线重连**：CTP 连接断开后需重启服务
- **同账号互斥**：CTP 不允许同一账号重复登录
- **订单推送未暴露**：`OnRtnOrder`/`OnRtnTrade` 仅记录日志，未来可通过 WebSocket 推送
- **Linux 部署**：`openctp-ctp` 的 `.so` 仅支持 Linux x86_64（Windows `.pyd` 可用于开发）
- **交易所推断**：静态前缀匹配表，新合约需手动更新

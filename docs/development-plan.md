# Plutos 开发方案

## 1. 项目背景

基于 [OpenCTP](https://github.com/openctp/openctp) + FastAPI 构建期货交易 REST API 服务。原始需求来自 `Initial.md`，详细接口规范见 `README.md`。

参考实现：
- [td_demo.py](../td_demo.py) — 完整 CTP 交易 API 示例（认证→登录→结算→查询→下单/撤单）
- [md_demo.py](../md_demo.py) — 最小行情订阅示例（连接→登录→订阅→接收 Tick）

## 2. 文件结构

```
Plutos/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI 入口，lifespan 管理 CTP 客户端生命周期
│   ├── config.py             # 配置管理 (.env → pydantic-settings)
│   ├── models.py             # Pydantic 请求/响应模型
│   ├── ctp/
│   │   ├── __init__.py
│   │   ├── bridge.py         # FutureStore — 线程安全 Future 注册表 (核心)
│   │   ├── md_client.py      # MdClient — 行情客户端 (CThostFtdcMdSpi)
│   │   └── trader_client.py  # TraderClient — 交易客户端 (CThostFtdcTraderSpi)
│   └── api/
│       ├── __init__.py
│       ├── account.py        # GET  /account/balance
│       ├── market.py         # GET  /market/{id}/price
│       └── order.py          # POST /order
├── docs/
│   ├── architecture.md       # 系统架构设计文档
│   └── development-plan.md   # 本文件
├── td_demo.py                # 交易 API 参考示例 (openctp)
├── md_demo.py                # 行情 API 参考示例 (openctp)
├── requirements.txt
├── .env.example
├── .gitignore
├── CLAUDE.md
├── Initial.md                # 原始需求
└── README.md                 # 项目文档 & API 规范
```

## 3. 依赖选择

| 依赖 | 版本 | 用途 |
|------|------|------|
| `fastapi` | ≥0.115.0 | Web 框架 |
| `uvicorn[standard]` | ≥0.30.0 | ASGI 服务器 |
| `pydantic-settings` | ≥2.5.0 | 配置管理（底层依赖 python-dotenv） |
| `python-dotenv` | ≥1.0.0 | .env 文件解析 |
| `openctp-ctp` | ≥6.7.11.0 | CTP API Python 绑定 |

## 4. 关键设计决策

### 4.1 累计器模式 (Accumulator Pattern)

CTP 查询回调的特征：多次中间回调传递单条记录 + 最后一次 `bIsLast=True` 表示结束。

`FutureStore` 采用**累计器模式**而非每次都重新 resolve：
- `accumulate(rid, item)` — 追加记录到 `_accumulators[rid]` 列表
- `resolve_with_accumulator(rid)` — 在 `bIsLast=True` 时一次性 resolve 完整列表

**优点**：避免频繁的 Future set/替换操作；路由 handler 拿到完整数据集。

### 4.2 双 Store 实例

查询和下单使用独立的 `FutureStore` 实例：
- **query_store**：超时 = `DEFAULT_TIMEOUT`（默认 10s）
- **order_store**：超时 = 15s（固定）

**理由**：下单涉及交易所排队，可能比查询慢；分开超时管理避免互相影响。

### 4.3 OrderRef 二级索引

`OnErrRtnOrderInsert` 是 CTP 内部错误推送，**不携带 `nRequestID`**。唯一的关联字段是 `OrderRef`（下单时分配）。

`FutureStore` 维护 `_order_ref_map: dict[str, int]` 将 OrderRef 映射到 request_id，在 `reject_by_order_ref()` 时查找。

**局限**：如果错误回调先于 `OnRspOrderInsert` 到达且 OrderRef 尚未入库，则无法关联。实际中错误推送通常在人机审单或交易所拒单时触发，晚于 `OnRspOrderInsert`。

### 4.4 交易所推断

用户 API 只需提供 `instrument_id`（如 `rb2510`），服务端通过静态前缀表推断交易所：

| 前缀 | 交易所 | 品类 |
|------|--------|------|
| IF/IC/IH/IM/T/TF/TS/TL | CFFEX | 股指/国债 |
| CU/AL/ZN/PB/NI/SN/AU/AG/RB/HC/... | SHFE | 金属/橡胶/沥青 |
| SC/LU/BC/NR | INE | 原油/国际铜 |
| M/Y/A/P/J/JM/I/L/V/PP/EG/... | DCE | 农产品/化工 |
| FG/SR/TA/MA/CF/RM/OI/AP/... | CZCE | 玻璃/白糖/PTA |
| SI/LC | GFEX | 工业硅/碳酸锂 |

匹配策略：**长前缀优先**（如 `JM` 焦煤优先于 `J` 焦炭匹配），避免歧义。

### 4.5 登录作为启动关卡

两个客户端的登录必须全部成功，服务才接受 HTTP 请求。任何登录失败直接抛异常，uvicorn 退出。

**理由**：行情和交易数据相互依赖——查到价格却下不了单，或下得了单却查不到价格，都不可用。

### 4.6 动态行情订阅与去重

`MdClient.ensure_subscribed()` 处理并发请求同一合约的去重：

```python
with self._pending_lock:
    if instrument_id in self._pending_subs:
        future = self._pending_subs[instrument_id]  # 复用已有订阅
    else:
        future = Future()
        self._pending_subs[instrument_id] = future   # 新建订阅标记
```

第一个请求发起订阅，后续并发请求共享同一个 Future，等待第一笔行情到达。超时后清理 pending 标记，允许后续重试。

## 5. 构建顺序

按依赖关系创建文件：

| 步骤 | 文件 | 依赖 |
|------|------|------|
| 1 | `requirements.txt` `.env.example` | 无 |
| 2 | `app/__init__.py` | 无 |
| 3 | `app/config.py` | pydantic-settings |
| 4 | `app/models.py` | pydantic |
| 5 | `app/ctp/__init__.py` | 无 |
| 6 | `app/ctp/bridge.py` | threading, concurrent.futures |
| 7 | `app/ctp/trader_client.py` | bridge, openctp_ctp |
| 8 | `app/ctp/md_client.py` | bridge, openctp_ctp |
| 9 | `app/api/__init__.py` | 无 |
| 10 | `app/api/account.py` | models, trader_client |
| 11 | `app/api/market.py` | models, md_client |
| 12 | `app/api/order.py` | models, trader_client |
| 13 | `app/main.py` | 全部组件 |

## 6. 验证方法

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 验证配置加载
python -c "from app.config import Settings; s = Settings(); print(s)"

# 3. 验证桥接层
python -c "
from app.ctp.bridge import FutureStore, CTPError, TraderState
fs = FutureStore(10)
fs.start()
f = fs.create(1)
fs.accumulate(1, 'a')
fs.resolve_with_accumulator(1)
assert f.result(2) == ['a']
print('Bridge OK')
"

# 4. 验证模型
python -c "
from app.models import OrderRequest
o = OrderRequest(instrument_id='rb2510', direction='buy', price=3450, volume=1)
print(o.model_dump())
"

# 5. 启动服务 (需要有效的 .env 配置)
cp .env.example .env  # 填入真实凭证
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 6. 验证接口
curl http://localhost:8000/health
curl http://localhost:8000/docs
curl http://localhost:8000/account/balance
curl http://localhost:8000/market/au2506/price
curl -X POST http://localhost:8000/order \
  -H 'Content-Type: application/json' \
  -d '{"instrument_id":"rb2510","direction":"buy","price":3450,"volume":1}'
```

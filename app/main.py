"""
Plutos — FastAPI REST API for CTP futures trading.

Lifespan manages CTP client connections:
  1. Read config from .env
  2. Create FutureStore instances (query + order, with separate timeouts)
  3. Connect and log in both MdClient and TraderClient
  4. Pre-subscribe instruments from config
  5. Store clients on app.state for route access
  6. On shutdown: stop stores, release CTP connections
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .ctp.bridge import FutureStore, OrderCache
from .ctp.trader_client import TraderClient
from .ctp.md_client import MdClient
from .api import account, market, order, position

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info(f"Starting Plutos... (env_file={settings.model_config.get('env_file', '.env')})")

    # Create FutureStore instances
    query_store = FutureStore(default_timeout=settings.DEFAULT_TIMEOUT)
    order_store = FutureStore(default_timeout=15.0)
    query_store.start()
    order_store.start()
    logger.info(
        f"FutureStores started (query timeout={settings.DEFAULT_TIMEOUT}s, order timeout=15s)"
    )

    # In-memory order state cache (updated by OnRtnOrder/OnRtnTrade pushes)
    order_cache = OrderCache()

    # Create and connect CTP clients
    md_client = MdClient(settings)
    trader_client = TraderClient(settings, query_store, order_store, order_cache)

    # 1. Market data client
    logger.info("Connecting to market data front...")
    md_client.init()
    try:
        md_client.await_login(timeout=15.0)
        logger.info("Market data client ready.")
    except Exception as e:
        logger.error(f"MdClient login failed: {e}")
        query_store.stop()
        order_store.stop()
        raise

    # 2. Trader client
    logger.info("Connecting to trader front...")
    trader_client.init()
    try:
        trader_client.await_login(timeout=30.0)
        logger.info("Trader client ready.")
    except Exception as e:
        logger.error(f"TraderClient login failed: {e}")
        md_client.release()
        query_store.stop()
        order_store.stop()
        raise

    # 3. Pre-subscribe instruments from config
    if settings.subscribe_list:
        logger.info(f"Pre-subscribing: {settings.subscribe_list}")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, md_client.subscribe, settings.subscribe_list)

    # Store on app.state for route access
    app.state.md_client = md_client
    app.state.trader_client = trader_client
    app.state.query_store = query_store
    app.state.order_store = order_store
    app.state.order_cache = order_cache

    logger.info("Plutos started successfully.")
    yield  # ── Server running ────────────────────────────────────────────────

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down Plutos...")
    query_store.stop()
    order_store.stop()
    trader_client.release()
    md_client.release()
    logger.info("Plutos stopped.")


app = FastAPI(
    title="Plutos",
    version="0.1.0",
    description="Futures trading REST API based on OpenCTP + FastAPI",
    lifespan=lifespan,
)

# Wire up routers
app.include_router(account.router)
app.include_router(market.router)
app.include_router(order.router)
app.include_router(position.router)


@app.get("/health")
async def health():
    return {"status": "ok"}

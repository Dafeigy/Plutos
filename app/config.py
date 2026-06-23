"""Configuration management via pydantic-settings, reading from .env file.

Use APP_ENV to switch environments:
  APP_ENV=prod uvicorn app.main:app   → loads .env.prod
  APP_ENV=test uvicorn app.main:app   → loads .env.test
  (no APP_ENV)                        → loads .env
"""

import os

from pydantic_settings import BaseSettings


def _get_env_file() -> str:
    """Resolve the .env file path based on APP_ENV.

    APP_ENV=prod  → .env.prod
    APP_ENV=test  → .env.test
    unset/empty   → .env
    """
    env_suffix = os.getenv("APP_ENV", "").strip()
    if env_suffix:
        return f".env.{env_suffix}"
    return ".env"


class Settings(BaseSettings):
    MD_FRONT: str = "tcp://180.168.146.187:10131"
    TRADE_FRONT: str = "tcp://180.168.146.187:10130"
    BROKER_ID: str = "9999"
    USER_ID: str = ""
    PASSWORD: str = ""
    APP_ID: str = "simnow_client_test"
    AUTH_CODE: str = "0000000000000000"
    SUBSCRIBE_INSTRUMENTS: str = ""
    DEFAULT_TIMEOUT: int = 10

    model_config = {"env_file": _get_env_file(), "env_file_encoding": "utf-8"}

    @property
    def subscribe_list(self) -> list[str]:
        if not self.SUBSCRIBE_INSTRUMENTS.strip():
            return []
        return [s.strip() for s in self.SUBSCRIBE_INSTRUMENTS.split(",") if s.strip()]

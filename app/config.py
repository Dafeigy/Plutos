"""Configuration management via pydantic-settings, reading from .env file."""

from pydantic_settings import BaseSettings


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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def subscribe_list(self) -> list[str]:
        if not self.SUBSCRIBE_INSTRUMENTS.strip():
            return []
        return [s.strip() for s in self.SUBSCRIBE_INSTRUMENTS.split(",") if s.strip()]

"""Settings and configuration management."""

from typing import List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class HTTPConfig(BaseModel):
    host: str = '0.0.0.0'
    port: int = 8080


class SMPPServerConfig(BaseModel):
    host: str = '0.0.0.0'
    port: int = 2775
    system_id: str
    password: str


class SMPPClientConfig(BaseModel):
    host: str
    port: int = 2775
    system_id: str
    password: str
    system_type: str = ''


class SMPPConfig(BaseModel):
    servers: List[SMPPServerConfig] = []
    clients: List[SMPPClientConfig] = []


class DeliveryConfig(BaseModel):
    max_retries: int = 3
    retry_delay: int = 5  # seconds
    worker_count: int = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        env_nested_delimiter='__',
        case_sensitive=False,
        env_prefix='porth_',  # This will map PORTH_CONFIG_FILE to config_file
    )

    http: HTTPConfig = HTTPConfig()
    smpp: SMPPConfig = SMPPConfig()
    delivery: DeliveryConfig = DeliveryConfig()

    # Configuration and logging
    config_file: Optional[str] = None
    log_level: str = "INFO"
    debug: bool = False

    @classmethod
    def load_from_file(cls, config_file: str) -> 'Settings':
        """Load settings from YAML config file."""
        import yaml

        with open(config_file, 'r') as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)

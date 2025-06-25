Based on the requirements and focusing on a minimal MVP structure, here's a streamlined project layout that prioritizes core functionality while maintaining extensibility:

````python
```
porth/
├── README.md
├── pyproject.toml                    # UV-based project config
├── uv.lock                          # UV lockfile
├── .python-version                  # Python version for UV
├── docs/
│   └── product/                     # Existing product docs
│       ├── 01-features-outline.md
│       ├── 02-user-stories.md
│       ├── 03-solution-design.md
│       └── 04-delivery-plan.md
├── config/
│   ├── development.yml              # Development config
│   ├── production.yml               # Production config
│   └── test.yml                     # Test config
├── tests/
│   ├── __init__.py
│   ├── conftest.py                  # Pytest configuration
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_core/
│   │   ├── test_protocols/
│   │   └── test_config/
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_smpp_flow.py
│   │   └── test_http_flow.py
│   └── e2e/
│       ├── __init__.py
│       └── test_message_flow.py
├── src/
│   └── porth/
│       ├── __init__.py
│       ├── main.py                  # Application entry point
│       ├── config/
│       │   ├── __init__.py
│       │   ├── settings.py          # Pydantic settings with hot reload
│       │   └── loader.py            # Config loading logic
│       ├── core/
│       │   ├── __init__.py
│       │   ├── message.py           # Message models and types
│       │   ├── queue.py             # Async message queue (asyncio.Queue)
│       │   ├── delivery.py          # Delivery engine with retry logic
│       │   ├── dlr.py               # DLR handling and correlation
│       │   └── exceptions.py        # Core exceptions
│       ├── protocols/
│       │   ├── __init__.py
│       │   ├── base.py              # Protocol handler interface
│       │   ├── smpp/
│       │   │   ├── __init__.py
│       │   │   ├── client.py        # SMPP client (smppai wrapper)
│       │   │   ├── server.py        # SMPP server (smppai wrapper)
│       │   │   └── models.py        # SMPP-specific models
│       │   ├── http/
│       │   │   ├── __init__.py
│       │   │   ├── api.py           # aiohttp REST endpoints
│       │   │   ├── models.py        # Request/response models
│       │   │   └── handlers.py      # HTTP request handlers
│       │   └── kannel/
│       │       ├── __init__.py
│       │       ├── api.py           # Kannel-compatible endpoints
│       │       └── models.py        # Kannel-specific models
│       └── utils/
│           ├── __init__.py
│           ├── async_helpers.py     # Async utilities
│           ├── encoding.py          # SMS encoding (Unicode, GSM)
│           └── constants.py         # Application constants
└── scripts/
    └── dev-setup.sh                 # Development environment setup
```
````

## Key Files Configuration:

### pyproject.toml (UV-based)
````toml
```toml
[project]
name = "porth"
version = "0.1.0"
description = "Next-generation async Python SMS Gateway"
requires-python = ">=3.11"
dependencies = [
    "aiohttp>=3.9.0",
    "pydantic>=2.5.0",
    "pydantic-settings>=2.1.0",
    "smppai @ git+https://github.com/codcod/smppai.git",
    "pyyaml>=6.0",
    "structlog>=23.2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=4.1.0",
    "ruff>=0.1.0",
    "mypy>=1.7.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 88
target-version = "py311"
```
````

### `src/porth/main.py` (Async Application Entry Point)
````python
```python
# filepath: src/porth/main.py
import asyncio
import logging
import signal
from typing import List

from aiohttp import web

from porth.config.settings import Settings
from porth.core.queue import MessageQueue
from porth.core.delivery import DeliveryEngine
from porth.protocols.http.api import create_http_app
from porth.protocols.smpp.server import SMPPServer
from porth.protocols.smpp.client import SMPPClient


class SMSGateway:
    """Main SMS Gateway application."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.message_queue = MessageQueue()
        self.delivery_engine = DeliveryEngine(self.message_queue, settings)
        self.servers: List[web.BaseRunner] = []
        self.smpp_servers: List[SMPPServer] = []
        self.smpp_clients: List[SMPPClient] = []
        
    async def start(self):
        """Start all gateway components."""
        # Start delivery engine
        await self.delivery_engine.start()
        
        # Start HTTP API server
        http_app = create_http_app(self.message_queue, self.settings)
        http_runner = web.AppRunner(http_app)
        await http_runner.setup()
        
        http_site = web.TCPSite(
            http_runner, 
            self.settings.http.host, 
            self.settings.http.port
        )
        await http_site.start()
        self.servers.append(http_runner)
        
        # Start SMPP servers (if configured)
        for smpp_config in self.settings.smpp.servers:
            smpp_server = SMPPServer(smpp_config, self.message_queue)
            await smpp_server.start()
            self.smpp_servers.append(smpp_server)
            
        # Start SMPP clients (if configured)
        for client_config in self.settings.smpp.clients:
            smpp_client = SMPPClient(client_config, self.delivery_engine)
            await smpp_client.connect()
            self.smpp_clients.append(smpp_client)
            
        logging.info("SMS Gateway started successfully")
        
    async def stop(self):
        """Stop all gateway components."""
        # Stop SMPP clients
        for client in self.smpp_clients:
            await client.disconnect()
            
        # Stop SMPP servers
        for server in self.smpp_servers:
            await server.stop()
            
        # Stop HTTP servers
        for runner in self.servers:
            await runner.cleanup()
            
        # Stop delivery engine
        await self.delivery_engine.stop()
        
        logging.info("SMS Gateway stopped")


async def main():
    """Main entry point."""
    settings = Settings()
    gateway = SMSGateway(settings)
    
    # Setup signal handlers for graceful shutdown
    def signal_handler():
        asyncio.create_task(gateway.stop())
        
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, f: signal_handler())
    
    try:
        await gateway.start()
        # Keep running until shutdown
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await gateway.stop()


if __name__ == "__main__":
    asyncio.run(main())
```
````

### `src/porth/config/settings.py` (Hot-reloadable Config)
````python
```python
# filepath: src/porth/config/settings.py
from typing import List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class HTTPConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class SMPPServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 2775
    system_id: str
    password: str


class SMPPClientConfig(BaseModel):
    host: str
    port: int = 2775
    system_id: str
    password: str
    system_type: str = ""


class SMPPConfig(BaseModel):
    servers: List[SMPPServerConfig] = []
    clients: List[SMPPClientConfig] = []


class DeliveryConfig(BaseModel):
    max_retries: int = 3
    retry_delay: int = 5  # seconds
    worker_count: int = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False
    )
    
    http: HTTPConfig = HTTPConfig()
    smpp: SMPPConfig = SMPPConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    
    # Hot reload support
    config_file: Optional[str] = None
    
    @classmethod
    def load_from_file(cls, config_file: str) -> "Settings":
        """Load settings from YAML config file."""
        import yaml
        
        with open(config_file, 'r') as f:
            config_data = yaml.safe_load(f)
            
        return cls(**config_data)
```
````

### `src/porth/core/message.py` (Core Message Models)
````python
```python
# filepath: src/porth/core/message.py
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class MessageStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXPIRED = "expired"


class MessageType(str, Enum):
    SMS = "sms"
    DELIVERY_RECEIPT = "delivery_receipt"


class SMSMessage(BaseModel):
    """Core SMS message model."""
    
    # Message identification
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None
    
    # Message content
    source_addr: str
    destination_addr: str
    message_text: str
    
    # Message properties
    message_type: MessageType = MessageType.SMS
    status: MessageStatus = MessageStatus.PENDING
    
    # Protocol information
    protocol: str  # "smpp", "http", "kannel"
    protocol_data: Dict[str, Any] = Field(default_factory=dict)
    
    # Timing
    created_at: datetime = Field(default_factory=datetime.utcnow)
    scheduled_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    
    # Retry information
    retry_count: int = 0
    max_retries: int = 3
    
    # DLR information
    dlr_requested: bool = True
    dlr_url: Optional[str] = None
    
    class Config:
        use_enum_values = True


class DeliveryReceipt(BaseModel):
    """Delivery receipt model."""
    
    original_message_id: str
    delivery_status: str
    delivery_time: datetime = Field(default_factory=datetime.utcnow)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    
    # Protocol-specific data
    protocol_data: Dict[str, Any] = Field(default_factory=dict)
```
````

### `src/porth/core/queue.py` (Async Message Queue)
````python
```python
# filepath: src/porth/core/queue.py
import asyncio
import logging
from typing import Optional
from porth.core.message import SMSMessage

logger = logging.getLogger(__name__)


class MessageQueue:
    """Async message queue for SMS messages."""
    
    def __init__(self, maxsize: int = 0):
        self._queue: asyncio.Queue[SMSMessage] = asyncio.Queue(maxsize=maxsize)
        self._running = False
        
    async def put(self, message: SMSMessage) -> None:
        """Add a message to the queue."""
        try:
            await self._queue.put(message)
            logger.debug(f"Message {message.message_id} added to queue")
        except asyncio.QueueFull:
            logger.error(f"Queue full, cannot add message {message.message_id}")
            raise
            
    async def get(self, timeout: Optional[float] = None) -> SMSMessage:
        """Get a message from the queue."""
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            logger.debug(f"Message {message.message_id} retrieved from queue")
            return message
        except asyncio.TimeoutError:
            raise
            
    def task_done(self) -> None:
        """Mark a task as done."""
        self._queue.task_done()
        
    async def join(self) -> None:
        """Wait until all tasks are done."""
        await self._queue.join()
        
    def qsize(self) -> int:
        """Return the queue size."""
        return self._queue.qsize()
        
    def empty(self) -> bool:
        """Return True if queue is empty."""
        return self._queue.empty()
```
````

### `src/porth/protocols/base.py` (Protocol Handler Interface)
````python
```python
# filepath: src/porth/protocols/base.py
from abc import ABC, abstractmethod
from typing import Any, Dict
from porth.core.message import SMSMessage


class ProtocolHandler(ABC):
    """Base class for protocol handlers."""
    
    @abstractmethod
    async def start(self) -> None:
        """Start the protocol handler."""
        pass
        
    @abstractmethod
    async def stop(self) -> None:
        """Stop the protocol handler."""
        pass
        
    @abstractmethod
    async def send_message(self, message: SMSMessage) -> Dict[str, Any]:
        """Send a message via this protocol."""
        pass
        
    @abstractmethod
    async def handle_delivery_receipt(self, receipt_data: Dict[str, Any]) -> None:
        """Handle a delivery receipt."""
        pass
```
````

This minimal structure provides:

1. **MVP Focus**: Only essential components for Phase 1 delivery
2. **Async-first**: All core components use async/await
3. **Clean Protocol Separation**: Each protocol in its own module with unified interface
4. **Hot-reloadable Config**: Pydantic-based settings with file support
5. **UV Integration**: Modern Python tooling with uv
6. **Extensibility**: Clear interfaces for future plugins/webhooks
7. **Testing Structure**: Organized test hierarchy

The structure omits the out-of-scope items (security, observability, Docker, etc.) while maintaining clear extension points for future phases. Each protocol handler can be developed independently, and the core messaging pipeline is protocol-agnostic.





---

Full project structure:

````python
```
porth/
├── README.md
├── pyproject.toml                    # Modern Python packaging with dependencies
├── Dockerfile
├── docker-compose.yml               # For local development
├── .github/
│   └── workflows/
│       ├── ci.yml                   # CI/CD pipeline
│       ├── security.yml             # Security scanning
│       └── release.yml              # Release automation
├── docs/
│   ├── product/                     # Existing product docs
│   ├── api/                         # API documentation
│   ├── deployment/                  # Deployment guides
│   └── development/                 # Development setup
├── scripts/
│   ├── setup-dev.sh                # Development environment setup
│   ├── run-tests.sh                # Test runner
│   └── generate-certs.sh           # TLS certificate generation
├── kubernetes/
│   ├── base/                        # Base K8s manifests
│   ├── overlays/                    # Environment-specific overlays
│   └── helm/                        # Helm chart
├── tests/
│   ├── unit/                        # Unit tests
│   ├── integration/                 # Integration tests
│   ├── e2e/                         # End-to-end tests
│   ├── load/                        # Load testing
│   ├── conftest.py                  # Pytest configuration
│   └── fixtures/                    # Test fixtures
├── src/
│   └── porth/                       # Main package
│       ├── __init__.py
│       ├── main.py                  # Application entry point
│       ├── config/
│       │   ├── __init__.py
│       │   ├── settings.py          # Pydantic settings
│       │   ├── loader.py            # Config loading/reloading
│       │   └── schema.py            # Config validation schema
│       ├── protocols/
│       │   ├── __init__.py
│       │   ├── base.py              # Protocol handler interface
│       │   ├── smpp/
│       │   │   ├── __init__.py
│       │   │   ├── client.py        # SMPP client (smppai wrapper)
│       │   │   ├── server.py        # SMPP server (smppai wrapper)
│       │   │   ├── handlers.py      # Message handlers
│       │   │   └── models.py        # SMPP message models
│       │   ├── http/
│       │   │   ├── __init__.py
│       │   │   ├── api.py           # FastAPI REST endpoints
│       │   │   ├── models.py        # Pydantic request/response models
│       │   │   ├── dependencies.py  # FastAPI dependencies (auth, etc.)
│       │   │   └── middleware.py    # Custom middleware
│       │   └── kannel/
│       │       ├── __init__.py
│       │       ├── api.py           # Kannel-compatible endpoints
│       │       ├── config_mapper.py # Kannel config migration
│       │       └── models.py        # Kannel-specific models
│       ├── core/
│       │   ├── __init__.py
│       │   ├── message.py           # Message models and types
│       │   ├── queue.py             # Async message queue
│       │   ├── delivery/
│       │   │   ├── __init__.py
│       │   │   ├── engine.py        # Delivery engine
│       │   │   ├── retry.py         # Retry logic and backoff
│       │   │   └── router.py        # Message routing
│       │   ├── dlr/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py       # DLR processing
│       │   │   └── correlation.py   # Message correlation
│       │   └── sms/
│       │       ├── __init__.py
│       │       ├── encoding.py      # Unicode/GSM encoding
│       │       ├── concatenation.py # Multi-part SMS handling
│       │       └── validation.py    # SMS validation
│       ├── security/
│       │   ├── __init__.py
│       │   ├── auth.py              # Authentication providers
│       │   ├── tls.py               # TLS configuration
│       │   ├── rate_limiting.py     # Async rate limiter
│       │   └── permissions.py       # Authorization logic
│       ├── observability/
│       │   ├── __init__.py
│       │   ├── metrics.py           # Prometheus metrics
│       │   ├── logging.py           # Structured logging setup
│       │   ├── tracing.py           # OpenTelemetry tracing
│       │   └── health.py            # Health check endpoints
│       ├── plugins/
│       │   ├── __init__.py
│       │   ├── manager.py           # Plugin manager
│       │   ├── hooks.py             # Plugin hook definitions
│       │   ├── base.py              # Base plugin interface
│       │   └── examples/            # Example plugins
│       │       ├── __init__.py
│       │       ├── logger_plugin.py
│       │       └── transformer_plugin.py
│       ├── webhooks/
│       │   ├── __init__.py
│       │   ├── manager.py           # Webhook manager
│       │   ├── client.py            # Async webhook client
│       │   └── models.py            # Webhook event models
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py              # Storage interface
│       │   ├── memory.py            # In-memory storage
│       │   ├── redis.py             # Redis storage
│       │   └── models.py            # Storage models
│       └── utils/
│           ├── __init__.py
│           ├── async_helpers.py     # Async utilities
│           ├── validators.py        # Common validators
│           ├── exceptions.py        # Custom exceptions
│           └── constants.py         # Application constants
├── config/
│   ├── development.yml              # Development config
│   ├── production.yml               # Production config
│   ├── test.yml                     # Test config
│   └── examples/
│       ├── smpp_providers.yml       # SMPP provider examples
│       └── kannel_migration.yml     # Kannel migration example
└── migrations/                      # Database migrations (if needed)
    └── kannel/
        ├── config_parser.py         # Kannel config parser
        └── data_migrator.py         # Data migration tools
```
````

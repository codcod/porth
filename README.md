# Porth SMS Gateway

A next-generation async Python SMS Gateway designed for high-performance, cloud-native telecommunications infrastructure.

## Overview

Porth is a modern, async-first SMS Gateway that provides reliable message delivery through multiple protocols including SMPP, HTTP/REST APIs, and Kannel compatibility. Built with Python 3.13+ and designed for production-scale telecom operations.

## Key Features

- **Async-First Architecture**: Built on asyncio for high concurrency and performance
- **Multi-Protocol Support**: SMPP v3.4, HTTP/REST APIs, and Kannel-compatible endpoints
- **Production Ready**: Message queuing, retry logic, delivery receipts (DLR), and comprehensive error handling
- **Cloud Native**: Containerized deployment with Kubernetes support
- **Unicode & Concatenated SMS**: Full support for international messaging and long messages
- **Hot-Reloadable Configuration**: YAML-based configuration with runtime updates

## Quick Start

### Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd porth

# Setup development environment
make dev

# Run the application
make dev-run
```

### Basic Usage

```bash
# Start the SMS Gateway
make run

# Health check
curl http://localhost:8080/health

# Send SMS via REST API
curl -X POST http://localhost:8080/api/v1/sms \
  -H "Content-Type: application/json" \
  -d '{
    "source_addr": "+1234567890",
    "destination_addr": "+0987654321",
    "message_text": "Hello from Porth!"
  }'
```

## Development

```bash
# Install development dependencies
make dev-install

# Run tests
make test

# Code quality checks
make check

# Format code
make format

# See all available commands
make help
```

## Configuration

Configure the gateway using YAML files in the `config/` directory:

```yaml
# config/development.yml
http:
  host: "0.0.0.0"
  port: 8080

smpp:
  servers:
    - host: "0.0.0.0"
      port: 2775
      system_id: "test"
      password: "test"

delivery:
  max_retries: 3
  retry_delay: 5
  worker_count: 10
```

## Architecture

- **Protocol Handlers**: Modular SMPP, HTTP, and Kannel protocol support
- **Message Queue**: Async message queuing with configurable backends
- **Delivery Engine**: Intelligent routing, retry logic, and delivery management
- **DLR Processing**: Comprehensive delivery receipt handling and correlation


# Porth SMS Gateway

An async Python SMS Gateway designed for high-performance, cloud-native
telecommunications infrastructure. Serves a Kannel-compatible `sendsms` API, so existing
Kannel HTTP clients can switch to porth unmodified by changing only the host (port 13013).

The user manual (`docs/`, built with `make docs-build`) is attached as PDF and EPUB to every
GitHub release.

### Installation

```bash
# Clone the repository
git clone git@github.com:codcod/porth.git
cd porth

# Setup development environment
make dev

# PostgreSQL (Docker) and porth's tables
make db-up
make migrate

# Run the application (config/config.toml)
make run
```

### Basic Usage

```bash
# Start the SMS Gateway
make run

# Health check
curl http://localhost:8080/health

# Send SMS via REST API
curl -X POST http://localhost:8080/api/v1/sms/send \
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

Configure the gateway in one TOML file, `config/config.toml` (the path is the command's
argument). Unknown keys and wrong types stop startup:

```toml
[porth]
db = "postgresql+asyncpg://porth:porth@localhost:5432/porth"
log_level = "INFO"

[porth.http]
host = "0.0.0.0"
port = 8080

[porth.smsc.op-a]
host = "smsc.op-a.example"
port = 2775
system_id = "porth"
password = "secret"
throughput = 50  # submit_sm PDUs/s; unset = unlimited

[porth.smsc.op-b]
host = "smsc.op-b.example"
system_id = "porth"
password = "secret-b"

[porth.routing]
default = "op-a"

[porth.routing.prefixes]
"4470" = "op-b"

[porth.delivery]
max_retries = 3
retry_delay = 5
backoff_factor = 2
max_retry_delay = 300
worker_count = 10
```

Each `[porth.smsc.<name>]` is an upstream SMSC. porth binds to each as a transceiver, with
its own queue, workers and `throughput` cap. A message goes out through the SMSC of the
longest `[porth.routing.prefixes]` entry its number starts with (without a leading `+`), else
the `default`; a number nothing routes is refused at submit. It is sent as one `submit_sm`
per part, and marked `sent` once the SMSC accepts every part. If the SMSC is unreachable, the gateway still starts and retries the bind every
10 s. Text longer than one SMS (160 GSM-7 characters, where extension characters such as `€`
count as two, or 70 UCS-2 characters) is sent as a concatenated SMS of up to 255 parts.

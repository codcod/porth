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

Configure the gateway using YAML files in the `config/` directory:

```yaml
# config/development.yml
http:
  host: "0.0.0.0"
  port: 8080

smpp:
  clients:
    - host: "smsc.example.com"
      port: 2775
      system_id: "porth"
      password: "secret"

delivery:
  max_retries: 3
  retry_delay: 5
  worker_count: 10
```

`smpp.clients` holds the single upstream SMSC bind. porth binds to it as a transceiver and
sends every message there as one `submit_sm` per part. A message is marked `sent` once the
SMSC accepts every part. If no client is configured, messages end `failed` after their
retries. If the SMSC is unreachable, the gateway still starts and the next send retries the
bind. Text longer than one SMS (160 GSM-7 characters, where extension characters such as `€`
count as two, or 70 UCS-2 characters) is sent as a concatenated SMS of up to 255 parts.

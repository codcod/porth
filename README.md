# Porth SMS Gateway

An async SMS gateway that sits between clients and an SMS center (
[SMSC](https://en.wikipedia.org/wiki/Short_Message_service_center), [example
SMSC
provider](https://biznes.t-mobile.pl/pl/produkty-i-uslugi/telefonia-i-komunikacja/bezposredni-dostep-do-smsc-mmsc)
).

Porth takes messages over a REST API or a Kannel-compatible `sendsms` API,
routes each by number prefix to a SMSC, sends it over Short Message Peer-to-Peer
([SMPP](https://smpp.org)) protocol with
[smppai](https://github.com/codcod/smppai), retries on failure, and reports
delivery receipts back by callback.

Existing Kannel clients can switch to Porth by changing only the host (port
13013). The user manual's *Porth and Kannel* chapter lists the differences.

The [user manual](https://github.com/codcod/porth/releases) is attached as PDF
and EPUB to every GitHub release (build it locally with `make docs-build`).

## Try it

No carrier needed: `compose-demo-simple.yaml` runs Porth against Smppai's simulator
SMSC, which answers every message with a delivery receipt. The steps below
use `jq`.

```bash
git clone https://github.com/codcod/porth.git && cd porth
docker compose -f compose-demo-simple.yaml up -d
until curl -sf localhost:8080/ready; do sleep 1; done

# Send, then poll: "status": "delivered" a second later
id=$(curl -s -X POST localhost:8080/api/v1/sms/send \
    -H 'Content-Type: application/json' \
    -d '{"from_number":"ACME","to_number":"306900000000","message":"hello"}' \
    | jq -r .message_id)
curl -s localhost:8080/api/v1/sms/status/$id

# An MO from the simulator: porth logs "MO from ***000 dropped: mo.url is not set"
curl -s -X POST 'localhost:2776/mo?from=306900000000&to=ACME&text=hi'
docker compose -f compose-demo-simple.yaml logs porth | grep 'MO from'

docker compose -f compose-demo-simple.yaml down -v
```

The user manual's *Try it without a carrier* covers failed receipts and
forwarding MOs.

`compose-demo-advanced.yaml` goes further: three simulated SMSCs (`smsc-t-mobile`
for Poland, `smsc-orange-1` and `smsc-orange-2` for the rest, each other's fallback),
Prometheus and Grafana with the shipped dashboard, and k6 load over REST and
Kannel (`make perf-advanced`, `make perf-kannel`). See the manual's *Try the
advanced demo*.

`compose-demo-auth.yaml` is the simple demo with users on both APIs: a Bearer
token on `/api/v1` and a username/password on Kannel's `sendsms`. Its header
lists the curl calls that are refused and accepted.

## How it works

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant API as Porth REST API
    participant DB as Message store
    participant Q as SMSC queue
    participant W as Delivery worker
    participant S as SMSC

    Note over W,S: At startup, per configured SMSC
    W->>S: bind_transceiver (or bind_transmitter + bind_receiver)
    S-->>W: bind_resp
    loop while bound
        W->>S: enquire_link
        S-->>W: enquire_link_resp
    end

    C->>API: POST /api/v1/sms/send
    API->>API: validate, route by longest prefix
    alt no route / invalid
        API-->>C: 400
    else store unavailable
        API-->>C: 503
    end
    API->>DB: store message (queued)
    API->>Q: enqueue
    API-->>C: 200 {message_id, status: queued}

    Q->>W: dequeue
    loop each SMS part
        W->>S: submit_sm
        S-->>W: submit_sm_resp (smsc message_id)
    end
    alt every part accepted
        W->>DB: status = sent, store SMSC ids
    else transient error / bind lost
        W->>Q: requeue after backoff (rebind every reconnect_delay, 10 s)
    else permanent error or retries exhausted
        W->>DB: status = failed
    end

    S->>W: deliver_sm (delivery receipt)
    W-->>S: deliver_sm_resp
    W->>DB: status = delivered (once all parts delivered)
    opt callback_url set
        W->>C: POST callback_url {status}
    end

    C->>API: GET /api/v1/sms/status/{message_id}
    API->>DB: read
    API-->>C: {status: delivered}
```

The Kannel API follows the same path; its `dlr-url` gets a GET instead of the
JSON POST.

## Installation

```bash
git clone https://github.com/codcod/porth.git && cd porth
make dev

# PostgreSQL (Docker) and porth's tables; on SQLite skip both (see the manual)
make db-up
make migrate

# Runs bin/porth config/config.toml
make run
```

Prebuilt images are at `ghcr.io/codcod/porth:<version>`; or build one with
`make docker-build`. The user manual's *Container image* has the migrate and run
commands.

## Configuration

Porth reads one TOML file, passed as `bin/porth <path>`; `make run` uses
`config/config.toml`. Unknown keys and wrong types stop startup:

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

- **Binding.** Each `[porth.smsc.<name>]` is an upstream SMSC. Porth binds to
  each as a transceiver (or, with `transceiver = false`, a transmitter and a
  receiver), with its own queue, workers and `throughput` cap.
- **Routing.** A message goes out through the SMSC of the longest
  `[porth.routing.prefixes]` entry its number starts with (digits only), else
  the `default`. A number nothing routes is refused at submit.
- **Sending.** A message is sent as one `submit_sm` per part, and marked `sent`
  once the SMSC accepts every part.
- **SMSC unreachable.** The gateway still starts and retries the bind every
  `reconnect_delay` (10 s by default).
- **Long messages.** Text longer than one SMS (160 GSM-7 characters, where
  extension characters such as `€` count as two, or 70 UCS-2 characters) is
  sent as a concatenated SMS of up to 255 parts.

## Usage

```bash
curl -s localhost:8080/health

# REST API
curl -s -X POST localhost:8080/api/v1/sms/send \
    -H 'Content-Type: application/json' \
    -d '{"from_number":"ACME","to_number":"306900000000","message":"hello"}'

# Kannel-compatible API
curl -s 'localhost:13013/cgi-bin/sendsms?from=ACME&to=306900000000&text=hello'
```

The REST API also accepts `source_addr`, `destination_addr` and `message_text`
as aliases for `from_number`, `to_number` and `message`.

## Development

```bash
make dev
make test
make check
make format
make help   # every target
```

## Code layout

The code under `src/porth/` uses the layered layout described in
[monolith](https://github.com/codcod/monolith):
* `domain/` holds the message model, with no I/O;
* `adapters/` holds the database engine, tables and repository, the SMPP client,
  the in-process queue, and the outbound HTTP client for DLR and MO calls;
* `service_layer/` holds the unit of work, the delivery engine, the receipt, MO
  and sweep handlers, routing, and the `submit` and `recover` services;
* `entrypoints/` holds the REST and Kannel aiohttp apps and `build_app()`, which
  `bin/porth` runs.

Tests are grouped into `tests/unit`, `tests/e2e`, `tests/integration`
(PostgreSQL or SQLite, `RUN_INTEGRATION_TESTS=1`) and `tests/performance/k6`.

## License

Porth is licensed under the [GNU Affero General Public License v3.0 or later](LICENSE).
Its source code is available at <https://github.com/codcod/porth>. If you run a
modified porth as a network service, section 13 of the license requires you to
offer your modified source to its users.

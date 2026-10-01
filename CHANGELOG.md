# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

**Added**

- Messages are sent to the upstream SMSC as a real `submit_sm` over one smppai transceiver
  bind. GSM 03.38 text goes out as `data_coding` 0 and anything else as UCS-2. A failed
  transport drops the bind, and the next send rebinds (POR-001).
- User manual (AsciiDoc, built with snowball), attached as PDF and EPUB to the GitHub release
  of every version tag (POR-007).
- The Kannel-compatible `GET /cgi-bin/sendsms` endpoint, on its own port `kannel.port`
  (default 13013, smsbox's default). `to` and `text` are required; `username` and `password`
  are ignored (POR-008). `to` may list several space-separated numbers, one message each; a
  repeated number is sent once, and one with a character outside `0123456789+-` is dropped.
  `from` falls back to `kannel.default_sender` (POR-016).
- `GET /api/v1/sms/status/{message_id}` returns the message's real status and timestamps from
  the message store, and `404` for an unknown id. Before, it answered `pending` for
  any id (POR-009).
- Text longer than one SMS is sent as a concatenated SMS (UDH), up to 255 parts. If a part
  fails, the whole message is retried (POR-006).
- Delivery receipts from the SMSC move a message to `delivered` (once every part is
  delivered), `failed` or `expired`, which the HTTP status poll shows. A Kannel message with
  a `dlr-url` and a matching `dlr-mask` (1 delivered, 2 not delivered) gets it fetched once,
  with `%d`, `%I`, `%F`, `%A`, `%t` and `%T` substituted, in up to 3 attempts (POR-002).
- Mobile-originated SMS is forwarded once to `mo.url` (Kannel's `get-url`), with `%p`, `%P`,
  `%k`, `%r`, `%a`, `%b`, `%t`, `%T`, `%c` and `%C` substituted as Kannel 1.4.5 does. A
  non-blank `text/plain` `200` or `202` answer, stripped of surrounding whitespace, is sent back to the subscriber as an SMS unless
  `mo.reply` is off (POR-015).
- Messages, their status, receipt correlation and pending `dlr-url` calls persist in
  PostgreSQL (schema `porth`) across restart. A message is stored before it is accepted; on
  startup, messages not yet sent are queued again and unfinished `dlr-url` calls are made
  again. Sent messages are not resent. The database is the new, required `db` setting
  (`PORTH_DB`); `make db-up` starts one and `make migrate` creates its tables (POR-018).
- An SMSC's `throughput` caps its `submit_sm` PDUs in any one-second window, spaced evenly,
  across its delivery workers, as Kannel's per-SMSC `throughput`; a concatenated message
  counts once per part. Unset keeps today's unlimited rate (POR-014, POR-024, POR-020).
- porth binds to several SMSCs, each a `[porth.smsc.<name>]` table, and routes each message
  once at submit: to the SMSC a Kannel client names with `smsc`, else the longest matching
  destination prefix in `[porth.routing.prefixes]` (matched on the number's digits only),
  else `routing.default`. Each SMSC has its own queue, workers and bind, so a slow or down
  SMSC holds up only its own messages. Receipts correlate by SMSC and id, an MO reply leaves
  through the SMSC the MO arrived on, and `%i` in `mo.url` and `dlr-url` is the SMSC's name
  (POR-020).
- `python -m porth.main --check <file>`, which `make config-check` now runs, validates a
  configuration as startup does, routing rules and each SMSC's `throughput` included,
  without connecting to anything (POR-020).

**Changed**

- **Breaking:** configuration is one TOML file, `config/config.toml` by default, its path
  the command's argument (`python -m porth.main [config]`): the `[porth]` table, validated
  strictly (unknown key, wrong type, missing required key, unknown `log_level`). The YAML
  files, `PORTH_*` variables and `.env` are no longer read. `smpp.clients` became the single
  `[porth.smpp.client]` table. `log_level` takes effect, and `make migrate` reads `db` from the
  same file (POR-012).
- Python 3.13 or later is required (POR-018).
- **Breaking:** `[porth.smpp.client]` became `[porth.smsc.<name>]`, and `delivery.throughput`
  became each SMSC's `throughput`. A number no SMSC takes is refused at submit: Kannel's
  `403 Not routable. Do not try again.` when no number in the request routes (others are
  dropped), the HTTP API's `400`. `delivery.worker_count` is per SMSC (POR-020).
- Addresses carry their SMPP TON/NPI: `+<digits>` is sent as international/ISDN without the
  `+`, and a non-numeric sender as alphanumeric. Before, every address went out as given with
  TON/NPI 0 (POR-011).
- An unknown configuration key is an error at startup (POR-011).
- smppai is upgraded from 0.2.8 to 0.9.1 (POR-011).
- Every message requests a delivery receipt (`registered_delivery = 1`), not only Kannel
  messages and HTTP messages with `dlr_url` (POR-009).
- The HTTP API rejects a body with a non-empty `dlr_url` with `400`. Status is polled, never
  pushed. Before, `dlr_url` was accepted and no callback was ever sent (POR-009).
- The SMPP bind is retried in the background every 10 s while it is down, including after a
  failed startup bind. Before, only the next send rebound it (POR-013).
- A failed send is retried after a wait that grows by `delivery.backoff_factor` (default 2)
  per attempt, up to `delivery.max_retry_delay` (default 300 s). Before, every retry waited
  `delivery.retry_delay` (POR-010).
- Each message waits for its retry on its own. Before, one retry worker took failed messages
  one at a time, so N failures took about N × `retry_delay` to drain (POR-010).
- An SMSC rejection with a permanent error status, such as an invalid destination, fails the
  message at once. Only `ESME_RTHROTTLED`, `ESME_RMSGQFUL`, `ESME_RX_T_APPN` and
  `ESME_RSYSERR` are retried, as in Kannel. A timeout, a dropped connection, a refused bind or
  `ESME_RINVBNDSTS` (the SMSC lost the session; porth rebinds) is still retried (POR-010).
- A destination number is matched against routing prefixes on its digits only: `+`, dashes
  and spaces are dropped, so `+30-694-1234567` routes like `306941234567`. Kannel matches the
  number as written. Log lines about an SMSC's bind, inbound stream or delivery engine start
  with `SMSC <name>:`, and its workers are named `<name>/worker-<n>` (POR-029).

**Removed**

- The `smpp.servers` setting and its inert SMPP server stub. porth has no SMSC role
  (design.md §2), and a config that still sets `smpp.servers` now fails to load (POR-011).
- Unused config hot-reload code and the `docker-*`, `db-migrate`, `logs` and `test-e2e`
  Makefile targets (POR-011).
- The `pydantic`, `pydantic-settings`, `structlog` and `watchdog` dependencies. Settings and
  request validation use the standard library (POR-011).
- The `debug` setting (it had no effect), the `pyyaml` dependency, and the `dev-run`,
  `create-env`, `config-dev` and `config-test` Makefile targets (POR-012).

**Fixed**

- A part's delivery receipt that arrives before the message's later parts are submitted is
  looked up again for about 15 s instead of being ignored, so the message still becomes
  `delivered` (POR-013).
- Receipts the SMSC holds after a lost or SMSC-unbound bind arrive without new traffic: the
  bind is reopened in the background (POR-013).
- Receipts already received when porth closes a bind, after a send timeout or at shutdown,
  are applied instead of dropped (POR-013).
- `%%` in a Kannel `dlr-url` is a literal `%`, as in Kannel, so percent-encoded bytes can be
  kept intact as `%%C3%%A9` (POR-013).
- An SMSC named `""` is refused at startup instead of being silently skipped by routing, and
  a `throughput` below 1 names its key: `invalid porth.smsc.<name>.throughput` (POR-029).

## [0.1.0] - 2026-09-22

**Added**

- Initial documented baseline: an async SMS gateway skeleton with an HTTP/REST submit API, an
  in-process queue with retries, and an SMPP client (via `smppai`). This baseline did not yet
  send over SMPP, serve the Kannel-compatible API, poll message status, or route DLRs. See
  `development/porth/design.md` for the target MVP scope.

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
  (default 13013, smsbox's default). `to` (one recipient), `from` and `text` are required;
  `username` and `password` are ignored (POR-008).
- `GET /api/v1/sms/status/{message_id}` returns the message's real status and timestamps from
  an in-memory message store, and `404` for an unknown id. Before, it answered `pending` for
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

**Changed**

- Addresses carry their SMPP TON/NPI: `+<digits>` is sent as international/ISDN without the
  `+`, and a non-numeric sender as alphanumeric. Before, every address went out as given with
  TON/NPI 0 (POR-011).
- An unknown configuration key, in the YAML file or as a `PORTH_` variable, is an error at
  startup (POR-011).
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

**Removed**

- The `smpp.servers` setting and its inert SMPP server stub. porth has no SMSC role
  (design.md §2), and a config that still sets `smpp.servers` now fails to load (POR-011).
- Unused config hot-reload code and the `docker-*`, `db-migrate`, `logs` and `test-e2e`
  Makefile targets (POR-011).
- The `pydantic`, `pydantic-settings`, `structlog` and `watchdog` dependencies. Settings and
  request validation use the standard library (POR-011).

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

## [0.1.0] - 2026-09-22

**Added**

- Initial documented baseline: an async SMS gateway skeleton with an HTTP/REST submit API, an
  in-process queue with retries, and an SMPP client (via `smppai`). This baseline did not yet
  send over SMPP, serve the Kannel-compatible API, poll message status, or route DLRs. See
  `development/porth/design.md` for the target MVP scope.

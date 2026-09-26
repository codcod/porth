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

**Removed**

- The `smpp.servers` setting and its inert SMPP server stub. porth has no SMSC role
  (design.md §2), and a config that still sets `smpp.servers` now fails to load (POR-011).
- Unused config hot-reload code and the `docker-*`, `db-migrate`, `logs` and `test-e2e`
  Makefile targets (POR-011).
- The `pydantic`, `pydantic-settings`, `structlog` and `watchdog` dependencies. Settings and
  request validation use the standard library (POR-011).

## [0.1.0] - 2026-09-22

**Added**

- Initial documented baseline: an async SMS gateway skeleton with an HTTP/REST submit API, an
  in-process queue with retries, and an SMPP client (via `smppai`). This baseline did not yet
  send over SMPP, serve the Kannel-compatible API, poll message status, or route DLRs. See
  `development/porth/design.md` for the target MVP scope.

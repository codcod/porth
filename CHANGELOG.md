# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2026-09-22

**Added**

- Initial documented baseline: async SMS gateway with SMPP client egress (via `smppai`), an
  HTTP/REST API, and a Kannel-compatible HTTP API, backed by an in-process queue with
  retry/backoff and DLR correlation back to the originating client. See
  `development/porth/design.md` for the full MVP scope.

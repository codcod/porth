# Graph Report - porth  (2026-09-22)

## Corpus Check
- Corpus is ~5,422 words - fits in a single context window. You may not need a graph.

## Summary
- 341 nodes · 501 edges · 35 communities (18 shown, 2 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 45 edges (avg confidence: 0.92)
- Token cost: 58,501 input · 0 output

## Community Hubs (Navigation)
- Message & Delivery Core Models
- HTTP/REST API
- Async Message Queue
- Config Loading & Hot Reload
- Env Config, CI & Repo Rationale
- Delivery Engine Worker
- Async Helper Utilities
- DLR Handling & Correlation
- SMS Encoding (GSM/Unicode)
- Kannel-Compatible API
- SMPP Client
- Settings & SMPP Server Stub
- Domain Exceptions
- Pytest Fixtures
- SMPP PDU Models
- E2E Message Flow Test
- HTTP Integration Test
- SMPP Integration Test
- App Constants
- Package Root

## God Nodes (most connected - your core abstractions)
1. `SMSMessage` - 33 edges
2. `MessageQueue` - 22 edges
3. `DeliveryEngine` - 21 edges
4. `Settings` - 19 edges
5. `SMPPClient` - 16 edges
6. `SMPPServer` - 15 edges
7. `ProtocolHandler` - 12 edges
8. `Porth SMS Gateway (project overview)` - 12 edges
9. `SMSGateway` - 11 edges
10. `create_http_app()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `test_settings()` --uses--> `Settings`  [INFERRED]
  tests/conftest.py → src/porth/config/settings.py
- `Porth SMS Gateway (project overview)` --references--> `Development Configuration (config/development.yml)`  [AMBIGUOUS]
  README.md → config/development.yml
- `message_queue()` --calls--> `MessageQueue`  [EXTRACTED]
  tests/conftest.py → src/porth/core/queue.py
- `uv Python Dependency Updates` --conceptually_related_to--> `uv Package Manager`  [INFERRED]
  .github/dependabot.yml → README.md
- `ruff-pre-commit (ruff, ruff-format)` --conceptually_related_to--> `Makefile Dev Workflow (make dev/test/check/format)`  [INFERRED]
  .pre-commit-config.yaml → README.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Environment Configuration Profiles (dev/prod/test)** — config_development_file, config_production_file, config_test_file, config_delivery_schema [INFERRED 0.85]
- **Porth Gateway Architecture Components** — readme_protocol_handlers, readme_message_queue, readme_delivery_engine, readme_dlr_processing [EXTRACTED 1.00]
- **Repo Dependency & Code-Quality Automation** — pre_commit_config_hooks, pre_commit_config_ruff, github_dependabot_uv_ecosystem [INFERRED 0.75]

## Communities (35 total, 2 thin omitted)

### Community 0 - "Message & Delivery Core Models"
Cohesion: 0.08
Nodes (27): ABC, Delivery engine with retry logic., # TODO: Implement actual message delivery based on protocol, # TODO: Implement DLR sending based on original protocol, Config, MessageStatus, MessageType, Enum (+19 more)

### Community 1 - "HTTP/REST API"
Cohesion: 0.10
Nodes (29): Application, create_http_app(), get_sms_status(), health_check(), Request, Response, HTTP/REST API implementation using aiohttp., Health check endpoint. (+21 more)

### Community 2 - "Async Message Queue"
Cohesion: 0.07
Nodes (18): MessageQueue, Async message queue for SMS messages., Get a message from the queue., Wait until all tasks are done., Return the queue size., Return True if queue is empty., Main application entry point., Main SMS Gateway application. (+10 more)

### Community 3 - "Config Loading & Hot Reload"
Cohesion: 0.10
Nodes (16): BaseSettings, FileSystemEventHandler, Path, ConfigFileHandler, ConfigLoader, Configuration loading and hot-reload functionality., File system event handler for config file changes., Handle file modification events. (+8 more)

### Community 4 - "Env Config, CI & Repo Rationale"
Cohesion: 0.13
Nodes (22): Shared Environment Config Schema (http/smpp/delivery/debug/log_level), Development Configuration (config/development.yml), Production Configuration (config/production.yml), Test Configuration (config/test.yml), codcod Reviewer/Team, Dependabot Configuration, uv Python Dependency Updates, Pre-commit Configuration (+14 more)

### Community 5 - "Delivery Engine Worker"
Cohesion: 0.13
Nodes (11): DeliveryEngine, Any, Process a single message., Simulate message delivery (replace with actual delivery logic)., Handle delivery failure and retry logic., Send delivery receipt to original client., Async delivery engine with retry logic., Start the delivery engine workers. (+3 more)

### Community 6 - "Async Helper Utilities"
Cohesion: 0.12
Nodes (12): Semaphore, async_retry(), decorator(), AsyncTimer, gather_with_semaphore(), Any, Async helper utilities., Decorator for async functions with retry logic. (+4 more)

### Community 7 - "DLR Handling & Correlation"
Cohesion: 0.12
Nodes (13): DLRHandler, Any, Delivery receipt handling and correlation., Delivery receipt handler with message correlation., # TODO: Replace with persistent storage for production, Store message for DLR correlation., Process incoming delivery receipt., # TODO: Send DLR back to original client via appropriate protocol (+5 more)

### Community 8 - "SMS Encoding (GSM/Unicode)"
Cohesion: 0.19
Nodes (16): calculate_sms_parts(), decode_gsm_7bit(), detect_encoding(), encode_gsm_7bit(), encode_sms_text(), Enum, str, SMS encoding utilities for Unicode and GSM. (+8 more)

### Community 9 - "Kannel-Compatible API"
Cohesion: 0.17
Nodes (14): kannel_send_sms(), kannel_status(), Request, Response, Kannel-compatible API implementation., Kannel-compatible SMS send endpoint., Kannel-compatible status endpoint., Config (+6 more)

### Community 10 - "SMPP Client"
Cohesion: 0.16
Nodes (9): Exception, Any, SMPP client for sending messages to SMSC., Start the SMPP client (alias for connect)., Stop the SMPP client (alias for disconnect)., Disconnect from SMSC., Send SMS message via SMPP., Handle delivery receipt from SMSC. (+1 more)

### Community 11 - "Settings & SMPP Server Stub"
Cohesion: 0.22
Nodes (11): DeliveryConfig, HTTPConfig, BaseModel, Settings and configuration management., SMPPClientConfig, SMPPConfig, SMPPServerConfig, SMPP server implementation using smppai. (+3 more)

### Community 12 - "Domain Exceptions"
Cohesion: 0.22
Nodes (12): ConfigurationError, DeliveryError, MessageError, PorthException, ProtocolError, QueueError, Configuration-related errors., Message processing errors. (+4 more)

### Community 13 - "Pytest Fixtures"
Cohesion: 0.28
Nodes (8): fixture, event_loop(), message_queue(), Pytest configuration and fixtures., Create an instance of the default event loop for the test session., Test settings fixture., Message queue fixture., test_settings()

### Community 14 - "SMPP PDU Models"
Cohesion: 0.28
Nodes (8): BaseModel, SMPP-specific models and data structures., SMPP deliver_sm PDU model., SMPP delivery receipt model., SMPP submit_sm PDU model., SMPPDeliverSM, SMPPDeliveryReceipt, SMPPSubmitSM

### Community 15 - "E2E Message Flow Test"
Cohesion: 0.33
Nodes (5): asyncio, End-to-end tests for complete message flow., Test complete message flow from submission to delivery., # TODO: Implement end-to-end test, test_complete_message_flow()

### Community 16 - "HTTP Integration Test"
Cohesion: 0.33
Nodes (5): asyncio, Integration tests for HTTP message flow., Test HTTP message submission and delivery flow., # TODO: Implement HTTP integration test, test_http_message_flow()

### Community 17 - "SMPP Integration Test"
Cohesion: 0.33
Nodes (5): asyncio, Integration tests for SMPP message flow., Test SMPP message submission and delivery flow., # TODO: Implement SMPP integration test, test_smpp_message_flow()

## Ambiguous Edges - Review These
- `Porth SMS Gateway (project overview)` → `Development Configuration (config/development.yml)`  [AMBIGUOUS]
  README.md · relation: references

## Knowledge Gaps
- **7 isolated node(s):** `porth`, `Config`, `Config`, `codcod Reviewer/Team`, `pre-commit-hooks (check-yaml, end-of-file-fixer, trailing-whitespace, check-toml)` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 171 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Porth SMS Gateway (project overview)` and `Development Configuration (config/development.yml)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `SMSMessage` connect `Message & Delivery Core Models` to `HTTP/REST API`, `Async Message Queue`, `Delivery Engine Worker`, `DLR Handling & Correlation`, `Kannel-Compatible API`, `SMPP Client`, `Settings & SMPP Server Stub`?**
  _High betweenness centrality (0.229) - this node is a cross-community bridge._
- **Why does `Settings` connect `Config Loading & Hot Reload` to `Message & Delivery Core Models`, `HTTP/REST API`, `Async Message Queue`, `Delivery Engine Worker`, `Settings & SMPP Server Stub`, `Pytest Fixtures`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Why does `DeliveryEngine` connect `Delivery Engine Worker` to `Message & Delivery Core Models`, `SMPP Client`, `Async Message Queue`, `Config Loading & Hot Reload`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `SMSMessage` (e.g. with `DeliveryEngine` and `DLRHandler`) actually correct?**
  _`SMSMessage` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `MessageQueue` (e.g. with `DeliveryEngine` and `SMSMessage`) actually correct?**
  _`MessageQueue` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `DeliveryEngine` (e.g. with `Settings` and `MessageStatus`) actually correct?**
  _`DeliveryEngine` has 6 INFERRED edges - model-reasoned connections that need verification._
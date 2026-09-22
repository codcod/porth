# Graph Report - porth  (2026-09-22)

## Corpus Check
- 44 files · ~5,618 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 7 file(s) not represented in the graph (top: (none) 6, .lock 1)

## Summary
- 348 nodes · 505 edges · 37 communities (17 shown, 5 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 45 edges (avg confidence: 0.92)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `5439dbd9`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- settings.py
- http/api.py
- SMPPServer
- DeliveryEngine
- Porth SMS Gateway (project overview)
- message.py
- AsyncTimer
- SMSMessage
- encoding.py
- kannel/api.py
- SMPPClient
- Changelog
- PorthException
- MessageQueue
- smpp/models.py
- test_message_flow.py
- test_http_flow.py
- test_smpp_flow.py
- constants.py
- porth
- PACKAGING.md
- RELEASING.md

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
- `Porth SMS Gateway (project overview)` --references--> `Development Configuration (config/development.yml)`  [AMBIGUOUS]
  README.md → config/development.yml
- `test_settings()` --uses--> `Settings`  [INFERRED]
  tests/conftest.py → src/porth/config/settings.py
- `Async-First Architecture (asyncio)` --conceptually_related_to--> `Shared Environment Config Schema (http/smpp/delivery/debug/log_level)`  [INFERRED]
  README.md → config/development.yml
- `uv Python Dependency Updates` --conceptually_related_to--> `uv Package Manager`  [INFERRED]
  .github/dependabot.yml → README.md
- `ruff-pre-commit (ruff, ruff-format)` --conceptually_related_to--> `Makefile Dev Workflow (make dev/test/check/format)`  [INFERRED]
  .pre-commit-config.yaml → README.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Porth Gateway Architecture Components** — readme_protocol_handlers, readme_message_queue, readme_delivery_engine, readme_dlr_processing [EXTRACTED 1.00]
- **Repo Dependency & Code-Quality Automation** — pre_commit_config_hooks, pre_commit_config_ruff, github_dependabot_uv_ecosystem [INFERRED 0.75]
- **Environment Configuration Profiles (dev/prod/test)** — config_development_file, config_production_file, config_test_file, config_delivery_schema [INFERRED 0.85]

## Communities (37 total, 5 thin omitted)

### Community 0 - "settings.py"
Cohesion: 0.08
Nodes (24): ABC, DeliveryConfig, HTTPConfig, BaseModel, Settings and configuration management., SMPPClientConfig, SMPPConfig, SMPPServerConfig (+16 more)

### Community 1 - "http/api.py"
Cohesion: 0.10
Nodes (29): Application, create_http_app(), get_sms_status(), health_check(), Request, Response, HTTP/REST API implementation using aiohttp., Health check endpoint. (+21 more)

### Community 2 - "SMPPServer"
Cohesion: 0.13
Nodes (9): Start all gateway components., Any, SMPP server for receiving messages from ESMEs., Start the SMPP server., Stop the SMPP server., Send message (not applicable for server)., Handle delivery receipt (not applicable for server)., Handle incoming submit_sm PDU from ESME. (+1 more)

### Community 3 - "DeliveryEngine"
Cohesion: 0.06
Nodes (30): BaseSettings, FileSystemEventHandler, Path, ConfigFileHandler, ConfigLoader, Configuration loading and hot-reload functionality., File system event handler for config file changes., Handle file modification events. (+22 more)

### Community 4 - "Porth SMS Gateway (project overview)"
Cohesion: 0.13
Nodes (22): Shared Environment Config Schema (http/smpp/delivery/debug/log_level), Development Configuration (config/development.yml), Production Configuration (config/production.yml), Test Configuration (config/test.yml), codcod Reviewer/Team, Dependabot Configuration, uv Python Dependency Updates, Pre-commit Configuration (+14 more)

### Community 5 - "message.py"
Cohesion: 0.24
Nodes (9): Delivery engine with retry logic., # TODO: Implement actual message delivery based on protocol, # TODO: Implement DLR sending based on original protocol, MessageStatus, MessageType, Enum, str, Core message models and types. (+1 more)

### Community 6 - "AsyncTimer"
Cohesion: 0.12
Nodes (12): Semaphore, async_retry(), decorator(), AsyncTimer, gather_with_semaphore(), Any, Async helper utilities., Decorator for async functions with retry logic. (+4 more)

### Community 7 - "SMSMessage"
Cohesion: 0.09
Nodes (19): Process a single message., Simulate message delivery (replace with actual delivery logic)., Handle delivery failure and retry logic., DLRHandler, Any, Delivery receipt handling and correlation., Delivery receipt handler with message correlation., # TODO: Replace with persistent storage for production (+11 more)

### Community 8 - "encoding.py"
Cohesion: 0.19
Nodes (16): calculate_sms_parts(), decode_gsm_7bit(), detect_encoding(), encode_gsm_7bit(), encode_sms_text(), Enum, str, SMS encoding utilities for Unicode and GSM. (+8 more)

### Community 9 - "kannel/api.py"
Cohesion: 0.17
Nodes (14): kannel_send_sms(), kannel_status(), Request, Response, Kannel-compatible API implementation., Kannel-compatible SMS send endpoint., Kannel-compatible status endpoint., Config (+6 more)

### Community 10 - "SMPPClient"
Cohesion: 0.15
Nodes (9): Exception, Any, SMPP client for sending messages to SMSC., Start the SMPP client (alias for connect)., Stop the SMPP client (alias for disconnect)., Disconnect from SMSC., Send SMS message via SMPP., Handle delivery receipt from SMSC. (+1 more)

### Community 12 - "PorthException"
Cohesion: 0.22
Nodes (12): ConfigurationError, DeliveryError, MessageError, PorthException, ProtocolError, QueueError, Configuration-related errors., Message processing errors. (+4 more)

### Community 13 - "MessageQueue"
Cohesion: 0.10
Nodes (13): fixture, MessageQueue, Async message queue for SMS messages., Add a message to the queue., Get a message from the queue., Wait until all tasks are done., Return the queue size., Return True if queue is empty. (+5 more)

### Community 14 - "smpp/models.py"
Cohesion: 0.28
Nodes (8): BaseModel, SMPP-specific models and data structures., SMPP deliver_sm PDU model., SMPP delivery receipt model., SMPP submit_sm PDU model., SMPPDeliverSM, SMPPDeliveryReceipt, SMPPSubmitSM

### Community 15 - "test_message_flow.py"
Cohesion: 0.33
Nodes (5): asyncio, End-to-end tests for complete message flow., Test complete message flow from submission to delivery., # TODO: Implement end-to-end test, test_complete_message_flow()

### Community 16 - "test_http_flow.py"
Cohesion: 0.33
Nodes (5): asyncio, Integration tests for HTTP message flow., Test HTTP message submission and delivery flow., # TODO: Implement HTTP integration test, test_http_message_flow()

### Community 17 - "test_smpp_flow.py"
Cohesion: 0.33
Nodes (5): asyncio, Integration tests for SMPP message flow., Test SMPP message submission and delivery flow., # TODO: Implement SMPP integration test, test_smpp_message_flow()

## Ambiguous Edges - Review These
- `Development Configuration (config/development.yml)` → `Porth SMS Gateway (project overview)`  [AMBIGUOUS]
  README.md · relation: references

## Knowledge Gaps
- **10 isolated node(s):** `[0.1.0] - 2026-09-22`, `Packaging`, `Releasing`, `Config`, `Config` (+5 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 177 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Development Configuration (config/development.yml)` and `Porth SMS Gateway (project overview)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `SMSMessage` connect `SMSMessage` to `settings.py`, `http/api.py`, `SMPPServer`, `DeliveryEngine`, `message.py`, `kannel/api.py`, `SMPPClient`, `MessageQueue`?**
  _High betweenness centrality (0.220) - this node is a cross-community bridge._
- **Why does `Settings` connect `DeliveryEngine` to `settings.py`, `http/api.py`, `message.py`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._
- **Why does `DeliveryEngine` connect `DeliveryEngine` to `settings.py`, `message.py`, `SMSMessage`, `SMPPClient`, `MessageQueue`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `SMSMessage` (e.g. with `DeliveryEngine` and `DLRHandler`) actually correct?**
  _`SMSMessage` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `MessageQueue` (e.g. with `DeliveryEngine` and `SMSMessage`) actually correct?**
  _`MessageQueue` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `DeliveryEngine` (e.g. with `Settings` and `MessageStatus`) actually correct?**
  _`DeliveryEngine` has 6 INFERRED edges - model-reasoned connections that need verification._
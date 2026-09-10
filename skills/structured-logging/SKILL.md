---
name: structured-logging
description: Emit JSON structured logs with the platform's required fields and no PII, shipped via the standard Alloy/Fluent Bit pipeline.
version: 2.3.0
standard_id: STD-LOG-001
policy_ids: [logging.required_fields, logging.no_pii]
owner: platform-observability
harness_support: [claude-code, copilot-vscode, copilot-cli]
telemetry:
  emit: true
  success_signal: policy
---

# Structured logging

Use the platform logging wrapper. Every log line MUST include: `timestamp`, `level`,
`service`, `trace_id`, `span_id`, `message`. Never log request bodies, tokens, PANs or
personal data. See `logging.required_fields` and `logging.no_pii` policies for the
deterministic checks applied in CI.

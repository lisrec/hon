# 0.8.6 — inherited hardening

Code-only release candidate; not installed or physically commissioned.

- Verify TLS certificates and reject redirects without replaying credentials or commands.
- Sanitize rendered transport errors; serialize authentication refresh, stop on auth failure, and clean up failed setup/config flows.
- Reject HTTP/protocol/command failures consistently in both send paths. Quiet AC commands remain opt-in (default false), applied by the same helper; no noisy retry fallback.
- Preserve reported telemetry on rejected commands and failed polls. Climate availability follows coordinator and required input validity; missing measurements are unknown, not zero/OFF.
- Build isolated commands and strictly validate all requested fields. Invalid, unknown, fixed-conflicting, or unsupported requests fail before sending; missing current basis is not replaced by defaults.
- Switch commands share the strict builder, including fixed schema fields. Correct offset/fractional range validation and duplicate number setup.
- Resolve service targets to their current account owner, not the first loaded account.
- Standard combined climate mode + target is built as one command. OFF + target is rejected explicitly. Existing schema-aware start/settings dictionaries accept batch intent without a new controller or debounce.

## Compatibility and acceptance boundaries

Schema-aware start/settings intentionally no longer provide best-effort default substitution. Existing raw `send_custom_request` remains explicitly low-level. Separate commands use last successful reported telemetry, not a pending-state merge; combine intended changes in one batch when needed. Per-device transport ordering does not establish physical atomicity or a transaction across devices. No automatic retry after an uncertain command outcome.

Validation uses synthetic representative device schemas and local fixtures. It does not establish actual model schema support, cloud acceptance, physical HVAC output, or audible behavior. Installation/commissioning requires a separate authorized operation. Suppressed exception context may still retain the original aiohttp exception; ordinary formatted errors/logs are sanitized. Default aiohttp timeout remains unchanged.

Integration domain/config-entry remain `hon`. Release 0.8.5 is not replaced or modified.

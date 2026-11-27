# Model Settings And Safe Errors

## Settings

Inject a native Pydantic AI `Model` and supply settings on that object using
the SDK's native API. Foliorun does not add a token ceiling, reasoning setting,
temperature, provider fallback or monetary budget. For example, a host can set
the native `max_tokens` or `thinking` where the model supports it; that model's
SDK translates the setting for its provider. An effort level does not define a
precise reasoning-token allocation or guarantee enough visible output.

Agent retries are disabled to avoid implicit repetition. The injected
provider/transport owns lower-level retry and routing policy; configure these
explicitly rather than assuming the harness can override them. See
[migration](migration.md) for binding changes.

## Error Contract

`RunResult.error` remains a stable code. `RunResult.error_detail` is optional
`SafeError` data, also available as `RuntimeFault.detail`. Legacy results without
this field remain readable. There are no arbitrary strings or extension maps
inside this detail:

| Field | Values |
| --- | --- |
| `cause` | `context_length`, `output_truncated`, `content_filter`, `http_error`, `timeout`, `transport_error`, `invalid_response`, `unknown` |
| `finish_reason` | normalized `length`, `stop`, `tool_call`, `content_filter`, `error`, or null |
| `truncated` | true for proven truncation, false for observed non-length termination, null when not established |
| `model_request_index` | positive per-attempt request ordinal or null |
| `http_status` | HTTP status integer or null |

Correlate with the existing `result.handle.run_id`. Provider IDs/headers are
deliberately excluded because they are arbitrary provider-controlled strings.
Raw exception names/messages/chains, prompts, histories, reasoning content,
credentials, full provider responses and tool arguments are never included.
Native journal/checkpoint storage remains private and is not an error payload.

| Code | Status | Evidence and behavior |
| --- | --- | --- |
| `model_context_length` | `FAILED` | SDK `ModelContextWindowExceededError`; request rejected before completion |
| `model_output_truncated` | `FAILED` | Completed response with `length`, or SDK `IncompleteToolCall`; no tool dispatch from the truncated response |
| `model_content_filtered` | `FAILED` | Explicit finish reason or SDK content-filter type |
| `model_response_invalid` | `FAILED` | Completed response rejected by the SDK, or normalized provider `error`; no invented truncation cause |
| `unknown_model_window` | `NEEDS_REVIEW` | Request/parsing exception with an unresolved durable intent; safe typed cause may refine diagnostics, never retry eligibility |
| `adapter_failed` | `NEEDS_REVIEW` | Unclassified adapter failure; cause stays `unknown` |
| `invalid_model_binding` | `FAILED` | Factory did not supply a native model or valid owned-client pair |
| `model_binding_mismatch` | `FAILED` | Injected model name differs from the profile, before model send |
| `model_setup_failed` | `FAILED` | Host model factory failed before model send; no exception details exported |

Missing injection raises `RuntimeFault("model_injection_required")` when
constructing the harness. Invalid constructor arguments raise
`RuntimeFault("invalid_model_binding")` there instead of starting a run.

Only finite exception types are classified; message matching and recursive
exception-chain inspection are forbidden. Missing wire finish reasons that the
SDK fills with `stop` are not exposed as observed provider evidence. An unknown
provider finish string is not echoed. A typed timeout is not proof that no
request reached the provider.

A truncated response is rejected even if it contains parseable tool arguments
or follows a previous `submit_result`. Failure does not advance the session
head or publish artifacts. Cancellation fences take precedence; unknown model,
tool and execution windows retain review/no-automatic-retry barriers. Existing
explicit native-restore fallback eligibility is unchanged.

## Verification Scope

Correctable frozen-input `read_file` errors are normal tool returns, not model
error evidence. See [File Inspection And Safe Tool Errors](tool-errors.md).

Controlled transports exercise the installed SDKs, wire serialization and
local classification. They do not verify provider enforcement, live model
behavior or task quality.

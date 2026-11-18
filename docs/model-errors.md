# Model Settings And Safe Errors

## Settings

Only OpenRouter `deepseek/deepseek-v4.1-flash-1017` is supported. Credentials
stay on the host. No additional provider, model fallback or retry is added.

`Profile.model_settings` accepts `ModelConfig` with these closed fields:

| Field | Accepted values | Request mapping |
| --- | --- | --- |
| `max_tokens` | strict positive integer or null | Pydantic AI `max_tokens` to Chat Completions `max_tokens`; null omits it |
| `reasoning_effort` | `max`, `high`, `low`, `none`, or null | `extra_body.reasoning.effort` to top-level `reasoning.effort`; null omits it |

Omitted/null configuration preserves the legacy ceiling. An explicit empty
configuration uses provider defaults. Provider routing always retains
`allow_fallbacks=false, require_parameters=true`; SDK retries remain disabled.
`temperature=0` is unchanged. See [migration](migration.md) for binding changes.

OpenRouter documents a shared reasoning/visible-output ceiling for most
reasoning providers. An effort level is not a precise reasoning token limit.
`reasoning.exclude` would hide returned reasoning, not reserve visible tokens,
so it is not used as a truncation fix. No numerical reasoning cap is exposed:
the exact model's declaration does not establish a hard token allocation.
Public model capabilities can change and are not a live acceptance guarantee.

## Error Contract

`RunResult.error` remains a stable code. `RunResult.error_detail` is optional
`SafeError` data, also available as `RuntimeFault.detail`. Legacy results without
this field remain readable. There are no arbitrary strings or extension maps
inside this detail:

| Field | Values |
| --- | --- |
| `cause` | `output_truncated`, `content_filter`, `http_error`, `timeout`, `transport_error`, `invalid_response`, `unknown` |
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
| `model_output_truncated` | `FAILED` | Completed response with `length`, or SDK `IncompleteToolCall`; no tool dispatch from the truncated response |
| `model_content_filtered` | `FAILED` | Explicit finish reason or SDK content-filter type |
| `model_response_invalid` | `FAILED` | Completed response rejected by the SDK, or normalized provider `error`; no invented truncation cause |
| `unknown_model_window` | `NEEDS_REVIEW` | Request/parsing exception with an unresolved durable intent; safe typed cause may refine diagnostics, never retry eligibility |
| `adapter_failed` | `NEEDS_REVIEW` | Unclassified adapter failure; cause stays `unknown` |

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

## Official References

Public references inspected on 2026-11-18, without authentication or model calls:

- OpenRouter parameters: `https://openrouter.ai/docs/api/reference/parameters`
- OpenRouter reasoning: `https://openrouter.ai/docs/guides/best-practices/reasoning-tokens`
- Exact model capability declaration: `https://openrouter.ai/api/v1/models`
- Endpoint declarations: `https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints`
- Pydantic AI fixed source: `https://github.com/pydantic/pydantic-ai/blob/v2.61.0/pydantic_ai_slim/pydantic_ai/models/openai.py`
- Pydantic AI fixed graph: `https://github.com/pydantic/pydantic-ai/blob/v2.61.0/pydantic_ai_slim/pydantic_ai/_agent_graph.py`
- OpenAI SDK fixed source: `https://github.com/openai/openai-python/blob/v3.51.0/src/openai/resources/chat/completions/completions.py`

The installed Pydantic AI and OpenAI SDK versions are pinned and tested through
`httpx.MockTransport`. This proves request serialization and local
classification only, not provider enforcement, live model behavior or task
quality.

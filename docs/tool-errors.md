# File Inspection And Safe Tool Errors

The builtin `read_file(path, offset=0, length=...)` and
`stat_file(path)` accept canonical absolute paths only. Frozen input roots are
`/documents`, `/materials`, `/inputs`, `/skills`; `/previous` aliases immediately
previous committed outputs. These read from the verified host VFS/blob catalog,
never an arbitrary host filesystem. `stat_file` on inputs uses frozen metadata
without fetching bytes. `/output` reads use only the current run's sandbox;
there is no sandbox ID, run ID, host root or command parameter.

Current outputs must be regular files with one link. Root/parent/leaf symlinks,
hardlinks, traversal, special files, noncanonical paths and other roots are
refused. Every component is opened with directory descriptors and no-follow
flags. Reads are bounded by `Limits.output_bytes`, with descriptor metadata
checked before/after reading. Tools must be foreground programs without
background writers. Inspection observes a file at that time: it is not a seal,
publication or a guarantee that a later read/submission sees identical bytes.
`submit_result` still seals and collects independently.

Successful reads and stats return closed JSON objects:

- `stat_file`: `{"kind":"json","value":{"path":str,"size":int,"sha256":str}}`.
  `size` is a nonnegative byte count and `sha256` is 64 lowercase hex characters.
- `read_file`: `{"kind":"json","value":{"text":str,"next_offset":int,"eof":bool}}`.
  Offsets/length count Unicode characters; `next_offset=offset+len(text)`.
  Supply pagination arguments as integers (not floating-point numbers).
  Length is positive and at most `Limits.text_bytes`. JSON envelope and
  escaping count toward that byte budget; the returned page may be shorter.
  Empty files and offsets beyond EOF return empty text with `eof=true`.

Text must be UTF-8 without control characters other than tab, newline and
carriage return. Binary files can be inspected for size/hash, but cannot be
read as text. Large files may be read in text pages only within the total
output file byte limit. `/workspace` is not an inspection root.

## Correctable Frozen Input Reads

For `read_file` on a canonical path in the authorized frozen input catalog, after
the bytes pass integrity verification, invalid UTF-8 returns exactly
`{"kind":"json","value":{"error":{"code":"file_not_utf8"}}}`. Valid UTF-8 with
unsupported controls returns the same shape with `file_not_text`. Neither result
includes file bytes, file paths, arguments, or exception details. Consumers of
`read_file` tool returns must distinguish the error object from a text page.
The result still counts as a tool call and must fit the normal result byte budget.

The gateway journals the result as `done` before returning it. A matching call
ID and identity can reuse that result under the existing guards; an incomplete
intent still requires review. This does not retry the read, reset budgets, or
submit anything. The model may choose a different authorized tool in the same run,
such as `stat_file` for metadata or an approved tool for that file type.
`/previous` and `/output` text errors remain fatal, as do failures to retrieve or
verify frozen input bytes. Storage and sandbox faults are not tool results.

## Product Error Projection

`RuntimeFault.code: str` projects to `RunResult.error: str | None`.
`RunResult.status` is the existing `Status` enum. These fields carry no path,
arguments, exception text or dynamic extension object. For the file/tool
failures below, `RuntimeFault.detail` and `RunResult.error_detail` are null.
`SafeError` describes model/provider evidence (or an unknown adapter failure),
not a general tool error object. Hosts may map `error` to a localized message;
do not serialize raw exceptions, private journals/checkpoints or SDK objects.

| Allowed code(s) | Status | Meaning |
| --- | --- | --- |
| `invalid_path`, `invalid_output_path`, `reserved_input_path` | `FAILED` | Noncanonical or unauthorized root; no arbitrary host fallback |
| `input_missing` | `FAILED` | Canonical input/previous path absent from this run's frozen catalog |
| `input_unavailable`, `input_provider_unavailable`, `input_lease_expired`, `input_changed` | `FAILED` | Frozen bytes cannot be retrieved/verified |
| `byte_budget`, `blob_integrity` | `FAILED` | Existing frozen byte/integrity constraints reject the read |
| `output_missing` | `FAILED` | Current sandbox output does not exist |
| `output_unsafe_file` | `FAILED` | Link, special file or unsafe/inaccessible filesystem entry |
| `output_changed` | `FAILED` | File size/identity/timestamps changed during inspection |
| `file_too_large` | `FAILED` | Current output exceeds the output file byte bound |
| `file_not_utf8`, `file_not_text` | `FAILED` | File cannot be read as supported text outside the correctable frozen-input path |
| `file_page_budget` | `FAILED` | Even a progress-making text page cannot fit the JSON byte limit |
| `file_inspection_unavailable` | `FAILED` | Custom sandbox session lacks safe inspection support |
| `file_inspection_invalid` | `FAILED` | Inspector returned a malformed or unrecognized result/code |
| `file_inspection_failed` | `FAILED` | Read-only inspection command/transport failed; root cause remains unknown |
| `output_sealed`, `result_already_submitted` | `FAILED` | Tool use attempted beyond submission/sealing boundary |
| `tool_not_authorized`, `tool_arguments_invalid`, `tool_arguments_budget`, `tool_budget`, `tool_result_invalid` | `FAILED` | Existing tool declaration/schema/count/result limits |
| `unknown_tool_window`, `unknown_model_window`, `unknown_execution_window`, `adapter_failed` | `NEEDS_REVIEW` | Unresolved execution/effect or unclassified adapter outcome; no automatic replay |
| `publication_fenced`, `run_closed`, `deadline_exceeded` | `FAILED` | Existing execution/publication guard |
| `cancelled` | `CANCELLED` | Cancellation fence wins; no successful publication |

This table is an allowlist for this interface, not an enum replacing every
framework error. Other existing collection/session errors retain their
contracts. Unknown/new codes must remain unknown in a host's presentation;
never infer a model cause, retry permission or success. Unclassified failures
outside the read-only inspector still use `adapter_failed` with unknown safe
evidence and `NEEDS_REVIEW`. Existing `unknown_*` windows retain review status.

Tools use durable intent-before-send journals. A rejected call may leave an
intent; reusing an incomplete call ID fails as `unknown_tool_window`. No new
automatic retry or error-to-model retry loop is introduced. Cancellation is
checked before dispatch and after the result; collection and publication
fences remain in force. A fatal inspection error never creates a successful
revision.

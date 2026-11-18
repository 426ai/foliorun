# Formats And Migration

## Source Compatibility

The public distribution version is **0.1.0**. Earlier source snapshots also
used this version and are not interchangeable. Identify an installation by
its full source commit and archive SHA-256, in addition to its version.
Preserve earlier archives instead of overwriting them. Development candidates
are separate artifacts and are not republished under their candidate names.

Persisted profile compatibility markers, native checkpoint formats and adapter
identities are intentionally unchanged. The generated `package_version` and distribution version are both `0.1.0`;
keep other generated compatibility fields unchanged. Use `Profile` and
`model_dump_json()` to produce metadata, and keep original bytes for old objects.
Existing checkpoint readers and explicit compatibility paths still apply.

## Output Inspection And Tool Errors

`read_file` now routes `/output/...` to the current run's sandbox before
submission; frozen inputs and `/previous` remain host VFS reads. `stat_file`
is a new reserved builtin name: rename any injected tool with that name before
installing its registry. `list_files` still lists the frozen/previous catalog;
it does not enumerate mutable sandbox files. Use output paths from approved
tool receipts with `stat_file` and `read_file`.

Custom sandbox sessions can implement async
`inspect_file(path, limits, *, read=False, offset=0, length=1)` returning the
same closed `kind/value` result as the builtins. They must enforce the same
run ownership, path, link, byte and cancellation boundaries as the supplied
OpenSandbox adapter. Sessions without the method fail explicitly with
`file_inspection_unavailable`; there is no host-file or SDK download fallback.
The bundled OpenSandbox inspector ships with the wheel and runs as `app`
using isolated Python in the sandbox. No image rebuild or new executable
binding is required; images still need Python and the existing app user.

Frozen non-UTF-8 read failures now use `file_not_utf8` in place of
`input_not_utf8`. UTF-8 containing control characters other than tab/newline/
carriage return is rejected as `file_not_text`. Pagination may return fewer
characters than requested to fit the serialized JSON byte budget. Continue
from `next_offset`; a non-final empty page is never returned.

Product adapters should allowlist the finite codes and statuses in
[tool/path errors](tool-errors.md), retaining `unknown` for unclassified
failures. `error_detail` stays null for these failures; do not manufacture a
model cause. Offline validation does not establish live workflow acceptance.
There are no session, GC, model, count or monetary budget changes.

## Model Configuration

An omitted or null `model_settings` preserves the old request ceiling of 1800
and the exact old profile serialization/binding hash. Upgrading the wheel alone
does **not** change that ceiling. Existing count semantics remain unchanged:
model requests and tools default to no count limit; positive strict integers
remain optional.

To opt in, construct a **new profile version** with an explicit `ModelConfig`:

```python
from foliorun import ModelConfig, Profile

new_profile = Profile.model_validate(
    old_profile.model_dump() | {
        "version": "host-reviewed-model-settings",
        "model_settings": ModelConfig(
            max_tokens=None,
            reasoning_effort="low",
        ).model_dump(),
    }
)
```

Here `None` omits the per-response ceiling, leaving the provider default, and
`low` requests the model's declared effort level. Neither guarantees enough
visible output. A host may instead set a reviewed positive `max_tokens` based
on its workload and available context. These settings are not request or
monetary budgets. Exact reasoning-token allocation is not exposed.

Explicit settings, even `{}`, participate in the profile binding and job
fingerprint. Install the same serialized profile on submitters and workers.
Use a fresh idempotency key and a new session for changed settings. Existing
session continuation requires the exact original binding; it fails closed on
changes. Old in-flight work must retain its original profile and worker.
No session rewriting, GC changes or in-place job migration is provided.

Read `RunResult.error` as the stable code and optional `error_detail` as finite
evidence. Do not serialize the SDK exception, its chain, messages or raw
responses. See [model errors](model-errors.md) for the complete contract.
Product error passthrough and live retesting are separate integration work.

## Legacy Reads

`foliorun.migration` exposes:

- `read_legacy_profile(body)`: original bytes/hash plus frozen old Profile.
  Omitted legacy counts retain **8/20**, including sparse profiles.
- `read_legacy_job(body)`: inspection only, preserving source identity.
  Execute in-flight jobs with their original worker.
- `read_legacy_checkpoint(body)`: explicit `legacy-output-only` scope.
  Native history and output references do not prove original inputs are present.

These return `LegacyRecord(raw, value, sha256, recovery_scope)` without mutating
old JSON, bindings or hashes. Unsupported formats fail closed.

```python
from foliorun.migration import migrate_profile_v010

profile = migrate_profile_v010(old_profile_bytes, source_version="0.1.0")
assert profile.limits.model_requests == 8  # When absent in the legacy profile.
```

`upgrade_legacy_revision` requires a verified original JobSpec and full legacy
input/output blobs. It returns a new revision with migration provenance;
`runtime.sessions.import_revision(revision)` creates a new session only.
For multi-round legacy sessions, supply a confirmed effective-input snapshot
or receive `legacy_multiround_requires_confirmed_baseline`. Missing inputs
produce `legacy_input_unavailable`. Native restore remains adapter-specific.

## Deployment And Rollback

Keep old workers and isolated queues until their in-flight work drains.
Do not send new settings or new error fields to readers that cannot accept
them. Before rollback, drain work using this source snapshot; do not delete compatibility fields
or claim cross-adapter history equivalence. Image digests, tools releases and
profile bindings remain exact identities.

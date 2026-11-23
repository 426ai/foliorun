# Formats And Migration

## Source Compatibility

The profile schema is `0.4`. It intentionally does not reproduce `0.3` profile
hashes or accept those serialized profiles as current bindings. The JobSpec,
handle, and session wire protocol is still `0.3`; the native Pydantic messages
and checkpoint envelope are unchanged. The profile's `package_version` is a
historical format marker, not the installed distribution version. Keep original
bytes for old objects and do not rewrite job or session references.

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
Session, GC and count-limit behavior is otherwise unchanged.

## Reconfigure Host Bindings

Build a fresh `Profile` with an explicit `model` and `image` and the desired
allowlist, limits, retention, and binding version. In Pydantic AI, `model` must
match the injected native `Model.model_name`. Replace `openrouter_api_key=`,
`openrouter_model` and `egress_check` with a host-constructed Model or trusted
`model_factory=`. Do not pass SDK model names as lazy lookup strings to the
harness or add provider credentials to a profile.

```python
from foliorun import Profile
from foliorun.adapters.pydantic_ai import PydanticHarness

new_profile = Profile(
    name="fictional-documents",
    version="new-host-binding",
    model=native_model.model_name,
    image=reviewed_image_ref,
    allowed_tools=tuple(approved_tool_names),
)
harness = PydanticHarness(model=native_model)
```

`Profile.model_settings` and `foliorun.ModelConfig` are removed. Configure
caps, reasoning and routing on the native model and provider. The old 1800
output-token ceiling is **not** retained automatically. To reproduce that
policy, explicitly use native `settings={"max_tokens": 1800}` on a suitable
model. Review reasoning settings against the selected SDK/provider; the old
`reasoning_effort` values are not silently translated. The framework does not
impose a monetary budget. Request/tool counts still default to unbounded.

Replace `OpenSandboxProvider(api_key=..., endpoint=...)` with explicit native
`ConnectionConfig`, `owner`, `seal_argv`, and any tool mappings. Removed
`image_manifest` was not verified. Replace removed `skills_manifest` with an
explicit `verify_image` hook if the deployment requires verified image Skills;
the DOCX example does this. An image tag can be valid yet mutable, so pin and
verify the deployed image separately when reproducibility matters.

Native settings are not hashed. Assign a new public profile `version` when
changing host policy not represented in the profile fields. Existing sessions
with an old binding do not transparently continue. Use a fresh idempotency key
and session for the new binding. Finish old in-flight jobs with matching old
workers and queues. No in-place job or session migration is provided for `0.3`
profiles, and compatible native bytes alone do not establish compatible routing.

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

profile = migrate_profile_v010(
    old_profile_bytes,
    source_version="0.1.0",
    model=native_model.model_name,
    image=reviewed_image_ref,
)
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

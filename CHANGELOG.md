# Changelog

## 0.1.1.dev5

- Return a journaled, code-only tool result for `read_file` decoding or text-type
  errors on verified frozen inputs, so the model can choose another authorized tool.
- Keep inspection of previous and current outputs, integrity failures, and unknown
  execution windows fail-closed.

## 0.1.1.dev4

- Inject native Pydantic AI models without a framework-chosen provider or model policy.
- Separate Pydantic AI SDK and optional OpenAI provider dependencies.
- Pass the host image reference through OpenSandbox, with explicit tool/sealer bindings.
- Use a distinct profile schema for public binding metadata without native settings or
  image-specific manifests. Old profile bindings require explicit reconfiguration.

## 0.1.0

- Route bounded `read_file` and new `stat_file` inspection to the current run's
  sandbox outputs, retaining verified host VFS reads for inputs/previous files.
- Reject traversal, links, special files, oversized/non-text reads and unsafe
  inspection receipts without exposing command or exception details.
- Document finite tool/path errors separately from model error evidence.
- Keep the public README focused on functionality, architecture and usage.

- Opt-in, fingerprinted output-token and reasoning-effort profile settings.
- Fail closed on truncated responses before any tool dispatch or publication.
- Finite safe error evidence; preserve unknown request and side-effect barriers.
- Preserve existing profile/checkpoint compatibility identities and default counts.
- Synthetic SDK transport, cancellation, identity and isolated-install regressions.

Independent framework source snapshot with explicit persisted-format boundaries.

- Injected host/sandbox tools with actual preinstalled binding dispatch.
- Metadata-only VFS freeze, separate access leases, on-demand verified reads.
- Durable output seals, per-file receipts and collector-only recovery.
- Complete input/native/output session revisions, explicit input changes.
- Bounded opt-in whole-round fallback with new run identities and side-effect guards.
- Injectable blob/index protocols, local FS/SQLite defaults and optional caller-client S3.
- Default unlimited request/tool counts, strict optional finite limits, legacy 8/20 readers.
- Offline executable examples and explicit migration/operational boundaries.

No live model/service acceptance, rebuilt image, deployment or package publication
is implied by this source snapshot.

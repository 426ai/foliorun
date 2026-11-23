"""Read-only legacy decoders and explicit new-identity upgrades; never rewrite old objects."""

from dataclasses import dataclass

from . import legacy
from .contracts import (
    PROFILE_SCHEMA,
    BlobRef,
    Checkpoint,
    InputFile,
    Profile,
    RuntimeFault,
    SessionVersion,
    Snapshot,
    canonical,
    digest,
)
from .vfs import MATERIALIZED, validate_catalog


@dataclass(frozen=True)
class LegacyRecord:
    raw: bytes
    value: object
    sha256: str
    recovery_scope: str


def _read(body, cls, recovery_scope):
    raw = body.encode() if isinstance(body, str) else bytes(body)
    value = cls.model_validate_json(raw)
    return LegacyRecord(raw, value, digest(raw), recovery_scope)


def read_legacy_profile(body):
    """Sparse old fields keep their original 8/20 semantics and source identity."""
    return _read(body, legacy.Profile, "legacy-profile")


def read_legacy_job(body):
    """Inspection only. Execute old in-flight jobs on a matching old worker."""
    return _read(body, legacy.JobSpec, "read-only-job")


def read_legacy_checkpoint(body):
    """The old format has outputs, not a proven effective input snapshot."""
    return _read(body, legacy.Checkpoint, "legacy-output-only")


def migrate_profile_v010(body, *, source_version, model: str, image: str):
    if source_version != "0.1.0":
        raise RuntimeFault("unsupported_migration_source")
    old = read_legacy_profile(body).value
    data = old.model_dump()
    for removed in ("capabilities", "image_manifest", "skills_manifest"):
        data.pop(removed)
    data.update(model=model, image=image)
    data["schema_version"] = PROFILE_SCHEMA
    return Profile.model_validate(data)


def upgrade_legacy_revision(
    *,
    store,
    checkpoint_body,
    old_job_ref: BlobRef,
    session_id: str,
    profile: Profile,
    tools_ref,
    confirmed_effective_input: Snapshot | None = None,
):
    """Build an importable new revision from verified legacy blobs, never invent old inputs.

    Multi-round legacy views cannot be reconstructed from the last job alone. A caller
    must explicitly provide a confirmed baseline, or this path refuses the upgrade.
    """
    old_job = read_legacy_job(store.read(old_job_ref)).value
    old_checkpoint = read_legacy_checkpoint(checkpoint_body).value
    if old_job.scope != store.scope:
        raise RuntimeFault("scope_mismatch")
    try:
        old_inputs = legacy.Snapshot.model_validate(
            store.read_json(BlobRef.model_validate(old_job.vfs_snapshot_ref.model_dump()))
        )
        old_outputs = legacy.Snapshot.model_validate(
            store.read_json(BlobRef.model_validate(old_checkpoint.file_snapshot_ref.model_dump()))
        )
        native = BlobRef.model_validate(old_checkpoint.checkpoint_ref.model_dump())
        store.read(native)
        for file in (*old_inputs.files, *old_outputs.files):
            store.read(BlobRef.model_validate(file.blob.model_dump()))
    except Exception:
        raise RuntimeFault("legacy_input_unavailable") from None
    if old_job.session_revision and confirmed_effective_input is None:
        raise RuntimeFault("legacy_multiround_requires_confirmed_baseline")
    submitted = Snapshot(
        provider=MATERIALIZED,
        scope=store.scope,
        revision=old_inputs.revision,
        files=tuple(InputFile.model_validate(f.model_dump()) for f in old_inputs.files),
    )
    effective = confirmed_effective_input or submitted
    if effective.scope != store.scope:
        raise RuntimeFault("scope_mismatch")
    validate_catalog(effective.files, profile.limits)
    for file in effective.files:
        store.read(file.blob)
    inputs_ref, effective_ref = store.put_json(submitted), store.put_json(effective)
    outputs_ref = store.put_json(
        Snapshot(
            provider=MATERIALIZED,
            scope=store.scope,
            revision=old_outputs.revision,
            files=tuple(InputFile.model_validate(f.model_dump()) for f in old_outputs.files),
        )
    )
    checkpoint = Checkpoint(
        harness=old_checkpoint.harness,
        adapter_version=old_checkpoint.adapter_version,
        harness_version=old_checkpoint.harness_version,
        format_version=old_checkpoint.format_version,
        checkpoint_ref=native,
        file_snapshot_ref=outputs_ref,
    )
    manifest = store.put(
        canonical(
            {
                "schema_version": "0.3",
                "migration": "legacy-output-only-to-complete",
                "source_job_ref": old_job_ref.model_dump(),
                "source_checkpoint_sha256": digest(
                    checkpoint_body.encode()
                    if isinstance(checkpoint_body, str)
                    else checkpoint_body
                ),
                "submitted_input_ref": inputs_ref.model_dump(),
                "effective_input_ref": effective_ref.model_dump(),
                "checkpoint": checkpoint.model_dump(),
                "profile_ref": profile.ref().model_dump(),
                "tools_ref": tools_ref.model_dump(),
            }
        )
    )
    return SessionVersion(
        session_id=session_id,
        revision=1,
        run_id="legacy-import-" + manifest.sha256[:24],
        checkpoint=checkpoint,
        manifest_ref=manifest,
        submitted_input_ref=inputs_ref,
        effective_input_ref=effective_ref,
        output_snapshot_ref=outputs_ref,
        profile_ref=profile.ref(),
        tools_ref=tools_ref,
    )

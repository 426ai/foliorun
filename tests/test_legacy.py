"""Legacy 0.2 reads retain 8/20 and output-only scope; upgrades have a distinct identity."""

import json

import pytest
from pydantic import ValidationError

from foliorun import Profile, RuntimeFault, legacy
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import BlobRef, JobSpec, Snapshot, canonical, digest
from foliorun.migration import (
    migrate_profile_v010,
    read_legacy_checkpoint,
    read_legacy_job,
    read_legacy_profile,
    upgrade_legacy_revision,
)


def sparse_profile():
    return canonical(
        {
            "name": "synthetic-legacy",
            "version": "1",
            "package_version": "0.1.0",
            "image": "localhost/synthetic@sha256:" + "a" * 64,
            "image_manifest": "b" * 64,
            "skills_manifest": "c" * 64,
            "allowed_tools": [],
        }
    )


def test_legacy_sparse_profile_is_read_only_8_20_and_explicit_upgrade_distinct():
    body = sparse_profile()
    record = read_legacy_profile(body)
    assert record.raw == body and record.sha256 == digest(body)
    assert record.value.limits.model_requests == 8
    assert record.value.limits.tools == 20
    before = record.value.ref()
    # Golden identity computed from the frozen 0.2 contract, not the new reader.
    assert before.sha256 == "0771aa09b5dee125f9a8fe6e5f0e9e19fb315664f5ef0b862c99510f05c1e736"
    selected = {"model": "fictional-replacement", "image": "registry.example.test/alternate:1"}
    upgraded = migrate_profile_v010(body, source_version="0.1.0", **selected)
    assert upgraded.limits.model_requests == 8 and upgraded.limits.tools == 20
    assert upgraded.ref().sha256 != before.sha256
    assert read_legacy_profile(body).value.ref() == before
    assert upgraded.schema_version == "0.4"
    assert upgraded.model == selected["model"] and upgraded.image == selected["image"]
    assert "skills_manifest" not in upgraded.model_dump()
    with pytest.raises(ValidationError, match="legacy_profile_requires_explicit_migration"):
        Profile.model_validate_json(body)
    with pytest.raises(ValidationError):
        Profile.model_validate(json.loads(body))
    no_marker = json.loads(body)
    no_marker.pop("package_version")
    with pytest.raises(ValidationError):
        Profile.model_validate_json(json.dumps(no_marker))
    assert read_legacy_profile(json.dumps(no_marker)).value.ref() == before
    with pytest.raises(RuntimeFault, match="unsupported_migration_source"):
        migrate_profile_v010(body, source_version="0.2.0", **selected)


@pytest.mark.parametrize("counts", [{"tools": None}, {"tools": 65}, {"model_requests": 17}])
def test_old_reader_rejects_new_count_contract(counts):
    body = json.loads(sparse_profile())
    body["limits"] = counts
    with pytest.raises(ValidationError):
        read_legacy_profile(json.dumps(body))


def legacy_objects(c, *, rounds=0):
    input_ref = c.store.put(b"Fictional old input")
    output_ref = c.store.put(b"Fictional old output")
    snapshot = legacy.Snapshot(
        provider=legacy.BindingRef(name="materialized-blobs", version="1", sha256="d" * 64),
        revision="synthetic-old-revision",
        scope=c.store.scope,
        expires_at=9999999999,
        files=(legacy.FileRef(path="/documents/old.txt", blob=input_ref.model_dump()),),
    )
    outputs = snapshot.model_copy(
        update={
            "files": (legacy.FileRef(path="/output/old.txt", blob=output_ref.model_dump()),),
        }
    )
    checkpoint = legacy.Checkpoint(
        checkpoint_ref=c.store.put(b"[]").model_dump(),
        file_snapshot_ref=c.store.put_json(outputs).model_dump(),
    )
    job = legacy.JobSpec(
        scope=c.store.scope,
        session_id="old-session",
        session_revision=rounds,
        prompt_ref=c.store.put(b"Fictional old prompt").model_dump(),
        vfs_snapshot_ref=c.store.put_json(snapshot).model_dump(),
        vfs_provider_ref=snapshot.provider,
        tools_registry_ref=c.tools.seal().model_dump(),
        runtime_profile_ref=read_legacy_profile(sparse_profile()).value.ref(),
        limits=legacy.Limits(),
        retention_seconds=3600,
    )
    return checkpoint, c.store.put_json(job), snapshot


def test_checkpoint_is_output_only_until_verified_input_upgrade(case):
    c = case
    checkpoint, job_ref, old_inputs = legacy_objects(c)
    record = read_legacy_checkpoint(canonical(checkpoint))
    assert record.recovery_scope == "legacy-output-only"
    assert not hasattr(record.value, "effective_input_ref")
    harness = PydanticHarness(model_factory=lambda: None)
    harness.validate_checkpoint(record.value)
    assert (
        harness.restore(
            c.store.read(BlobRef.model_validate(checkpoint.checkpoint_ref.model_dump()))
        )
        == []
    )
    assert read_legacy_job(c.store.read(job_ref)).recovery_scope == "read-only-job"
    with pytest.raises(ValidationError):
        JobSpec.model_validate_json(c.store.read(job_ref))
    revision = upgrade_legacy_revision(
        store=c.store,
        checkpoint_body=canonical(checkpoint),
        old_job_ref=job_ref,
        session_id="new-session",
        profile=c.profile,
        tools_ref=c.tools.seal(),
    )
    inputs = Snapshot.model_validate(c.store.read_json(revision.effective_input_ref))
    assert inputs.files[0].path == "/documents/old.txt"
    assert inputs.files[0].blob.sha256 == old_inputs.files[0].blob.sha256
    assert b"source_job_ref" in c.store.read(revision.manifest_ref)
    c.runtime.sessions.import_revision(revision)
    assert c.runtime.sessions.get("new-session")["revision"] == 1
    with pytest.raises(RuntimeFault, match="cas_conflict"):
        c.runtime.sessions.import_revision(revision)


def test_upgrade_refuses_unproven_multiround_or_missing_input(case):
    checkpoint, job_ref, _ = legacy_objects(case, rounds=2)
    with pytest.raises(RuntimeFault, match="legacy_multiround_requires_confirmed_baseline"):
        upgrade_legacy_revision(
            store=case.store,
            checkpoint_body=canonical(checkpoint),
            old_job_ref=job_ref,
            session_id="new-session",
            profile=case.profile,
            tools_ref=case.tools.seal(),
        )
    job = read_legacy_job(case.store.read(job_ref)).value
    ref = BlobRef.model_validate(job.vfs_snapshot_ref.model_dump())
    case.store.blob_store.delete(ref)
    with pytest.raises(RuntimeFault, match="legacy_input_unavailable"):
        upgrade_legacy_revision(
            store=case.store,
            checkpoint_body=canonical(checkpoint),
            old_job_ref=job_ref,
            session_id="new-session",
            profile=case.profile,
            tools_ref=case.tools.seal(),
        )

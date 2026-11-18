"""Offline migration boundaries and real Temporal SDK loading; no service processes."""

import tomllib
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from foliorun import Handle, Profile, __version__
from foliorun.adapters.temporal import TemporalScheduler
from foliorun.adapters.temporal_workflow import DocumentWorkflow, DocumentWorkflowV03
from foliorun.contracts import PROFILE_SCHEMA, PROTOCOL, VERSION, BlobRef, Checkpoint, JobSpec


@pytest.mark.parametrize("old_version", ["0.2.0", "0.2.1"])
def test_old_persisted_versions_require_explicit_migration(case, old_version):
    old_profile = case.profile.model_dump() | {"package_version": old_version}
    with pytest.raises(ValidationError):
        Profile.model_validate(old_profile)
    blob = case.store.put(b"Entirely fictional checkpoint")
    from foliorun import RuntimeFault
    from foliorun.adapters.pydantic_ai import PydanticHarness

    harness = PydanticHarness(model_factory=lambda: None)
    checkpoint = harness.checkpoint(blob, blob)
    assert checkpoint.adapter_version == "0.3"
    with pytest.raises(RuntimeFault, match="checkpoint_incompatible"):
        harness.validate_checkpoint(
            Checkpoint.model_validate(checkpoint.model_dump() | {"adapter_version": old_version})
        )


def test_distribution_version_is_independent_of_protocol(case):
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert version("foliorun") == __version__ == VERSION == declared_version
    assert case.profile.package_version == "0.1.0"
    assert case.profile.schema_version == PROFILE_SCHEMA == "0.4"
    assert Profile.model_validate_json(case.profile.model_dump_json()) == case.profile
    blob = case.store.put(b"Entirely fictional checkpoint")
    checkpoint = case.harness.checkpoint(blob, blob)
    assert Checkpoint.model_validate_json(checkpoint.model_dump_json()) == checkpoint
    assert Handle(run_id="synthetic", job_sha256="a" * 64).protocol_version == PROTOCOL == "0.3"
    assert JobSpec.model_fields["protocol_version"].default == "0.3"


async def test_temporal_sdk_loads_renamed_workflow_in_sandbox():
    for cls in (DocumentWorkflow, DocumentWorkflowV03):
        definition = workflow._Definition.must_from_class(cls)
        SandboxedWorkflowRunner().prepare_workflow(definition)


async def test_temporal_start_and_lookup_share_foliorun_identity():
    calls = []
    sentinel = object()

    async def start(fn, request, **options):
        calls.append((fn, request, options))

    def lookup(workflow_id):
        assert workflow_id == calls[0][2]["id"] == "foliorun-synthetic-run"
        return sentinel

    scheduler = TemporalScheduler(
        SimpleNamespace(start_workflow=start, get_workflow_handle=lookup),
        task_queue="synthetic-foliorun-queue",
    )
    handle = Handle(run_id="synthetic-run", job_sha256="a" * 64)
    reference = BlobRef(scope="synthetic", sha256="b" * 64, size=1)
    await scheduler.start(handle, reference)
    assert scheduler.workflow(handle) is sentinel
    assert calls[0][1]["job_ref"] == reference.model_dump()
    assert calls[0][2]["task_queue"] == "synthetic-foliorun-queue"

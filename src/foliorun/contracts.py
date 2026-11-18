"""Protocol 0.3 metadata. Distribution, wire and native format versions are independent."""

import hashlib
import json
import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

VERSION = "0.1.1.dev4"
PROTOCOL = "0.3"
PROFILE_SCHEMA = "0.4"
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RuntimeFault(Exception):
    """Only stable codes cross the adapter/History/log boundary."""

    def __init__(self, code: str, *, detail: "SafeError | None" = None):
        self.code = code
        self.detail = SafeError.model_validate(detail) if detail is not None else None
        super().__init__(code)


def canonical(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_checked(path: str, *, output: bool = False) -> str:
    p = PurePosixPath(path)
    if (
        not path.startswith("/")
        or str(p) != path
        or ".." in p.parts
        or "\\" in path
        or any(ord(c) < 32 for c in path)
        or len(path) > 1024
        or len(p.parts) > 20
        or len(p.parts) < 3
    ):
        raise RuntimeFault("invalid_path")
    if output and p.parts[1] != "output":
        raise RuntimeFault("invalid_output_path")
    if not output and p.parts[1] not in {"documents", "materials", "skills", "inputs"}:
        raise RuntimeFault("reserved_input_path")
    return path


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise RuntimeFault("invalid_identifier")
    return value


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SafeError(Frozen):
    """Finite evidence only; no messages, bodies, headers, IDs or exception objects."""

    cause: Literal[
        "output_truncated",
        "content_filter",
        "http_error",
        "timeout",
        "transport_error",
        "invalid_response",
        "unknown",
    ] = "unknown"
    finish_reason: Literal["length", "stop", "tool_call", "content_filter", "error"] | None = None
    truncated: bool | None = Field(default=None, strict=True)
    model_request_index: Annotated[int, Field(strict=True, ge=1)] | None = None
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None


class BlobRef(Frozen):
    scope: str
    sha256: Sha256
    size: int = Field(ge=0)


class FileRef(Frozen):
    path: str
    blob: BlobRef
    media_type: str = "application/octet-stream"

    @field_validator("path")
    @classmethod
    def valid_path(cls, v):
        # Snapshot artifacts include /output; inputs are checked separately at freeze.
        if v.startswith("/output/"):
            return path_checked(v, output=True)
        return path_checked(v)


class BindingRef(Frozen):
    name: str
    version: str
    sha256: Sha256


class Snapshot(Frozen):
    schema_version: Literal["0.3"] = PROTOCOL
    provider: BindingRef
    revision: str
    scope: str
    files: tuple["InputFile", ...]


class SourceRef(Frozen):
    provider: BindingRef
    revision: str
    path: str


class InputFile(FileRef):
    source: SourceRef | None = None
    skill_name: str | None = None
    skill_description: str | None = None


class AccessLease(Frozen):
    """Host-only availability metadata, deliberately absent from content identities."""

    provider: BindingRef
    revision: str
    expires_at: int


class AdapterDescriptor(Frozen):
    name: str
    version: str
    native_format: str | None = None
    wire_versions: tuple[str, ...] = (PROTOCOL,)


class FallbackPolicy(Frozen):
    max_attempts: int = Field(default=0, strict=True, ge=0, le=5)
    allow_external_repetition: bool = False


class Limits(Frozen):
    files: int = Field(default=64, ge=1, le=1000)
    input_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    output_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    text_bytes: int = Field(default=32768, ge=1, le=131072)
    model_requests: Annotated[int, Field(strict=True, ge=1)] | None = None
    tools: Annotated[int, Field(strict=True, ge=1)] | None = None
    command_seconds: int = Field(default=90, ge=1, le=120)
    deadline_seconds: int = Field(default=420, ge=10, le=600)


class Profile(Frozen):
    schema_version: Literal["0.4"] = PROFILE_SCHEMA
    name: str
    version: str
    # Persisted compatibility identity is independent of distribution releases.
    package_version: Literal["0.1.0"] = "0.1.0"
    harness: str = "pydantic-ai"
    harness_version: str = "2.61.0"
    checkpoint_format: str = "pydantic-messages-v1"
    sandbox: str = "opensandbox"
    sdk_version: str = "0.1.16"
    model: str
    image: str
    allowed_tools: tuple[str, ...]
    limits: Limits = Limits()
    retention_seconds: int = Field(default=604800, ge=3600)
    collection_seconds: int = Field(default=1800, ge=60, le=86400)
    fallback: FallbackPolicy = FallbackPolicy()

    @model_validator(mode="before")
    @classmethod
    def require_schema_for_persisted(cls, value, info: ValidationInfo):
        persisted = info.mode == "json" or (info.context or {}).get("persisted_profile")
        if (
            isinstance(value, dict)
            and "schema_version" not in value
            and (persisted or "package_version" in value)
        ):
            raise ValueError("legacy_profile_requires_explicit_migration")
        if isinstance(value, dict) and value.get("schema_version") == "0.3":
            raise ValueError("profile_requires_explicit_reconfiguration")
        return value

    @classmethod
    def model_validate(cls, obj, **kwargs):
        context = {**(kwargs.pop("context", None) or {}), "persisted_profile": True}
        return super().model_validate(obj, context=context, **kwargs)

    @field_validator("model", "image")
    @classmethod
    def explicit_reference(cls, value):
        if not value or any(
            character.isspace() or not character.isprintable() for character in value
        ):
            raise ValueError("invalid_reference")
        return value

    def ref(self):
        return BindingRef(name=self.name, version=self.version, sha256=digest(canonical(self)))


class JobSpec(Frozen):
    protocol_version: Literal["0.3"] = PROTOCOL
    scope: str
    session_id: str
    session_revision: int = Field(ge=0)
    prompt_ref: BlobRef
    vfs_snapshot_ref: BlobRef
    effective_input_ref: BlobRef
    vfs_provider_ref: BindingRef
    tools_registry_ref: BindingRef
    runtime_profile_ref: BindingRef
    limits: Limits
    retention_seconds: int
    input_mode: Literal["inherit", "replace"] = "inherit"
    input_changes: dict[str, str | None] = Field(default_factory=dict)


class Handle(Frozen):
    run_id: str
    job_sha256: Sha256
    protocol_version: Literal["0.3"] = PROTOCOL


class Status(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


TERMINAL = {Status.SUCCEEDED, Status.FAILED, Status.CANCELLED, Status.NEEDS_REVIEW}


class Checkpoint(Frozen):
    schema_version: Literal["0.3"] = PROTOCOL
    harness: str
    adapter_version: str
    harness_version: str
    format_version: str
    checkpoint_ref: BlobRef
    file_snapshot_ref: BlobRef

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_identity(cls, value, info: ValidationInfo):
        if (
            isinstance(value, dict)
            and (info.mode == "json" or (info.context or {}).get("persisted_checkpoint"))
            and "schema_version" not in value
        ):
            raise ValueError("legacy_checkpoint_requires_explicit_migration")
        return value

    @classmethod
    def model_validate(cls, obj, **kwargs):
        context = {**(kwargs.pop("context", None) or {}), "persisted_checkpoint": True}
        return super().model_validate(obj, context=context, **kwargs)


class SessionVersion(Frozen):
    schema_version: Literal["0.3"] = PROTOCOL
    session_id: str
    revision: int
    run_id: str
    checkpoint: Checkpoint
    manifest_ref: BlobRef
    submitted_input_ref: BlobRef
    effective_input_ref: BlobRef
    output_snapshot_ref: BlobRef
    profile_ref: BindingRef
    tools_ref: BindingRef
    parent_manifest_ref: BlobRef | None = None


class SealedFile(Frozen):
    path: str
    size: int = Field(ge=0, le=128 * 1024 * 1024)
    sha256: Sha256
    media_type: str = "application/octet-stream"

    @field_validator("path")
    @classmethod
    def output_path(cls, value):
        return path_checked(value, output=True)


class OutputSeal(Frozen):
    schema_version: Literal["0.3"] = PROTOCOL
    run_id: str
    sandbox_id: str
    scope: str
    retain_until: int
    files: tuple[SealedFile, ...]

    @model_validator(mode="after")
    def unique_files(self):
        paths = [f.path for f in self.files]
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("invalid_output_manifest")
        return self


class RunResult(Frozen):
    handle: Handle
    status: Status
    manifest_ref: BlobRef | None = None
    artifacts: tuple[FileRef, ...] = ()
    session_version: SessionVersion | None = None
    error: str | None = None
    error_detail: SafeError | None = None
    collection_ref: BlobRef | None = None
    attempt_of: str | None = None
    cleanup: Literal["not_created", "destroyed", "pending"] = "not_created"
    expires_at: int

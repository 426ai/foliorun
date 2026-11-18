"""Protocol 0.2: closed, immutable metadata. No vendor or product imports."""

import hashlib
import json
import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION = "0.1.0"
PROTOCOL = "0.2"
MODEL = "deepseek/deepseek-v4.1-flash-1017"
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RuntimeFault(Exception):
    """Only stable codes cross the adapter/History/log boundary."""

    def __init__(self, code: str):
        self.code = code
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
    provider: BindingRef
    revision: str
    scope: str
    files: tuple[FileRef, ...]
    expires_at: int


class Limits(Frozen):
    files: int = Field(default=64, ge=1, le=1000)
    input_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    output_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    text_bytes: int = Field(default=32768, ge=1, le=131072)
    model_requests: int = Field(default=8, ge=1, le=16)
    tools: int = Field(default=20, ge=1, le=64)
    command_seconds: int = Field(default=90, ge=1, le=120)
    deadline_seconds: int = Field(default=420, ge=10, le=600)


class Profile(Frozen):
    name: str
    version: str
    package_version: Literal["0.1.0"] = VERSION
    harness: Literal["pydantic-ai"] = "pydantic-ai"
    harness_version: Literal["2.61.0"] = "2.61.0"
    checkpoint_format: Literal["pydantic-messages-v1"] = "pydantic-messages-v1"
    sandbox: Literal["opensandbox"] = "opensandbox"
    sdk_version: Literal["0.1.16"] = "0.1.16"
    model: Literal["deepseek/deepseek-v4.1-flash-1017"] = MODEL
    image: str
    image_manifest: Sha256
    skills_manifest: Sha256
    capabilities: tuple[str, ...] = ("docx-v1",)
    allowed_tools: tuple[str, ...]
    limits: Limits = Limits()
    retention_seconds: int = Field(default=604800, ge=3600)

    @field_validator("image")
    @classmethod
    def immutable_image(cls, value):
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", value):
            raise RuntimeFault("immutable_image_required")
        return value

    def ref(self):
        return BindingRef(name=self.name, version=self.version, sha256=digest(canonical(self)))


class JobSpec(Frozen):
    protocol_version: Literal["0.2"] = PROTOCOL
    scope: str
    session_id: str
    session_revision: int = Field(ge=0)
    prompt_ref: BlobRef
    vfs_snapshot_ref: BlobRef
    vfs_provider_ref: BindingRef
    tools_registry_ref: BindingRef
    runtime_profile_ref: BindingRef
    limits: Limits
    retention_seconds: int


class Handle(Frozen):
    run_id: str
    job_sha256: Sha256
    protocol_version: Literal["0.2"] = PROTOCOL


class Status(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


TERMINAL = {Status.SUCCEEDED, Status.FAILED, Status.CANCELLED, Status.NEEDS_REVIEW}


class Checkpoint(Frozen):
    harness: Literal["pydantic-ai"] = "pydantic-ai"
    adapter_version: Literal["0.1.0"] = VERSION
    harness_version: Literal["2.61.0"] = "2.61.0"
    format_version: Literal["pydantic-messages-v1"] = "pydantic-messages-v1"
    checkpoint_ref: BlobRef
    file_snapshot_ref: BlobRef


class SessionVersion(Frozen):
    session_id: str
    revision: int
    run_id: str
    checkpoint: Checkpoint
    manifest_ref: BlobRef


class RunResult(Frozen):
    handle: Handle
    status: Status
    manifest_ref: BlobRef | None = None
    artifacts: tuple[FileRef, ...] = ()
    session_version: SessionVersion | None = None
    error: str | None = None
    cleanup: Literal["not_created", "destroyed", "pending"] = "not_created"
    expires_at: int

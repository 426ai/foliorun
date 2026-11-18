"""Immutable catalogs, host-only renewable leases and verified on-demand reads."""

import mimetypes
import os
import stat
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import (
    AccessLease,
    BindingRef,
    BlobRef,
    InputFile,
    Limits,
    RuntimeFault,
    Snapshot,
    SourceRef,
    canonical,
    digest,
    path_checked,
)
from .paths import open_regular

MATERIALIZED = BindingRef(name="materialized-blobs", version="1", sha256=digest(b"materialized-v1"))


@dataclass(frozen=True)
class Entry:
    path: str
    sha256: str
    size: int
    media_type: str = "application/octet-stream"
    skill_name: str | None = None
    skill_description: str | None = None


@dataclass(frozen=True)
class VFSRevision:
    revision: str
    entries: tuple[Entry, ...]
    expires_at: int


class VirtualFS(Protocol):
    async def freeze(self) -> VFSRevision: ...
    async def list(self, revision: str, path: str) -> tuple[Entry, ...]: ...
    async def stat(self, revision: str, path: str) -> Entry: ...
    def open_read(
        self, revision: str, path: str
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]: ...


class MemoryFS:
    """Also serves as DBFrozenFS example: callers export an authorized consistent view first."""

    def __init__(self, files: dict[str, bytes], retention_seconds=604800):
        self._files = {path_checked(p): bytes(b) for p, b in files.items()}
        self._entries = tuple(
            Entry(
                p,
                digest(b),
                len(b),
                mimetypes.guess_type(p)[0] or "application/octet-stream",
                **skill_metadata(p, b),
            )
            for p, b in sorted(self._files.items())
        )
        self._revision = digest(
            b"".join(p.encode() + digest(b).encode() for p, b in sorted(self._files.items()))
        )
        self._expires = int(time.time()) + retention_seconds

    def _check(self, revision):
        if revision != self._revision or time.time() >= self._expires:
            raise RuntimeFault("input_changed")

    async def freeze(self):
        return VFSRevision(self._revision, self._entries, self._expires)

    async def list(self, revision, path="/"):
        self._check(revision)
        return tuple(e for e in self._entries if e.path.startswith(path.rstrip("/") + "/"))

    async def stat(self, revision, path):
        self._check(revision)
        for e in self._entries:
            if e.path == path:
                return e
        raise RuntimeFault("input_missing")

    @asynccontextmanager
    async def open_read(self, revision, path):
        await self.stat(revision, path)

        async def chunks():
            yield self._files[path]

        yield chunks()


class FilesFS(MemoryFS):
    """Capture caller-owned stable exports; refuse symlinks and detect changes across the set.

    The caller must prevent concurrent writes during capture. This is not a live filesystem
    or a transaction on an arbitrary changing directory. Subsequent reads use captured bytes.
    """

    def __init__(self, paths: dict[str, Path], retention_seconds=604800):
        before, bodies = {}, {}
        for virtual, local in paths.items():
            path_checked(virtual)
            local = Path(local)
            if any(p.is_symlink() for p in (local, *local.parents)):
                raise RuntimeFault("symlink_input")
            fd = open_regular(local)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > 128 * 1024 * 1024
                    or info.st_nlink != 1
                ):
                    raise RuntimeFault("invalid_input_file")
                before[virtual] = info
                bodies[virtual] = stream.read(128 * 1024 * 1024 + 1)
        for virtual, local in paths.items():
            fd = open_regular(local)
            try:
                info = os.fstat(fd)
            finally:
                os.close(fd)
            a = before[virtual]
            if (a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns) != (
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ):
                raise RuntimeFault("input_changed")
        super().__init__(bodies, retention_seconds)


def skill_metadata(path, body):
    if not path.startswith("/skills/") or not path.endswith("/SKILL.md"):
        return {}
    header = body[:8192].decode("utf-8", errors="replace")
    meta = {}
    if header.startswith("---\n") and "\n---" in header[4:]:
        for line in header.split("---", 2)[1].splitlines():
            key, sep, value = line.partition(":")
            if sep and key in {"name", "description"}:
                meta["skill_" + key] = value.strip()[:1024]
    return meta


def validate_catalog(files, limits):
    seen, total = set(), 0
    for file in files:
        path_checked(file.path)
        folded = file.path.casefold()
        if folded in seen or any(
            folded.startswith(p + "/") or p.startswith(folded + "/") for p in seen
        ):
            raise RuntimeFault("input_changed")
        seen.add(folded)
        total += file.blob.size
    if len(files) > limits.files or total > limits.input_bytes:
        raise RuntimeFault("byte_budget")


async def freeze_vfs(
    vfs: VirtualFS, store, limits: Limits, retention_seconds: int, *, bindings=None
):
    """Freeze metadata only. Providers must already know hashes and retain their revisions."""
    revision = await vfs.freeze()
    entries = await vfs.list(revision.revision, "/")
    if entries != revision.entries or len(entries) > limits.files:
        raise RuntimeFault("invalid_vfs_manifest")
    if revision.expires_at <= int(time.time()):
        raise RuntimeFault("input_retention_too_short")
    provider = getattr(vfs, "binding", None)
    if provider is None:
        provider = BindingRef(
            name="caller-vfs",
            version="1",
            sha256=digest(
                canonical({"revision": revision.revision, "entries": [e.__dict__ for e in entries]})
            ),
        )
    files = []
    for entry in entries:
        path_checked(entry.path)
        if await vfs.stat(revision.revision, entry.path) != entry:
            raise RuntimeFault("input_changed")
        files.append(
            InputFile(
                path=entry.path,
                blob=BlobRef(scope=store.scope, sha256=entry.sha256, size=entry.size),
                media_type=entry.media_type,
                source=SourceRef(provider=provider, revision=revision.revision, path=entry.path),
                skill_name=entry.skill_name,
                skill_description=entry.skill_description,
            )
        )
    validate_catalog(files, limits)
    if await vfs.list(revision.revision, "/") != entries:
        raise RuntimeFault("input_changed")
    snapshot = Snapshot(
        provider=provider,
        revision=revision.revision,
        scope=store.scope,
        files=tuple(files),
    )
    lease_key = "access/" + provider.sha256 + "/" + digest(revision.revision.encode())
    with store.transaction() as db:
        old = store.get_record(lease_key, db)
        catalog_hash = digest(canonical(snapshot))
        if old and old[1]["catalog_hash"] != catalog_hash:
            raise RuntimeFault("provider_revision_rebound")
        lease = AccessLease(
            provider=provider, revision=revision.revision, expires_at=revision.expires_at
        )
        store.cas(
            lease_key,
            old[0] if old else 0,
            {"catalog_hash": catalog_hash, "lease": lease.model_dump()},
            db,
        )
    if bindings is not None:
        bindings.install_vfs(provider, vfs)
    return snapshot


class LazyFiles:
    def __init__(self, store, bindings, files, limits):
        self.store, self.bindings, self.files, self.limits = store, bindings, files, limits
        self.materialized = set()

    async def read(self, path):
        file = self.files.get(path)
        if file is None:
            raise RuntimeFault("input_missing")
        try:
            return self.store.read(file.blob, self.limits.input_bytes)
        except RuntimeFault as exc:
            if exc.code != "blob_unavailable":
                raise
        source = getattr(file, "source", None)
        if source is None:
            raise RuntimeFault("input_unavailable")
        provider = self.bindings.vfs_providers.get(source.provider.sha256)
        if provider is None or provider[0] != source.provider:
            raise RuntimeFault("input_provider_unavailable")
        key = "access/" + source.provider.sha256 + "/" + digest(source.revision.encode())
        lease = self.store.get_record(key)
        if lease is None or time.time() >= lease[1]["lease"]["expires_at"]:
            raise RuntimeFault("input_lease_expired")
        vfs = provider[1]
        entry = await vfs.stat(source.revision, source.path)
        if (entry.size, entry.sha256) != (file.blob.size, file.blob.sha256):
            raise RuntimeFault("input_changed")
        data = bytearray()
        async with vfs.open_read(source.revision, source.path) as chunks:
            async for chunk in chunks:
                data.extend(chunk)
                if len(data) > file.blob.size or len(data) > self.limits.input_bytes:
                    raise RuntimeFault("byte_budget")
        if len(data) != file.blob.size or digest(data) != file.blob.sha256:
            raise RuntimeFault("input_changed")
        if await vfs.stat(source.revision, source.path) != entry:
            raise RuntimeFault("input_changed")
        ref = self.store.put(bytes(data))
        if ref != file.blob:
            raise RuntimeFault("input_changed")
        return bytes(data)

    async def materialize(self, box, paths):
        for path in paths:
            if path not in self.materialized:
                body = await self.read(path)
                await box.materialize(path, body, self.files[path])
                self.materialized.add(path)

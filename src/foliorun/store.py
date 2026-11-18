"""Injectable immutable blobs and a genuinely transactional session index (local by default)."""

import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .contracts import BlobRef, RuntimeFault, canonical, digest, identifier
from .paths import parent_directory


class BlobStore(Protocol):
    scope: str

    def put(self, data: bytes) -> BlobRef: ...
    def read(self, ref: BlobRef, limit: int = 128 * 1024 * 1024) -> bytes: ...
    def delete(self, ref: BlobRef) -> None: ...


class SessionRepository(Protocol):
    """Operations sharing opaque tx must share one serializable transaction.

    CAS conflicts must abort; scan must see writes in that transaction. The host adapter
    implements these guarantees in its database, not with object listing and PUTs.
    """

    scope: str

    def transaction(self): ...
    def get_record(self, key: str, db=None): ...
    def cas(self, key: str, expected: int, value: dict, db=None) -> int: ...
    def scan(self, prefix: str, db=None) -> list: ...
    def remove(self, key: str, db) -> None: ...


def private_root(root, scope):
    root = Path(root)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise RuntimeFault("unsafe_store_root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root.resolve() / identifier(scope)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeFault("unsafe_store_root")
    return directory


class LocalSessionRepository:
    """SQLite BEGIN IMMEDIATE + FULL/WAL; local disk only, not an NFS/distributed lock."""

    def __init__(self, root: Path, scope: str):
        self.scope = identifier(scope)
        self.root = private_root(root, scope)
        with self.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, "
                "revision INTEGER NOT NULL, value TEXT NOT NULL)"
            )

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.root / "cas.sqlite", timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get_record(self, key, db=None):
        if db is None:
            with self.transaction() as tx:
                return self.get_record(key, tx)
        row = db.execute("SELECT revision,value FROM records WHERE key=?", (key,)).fetchone()
        return None if row is None else (row[0], json.loads(row[1]))

    def cas(self, key, expected, value, db=None):
        if db is None:
            with self.transaction() as tx:
                return self.cas(key, expected, value, tx)
        old = self.get_record(key, db)
        actual = old[0] if old else 0
        if actual != expected:
            raise RuntimeFault("cas_conflict")
        revision = actual + 1
        db.execute(
            "INSERT INTO records VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "revision=excluded.revision,value=excluded.value",
            (key, revision, canonical(value).decode()),
        )
        return revision

    def scan(self, prefix="", db=None):
        if db is None:
            with self.transaction() as tx:
                return self.scan(prefix, tx)
        return [
            (key, revision, json.loads(value))
            for key, revision, value in db.execute(
                "SELECT key,revision,value FROM records ORDER BY key"
            )
            if key.startswith(prefix)
        ]

    def remove(self, key, db):
        db.execute("DELETE FROM records WHERE key=?", (key,))


class LocalFSBlobStore:
    def __init__(self, root: Path, scope: str):
        self.scope = identifier(scope)
        self.root = private_root(root, scope)
        self.blobs = self.root / "blobs"
        if self.blobs.is_symlink():
            raise RuntimeFault("unsafe_store_root")
        self.blobs.mkdir(mode=0o700, exist_ok=True)

    def put(self, data: bytes) -> BlobRef:
        ref = BlobRef(scope=self.scope, sha256=digest(data), size=len(data))
        path = self.blobs / ref.sha256
        if path.exists():
            self.read(ref)
            return ref
        tmp = self.blobs / ("." + uuid4().hex)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            folder = os.open(self.blobs, os.O_DIRECTORY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        finally:
            tmp.unlink(missing_ok=True)
        self.read(ref)
        return ref

    def read(self, ref: BlobRef, limit=128 * 1024 * 1024) -> bytes:
        if ref.scope != self.scope:
            raise RuntimeFault("scope_mismatch")
        if ref.size > limit:
            raise RuntimeFault("byte_budget")
        try:
            fd = os.open(self.blobs / ref.sha256, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RuntimeFault("blob_integrity")
                body = stream.read(limit + 1)
        except OSError:
            raise RuntimeFault("blob_unavailable") from None
        return verified(ref, body, limit)

    def delete(self, ref):
        if ref.scope != self.scope:
            raise RuntimeFault("scope_mismatch")
        (self.blobs / ref.sha256).unlink(missing_ok=True)
        folder = os.open(self.blobs, os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


def verified(ref, data, limit):
    if ref.size > limit:
        raise RuntimeFault("byte_budget")
    if len(data) != ref.size or digest(data) != ref.sha256:
        raise RuntimeFault("blob_integrity")
    return data


class S3BlobStore:
    """Optional caller-owned synchronous client; no cloud SDK or index emulation.

    The service must support atomic IfNoneMatch='*'. No bucket/list/lifecycle operations.
    Credentials/client never enter serializable records.
    """

    def __init__(self, *, client, bucket: str, prefix: str, scope: str):
        if not bucket or not prefix.strip("/") or ".." in prefix.split("/"):
            raise RuntimeFault("invalid_blob_prefix")
        self.client, self.bucket = client, bucket
        self.scope = identifier(scope)
        self.prefix = prefix.strip("/") + "/" + self.scope + "/blobs/"

    def _key(self, ref):
        if ref.scope != self.scope:
            raise RuntimeFault("scope_mismatch")
        return self.prefix + ref.sha256

    def put(self, data):
        ref = BlobRef(scope=self.scope, sha256=digest(data), size=len(data))
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=self._key(ref), Body=data, IfNoneMatch="*"
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "412"}:
                raise RuntimeFault("blob_write_unavailable") from None
        self.read(ref)
        return ref

    def read(self, ref, limit=128 * 1024 * 1024):
        key = self._key(ref)
        if ref.size > limit:
            raise RuntimeFault("byte_budget")
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
            stream = result["Body"]
            try:
                data = stream.read(limit + 1)
            finally:
                stream.close()
        except Exception:
            raise RuntimeFault("blob_unavailable") from None
        return verified(ref, data, limit)

    def delete(self, ref):
        key = self._key(ref)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            raise RuntimeFault("blob_delete_unavailable") from None


class Storage:
    """Composition for blobs + transactional index. Managed writes pin before uploading."""

    def __init__(self, *, blobs: BlobStore, sessions: SessionRepository):
        if blobs.scope != sessions.scope:
            raise RuntimeFault("scope_mismatch")
        self.blob_store, self.repository, self.scope = blobs, sessions, blobs.scope

    def transaction(self):
        return self.repository.transaction()

    def get_record(self, key, db=None):
        return self.repository.get_record(key, db)

    def cas(self, key, expected, value, db=None):
        if db is None:
            with self.transaction() as tx:
                return self.cas(key, expected, value, tx)

        def references(item):
            refs = set()
            if isinstance(item, dict):
                if {"scope", "sha256", "size"} <= item.keys():
                    ref = BlobRef.model_validate(
                        {field: item[field] for field in ("scope", "sha256", "size")}
                    )
                    refs.add((ref.scope, ref.sha256, ref.size))
                else:
                    for child in item.values():
                        refs.update(references(child))
            elif isinstance(item, (tuple, list)):
                for child in item:
                    refs.update(references(child))
            return refs

        old = self.get_record(key, db)
        if (old[0] if old else 0) != expected:
            raise RuntimeFault("cas_conflict")
        previous = references(old[1]) if old else set()
        # Re-reference is checked under GC's exclusion lock, not before acquiring it.
        for scope, sha, size in references(value) - previous:
            self.read(BlobRef(scope=scope, sha256=sha, size=size))
        return self.repository.cas(key, expected, value, db)

    def put(self, data: bytes):
        ref = BlobRef(scope=self.scope, sha256=digest(data), size=len(data))
        # GC, managed put and re-reference serialize through the same index lock.
        with self.transaction() as db:
            key = "blob/" + ref.sha256
            old = self.get_record(key, db)
            self.repository.cas(
                key,
                old[0] if old else 0,
                {"ref": ref.model_dump(), "protected_until": time.time() + 3600},
                db,
            )
            actual = self.blob_store.put(data)
            if actual != ref:
                raise RuntimeFault("blob_integrity")
            self.read(ref)
        return ref

    def put_json(self, value):
        return self.put(canonical(value))

    def read(self, ref, limit=128 * 1024 * 1024):
        if ref.scope != self.scope:
            raise RuntimeFault("scope_mismatch")
        return verified(ref, self.blob_store.read(ref, limit), limit)

    def read_json(self, ref):
        return json.loads(self.read(ref))

    def release_run(self, run_id):
        """Drop expired, quiescent run roots; keep sessions/artifacts and idempotency identity."""
        key = "run/" + identifier(run_id)
        with self.transaction() as db:
            old = self.get_record(key, db)
            if old is None:
                raise RuntimeFault("run_unavailable")
            state = old[1]
            if state.get("released"):
                return
            if (
                not state["terminal"]
                or not state.get("quiescent")
                or state["cleanup"] == "pending"
                or time.time() < state["expires_at"]
                or state["result"]["status"] == "NEEDS_REVIEW"
            ):
                raise RuntimeFault("run_still_required")
            for child_key, _, _ in self.repository.scan("journal/" + run_id + "/", db):
                self.repository.remove(child_key, db)
            self.repository.remove("collection/" + run_id, db)
            self.cas(
                key,
                old[0],
                {
                    "handle": state["handle"],
                    "terminal": True,
                    "released": True,
                    "cleanup": state["cleanup"],
                    "expires_at": state["expires_at"],
                    "result": {
                        "handle": state["handle"],
                        "status": "FAILED",
                        "error": "run_expired",
                        "cleanup": state["cleanup"],
                        "expires_at": state["expires_at"],
                    },
                },
                db,
            )

    def expire_artifacts(self):
        with self.transaction() as db:
            for key, _, value in self.repository.scan("artifact/", db):
                if time.time() >= value["expires_at"]:
                    self.repository.remove(key, db)

    def gc(self, *, grace_seconds=3600):
        """Conservative graph mark/sweep. Never expires roots or touches unknown objects."""
        if grace_seconds < 3600:
            raise RuntimeFault("gc_grace_too_short")
        with self.transaction() as db:
            rows = self.repository.scan("", db)
            catalog = {k[5:]: (r, v) for k, r, v in rows if k.startswith("blob/")}
            marked = set()

            def mark(value):
                if isinstance(value, dict):
                    if {"scope", "sha256", "size"} <= value.keys():
                        ref = BlobRef.model_validate(
                            {k: value[k] for k in ("scope", "sha256", "size")}
                        )
                        if ref.scope != self.scope or ref.sha256 in marked:
                            return
                        marked.add(ref.sha256)
                        if ref.sha256 in catalog:
                            body = self.read(ref)
                            try:
                                nested = json.loads(body)
                            except (ValueError, UnicodeError):
                                return
                            mark(nested)
                    else:
                        for child in value.values():
                            mark(child)
                elif isinstance(value, list):
                    for child in value:
                        mark(child)

            for key, _, value in rows:
                if not key.startswith("blob/"):
                    mark(value)
            removed = []
            for sha, (revision, value) in catalog.items():
                key = "blob/" + sha
                if sha in marked:
                    if value.get("orphan_since") is not None:
                        self.cas(key, revision, {**value, "orphan_since": None}, db)
                    continue
                if time.time() < value["protected_until"]:
                    continue
                if value.get("orphan_since") is None:
                    self.cas(key, revision, {**value, "orphan_since": time.time()}, db)
                elif time.time() >= value["orphan_since"] + grace_seconds:
                    ref = BlobRef.model_validate(value["ref"])
                    self.blob_store.delete(ref)
                    self.repository.remove(key, db)
                    removed.append(sha)
            return tuple(removed)

    async def open(self, ref):
        body = self.read(ref)
        for offset in range(0, len(body), 65536):
            yield body[offset : offset + 65536]

    async def download_to(self, artifact, destination: Path, *, expires_at: int):
        # Low-level export; Runtime.artifacts performs receipt authorization first.
        if time.time() >= expires_at:
            raise RuntimeFault("artifact_expired")
        body = self.read(artifact.blob)
        with parent_directory(Path(destination), create=True) as (parent, leaf):
            temp = "." + uuid4().hex
            try:
                fd = os.open(
                    temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temp, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            finally:
                os.unlink(temp, dir_fd=parent)


class LocalArtifactStore(Storage):
    def __init__(self, root: Path, scope: str):
        blobs = LocalFSBlobStore(root, scope)
        super().__init__(blobs=blobs, sessions=LocalSessionRepository(root, scope))
        self.root, self.blobs = blobs.root, blobs.blobs

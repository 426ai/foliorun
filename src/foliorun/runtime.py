"""Public SDK and host-side bindings. Submit freezes data; never starts services."""

import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .contracts import (
    BlobRef,
    Handle,
    JobSpec,
    Profile,
    RunResult,
    RuntimeFault,
    SessionVersion,
    Snapshot,
    Status,
    canonical,
    digest,
    identifier,
)
from .vfs import FilesFS, freeze_vfs, validate_catalog


@dataclass(frozen=True)
class RuntimeConfig:
    profile: Profile
    scheduler: object


class WorkerBindings:
    def __init__(self):
        self.profiles, self.registries = {}, {}
        self.vfs_providers = {}

    def install_vfs(self, ref, provider):
        """Reinstall retained providers on each worker; never serialize clients or credentials."""
        self.vfs_providers[ref.sha256] = (ref, provider)
        return ref

    def install_profile(self, profile):
        ref = profile.ref()
        self.profiles[ref.sha256] = Profile.model_validate_json(canonical(profile))
        return ref

    def install_tools(self, registry):
        ref = registry.seal()
        self.registries[ref.sha256] = registry
        return ref

    def require(self, job):
        profile = self.profiles.get(job.runtime_profile_ref.sha256)
        registry = self.registries.get(job.tools_registry_ref.sha256)
        if (
            profile is None
            or profile.ref() != job.runtime_profile_ref
            or registry is None
            or registry.seal() != job.tools_registry_ref
        ):
            raise RuntimeFault("binding_unavailable")
        if job.limits != profile.limits or job.retention_seconds != profile.retention_seconds:
            raise RuntimeFault("profile_mismatch")
        return profile, registry


class WaitTimeout(TimeoutError):
    def __init__(self, handle):
        self.handle = handle
        super().__init__("wait_timeout")


class Runtime:
    def __init__(self, *, config: RuntimeConfig, bindings: WorkerBindings, store):
        self.config, self.bindings, self.store = config, bindings, store
        self.artifacts = ArtifactAccess(store)
        self.sessions = Sessions(store)
        bindings.install_profile(config.profile)

    async def submit(
        self,
        *,
        prompt,
        vfs,
        tools,
        idempotency_key,
        session_id=None,
        session_revision=0,
        input_mode="inherit",
        overrides=None,
        tombstones=None,
    ):
        profile = self.config.profile
        if not isinstance(prompt, str) or not 0 < len(prompt.encode()) <= profile.limits.text_bytes:
            raise RuntimeFault("invalid_prompt_budget")
        registry_ref = tools.seal()
        self.store.put(tools.descriptor)
        self.store.put_json(profile)
        if registry_ref.sha256 not in self.bindings.registries:
            raise RuntimeFault("binding_unavailable")
        if input_mode not in {"inherit", "replace"}:
            raise RuntimeFault("invalid_input_mode")
        snapshot = await freeze_vfs(
            vfs, self.store, profile.limits, profile.retention_seconds, bindings=self.bindings
        )
        key = "idempotency/" + digest(idempotency_key.encode())
        # Retries without an explicit session id recover the original allocation.
        old = self.store.get_record(key)
        sid = session_id or (old[1]["session_id"] if old else uuid4().hex)
        identifier(sid)
        prior = self.store.get_record("session/" + sid)
        previous = None
        if session_revision:
            if not prior or len(prior[1]["versions"]) < session_revision:
                raise RuntimeFault("session_conflict")
            previous = SessionVersion.model_validate(prior[1]["versions"][session_revision - 1])
            if previous.profile_ref != profile.ref() or previous.tools_ref != registry_ref:
                raise RuntimeFault("session_binding_mismatch")
        overrides, tombstones = overrides or {}, tombstones or {}
        if set(overrides) & set(tombstones):
            raise RuntimeFault("input_change_conflict")
        changes = {**overrides, **tombstones}
        inherited = (
            self.snapshot(previous.effective_input_ref).files
            if previous and input_mode == "inherit"
            else ()
        )
        merged = {f.path: f for f in inherited}
        incoming = {f.path: f for f in snapshot.files}
        for path, expected in changes.items():
            if path not in merged or merged[path].blob.sha256 != expected:
                raise RuntimeFault("input_override_conflict")
        for path in tombstones:
            if path in incoming:
                raise RuntimeFault("input_change_conflict")
            del merged[path]
        for path, file in incoming.items():
            old_file = merged.get(path)
            if old_file and old_file.blob != file.blob and path not in overrides:
                raise RuntimeFault("input_override_required")
            merged[path] = file
        if set(overrides) - incoming.keys():
            raise RuntimeFault("input_override_missing")
        validate_catalog(tuple(merged.values()), profile.limits)
        effective = Snapshot(
            provider=snapshot.provider,
            scope=self.store.scope,
            revision=digest(canonical([merged[p].model_dump() for p in sorted(merged)])),
            files=tuple(merged[p] for p in sorted(merged)),
        )
        job = JobSpec(
            scope=self.store.scope,
            session_id=sid,
            session_revision=session_revision,
            prompt_ref=self.store.put(prompt.encode()),
            vfs_snapshot_ref=self.store.put_json(snapshot),
            effective_input_ref=self.store.put_json(effective),
            vfs_provider_ref=snapshot.provider,
            tools_registry_ref=registry_ref,
            runtime_profile_ref=profile.ref(),
            limits=profile.limits,
            retention_seconds=profile.retention_seconds,
            input_mode=input_mode,
            input_changes=changes,
        )
        self.bindings.require(job)
        job_ref = self.store.put_json(job)
        with self.store.transaction() as db:
            old = self.store.get_record(key, db)
            if old:
                if old[1]["job_sha256"] != job_ref.sha256:
                    raise RuntimeFault("idempotency_conflict")
                handle = Handle.model_validate(old[1]["handle"])
            else:
                sess = self.store.get_record("session/" + sid, db)
                current = sess[1] if sess else {"revision": 0, "active": None, "versions": []}
                if (
                    current["revision"] != session_revision
                    or current["active"]
                    or current.get("state", "open") != "open"
                ):
                    raise RuntimeFault("session_conflict")
                handle = Handle(run_id=uuid4().hex, job_sha256=job_ref.sha256)
                self.store.cas(
                    "session/" + sid,
                    sess[0] if sess else 0,
                    {**current, "active": handle.run_id, "state": "busy"},
                    db,
                )
                self.store.cas(
                    "run/" + handle.run_id,
                    0,
                    {
                        "handle": handle.model_dump(),
                        "job_ref": job_ref.model_dump(),
                        "cancelled": False,
                        "terminal": False,
                        "prepared": None,
                        "cleanup": "not_created",
                        "sandbox_id": None,
                        "expires_at": int(time.time()) + profile.retention_seconds,
                    },
                    db,
                )
                self.store.cas(
                    key,
                    0,
                    {
                        "job_sha256": job_ref.sha256,
                        "session_id": sid,
                        "handle": handle.model_dump(),
                    },
                    db,
                )
        # Fixed run_id repairs a crash between CAS binding and Temporal start.
        await self.config.scheduler.start(handle, job_ref)
        return handle

    async def get(self, handle):
        record = self.store.get_record("run/" + handle.run_id)
        if record is None or record[1]["handle"] != handle.model_dump():
            raise RuntimeFault("run_unavailable")
        while record[1].get("next_attempt"):
            record = self.store.get_record("run/" + record[1]["next_attempt"])
        if record[1].get("result"):
            result = RunResult.model_validate(record[1]["result"])
            if record[1]["terminal"]:
                return result
        if (record[1].get("prepared") or {}).get("status") == "RECOVERY_PENDING":
            return RunResult(
                handle=Handle.model_validate(record[1]["handle"]),
                status=Status.RECOVERY_PENDING,
                cleanup=record[1]["cleanup"],
                expires_at=record[1]["expires_at"],
                collection_ref=BlobRef.model_validate(record[1]["prepared"]["collection_ref"]),
                error=record[1]["prepared"].get("error"),
                error_detail=record[1]["prepared"].get("error_detail"),
                attempt_of=record[1].get("attempt_of"),
            )
        status = await self.config.scheduler.status(handle)
        return RunResult(handle=handle, status=Status(status), expires_at=record[1]["expires_at"])

    async def wait(self, handle, timeout=None):
        try:
            await self.config.scheduler.wait(handle, timeout)
        except TimeoutError:
            raise WaitTimeout(handle) from None
        return await self.get(handle)

    async def cancel(self, handle):
        current = await self.get(handle)
        if current.status in {
            Status.SUCCEEDED,
            Status.FAILED,
            Status.CANCELLED,
            Status.NEEDS_REVIEW,
        }:
            return False
        return await self.config.scheduler.cancel(handle)

    async def run(self, **kwargs):
        return await self.wait(await self.submit(**kwargs))

    async def run_files(
        self, *, prompt, documents, tools, idempotency_key, skills_dir=None, **session
    ):
        paths = {"/documents/" + Path(p).name: Path(p) for p in documents}
        if len(paths) != len(documents):
            raise RuntimeFault("duplicate_input_path")
        if skills_dir:
            root = Path(skills_dir)
            for local in root.rglob("*"):
                if local.is_symlink():
                    raise RuntimeFault("symlink_input")
                if local.is_file():
                    paths["/skills/" + local.relative_to(root).as_posix()] = local
        return await self.run(
            prompt=prompt,
            vfs=FilesFS(paths, self.config.profile.retention_seconds + 60),
            tools=tools,
            idempotency_key=idempotency_key,
            **session,
        )

    def session_versions(self, session_id):
        record = self.store.get_record("session/" + identifier(session_id))
        if record is None:
            raise RuntimeFault("session_unavailable")
        return tuple(SessionVersion.model_validate(v) for v in record[1]["versions"])

    def snapshot(self, ref):
        return Snapshot.model_validate(self.store.read_json(ref))

    async def checkpoint(self, handle):
        result = await self.get(handle)
        if result.status != Status.SUCCEEDED or result.session_version is None:
            raise RuntimeFault("checkpoint_unavailable")
        return result.session_version

    def collection(self, handle):
        record = self.store.get_record("run/" + handle.run_id)
        if record is None or record[1]["handle"] != handle.model_dump():
            raise RuntimeFault("run_unavailable")
        from .collection import Collector

        return Collector(self.store).state(handle.run_id)


class ArtifactAccess:
    def __init__(self, store):
        self.store = store

    async def download_to(self, artifact, destination, *, expires_at=None):
        key = "artifact/" + digest(canonical(artifact))
        receipt = self.store.get_record(key)
        if not receipt or receipt[1]["file"] != artifact.model_dump():
            raise RuntimeFault("artifact_not_committed")
        expiry = receipt[1]["expires_at"]
        if expires_at is not None:
            expiry = min(expiry, expires_at)
        await self.store.download_to(artifact, destination, expires_at=expiry)


class Sessions:
    def __init__(self, store):
        self.store = store

    def get(self, session_id):
        record = self.store.get_record("session/" + identifier(session_id))
        if record is None:
            raise RuntimeFault("session_unavailable")
        return record[1]

    def create(self, session_id=None):
        sid = identifier(session_id or uuid4().hex)
        self.store.cas(
            "session/" + sid,
            0,
            {
                "revision": 0,
                "active": None,
                "versions": [],
                "state": "open",
            },
        )
        return sid

    def import_revision(self, revision: SessionVersion):
        """Explicit new session only; legacy source records and old head remain untouched."""
        if revision.revision != 1:
            raise RuntimeFault("invalid_import_revision")
        self.store.read(revision.manifest_ref)
        self.store.cas(
            "session/" + identifier(revision.session_id),
            0,
            {
                "revision": 1,
                "active": None,
                "versions": [revision.model_dump()],
                "state": "open",
            },
        )

    def close(self, session_id, *, expected_revision):
        self._terminal(session_id, expected_revision, "closed")

    def delete(self, session_id, *, expected_revision):
        self._terminal(session_id, expected_revision, "deleted")

    def _terminal(self, session_id, expected_revision, state):
        key = "session/" + identifier(session_id)
        with self.store.transaction() as db:
            old = self.store.get_record(key, db)
            if not old:
                raise RuntimeFault("session_unavailable")
            current = old[1]
            if current["active"] or current.get("state") == "review":
                raise RuntimeFault("session_busy")
            if current["revision"] != expected_revision:
                raise RuntimeFault("session_conflict")
            self.store.cas(
                key,
                old[0],
                {
                    **current,
                    "state": state,
                    "versions": [] if state == "deleted" else current["versions"],
                },
                db,
            )

"""Single-use agent attempts, collector-only recovery and fenced revision publication."""

import asyncio
import logging
import time
from uuid import uuid4

from .collection import Collector
from .contracts import (
    BlobRef,
    Checkpoint,
    FileRef,
    Handle,
    InputFile,
    JobSpec,
    RunResult,
    RuntimeFault,
    SafeError,
    SessionVersion,
    Snapshot,
    Status,
    canonical,
    digest,
)
from .tools import Gateway
from .vfs import MATERIALIZED, LazyFiles, validate_catalog

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, *, store, bindings, sandbox, harness):
        self.store, self.bindings, self.sandbox, self.harness = store, bindings, sandbox, harness

    def update_run(self, run_id, **values):
        with self.store.transaction() as db:
            old = self.store.get_record("run/" + run_id, db)
            updated = {**old[1], **values}
            if updated.get("result") and "cleanup" in values:
                updated["result"] = {**updated["result"], "cleanup": values["cleanup"]}
            self.store.cas("run/" + run_id, old[0], updated, db)

    def cancel_fence(self, run_id):
        with self.store.transaction() as db:
            old = self.store.get_record("run/" + run_id, db)
            if old is None:
                raise RuntimeFault("run_unavailable")
            while old[1].get("next_attempt"):
                run_id = old[1]["next_attempt"]
                old = self.store.get_record("run/" + run_id, db)
            if old[1]["terminal"]:
                return False
            self.store.cas("run/" + run_id, old[0], {**old[1], "cancelled": True}, db)
            return True

    def job(self, state):
        return JobSpec.model_validate(
            self.store.read_json(BlobRef.model_validate(state["job_ref"]))
        )

    def _claim(self, run_id, job, profile):
        with self.store.transaction() as db:
            current = self.store.get_record("run/" + run_id, db)
            if current[1]["cancelled"]:
                return self._failure(run_id, "cancelled", db=db)
            if current[1].get("claimed"):
                return self._failure(run_id, "unknown_execution_window", db=db)
            sess = self.store.get_record("session/" + job.session_id, db)
            if sess[1]["active"] != run_id or sess[1]["revision"] != job.session_revision:
                raise RuntimeFault("session_conflict")
            fence = sess[1].get("fence", 0) + 1
            lease = {
                "owner": uuid4().hex,
                "fence": fence,
                "expires_at": time.time()
                + job.limits.deadline_seconds
                + profile.collection_seconds,
            }
            self.store.cas("session/" + job.session_id, sess[0], {**sess[1], "fence": fence}, db)
            self.store.cas(
                "run/" + run_id,
                current[0],
                {
                    **current[1],
                    "claimed": True,
                    "lease": lease,
                },
                db,
            )
        return None

    async def execute(self, run_id, job_ref):
        record = self.store.get_record("run/" + run_id)[1]
        if record["job_ref"] != job_ref.model_dump():
            raise RuntimeFault("job_mismatch")
        if record["prepared"]:
            return record["prepared"]
        job = self.job(record)
        if job.scope != self.store.scope:
            raise RuntimeFault("scope_mismatch")
        profile, registry = self.bindings.require(job)
        self.harness.validate_profile(profile)
        descriptor = self.sandbox.descriptor
        if descriptor.name != profile.sandbox or descriptor.version != profile.sdk_version:
            raise RuntimeFault("sandbox_incompatible")
        failed = self._claim(run_id, job, profile)
        if failed:
            return failed
        box, prepared, error, error_detail = None, None, None, None
        try:
            snapshot = Snapshot.model_validate(self.store.read_json(job.effective_input_ref))
            if snapshot.scope != job.scope:
                raise RuntimeFault("scope_mismatch")
            files = {f.path: f for f in snapshot.files}
            validate_catalog(tuple(files.values()), job.limits)
            if any(f.blob.scope != job.scope for f in files.values()):
                raise RuntimeFault("scope_mismatch")
            checkpoint = None
            if job.session_revision:
                session = self.store.get_record("session/" + job.session_id)[1]
                previous = SessionVersion.model_validate(
                    session["versions"][job.session_revision - 1]
                )
                if (
                    previous.profile_ref != job.runtime_profile_ref
                    or previous.tools_ref != job.tools_registry_ref
                ):
                    raise RuntimeFault("session_binding_mismatch")
                old_files = Snapshot.model_validate(
                    self.store.read_json(previous.output_snapshot_ref)
                )
                for file in old_files.files:
                    files["/previous/" + file.path.removeprefix("/output/")] = file
                if not record.get("skip_native"):
                    self.harness.validate_checkpoint(previous.checkpoint)
                    checkpoint = self.store.read(previous.checkpoint.checkpoint_ref)
                    try:
                        self.harness.restore(checkpoint)
                    except Exception:
                        raise RuntimeFault("native_restore_failed") from None
            if (
                len(files) > job.limits.files
                or sum(f.blob.size for f in files.values()) > job.limits.input_bytes
            ):
                raise RuntimeFault("restore_budget")
            async with asyncio.timeout(job.limits.deadline_seconds):
                box = await self.sandbox.create(profile, run_id, job.scope)
                self.update_run(
                    run_id, sandbox_id=box.id, cleanup="pending", retain_until=box.retain_until
                )
                await box.prepare(files, self.store, profile)
                lazy = LazyFiles(self.store, self.bindings, files, job.limits)
                gateway = Gateway(self.store, run_id, job, profile, registry, box, files, lazy=lazy)
                gateway.check()
                checkpoint_bytes = await self.harness.run(
                    self.store.read(job.prompt_ref).decode(), gateway, checkpoint
                )
                gateway.check()
                if not gateway.submitted:
                    raise RuntimeFault("result_not_submitted")
                native = self.store.put(checkpoint_bytes)
                # Export must complete durably before a collector can publish a session.
                metadata = self.harness.checkpoint(native, native).model_dump(
                    exclude={"checkpoint_ref", "file_snapshot_ref"}
                )
                self.update_run(
                    run_id,
                    native_ref=native.model_dump(),
                    checkpoint_metadata=metadata,
                    agent_finished=True,
                )
            state = Collector(self.store).state(run_id)
            artifacts = await Collector(self.store).collect(
                run_id, state["seal_ref"]["sha256"], box
            )
            prepared = self._prepare(run_id, artifacts)
        except RuntimeFault as exc:
            error = exc.code
            error_detail = exc.detail
        except asyncio.CancelledError:
            error = "unknown_execution_window"
        except Exception:
            error = "adapter_failed"
            error_detail = SafeError()
        finally:
            state = Collector(self.store).state(run_id)
            if prepared is None:
                if (
                    state
                    and state["seal_ref"]
                    and self.store.get_record("run/" + run_id)[1].get("agent_finished")
                    and error != "cancelled"
                ):
                    prepared = {
                        "status": "RECOVERY_PENDING",
                        "error": error or "collection_failed",
                        "collection_ref": state["seal_ref"],
                    }
                    if error_detail is not None:
                        prepared["error_detail"] = error_detail.model_dump()
                    self.update_run(run_id, prepared=prepared)
                else:
                    prepared = self._failure(
                        run_id, error or "unknown_execution_window", detail=error_detail
                    )
            self.update_run(run_id, quiescent=True)
            if box:
                keep = state and state["phase"] != "complete"
                if keep:
                    # Close the SDK transport, not the service resource. Its deadline is persisted.
                    try:
                        await box.close()
                    except Exception:
                        pass
                else:
                    await self._destroy(run_id, box)
        return prepared

    def _prepare(self, run_id, artifacts):
        record = self.store.get_record("run/" + run_id)[1]
        job = self.job(record)
        state = Collector(self.store).state(run_id)
        if state["phase"] != "complete":
            raise RuntimeFault("collection_incomplete")
        output = self.store.put_json(
            Snapshot(
                provider=MATERIALIZED,
                revision=state["seal_ref"]["sha256"],
                scope=job.scope,
                files=tuple(InputFile.model_validate(f.model_dump()) for f in artifacts),
            )
        )
        native = BlobRef.model_validate(record["native_ref"])
        self.store.read(native)
        checkpoint = Checkpoint(
            **record["checkpoint_metadata"], checkpoint_ref=native, file_snapshot_ref=output
        )
        parent = None
        if job.session_revision:
            session = self.store.get_record("session/" + job.session_id)[1]
            parent = SessionVersion.model_validate(
                session["versions"][job.session_revision - 1]
            ).manifest_ref
        manifest = self.store.put_json(
            {
                "schema_version": "0.3",
                "run_id": run_id,
                "seal_ref": state["seal_ref"],
                "files": [f.model_dump() for f in artifacts],
                "submitted_input_ref": job.vfs_snapshot_ref.model_dump(),
                "effective_input_ref": job.effective_input_ref.model_dump(),
                "native": checkpoint.model_dump(),
                "output_snapshot_ref": output.model_dump(),
                "profile_ref": job.runtime_profile_ref.model_dump(),
                "tools_ref": job.tools_registry_ref.model_dump(),
                "parent": parent.model_dump() if parent else None,
            }
        )
        version = SessionVersion(
            session_id=job.session_id,
            revision=job.session_revision + 1,
            run_id=run_id,
            manifest_ref=manifest,
            checkpoint=checkpoint,
            submitted_input_ref=job.vfs_snapshot_ref,
            effective_input_ref=job.effective_input_ref,
            output_snapshot_ref=output,
            profile_ref=job.runtime_profile_ref,
            tools_ref=job.tools_registry_ref,
            parent_manifest_ref=parent,
        )
        prepared = {
            "status": "SUCCEEDED",
            "manifest_ref": manifest.model_dump(),
            "artifacts": [f.model_dump() for f in artifacts],
            "session_version": version.model_dump(),
            "collection_ref": state["seal_ref"],
        }
        self.update_run(run_id, prepared=prepared)
        return prepared

    async def _destroy(self, run_id, box):
        try:
            await asyncio.shield(box.destroy())
            self.update_run(run_id, cleanup="destroyed")
        except Exception:
            self.update_run(run_id, cleanup="pending")
            logger.warning("foliorun_cleanup_pending", extra={"run_id": run_id})

    async def recover_collection(self, run_id, expected_manifest_hash):
        """Bounded collector-only recovery, with durable per-item receipts and no model replay."""
        record = self.store.get_record("run/" + run_id)[1]
        state = Collector(self.store).state(run_id)
        if not state or not state["seal_ref"]:
            raise RuntimeFault("seal_unavailable")
        if state["seal_ref"]["sha256"] != expected_manifest_hash:
            raise RuntimeFault("manifest_mismatch")
        if record["terminal"]:
            return record["result"]
        if not record.get("quiescent"):
            raise RuntimeFault("agent_not_quiescent")
        if record["cancelled"]:
            self._failure(run_id, "cancelled")
            return self.finalize(run_id)
        if time.time() >= state["retain_until"] and state["phase"] != "complete":
            self._failure(run_id, "output_source_lost")
            self.finalize(run_id)
            await self.cleanup(run_id, expired=True)
            return self.store.get_record("run/" + run_id)[1]["result"]
        box = None
        try:
            if state["phase"] == "complete":
                seal = self.store.read_json(BlobRef.model_validate(state["seal_ref"]))
                artifacts = tuple(
                    FileRef.model_validate(state["receipts"][f["path"]]) for f in seal["files"]
                )
                for file in artifacts:
                    self.store.read(file.blob)
            else:
                box = await self.sandbox.attach_owned(
                    record["sandbox_id"], run_id, self.store.scope
                )
                await box.verify_retention(state["retain_until"])
                artifacts = await Collector(self.store).collect(run_id, expected_manifest_hash, box)
            if record.get("native_ref") is None:
                raise RuntimeFault("checkpoint_unavailable")
            self._renew_publication(run_id)
            prepared = self._prepare(run_id, artifacts)
            if box:
                await self._destroy(run_id, box)
            self.finalize(run_id)
            return prepared
        except RuntimeFault as exc:
            if exc.code in {"output_source_lost", "source_ownership_mismatch"}:
                self._failure(run_id, exc.code)
                self.finalize(run_id)
            raise
        finally:
            if box:
                await box.close()

    def _renew_publication(self, run_id):
        # The agent is quiescent; only the original active run can renew its commit lease.
        with self.store.transaction() as db:
            old = self.store.get_record("run/" + run_id, db)
            job = self.job(old[1])
            sess = self.store.get_record("session/" + job.session_id, db)
            if (
                old[1]["terminal"]
                or old[1]["cancelled"]
                or not old[1].get("quiescent")
                or sess[1]["active"] != run_id
                or sess[1]["fence"] != old[1]["lease"]["fence"]
            ):
                raise RuntimeFault("publication_fenced")
            self.store.cas(
                "run/" + run_id,
                old[0],
                {
                    **old[1],
                    "lease": {**old[1]["lease"], "expires_at": time.time() + 600},
                },
                db,
            )

    async def cleanup(self, run_id, *, expired=False):
        record = self.store.get_record("run/" + run_id)
        if record is None:
            raise RuntimeFault("run_unavailable")
        state = record[1]
        if state["cleanup"] != "pending":
            return state["cleanup"]
        collection = Collector(self.store).state(run_id)
        if collection and collection["phase"] != "complete":
            if not expired or time.time() < collection["retain_until"]:
                raise RuntimeFault("source_still_required")
            if not state["terminal"]:
                raise RuntimeFault("run_not_terminal")
        for _ in range(2):
            try:
                await self.sandbox.destroy_owned(state["sandbox_id"], run_id, self.store.scope)
                self.update_run(run_id, cleanup="destroyed")
                return "destroyed"
            except Exception:
                continue
        return "pending"

    async def expire_collections(self):
        """Explicit maintenance sweep; never model/tools. Service TTL is an independent backstop."""
        outcomes = {}
        for key, _, state in self.store.repository.scan("collection/"):
            if state["phase"] == "complete" or time.time() < state["retain_until"]:
                continue
            run_id = key.removeprefix("collection/")
            run = self.store.get_record("run/" + run_id)[1]
            if not run.get("quiescent"):
                continue
            if not run["terminal"]:
                self._failure(run_id, "output_source_lost")
                self.finalize(run_id)
            outcomes[run_id] = await self.cleanup(run_id, expired=True)
        return outcomes

    def _failure(self, run_id, code, db=None, *, detail=None):
        status = (
            "CANCELLED"
            if code == "cancelled"
            else "NEEDS_REVIEW"
            if code.startswith("unknown_")
            or code
            in {
                "adapter_failed",
                "output_source_lost",
                "checkpoint_unavailable",
                "source_ownership_mismatch",
            }
            else "FAILED"
        )
        prepared = {"status": status, "error": code}
        if detail is not None:
            prepared["error_detail"] = SafeError.model_validate(detail).model_dump()
        old = self.store.get_record("run/" + run_id, db)
        if old[1]["terminal"]:
            return old[1]["prepared"]
        self.store.cas("run/" + run_id, old[0], {**old[1], "prepared": prepared}, db)
        return prepared

    def reserve_fallback(self, run_id):
        """Explicit whole-round retry only for known native restore failure before any model I/O."""
        with self.store.transaction() as db:
            old = self.store.get_record("run/" + run_id, db)
            state = old[1]
            if state.get("next_attempt"):
                child = self.store.get_record("run/" + state["next_attempt"], db)[1]
                return {"run_id": state["next_attempt"], "job_ref": child["job_ref"]}
            if state["terminal"] or state["cancelled"] or not state.get("quiescent"):
                return None
            if (state.get("prepared") or {}).get("error") != "native_restore_failed":
                return None
            job = self.job(state)
            profile, tools = self.bindings.require(job)
            index = state.get("attempt_index", 0)
            if index >= profile.fallback.max_attempts:
                return None
            if (
                any(
                    spec.effect == "external_side_effect"
                    and spec.retry_class != "idempotency_required"
                    for spec, _ in tools.entries.values()
                )
                and not profile.fallback.allow_external_repetition
            ):
                return None
            sess = self.store.get_record("session/" + job.session_id, db)
            if sess[1]["active"] != run_id or sess[1]["revision"] != job.session_revision:
                raise RuntimeFault("session_conflict")
            handle = Handle(run_id=uuid4().hex, job_sha256=state["handle"]["job_sha256"])
            child = {
                "handle": handle.model_dump(),
                "job_ref": state["job_ref"],
                "cancelled": False,
                "terminal": False,
                "prepared": None,
                "cleanup": "not_created",
                "sandbox_id": None,
                "expires_at": state["expires_at"],
                "attempt_of": state.get("attempt_of", run_id),
                "attempt_index": index + 1,
                "skip_native": True,
            }
            result = RunResult(
                handle=Handle.model_validate(state["handle"]),
                status=Status.FAILED,
                error="native_restore_failed",
                cleanup=state["cleanup"],
                expires_at=state["expires_at"],
                attempt_of=state.get("attempt_of"),
            )
            self.store.cas("run/" + handle.run_id, 0, child, db)
            self.store.cas(
                "run/" + run_id,
                old[0],
                {
                    **state,
                    "next_attempt": handle.run_id,
                    "terminal": True,
                    "result": result.model_dump(),
                },
                db,
            )
            self.store.cas(
                "session/" + job.session_id,
                sess[0],
                {
                    **sess[1],
                    "active": handle.run_id,
                },
                db,
            )
            return {"run_id": handle.run_id, "job_ref": state["job_ref"]}

    def finalize(self, run_id):
        with self.store.transaction() as db:
            rec = self.store.get_record("run/" + run_id, db)
            record = rec[1]
            if record["terminal"]:
                return record["result"]["status"]
            job = self.job(record)
            prepared = record["prepared"] or {
                "status": "NEEDS_REVIEW",
                "error": "unknown_execution_window",
            }
            if prepared["status"] == "RECOVERY_PENDING" and not record["cancelled"]:
                return "RECOVERY_PENDING"
            if record["cancelled"]:
                prepared = {"status": "CANCELLED", "error": "cancelled"}
            sess = self.store.get_record("session/" + job.session_id, db)
            state = sess[1]
            lease = record.get("lease")
            if state["active"] != run_id or state["revision"] != job.session_revision:
                prepared = {"status": "FAILED", "error": "session_conflict"}
            elif prepared["status"] == "SUCCEEDED" and (
                not lease or lease["expires_at"] <= time.time() or lease["fence"] != state["fence"]
            ):
                prepared = {"status": "NEEDS_REVIEW", "error": "publication_fenced"}
            result = RunResult(
                handle=Handle.model_validate(record["handle"]),
                cleanup=record["cleanup"],
                expires_at=record["expires_at"],
                attempt_of=record.get("attempt_of"),
                **prepared,
            )
            if state["active"] == run_id:
                next_state = {
                    **state,
                    "active": None,
                    "state": "review" if result.status == Status.NEEDS_REVIEW else "open",
                }
                if result.status == Status.SUCCEEDED:
                    next_state.update(
                        revision=job.session_revision + 1,
                        versions=[*state["versions"], result.session_version.model_dump()],
                    )
                    for file in result.artifacts:
                        key = "artifact/" + digest(canonical(file))
                        old = self.store.get_record(key, db)
                        self.store.cas(
                            key,
                            old[0] if old else 0,
                            {
                                "file": file.model_dump(),
                                "expires_at": max(
                                    record["expires_at"], old[1]["expires_at"] if old else 0
                                ),
                            },
                            db,
                        )
                self.store.cas("session/" + job.session_id, sess[0], next_state, db)
            self.store.cas(
                "run/" + run_id,
                rec[0],
                {
                    **record,
                    "terminal": True,
                    "result": result.model_dump(),
                },
                db,
            )
            return result.status.value

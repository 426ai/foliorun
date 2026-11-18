"""Durable seal + per-file receipts. This module cannot invoke a Harness or tool."""

import asyncio
import time
from uuid import uuid4

from .contracts import BlobRef, FileRef, OutputSeal, RuntimeFault, SealedFile, digest


class Collector:
    def __init__(self, store):
        self.store = store

    def state(self, run_id):
        record = self.store.get_record("collection/" + run_id)
        return record[1] if record else None

    async def seal(self, run_id, box, paths, limits):
        if not 0 < len(paths) <= limits.files or len(paths) != len(set(paths)):
            raise RuntimeFault("file_budget")
        for path in paths:
            from .contracts import path_checked

            path_checked(path, output=True)
        run = self.store.get_record("run/" + run_id)[1]
        key = "collection/" + run_id
        with self.store.transaction() as db:
            if self.store.get_record(key, db):
                raise RuntimeFault("unknown_seal_window")
            self.store.cas(
                key,
                0,
                {
                    "phase": "sealing",
                    "paths": paths,
                    "receipts": {},
                    "retain_until": run["retain_until"],
                    "seal_ref": None,
                    "fence": 0,
                    "lease": None,
                },
                db,
            )
        # A lost response cannot authorize another model/tool execution or a different manifest.
        entries = tuple(SealedFile.model_validate(e) for e in await box.seal(paths, limits))
        if [f.path for f in entries] != paths or sum(f.size for f in entries) > limits.output_bytes:
            raise RuntimeFault("output_manifest_invalid")
        seal = OutputSeal(
            run_id=run_id,
            sandbox_id=box.id,
            scope=self.store.scope,
            retain_until=run["retain_until"],
            files=entries,
        )
        ref = self.store.put_json(seal)
        with self.store.transaction() as db:
            old = self.store.get_record(key, db)
            self.store.cas(
                key,
                old[0],
                {
                    **old[1],
                    "phase": "sealed",
                    "seal_ref": ref.model_dump(),
                },
                db,
            )
        return seal

    def _claim(self, run_id, expected_hash):
        key = "collection/" + run_id
        with self.store.transaction() as db:
            old = self.store.get_record(key, db)
            if old is None or old[1]["seal_ref"] is None:
                raise RuntimeFault("seal_unavailable")
            state = old[1]
            if state["seal_ref"]["sha256"] != expected_hash:
                raise RuntimeFault("manifest_mismatch")
            if state.get("lease") and state["lease"]["expires_at"] > time.time():
                raise RuntimeFault("collector_busy")
            lease = {
                "owner": uuid4().hex,
                "fence": state["fence"] + 1,
                "expires_at": time.time() + 600,
            }
            self.store.cas(key, old[0], {**state, "lease": lease, "fence": lease["fence"]}, db)
            return lease

    def _update(self, run_id, token, **values):
        with self.store.transaction() as db:
            key = "collection/" + run_id
            old = self.store.get_record(key, db)
            if old[1]["lease"] != token or token["expires_at"] <= time.time():
                raise RuntimeFault("collector_fenced")
            self.store.cas(key, old[0], {**old[1], **values}, db)

    async def collect(self, run_id, expected_hash, box):
        lease = self._claim(run_id, expected_hash)
        state = self.state(run_id)
        seal = OutputSeal.model_validate(
            self.store.read_json(BlobRef.model_validate(state["seal_ref"]))
        )
        if seal.scope != self.store.scope or seal.run_id != run_id:
            raise RuntimeFault("scope_mismatch")
        if box.id != seal.sandbox_id:
            raise RuntimeFault("source_mismatch")
        receipts = dict(state["receipts"])
        try:
            self._update(run_id, lease, phase="collecting")
            for entry in seal.files:
                expected = BlobRef(scope=seal.scope, sha256=entry.sha256, size=entry.size)
                if entry.path in receipts:
                    file = FileRef.model_validate(receipts[entry.path])
                    if file.blob != expected:
                        raise RuntimeFault("receipt_integrity")
                    self.store.read(file.blob)
                    continue
                if time.time() >= seal.retain_until:
                    raise RuntimeFault("output_source_lost")
                # A put acknowledgement may have been lost. Reconcile by content before download.
                try:
                    self.store.read(expected)
                except RuntimeFault as exc:
                    if exc.code != "blob_unavailable":
                        raise
                    timeout = min(
                        120, seal.retain_until - time.time(), lease["expires_at"] - time.time()
                    )
                    if timeout <= 0:
                        raise RuntimeFault("collector_fenced")
                    data = await asyncio.wait_for(
                        box.read_output(entry.path, entry.size), timeout=timeout
                    )
                    if len(data) != entry.size or digest(data) != entry.sha256:
                        raise RuntimeFault("output_integrity")
                    ref = self.store.put(data)
                    if ref != expected:
                        raise RuntimeFault("output_integrity")
                self.store.read(expected)
                file = FileRef(path=entry.path, blob=expected, media_type=entry.media_type)
                receipts[entry.path] = file.model_dump()
                self._update(run_id, lease, receipts=receipts)
            self._update(run_id, lease, phase="complete", lease=None)
        except BaseException:
            # Receipt or transport failure preserves the original immutable source and fence.
            try:
                self._update(run_id, lease, phase="recovery_pending", lease=None)
            except Exception:
                pass
            raise
        return tuple(FileRef.model_validate(receipts[e.path]) for e in seal.files)

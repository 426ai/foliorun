# Operations

## Assembly And Trust

Use private, owner-controlled POSIX directories. Local FS/SQLite is not an NFS
or distributed coordination backend. Applications authenticate scopes and
authorize VFS revisions and tools before installing them. Storage objects and
native checkpoints may contain input/output content: never log or publish them.
Only stable error codes and finite `SafeError` evidence cross the execution
boundary; never expose SDK exception messages or bodies.

`Runtime` needs configuration, bindings and storage. Temporary workspaces are
owned by the Harness/Sandbox, not a caller-supplied fourth input. Reinstall exact
profiles, tool implementations and retained VFS providers on every worker.
The tool release is a host declaration, not proof of handler code integrity.
Provider clients, keys, signed URLs and resolver credentials remain host-only.

The supported production adapters are Pydantic AI 2.61.0, Temporal 1.33.0 and
OpenSandbox 0.1.16. Core consumes replaceable adapter descriptors rather than
closed vendor literals. Replacement does not establish conformance by itself.

## Temporal

Use matching workflow/Activity registrations on an isolated queue. The source
retains legacy workflow command sequences for inspection/loading; this worker
does not execute legacy JobSpecs. Keep original workers for old in-flight jobs.
Never send incompatible requests to an old worker.

The agent Activity has `maximum_attempts=1`. The workflow explicitly calls a
fallback reservation Activity and, only when allowed, executes a new run.
Maximum fallback attempts are separately configured (default zero, maximum
five). Known native restore failure is the only automatic fallback reason.
Tool/model send intents with unknown results are never replayed.

Cancellation/finalization/fallback reservation use the separate `-control`
queue. Cancel writes a durable fence before acknowledging. Gateway checks
between calls, and final publication rechecks cancellation, active run, expected
revision, lease deadline and fencing token in one transaction.

After `RECOVERY_PENDING`, the workflow waits without re-entering the agent.
Maintenance calls `recover_collection(run_id, expected_manifest_hash)`, then
`scheduler.collection_recovered(original_handle)`. Notify after expiration
maintenance as well so a waiting workflow observes the terminal receipt.
Collection Activity exists separately from agent Activity. If driving it with
custom orchestration, retries must honor the collector lease and fixed seal.

## Collection And Source Lifetime

OpenSandbox creation requests a deadline covering agent execution, the configured
collection window and transport slack. `get_info()` must confirm an explicit
service expiration covering the persisted `retain_until`. Manual/unbounded or
shorter service TTLs are rejected. Credentials never enter the sandbox.

`submit_result` persists a seal intent before issuing a synchronous trusted seal
command. Successful seal metadata fixes exact paths, sizes, hashes, scope, source
instance and deadline. The model cannot change that manifest on recovery.
Round-end native export is persisted before collector publication.

The collector holds a 600-second lease and monotonically increasing fence.
It writes each file and reads it back before persisting its receipt. Recovery
checks already saved receipts and reconciles acknowledgement loss by content
hash. Concurrent or late collectors cannot overwrite a newer fence.

If a file/receipt fails, preserve the sealed source, close only its SDK transport
and expose `RECOVERY_PENDING`. A replacement worker can attach only to the
matching owner/scope/run. Recovery never calls a Harness or document tool and
does not increase model/tool counts. The immutable seal and original byte hashes
remain authoritative after reconnect.

Call `expire_collections()` periodically. Expired incomplete collections become
`NEEDS_REVIEW/output_source_lost`, keep their saved receipts and attempt precise
owned-instance cleanup. The service TTL is a separate backstop, not proof that
cleanup succeeded. `cleanup="destroyed"` means kill succeeded or ownership lookup
confirmed 404. `cleanup="pending"` remains visible after failure.

Native-export failure after sealing and unknown seal responses are review
cases. The source is retained until the deadline but this version does not
reconstruct an absent native checkpoint or reconcile an unknown seal command.
Such a run cannot become a successful session through normal collection
recovery. Do not use this as authorization to rerun the model.

Inputs are uploaded only when selected through materialization. Default image
Skill bytes are independently verified during prepare, not caller VFS bodies.
Root bootstrap, execd, image code, tool bindings and the generic sealer are
trusted. Programs must finish in the foreground with no background writers.
This is not a network-isolation or hostile-root containment guarantee.

## Sessions And Retention

One active run per session. Successful revisions contain submitted/effective
input refs, native state, output snapshot, parent manifest and exact bindings.
Empty input deltas inherit; overrides and tombstones compare old hashes.
Failed/cancelled runs do not advance the head. Review sessions block continuation
and deletion; there is intentionally no generic automatic review-approval API.

Native messages are restricted to completed turns with no pending tools or
external URL/upload attachments. Export does not promise to preserve an SDK
conversation container's running usage/deferred execution state. The checkpoint
is opaque native data, not a universal conversation format.

Local reads archive input bytes. Unread inputs remain provider-backed. A session
retains stable source references, not credentials or an eternal availability
guarantee. Missing providers, revoked access, expired leases and missing blobs
fail explicitly. Hosts must retain original revisions and refresh leases for
their intended session lifetime. A memory-only provider must be reinstalled or
replaced with a durable retained provider after process restart.

Artifact receipts have independent expiration. Runtime downloads verify receipt
ownership, expiry, bytes and hash, and never overwrite an existing destination.
`Storage.download_to` is a trusted low-level export API, not receipt authorization.
Session close/delete does not delete artifact receipts or run roots.

## GC

All framework blob writes use `Storage.put`: under the same transactional index
lock as GC, it installs a one-hour upload pin before writing/read-back.
Do not bypass this managed write path while GC is enabled.

`store.gc(grace_seconds=3600)` conservatively walks all live record references
and immutable JSON manifests. It serializes mark/recheck/delete with new managed
puts and reference publication. Corrupt reachable blobs stop GC rather than
letting it guess. GC touches only its own blob catalog, never a whole bucket or
unknown object prefix. Failed unindexed writes can remain untracked orphans;
this version does not scan arbitrary backing stores to reclaim those objects.
An unreferenced object is first marked with `orphan_since`; deletion waits for
the configured zero-reference grace as well as upload protection. Managed CAS
validates newly introduced blob references under GC's lock, rejecting resurrection
of deleted objects. Re-publishing an old blob must use this managed path.

Explicit release order:

1. Confirm the run is terminal and quiescent and cleanup is not pending.
2. After run retention, `store.release_run(run_id)` drops its run/journal/seal
   roots but preserves a small expired-handle/idempotency identity record.
   Review runs require host reconciliation and are not auto-released.
3. Close/delete the intended session at an expected revision, when not busy/review.
4. `store.expire_artifacts()` drops only expired independent delivery roots.
5. Run GC after staging grace. Shared files survive while any remaining root
   references them.

Persistent session roots do not expire automatically. There is no automatic
session TTL manager, background sweeper, backup system or cloud lifecycle
configuration. Keep operational roots until their real writers have stopped.
Raw caller-created index entries and direct blob deletion are trusted operations;
the framework cannot make an unsafe custom repository safe.

## Acceptance

Offline evidence covers real installed SDKs with controlled transports and
synthetic inputs. It is not live Temporal history replay, OpenSandbox retention
acceptance, cloud consistency acceptance, image selfcheck or document quality
validation. No paid model call is needed for offline regression.

"""Live backend: Foliorun Runtime + Temporal + OpenSandbox + DeepSeek. All paths come from HostConfig/ProjectPack."""
import asyncio, hashlib, json, os, secrets
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from tender_harness.bundles import bundle, bundle_manifest
from tender_harness.contracts import Artifact, StageRequest, StageResult
from tender_harness.docx_ops import engine as e
from tender_harness.gates import expected_files, host_gate
from tender_harness.tools import ASSETS_PATH, BOUND, NAMES, PRIOR_PATH, entrypoint_argv, registry
def dump(p, v):
    p.parent.mkdir(parents=True, exist_ok=True); t = p.with_suffix(".tmp"); t.write_text(json.dumps(v, ensure_ascii=False, indent=2)); os.replace(t, p)
class Context:
    """Explicit run context: host config + pack + optional template subset."""
    def __init__(self, host, pack, template_ids=None):
        self.host = host; self.pack = pack; self.template_ids = template_ids
        self.files, self.scope = pack.asset_files(template_ids)
        self.ids = [t["id"] for t in self.scope["templates"]]
        self.state = host.state / pack.project_id
        self.assets = self.state / "assets" / (e.dh({"pack": pack.fingerprint, "ids": self.ids}) + ".zip")
        if not self.assets.exists():
            bundle(self.files, self.assets)
        e.require(bundle_manifest(self.assets)["files"] == {n: (hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else e.sha(v)) for n, v in self.files.items()}, "assets_bundle_changed")
    def inputs(self, parent, business, stage):
        files = {ASSETS_PATH: self.assets}; prior = self.assets
        if parent:
            for a in parent["artifacts"]:
                e.require(e.sha(a["path"]) == a["sha256"], "parent_frozen_artifact_changed")
            prior = self.state / "runs" / business / (stage + "-parent.zip"); wanted = {a["name"]: Path(a["path"]) for a in parent["artifacts"]}
            if not prior.exists():
                bundle(wanted, prior)
            else:
                e.require(bundle_manifest(prior)["files"] == {n: e.sha(p) for n, p in wanted.items()}, "parent_bundle_changed")
        files[PRIOR_PATH] = prior
        for rel, v in self.files.items():
            if rel.startswith("skills/"):
                files["/" + rel] = v
        return {"assets_bundle": ASSETS_PATH, "prior_bundle": PRIOR_PATH, "stage": stage}, files
    def vfs(self, files):
        from foliorun import FilesFS
        tmp = self.state / "vfs-bytes"; real = {}
        for k, v in files.items():
            if isinstance(v, bytes):
                p = tmp / hashlib.sha256(v).hexdigest(); p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_bytes(v)
                v = p
            real[k] = v
        return FilesFS(real), real
    def profile(self, name, version, tools, **limits):
        from foliorun import Profile
        pd = json.loads(json.dumps(self.host.profile)); pd.update(name=name, version=version, allowed_tools=list(tools.entries), image=self.host.image.image)
        pd["limits"].update(files=1000, input_bytes=134217728, output_bytes=134217728, text_bytes=131072, model_requests=16, tools=128, command_seconds=120, deadline_seconds=600); pd["limits"].update(limits)
        return Profile.model_validate(pd)
    def sandbox(self):
        from opensandbox.config import ConnectionConfig
        from foliorun.adapters.opensandbox import OpenSandboxProvider, PreinstalledTool, download_binary
        from tender_harness.config import load_service_key
        img = self.host.image
        async def verify(s, p):
            for name, path in img.code_paths.items():
                raw = await download_binary(s.box, path, 262144)
                e.require(hashlib.sha256(raw).hexdigest() == img.code_sha256[name], "image_code_hash")
        key = load_service_key(self.host.opensandbox_key_file)
        return OpenSandboxProvider(connection=ConnectionConfig(domain=self.host.opensandbox_domain, protocol="http", api_key=key.get_secret_value(), use_server_proxy=True, request_timeout=timedelta(seconds=180)), owner=self.host.sandbox_owner, tools={k: PreinstalledTool(v) for k, v in entrypoint_argv(img.tool_entrypoint).items()}, seal_argv=tuple(img.seal_argv), verify_image=verify)
    def prompt(self, stage, args):
        ids = self.ids; groups = [ids[i:i + 7] for i in range(0, len(ids), 7)]; f = self.pack.project.get("prompt_facts", {}); n = len(ids)
        resume = any(t["mode"] == "resume" for t in self.scope["templates"])
        people = ("person_ids=exact " + str(len(f.get("selected_people", []))) + " selected project roles " + json.dumps(f.get("selected_people", [])) + " (" + f.get("excluded_people_note", "") + "). ")
        other = ("For extract other_requirements may be {}, but semantic/fill must include ALL source-evidenced required other-certificate records (" + f.get("other_requirement_note", "") + ") with title/clause_id/quote exactly source and chosen candidate_certificate_ids as selected_certificate_ids. ") if resume else "other_requirements must be {} (no personnel resume unit in this scope). "
        return ("Synthetic tender POC project " + self.pack.project_id + ", ORIGINAL tender input, stage=" + stage + ". You must actually execute this stage for ALL " + str(n) + " inventoried response units, not repackage an old result. FIRST discover_skills. Then call " + NAMES["load"] + " for each of these batches so ALL dedicated entrypoints and authoritative contracts are really read: " + json.dumps(groups) + " . Use only this stage and path args " + json.dumps(args) + ". Then " + NAMES["inspect"] + ". Read source inventory and source clauses, choose an actual Plan: exact source_sha256/resources_sha256/contracts_revision; template_ids ALL ids in original order; source_ranges EXACT mappings from inspection; reason=ok; rule_policy=all_declared; " + people + other +
            "Do NOT infer additional requirements from candidate certificate names; unmentioned roles remain unknown labels. Call " + NAMES["execute"] + " ONCE with your plan and exact paths. All Word writing is constrained sandbox tooling; selection, contract/source coverage and reason decisions must come from actual read skills/source facts. Do not invent narrative/technical plan, credit screenshots, commitments, prices, dates, missing data or use tender cap as bid price. Deviation tables remain unfilled, source fixed text/notes/UI remnants preserved. Explicit semantic-only repeating and image maps; fill cannot construct layout. On any execute failure STOP, do not repeat write tools or improvise a fallback. "
            "After tool success read summary.json (not the huge detailed stage manifest), then submit_result ALL " + str(expected_files(self.scope)) + " returned paths, including combined Word, " + str(n) + " individual Word, stage-manifest and summary. Do not omit protected/reference units. Never submit intermediates or a prior stage. No secret data are in VFS.")
async def collect(ctx, stage, out, h, r, store, runtime, parent):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    history = ModelMessagesTypeAdapter.validate_json(store.read(r.session_version.checkpoint.checkpoint_ref)); calls = []; responses = []
    for m in history:
        for p in m.parts:
            if p.part_kind == "tool-call":
                calls.append({"name": p.tool_name, "args": p.args_as_dict()})
        if getattr(m, "kind", None) == "response":
            responses.append({"model": m.model_name, "provider": m.provider_name, "provider_response_id": m.provider_response_id, "input_tokens": m.usage.input_tokens, "output_tokens": m.usage.output_tokens})
    loaded = {t for c in calls if c["name"] == NAMES["load"] for t in c["args"]["template_ids"]}
    ok = loaded == set(ctx.ids) and any(c["name"] == "discover_skills" for c in calls) and sum(c["name"] == NAMES["execute"] for c in calls) == 1
    dump(out / "model-provenance.json", {"classification": "ACTUAL DeepSeek native PydanticAI/Foliorun", "all_dedicated_skills_really_loaded": ok, "calls": calls, "responses": responses})
    e.require(ok, "actual_skill_adherence_not_proven"); local = {}; artifacts = []
    for a in r.artifacts:
        p = out / Path(a.path).name; await runtime.artifacts.download_to(a, p); local[p.name] = p
        artifacts.append(Artifact(name=p.name, path=str(p), sha256=e.sha(p), size=p.stat().st_size))
    e.require(len(artifacts) == expected_files(ctx.scope), "collection_incomplete")
    manifest = e.read(local["stage-manifest.json"]); gate = host_gate(stage, manifest, local, parent, ctx.pack, ctx.scope); dump(out / "host-gate.json", gate)
    result = StageResult(stage=stage, success=True, run_id=h.run_id, source_sha256=manifest["source_sha256"], resources_sha256=manifest["resources_sha256"], manifest_sha256=e.sha(local["stage-manifest.json"]), artifacts=artifacts, actual_skill_read=True, gate=gate)
    dump(out / "stage-result.json", result.model_dump(mode="json")); print("LIVE_STAGE_VERIFIED", stage, h.run_id, "templates", len(ctx.ids), flush=True)
    return result.model_dump(mode="json")
async def run_stage(ctx, req, provider, client):
    from foliorun import LocalArtifactStore, Runtime, RuntimeConfig, WorkerBindings
    from foliorun.adapters.pydantic_ai import PydanticHarness
    from foliorun.adapters.temporal import TemporalActivities, TemporalScheduler
    from foliorun.contracts import Handle, RunResult
    from foliorun.execution import Executor
    from tender_harness.budget import Budget
    from tender_harness.providers import deepseek_factory
    r = StageRequest.model_validate(req); stage = r.stage; out = ctx.state / "runs" / r.business_id / stage; out.mkdir(parents=True, exist_ok=True)
    args, files = ctx.inputs(r.parent, r.business_id, stage); vfs, real = ctx.vfs(files); tools = registry(stage, ctx.pack.project_id)
    identity = {"stage": stage, "business_id": r.business_id, "pack": ctx.pack.fingerprint, "template_ids": ctx.ids, "files": {p: e.sha(f) for p, f in real.items()}, "provider": provider.metadata(), "image": ctx.host.image.model_dump(mode="json"), "tools_sha256": hashlib.sha256(tools.descriptor).hexdigest()}
    key = "th-" + e.dh(identity)
    if (out / "stage-result.json").exists():
        e.require(e.read(out / "identity.json") == identity, "resume_identity_changed"); value = e.read(out / "stage-result.json")
        for a in value["artifacts"]:
            e.require(e.sha(a["path"]) == a["sha256"], "completed_artifact_changed")
        return value
    e.require(not (out / "identity.json").exists() or e.read(out / "identity.json") == identity, "pending_identity_changed"); dump(out / "identity.json", identity)
    profile = ctx.profile("tender-" + ctx.pack.project_id + "-" + stage, key[-32:], tools); dump(out / "profile.json", profile.model_dump(mode="json"))
    bindings = WorkerBindings(); bindings.install_tools(tools); store = LocalArtifactStore(ctx.state / "store", "tender-harness")
    scheduler = TemporalScheduler(client, task_queue=ctx.host.task_queue_prefix + "-" + stage); runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
    if (out / "framework-result.json").exists():
        result = RunResult.model_validate_json((out / "framework-result.json").read_bytes()); e.require(str(result.status) == "SUCCEEDED", "definitive_failed_stage_no_automatic_resend")
        return await collect(ctx, stage, out, Handle.model_validate_json((out / "submitted.json").read_bytes()), result, store, runtime, r.parent)
    e.require(not (out / "submitted.json").exists(), "unknown_execution_window_no_resend")
    pr = ctx.host.pricing; e.require(pr.provider == provider.provider and pr.model == provider.model, "model_pricing_identity")
    budget = Budget(ctx.host.state / "budget-ledger.json", pr, ctx.host.budget_stop_usd)
    executor = Executor(store=store, bindings=bindings, sandbox=ctx.sandbox(), harness=PydanticHarness(model_factory=lambda: deepseek_factory(provider, budget))); activities = TemporalActivities(executor)
    async with activities.worker(client, scheduler.task_queue):
        h = await runtime.submit(prompt=ctx.prompt(stage, args), vfs=vfs, tools=tools, idempotency_key=key, session_id=key[-64:]); dump(out / "submitted.json", h.model_dump(mode="json")); result = await runtime.wait(h, timeout=750)
    dump(out / "framework-result.json", result.model_dump(mode="json")); e.require(str(result.status) == "SUCCEEDED", "framework_stage_" + str(result.status))
    return await collect(ctx, stage, out, h, result, store, runtime, r.parent)
def make_activities(ctx, provider, client):
    from temporalio import activity
    from tender_harness.workflow import ACTIVITY
    @activity.defn(name=ACTIVITY)
    async def stage(req: dict) -> dict:
        async def beat():
            while True:
                activity.heartbeat(req["stage"]); await asyncio.sleep(5)
        t = asyncio.create_task(beat())
        try:
            return await run_stage(ctx, req, provider, client)
        except Exception as ex:
            code = str(ex) if type(ex).__name__ == "GateError" else getattr(ex, "code", None) or type(ex).__name__
            dump(ctx.state / "runs" / req["business_id"] / (req["stage"] + "-error.json"), {"safe_code": code, "stage": req["stage"], "no_automatic_retry": True})
            raise RuntimeError("stage_failed_" + req["stage"]) from None
        finally:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
    return stage
async def run_business(ctx, resume=None):
    from temporalio.client import Client
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.worker import Worker
    from tender_harness.config import load_provider_config
    from tender_harness.workflow import TenderThreeStage
    c = load_provider_config(env_file=ctx.host.provider_env_file); c.require_key()
    business = resume or ("tb-" + e.dh({"pack": ctx.pack.fingerprint, "ids": ctx.ids, "image": ctx.host.image.model_dump(mode="json"), "provider": c.metadata()})[:24])
    req = {"business_id": business, "source_sha256": ctx.scope["source_sha256"], "resources_sha256": ctx.scope["resources_sha256"]}
    client = await Client.connect(ctx.host.temporal_address, namespace=ctx.host.temporal_namespace); q = ctx.host.task_queue_prefix + "-business"; wid = business + ("-recovery-1" if resume else "")
    async with Worker(client, task_queue=q, workflows=[TenderThreeStage], activities=[make_activities(ctx, c, client)], max_concurrent_activities=1):
        try:
            h = await client.start_workflow(TenderThreeStage.run, req, id=wid, task_queue=q)
        except WorkflowAlreadyStartedError:
            h = client.get_workflow_handle(wid)
        dump(ctx.state / "runs" / business / "business-handle.json", {"business_id": business, "workflow_id": wid, "run_id": h.result_run_id or (await h.describe()).run_id})
        r = await h.result()
    d = ctx.state / "runs" / business; dump(d / "business-result.json", r); (d / "temporal-business-history.json").write_text((await h.fetch_history()).to_json())
    print("LIVE_BUSINESS_SUCCESS", r["success"], business, flush=True); return business, r
async def preflight(ctx):
    """Authenticated infrastructure-only lifecycle: create -> health -> prepare -> inspect tool -> seal -> collect -> destroy. ZERO model calls."""
    from foliorun import LocalArtifactStore, Runtime, RuntimeConfig, WorkerBindings
    from foliorun.contracts import JobSpec
    from foliorun.tools import Gateway
    from foliorun.vfs import LazyFiles
    from temporalio.api.workflowservice.v1 import GetSystemInfoRequest
    from temporalio.client import Client
    from tender_harness.config import load_provider_config
    c = load_provider_config(env_file=ctx.host.provider_env_file); c.require_key()
    e.require(ctx.host.pricing.model == c.model, "model_pricing_identity")
    cli = await Client.connect(ctx.host.temporal_address, namespace=ctx.host.temporal_namespace); await cli.workflow_service.get_system_info(GetSystemInfoRequest())
    class Capture:
        async def start(self, h, ref): self.ref = ref
    sch = Capture(); tools = registry("extract", ctx.pack.project_id); profile = ctx.profile("tender-preflight", e.sha(ctx.assets)[:24], tools)
    bindings = WorkerBindings(); bindings.install_tools(tools); store = LocalArtifactStore(ctx.state / "preflight-store", "tender-harness-infra-only")
    runtime = Runtime(config=RuntimeConfig(profile, sch), bindings=bindings, store=store)
    args, files = ctx.inputs(None, "preflight", "extract"); vfs, _ = ctx.vfs(files)
    h = await runtime.submit(prompt="INFRASTRUCTURE ONLY, NO MODEL", vfs=vfs, tools=tools, idempotency_key="th-infra-" + secrets.token_hex(8), session_id="th-infra-" + secrets.token_hex(8))
    job = JobSpec.model_validate(store.read_json(sch.ref)); snap = runtime.snapshot(job.effective_input_ref); fs = {x.path: x for x in snap.files}; p = ctx.sandbox(); box = None; info = {}
    try:
        box = await p.create(profile, h.run_id, store.scope); healthy = await box.box.is_healthy(); e.require(healthy, "sandbox_unhealthy"); await box.prepare(fs, store, profile)
        gw = Gateway(store, h.run_id, job, profile, tools, box, fs, lazy=LazyFiles(store, bindings, fs, job.limits))
        found = await gw.invoke("discover_skills", {}, "discover"); e.require(len(found["value"]) == len(ctx.ids), "skills_discovered_count")
        args = {k: v for k, v in args.items() if k not in BOUND}
        ld = await gw.invoke(NAMES["load"], dict(args, template_ids=ctx.ids[:8]), "load"); e.require(len(ld["value"]["loaded_dedicated_skill_packages"]) == min(8, len(ctx.ids)), "skills_load")
        ins = await gw.invoke(NAMES["inspect"], dict(args), "inspect"); e.require([x["id"] for x in ins["value"]["templates"]] == ctx.ids, "inspect_inventory")
        info = {"classification": "REAL AUTHENTICATED SDK INFRASTRUCTURE ONLY", "model_calls": 0, "temporal_system_info": True, "sandbox_id": box.id, "sandbox_health": True, "prepared_files": len(fs), "image_code_hash_checked": True, "image": profile.image, "discovered_skills": len(found["value"]), "actual_tool_load_and_inspect": True, "frozen_vfs_revision": snap.revision, "provider": c.metadata(), "credential_present": True}
    finally:
        if box:
            await box.destroy(); info["sandbox_destroyed"] = True
    dump(ctx.state / "preflight.json", info); return info

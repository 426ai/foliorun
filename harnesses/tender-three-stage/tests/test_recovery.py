"""Anti-resend recovery: a persisted window never re-submits a model run. No network, no model."""
import asyncio, json
import pytest
from conftest import pack_path
from tender_harness.docx_ops import engine as e
PROFILE = {"schema_version": "0.4", "name": "t", "version": "t", "package_version": "0.1.0", "harness": "pydantic-ai", "harness_version": "2.61.0", "checkpoint_format": "pydantic-messages-v1", "sandbox": "opensandbox", "sdk_version": "0.1.16", "model": "deepseek-flash", "image": "x", "allowed_tools": [],
    "limits": {"files": 64, "input_bytes": 1, "output_bytes": 1, "text_bytes": 1, "model_requests": 16, "tools": 64, "command_seconds": 90, "deadline_seconds": 600}, "retention_seconds": 604800, "collection_seconds": 1800, "fallback": {"max_attempts": 0, "allow_external_repetition": False}}
def _ctx(tmp_path, monkeypatch):
    p = pack_path()
    if p is None:
        pytest.skip("TENDER_HARNESS_TEST_PACK not set")
    pytest.importorskip("foliorun")
    from tender_harness.config import HostConfig, ProviderConfig
    from tender_harness.live import Context
    from tender_harness.pack import load_project_pack
    host = HostConfig.model_validate({"state_dir": str(tmp_path / "state"), "temporal_address": "127.0.0.1:1", "opensandbox_domain": "127.0.0.1:1", "opensandbox_key_file": str(tmp_path / "none"), "provider_env_file": str(tmp_path / "none"),
        "image": {"image": "img@sha256:" + "0" * 64, "tool_entrypoint": ["python"], "code_paths": {}, "code_sha256": {}},
        "pricing": {"provider": "deepseek", "model": "deepseek-flash", "input_per_million_usd": 0.3, "output_per_million_usd": 1.2, "source_url": "x"}, "budget_stop_usd": 1, "profile": PROFILE})
    ctx = Context(host, load_project_pack(p), ["legal_rep_certificate"])
    def no_live(*a, **k): raise AssertionError("live path reached")
    monkeypatch.setattr(ctx, "sandbox", no_live)
    return ctx, ProviderConfig()
def _run(ctx, prov, biz="B1"):
    from tender_harness.live import run_stage
    return asyncio.run(run_stage(ctx, {"stage": "extract", "business_id": biz, "parent": None}, prov, None))
def _out(ctx, biz="B1"):
    return ctx.state / "runs" / biz / "extract"
def test_unknown_submission_window_halts(tmp_path, monkeypatch):
    ctx, prov = _ctx(tmp_path, monkeypatch); o = _out(ctx); o.mkdir(parents=True); (o / "submitted.json").write_text("{}")
    with pytest.raises(e.b.GateError, match="unknown_execution_window_no_resend"): _run(ctx, prov)
def test_identity_change_rejected(tmp_path, monkeypatch):
    ctx, prov = _ctx(tmp_path, monkeypatch); o = _out(ctx); o.mkdir(parents=True); (o / "identity.json").write_text(json.dumps({"other": 1}))
    with pytest.raises(e.b.GateError, match="pending_identity_changed"): _run(ctx, prov)
def test_completed_stage_returns_stored_result_without_resend(tmp_path, monkeypatch):
    ctx, prov = _ctx(tmp_path, monkeypatch); o = _out(ctx); o.mkdir(parents=True); (o / "submitted.json").write_text("{}")
    with pytest.raises(e.b.GateError): _run(ctx, prov)
    art = o / "a.bin"; art.write_bytes(b"x"); res = {"artifacts": [{"path": str(art), "sha256": e.sha(art)}]}; (o / "stage-result.json").write_text(json.dumps(res))
    before = (o / "identity.json").read_bytes(); assert _run(ctx, prov) == res and (o / "identity.json").read_bytes() == before
    art.write_bytes(b"y")
    with pytest.raises(e.b.GateError, match="completed_artifact_changed"): _run(ctx, prov)
def test_failed_framework_result_never_resent(tmp_path, monkeypatch):
    ctx, prov = _ctx(tmp_path, monkeypatch); o = _out(ctx); o.mkdir(parents=True)
    import foliorun.contracts as fc
    monkeypatch.setattr(fc.RunResult, "model_validate_json", classmethod(lambda cls, b: type("R", (), {"status": "FAILED"})()))
    (o / "framework-result.json").write_text("{}")
    with pytest.raises(e.b.GateError, match="definitive_failed_stage_no_automatic_resend"): _run(ctx, prov)

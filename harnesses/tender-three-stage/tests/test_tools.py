"""Constant tool fields (operation, stage) are host-bound: a model omitting them must not fail schema validation."""
import json
import pytest
from jsonschema import Draft202012Validator
from conftest import pack_path
from tender_harness.docx_ops import engine as e
from tender_harness.tools import NAMES, STAGES, entrypoint, entrypoint_argv, tool_schema
from tender_harness.tools import PATHS
def ok(op, stage, args):
    return Draft202012Validator(tool_schema(op, stage)).is_valid(args)
@pytest.mark.parametrize("op", list(NAMES))
@pytest.mark.parametrize("stage", STAGES)
def test_constants_optional_but_pinned(op, stage):
    assert ok(op, stage, dict(PATHS))
    assert ok(op, stage, dict(PATHS, operation=op, stage=stage))
    other = next(x for x in NAMES if x != op)
    assert not ok(op, stage, dict(PATHS, operation=other))
    assert not ok(op, stage, dict(PATHS, stage=next(x for x in STAGES if x != stage)))
    assert not ok(op, stage, dict(PATHS, unexpected=1))
    assert not ok(op, stage, {"assets_bundle": PATHS["assets_bundle"]})
    assert not ok(op, stage, dict(PATHS, assets_bundle="/materials/other.zip"))
    assert not ok(op, stage, dict(PATHS, prior_bundle="/skills/x/SKILL.md"))
def test_registry_paths_are_host_input_paths():
    pytest.importorskip("foliorun")
    from tender_harness.tools import registry
    for n, (s, h) in registry("fill", "p").entries.items():
        assert s.input_paths == tuple(PATHS) and all(s.input_schema["properties"][k]["const"] == v for k, v in PATHS.items())
        assert set(PATHS) <= set(s.input_schema["required"])
def test_registry_binds_entrypoint_per_operation_and_stage():
    pytest.importorskip("foliorun")
    from tender_harness.tools import registry
    r = registry("semantic", "p")
    assert {n: h.entrypoint for n, (s, h) in r.entries.items()} == {NAMES[o]: entrypoint(o, "semantic") for o in NAMES}
    for n, (s, h) in r.entries.items():
        assert "operation" not in s.input_schema["required"] and "stage" not in s.input_schema["required"]
def test_entrypoint_argv_is_trusted_prefix_plus_bound_constants():
    m = entrypoint_argv(["python", "-I", "-m", "tender_harness.docx_ops.engine"])
    assert len(m) == 9 and m[entrypoint("execute", "fill")] == ("python", "-I", "-m", "tender_harness.docx_ops.engine", "--operation", "execute", "--stage", "fill")
def test_bind_constants():
    assert e.bind_constants(dict(PATHS), "execute", "extract") == dict(PATHS, operation="execute", stage="extract")
    assert e.bind_constants(dict(PATHS, operation="execute"), "execute", "extract")["stage"] == "extract"
    for bad in ({"operation": "load"}, {"stage": "fill"}):
        with pytest.raises(e.b.GateError, match="bound_argument_mismatch"):
            e.bind_constants(dict(PATHS, **bad), "execute", "extract")
    with pytest.raises(e.b.GateError, match="missing_operation"):
        e.bind_constants(dict(PATHS, stage="extract"), None, None)
def _cli(root, payload, capsys, *extra):
    rc = e.cli([json.dumps(payload), "--call", "ab", *extra], root=root)
    return rc, capsys.readouterr().out
def test_engine_cli_accepts_payload_without_constants(tmp_path, capsys):
    p = pack_path()
    if p is None:
        pytest.skip("TENDER_HARNESS_TEST_PACK not set")
    from tender_harness.bundles import bundle
    from tender_harness.pack import load_project_pack
    files, _ = load_project_pack(p).asset_files(["legal_rep_certificate"])
    (tmp_path / "materials").mkdir(); bundle(files, tmp_path / "materials/assets.zip")
    rc, out = _cli(tmp_path, dict(PATHS), capsys, "--operation", "inspect", "--stage", "extract")
    assert rc == 0, out
    assert [t["id"] for t in json.loads(out)["templates"]] == ["legal_rep_certificate"]
    rc, out = _cli(tmp_path, dict(PATHS, template_ids=["legal_rep_certificate"]), capsys, "--operation", "load", "--stage", "extract")
    assert rc == 0, out
    rc, out = _cli(tmp_path, dict(PATHS, operation="load"), capsys, "--operation", "inspect", "--stage", "extract")
    assert rc == 2 and json.loads(out) == {"safe_gate": "bound_argument_mismatch"}

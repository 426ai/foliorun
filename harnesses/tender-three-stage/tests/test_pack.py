import json, shutil
import pytest
from tender_harness.pack import PackError, load_project_pack, sha256_file
from conftest import pack_path
@pytest.fixture
def copy_pack(tmp_path):
    p = pack_path()
    if p is None:
        pytest.skip("TENDER_HARNESS_TEST_PACK not set")
    d = tmp_path / "pack"; shutil.copytree(p, d); return d
def _proj(d):
    return json.loads((d / "project.json").read_text())
def _write(d, v):
    (d / "project.json").write_text(json.dumps(v))
def test_pack_loads_from_any_location(copy_pack):
    p = load_project_pack(copy_pack); assert len(p.templates) == 28 and p.root == copy_pack.resolve()
@pytest.mark.parametrize("rel", ["../x", "/etc/passwd", "inputs/../x", ".hidden/x", "a\\b"])
def test_unsafe_member_path(copy_pack, rel):
    v = _proj(copy_pack); v["files"][rel] = "0" * 64; _write(copy_pack, v)
    with pytest.raises(PackError): load_project_pack(copy_pack)
def test_wrong_hash(copy_pack):
    v = _proj(copy_pack); k = next(iter(v["files"])); v["files"][k] = "0" * 64; _write(copy_pack, v)
    with pytest.raises(PackError, match="pack_member_hash"): load_project_pack(copy_pack)
def test_undeclared_file(copy_pack):
    (copy_pack / "inputs/extra.json").write_text("{}")
    with pytest.raises(PackError, match="undeclared"): load_project_pack(copy_pack)
def test_symlink_member(copy_pack, tmp_path):
    t = tmp_path / "outside.json"; t.write_text("{}"); (copy_pack / "inputs/link.json").symlink_to(t)
    with pytest.raises(PackError, match="symlink"): load_project_pack(copy_pack)
def test_python_member_rejected(copy_pack):
    q = copy_pack / "inputs/hook.py"; q.write_text("print(1)"); v = _proj(copy_pack); v["files"]["inputs/hook.py"] = sha256_file(q); _write(copy_pack, v)
    with pytest.raises(PackError, match="forbidden_type"): load_project_pack(copy_pack)
def test_secret_content_rejected(copy_pack):
    q = copy_pack / "inputs/notes.md"; q.write_text("DEEPSEEK_API_KEY=abc"); v = _proj(copy_pack); v["files"]["inputs/notes.md"] = sha256_file(q); _write(copy_pack, v)
    with pytest.raises(PackError, match="secret"): load_project_pack(copy_pack)
def test_duplicate_json_member(copy_pack):
    raw = (copy_pack / "project.json").read_text(); (copy_pack / "project.json").write_text(raw.replace('"schema":', '"schema": "x", "schema":', 1))
    with pytest.raises(PackError, match="duplicate"): load_project_pack(copy_pack)
def test_contract_hash_mismatch(copy_pack):
    v = _proj(copy_pack); k = next(x for x in v["files"] if x.endswith("stage-contract.json")); q = copy_pack / k; q.write_text(q.read_text() + " "); v["files"][k] = sha256_file(q); _write(copy_pack, v)
    with pytest.raises(PackError, match="contract"): load_project_pack(copy_pack)
def test_subset_must_be_ordered_known(copy_pack):
    p = load_project_pack(copy_pack)
    with pytest.raises(PackError): p.select(["nope"])
    with pytest.raises(PackError): p.select(list(reversed(p.template_ids[:2])))
    s = p.select(["legal_rep_certificate"]); assert [t["id"] for t in s["templates"]] == ["legal_rep_certificate"] and s["subset_of_scope_sha256"]

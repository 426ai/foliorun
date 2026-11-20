import ast, subprocess, sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src" / "tender_harness"
FORBIDDEN_IMPORTS = ("workstreams", "adapter", "full_harness", "build_inputs")
def test_no_legacy_imports_or_machine_paths():
    bad = []
    for p in SRC.rglob("*.py"):
        text = p.read_text()
        for node in ast.walk(ast.parse(text)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            bad += [(p.name, n) for n in names if n.split(".")[0] in FORBIDDEN_IMPORTS]
        for marker in ("/Users/", "/home/", "workstreams", "full_harness"):
            # 't113:' SDT tag prefix and stage-output schema id are frozen data-format identifiers kept byte-compatible with the baseline.
            t = text.replace("'t113:'", "").replace("'t113-stage-output/1'", "") if p.name == "base.py" else text
            if marker in t:
                bad.append((p.name, marker))
    assert bad == []
def test_import_has_no_live_side_effects():
    code = "import sys,tender_harness,tender_harness.pack,tender_harness.cli,tender_harness.offline,tender_harness.gates,tender_harness.config;print(sorted(m for m in ('foliorun','temporalio','opensandbox','openai','pydantic_ai','httpx') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd="/", check=True).stdout.strip()
    assert out == "[]"

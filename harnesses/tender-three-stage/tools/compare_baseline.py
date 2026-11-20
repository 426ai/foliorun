"""Read-only comparison of a new live business run against a historical baseline business run directory."""
import argparse, json
from pathlib import Path
from tender_harness.compare import compare_dirs
def flat(v, p=""):
    if isinstance(v, dict):
        for k, x in v.items(): yield from flat(x, p + "/" + k)
    elif isinstance(v, list):
        for i, x in enumerate(v): yield from flat(x, p + "/" + str(i))
    else: yield p, v
def main():
    a = argparse.ArgumentParser(); a.add_argument("--baseline", required=True); a.add_argument("--new", required=True); a.add_argument("--legacy-combined", default="combined-response"); a.add_argument("--out", required=True); x = a.parse_args()
    rep = {}
    for st in ("extract", "semantic", "fill"):
        b, n = Path(x.baseline) / st, Path(x.new) / st
        r = compare_dirs(b, n, {"combined-response.%s.docx" % st: "%s.%s.docx" % (x.legacy_combined, st)})
        docs = {k: v for k, v in r.items() if k.endswith(".docx")}
        mb = dict(flat(json.loads((b / "stage-manifest.json").read_text()))); mn = dict(flat(json.loads((n / "stage-manifest.json").read_text())))
        mdiff = sorted({k.rsplit("/", 1)[-1] for k in set(mb) | set(mn) if mb.get(k) != mn.get(k)})
        gb = json.loads((b / "host-gate.json").read_text()); gn = json.loads((n / "host-gate.json").read_text())
        rep[st] = {"docx_compared": len(docs), "docx_ooxml_identical": sum(1 for v in docs.values() if v.get("same_members") and not v.get("differing_members")),
                   "docx_differing": {k: v for k, v in docs.items() if v.get("differing_members") or not v.get("same_members", True) or v.get("missing_in_baseline")},
                   "summary_equal": r.get("summary.json", {}).get("byte_equal"), "manifest_differing_leaf_keys": mdiff,
                   "host_gate_combined_equal_modulo_label": {k: v for k, v in gn["combined"].items()} == {k: v for k, v in gb["combined"].items()} or st != "fill",
                   "new_host_gate_combined": gn["combined"], "baseline_host_gate_combined": gb["combined"]}
    Path(x.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2)); print(json.dumps({k: {kk: v[kk] for kk in ("docx_compared", "docx_ooxml_identical", "summary_equal", "manifest_differing_leaf_keys")} for k, v in rep.items()}, ensure_ascii=False))
if __name__ == "__main__": main()

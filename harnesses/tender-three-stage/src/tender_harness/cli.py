"""tender-harness CLI. Every live command requires --host-config and explicit --allow-live where a model is used."""
import argparse, asyncio, json, sys, tempfile
from pathlib import Path
def _pack(a):
    from tender_harness.pack import load_project_pack
    return load_project_pack(a.pack)
def _ids(a):
    return a.template.split(",") if getattr(a, "template", None) else None
def cmd_validate(a):
    p = _pack(a); print(json.dumps({"ok": True, "project_id": p.project_id, "templates": len(p.templates), "files": len(p.project["files"]), "fingerprint": p.fingerprint}, ensure_ascii=False))
def cmd_offline(a):
    from tender_harness.offline import run_offline
    p = _pack(a); work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="tender-offline-")) / "root"
    out = run_offline(p, work, _ids(a))
    for stage, (r, d, _) in out.items():
        print("OFFLINE_STAGE", stage, "files", len(r["files"]), json.dumps(r.get("combined_counts")), flush=True)
    print("OFFLINE_ROOT", work)
def _ctx(a):
    from tender_harness.config import load_host_config
    from tender_harness.live import Context
    return Context(load_host_config(a.host_config), _pack(a), _ids(a))
def cmd_preflight(a):
    from tender_harness.live import preflight
    print(json.dumps(asyncio.run(preflight(_ctx(a))), ensure_ascii=False))
def cmd_run(a):
    if not a.allow_live:
        sys.exit("explicit --allow-live required (paid model calls)")
    from tender_harness.live import run_business
    b, r = asyncio.run(run_business(_ctx(a), a.resume_business)); print(json.dumps({"business_id": b, "success": r["success"]}))
def main(argv=None):
    ap = argparse.ArgumentParser(prog="tender-harness"); sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, live in [("validate-pack", cmd_validate, False), ("offline", cmd_offline, False), ("preflight", cmd_preflight, True), ("run", cmd_run, True)]:
        s = sub.add_parser(name); s.add_argument("--pack", required=True); s.set_defaults(fn=fn)
        if name != "validate-pack":
            s.add_argument("--template", help="comma separated ordered subset of template ids")
        if name == "offline":
            s.add_argument("--work")
        if live:
            s.add_argument("--host-config", required=True)
        if name == "run":
            s.add_argument("--allow-live", action="store_true"); s.add_argument("--resume-business")
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except Exception as ex:
        print("SAFE_STOP", getattr(ex, "code", None) or (str(ex) if type(ex).__name__ in ("GateError", "PackError") else type(ex).__name__), file=sys.stderr); raise SystemExit(2)
if __name__ == "__main__":
    main()

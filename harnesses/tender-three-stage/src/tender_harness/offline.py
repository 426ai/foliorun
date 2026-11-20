"""Deterministic OFFLINE three-stage execution of the in-image engine on the host. Never a live-model claim."""
import copy, shutil
from pathlib import Path
from tender_harness.bundles import bundle
from tender_harness.docx_ops.engine import Engine
def plan_from_inspection(eng):
    i = copy.deepcopy(eng.inspect())
    other = {x["person_id"]: {"title": x["required_title"], "clause_id": x["clause_id"], "quote": x["source_quote"], "selected_certificate_ids": x["candidate_certificate_ids"]} for x in i["other_certificate_requirements"]}
    return {k: i[k] for k in ["source_sha256", "resources_sha256", "contracts_revision"]} | {"template_ids": [x["id"] for x in i["templates"]], "source_ranges": {x["id"]: x["range"] for x in i["templates"]}, "reason": "ok", "rule_policy": "all_declared", "person_ids": i["selected_person_ids"], "other_requirements": other}
def run_offline(pack, work, template_ids=None):
    """work: empty directory that plays the sandbox root. Returns {stage: (result, output_dir, scope)}."""
    root = Path(work); root.mkdir(parents=True, exist_ok=False); (root / "materials").mkdir()
    files, scope = pack.asset_files(template_ids)
    bundle(files, root / "materials/assets.zip")
    args = {"assets_bundle": "/materials/assets.zip", "prior_bundle": "/materials/assets.zip", "stage": "extract", "operation": "execute"}
    out = {}
    for stage, call in [("extract", "aa"), ("semantic", "bb"), ("fill", "cc")]:
        args["stage"] = stage; args.pop("plan", None)
        args["plan"] = plan_from_inspection(Engine(args, root))
        r = Engine(args, root).execute(call); d = root / "output" / call
        out[stage] = (r, d, scope)
        if stage != "fill":
            p = bundle({p.name: p for p in d.iterdir() if p.is_file()}, root / "materials" / (stage + "-parent.zip"))
            args["prior_bundle"] = "/materials/" + p.name
    return out

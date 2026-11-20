"""Read-only host re-validation of a collected stage against the external project pack."""
import hashlib
from pathlib import Path
from tender_harness.contracts import Manifest, Slot
from tender_harness.docx_ops import engine as e
COMBINED = "combined-response.{stage}.docx"
def expected_files(scope):
    return len(scope["templates"]) + 3
def host_gate(stage, manifest, files, parent, pack, scope):
    """files: {name: local Path}; parent: previous StageResult dict or None; scope: the (possibly subset) scope."""
    Manifest.model_validate(manifest)
    res = pack.resources; assets = pack.root; source = pack.source_path(); ids = [x["id"] for x in scope["templates"]]
    e.require(manifest["template_ids"] == ids, "full_inventory_omission")
    e.require(manifest["source_sha256"] == scope["source_sha256"] and manifest["resources_sha256"] == scope["resources_sha256"], "snapshot_changed")
    cfgs = pack.contracts(ids); allnodes, allslots, gates = [], [], {}
    old = {a["name"]: Path(a["path"]) for a in parent["artifacts"]} if parent else {}
    plan = manifest["plan"]
    for inv in scope["templates"]:
        tid = inv["id"]; info = manifest["documents"][tid]; p = files[Path(info["path"]).name]
        e.require(e.sha(p) == info["sha256"], "artifact_hash"); meta = info["metadata"]; parts, root = e.b.load_doc(p)
        if stage == "extract":
            e.require([hashlib.sha256(e.canon(n)).hexdigest() for n in e.b.tbody(root) if n.tag != e.W + "sectPr"] == inv["source_node_hashes"], "raw_source_rows_or_styles_changed")
            e.b.package_fixed(source, p); gates[tid] = {"exact_original_nodes": True, "all_original_blank_rows_and_merges": True}
        elif stage == "semantic":
            _, expected, expected_meta = e.compile_one(old[tid + ".extract.docx"], cfgs[tid], res, plan)
            e.require(e.canon(expected) == e.canon(root) and expected_meta == meta, "semantic_outside_declared_recipe")
            e.b.package_fixed(old[tid + ".extract.docx"], p)
            for s in meta["slots"]:
                Slot.model_validate(s)
            if cfgs[tid]["mode"] not in ("base", "resume"):
                e.require({s["rule_id"] for s in meta["slots"] if s.get("origin") == "generic"} == set(scope["slot_requirement_inventory"][tid]["rule_ids"]), "source_required_rule_omitted")
            if cfgs[tid]["mode"] == "resume":
                per = cfgs[tid].get("required_slots_per_person", 11 + 5 * 4 + 8)
                e.require(len(meta["slots"]) == len(res["selected_people"]) * per, "resume_required_field_or_attachment_omission")
            gates[tid] = {"slots": len(meta["slots"]), "all_declared_source_requirements": True, "normalizations": meta.get("normalizations", [])}
        else:
            gates[tid] = e.check_fill(old[tid + ".semantic.docx"], p, meta, cfgs, res, assets, plan)
        allslots.extend(meta["slots"]); allnodes.extend(e.canon(n) for n in e.b.tbody(root) if n.tag != e.W + "sectPr")
    if stage == "semantic":
        from tender_harness.docx_ops.requirements import source_obligations, source_role_obligations
        resume = [t for t in ids if cfgs[t]["mode"] == "resume"]
        if resume:
            gates["independent_role_cert_requirements"] = source_role_obligations(source, res, cfgs[resume[0]])
        gates["independent_original_obligations"] = source_obligations(source, {t: files[t + ".semantic.docx"] for t in ids}, scope["templates"])
    combined = files[Path(manifest["combined"]["path"]).name]
    e.require(e.sha(combined) == manifest["combined"]["sha256"], "combined_hash")
    if stage == "fill":
        cg = e.check_fill(old[COMBINED.format(stage="semantic")], combined, {"slots": allslots}, cfgs, res, assets, plan)
    else:
        _, cr = e.b.load_doc(combined)
        e.require([e.canon(n) for n in e.b.tbody(cr) if n.tag != e.W + "sectPr"] == allnodes, "combined_missing_reordered_template")
        cg = {"all_units_in_source_order": len(ids)}
    return {"ok": True, "template_count": len(ids), "combined": cg, "templates": gates, "host_validation_read_only": True}

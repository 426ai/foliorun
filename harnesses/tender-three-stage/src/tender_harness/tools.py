"""Sealed Foliorun tool registry for one stage. Command contract is closed; tools run only in the sandbox."""
from tender_harness.contracts import Command
from tender_harness.schema import inline_schema
DESCRIPTIONS = {
 "load": "Actually read up to 8 independently copied dedicated SKILL.md and complete stage contracts from the frozen VFS asset bundle. Batch all source template ids; returns real content and hash proof.",
 "inspect": "Inspect complete original response inventory, exclusions, exact ranges, frozen selected personnel and source-clause-backed other-certificate candidates.",
 "execute": "Execute this contracted stage ONCE for all inventoried original formats. Source/hash/owner/type/slot/required coverage gates apply. Writes only sandbox outputs; returns all file paths to submit."}
NAMES = {"load": "load_template_skills", "inspect": "full_inspect", "execute": "full_execute"}
ENTRYPOINT = "tender-docx"
STAGES = ("extract", "semantic", "fill")
BOUND = ("operation", "stage")
ASSETS_PATH = "/materials/assets.zip"
PRIOR_PATH = "/materials/prior.zip"
PATHS = {"assets_bundle": ASSETS_PATH, "prior_bundle": PRIOR_PATH}
def entrypoint(op, stage):
    return ENTRYPOINT + "-" + op + "-" + stage
def entrypoint_argv(base_argv):
    """Trusted argv per (operation, stage): the constant fields are bound by the host, never required from the model."""
    return {entrypoint(op, st): tuple(base_argv) + ("--operation", op, "--stage", st) for op in NAMES for st in STAGES}
def tool_schema(op, stage):
    s = inline_schema(Command.model_json_schema())
    s["properties"]["operation"] = {"type": "string", "const": op}
    s["properties"]["stage"] = {"type": "string", "const": stage}
    for k, v in PATHS.items():
        s["properties"][k] = {"type": "string", "const": v}
    s["required"] = [k for k in s.get("required", []) if k not in BOUND]
    return s
def registry(stage, project_id):
    from foliorun import SandboxCommand, ToolRegistry, ToolSpec
    r = ToolRegistry("tender-harness-" + project_id + "-" + stage, "3", release="tender-three-stage-harness-0.1.2")
    for op, name in NAMES.items():
        s = tool_schema(op, stage)
        r.register(ToolSpec(name=name, description=DESCRIPTIONS[op] + " Always pass assets_bundle=" + ASSETS_PATH + " and prior_bundle=" + PRIOR_PATH + " exactly.", version="1", execution_location="sandbox", effect="workspace_write" if op == "execute" else "read_only", retry_class="never_auto", input_paths=("assets_bundle", "prior_bundle"), input_schema=s), SandboxCommand(entrypoint=entrypoint(op, stage)))
    r.seal()
    return r

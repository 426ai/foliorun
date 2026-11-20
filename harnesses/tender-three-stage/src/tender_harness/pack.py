"""External project pack: declarative data only (DOCX, JSON, Markdown). Never imported or executed."""
import hashlib, json, re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
SCHEMA = "tender-project-pack/1"
_SECRET_NAME = re.compile(r"(^|[._-])(env|secret|credential|token|pem|key)([._-]|$)", re.I)
_SECRET_TEXT = re.compile(rb"(sk-[A-Za-z0-9]{20,}|OPENSANDBOX_API_KEY\s*=|DEEPSEEK_API_KEY\s*=|BEGIN [A-Z ]*PRIVATE KEY)")
_TEXT_SUFFIX = {".json", ".md", ".txt", ".yaml", ".yml", ".csv"}
_FORBIDDEN_SUFFIX = {".py", ".pyc", ".so", ".dylib", ".sh", ".exe", ".sqlite", ".db", ".pid"}
class PackError(ValueError):
    pass
def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise PackError("duplicate_json_member")
        d[k] = v
    return d
def read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"), object_pairs_hook=_no_dupes)
def safe_rel(rel):
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise PackError("unsafe_pack_path")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(x in ("..", "") or x.startswith(".") for x in p.parts):
        raise PackError("unsafe_pack_path")
    return p
@dataclass(frozen=True)
class TemplateSpec:
    id: str
    mode: str
    start: int
    end: int
    contract_sha256: str
@dataclass(frozen=True)
class ProjectPack:
    root: Path
    project: dict
    scope: dict
    resources: dict
    fingerprint: str
    templates: tuple = field(default_factory=tuple)
    @property
    def project_id(self):
        return self.project["project_id"]
    @property
    def namespace(self):
        return self.project["owner_specialty"]
    @property
    def template_ids(self):
        return [t.id for t in self.templates]
    def path(self, rel):
        return self.root / safe_rel(rel)
    def skill_dir(self, tid):
        return self.root / "skills" / self.namespace / tid
    def contracts(self, ids=None):
        return {t: read_json(self.skill_dir(t) / "resources/stage-contract.json") for t in (ids or self.template_ids)}
    def source_path(self):
        return self.path(self.project["source"]["path"])
    def select(self, ids=None):
        """Scope restricted to an explicit ordered subset; full scope when ids is None."""
        if ids is None or list(ids) == self.template_ids:
            return self.scope
        ids = list(ids)
        order = [t for t in self.template_ids if t in ids]
        if order != ids or len(set(ids)) != len(ids) or not ids:
            raise PackError("template_subset_unknown_or_unordered")
        s = json.loads(json.dumps(self.scope))
        s["templates"] = [t for t in s["templates"] if t["id"] in ids]
        s["contract_sha256"] = {k: v for k, v in s["contract_sha256"].items() if k in ids}
        s["slot_requirement_inventory"] = {k: v for k, v in s["slot_requirement_inventory"].items() if k in ids}
        s["subset_of_scope_sha256"] = sha256_file(self.path(self.project["scope"]))
        return s
    def asset_files(self, ids=None):
        """Members for the frozen sandbox assets bundle (inputs + only the selected skills)."""
        scope = self.select(ids); ids = [t["id"] for t in scope["templates"]]
        out = {}
        for rel in sorted(self.project["files"]):
            parts = PurePosixPath(rel).parts
            if parts[0] == "inputs" or (parts[0] == "skills" and len(parts) > 2 and parts[2] in ids):
                out[rel] = self.root / rel
        out[self.project["scope"]] = json.dumps(scope, ensure_ascii=False, sort_keys=True).encode() if scope is not self.scope else self.path(self.project["scope"])
        return out, scope
def load_project_pack(path):
    given = Path(path)
    if given.is_symlink():
        raise PackError("pack_root_symlink")
    root = given.resolve(strict=True)
    pj = root / "project.json"
    if pj.is_symlink() or not pj.is_file():
        raise PackError("project_json_missing")
    project = read_json(pj)
    if project.get("schema") != SCHEMA:
        raise PackError("pack_schema")
    for k in ("project_id", "owner_specialty", "source", "scope", "resources", "files"):
        if k not in project:
            raise PackError("pack_field_missing_" + k)
    files = project["files"]
    for rel, digest in files.items():
        p = root / safe_rel(rel)
        cur = root
        for part in PurePosixPath(rel).parts:
            cur = cur / part
            if cur.is_symlink():
                raise PackError("pack_member_symlink")
        if not p.is_file():
            raise PackError("pack_member_missing")
        if p.suffix.lower() in _FORBIDDEN_SUFFIX or _SECRET_NAME.search(p.name):
            raise PackError("pack_member_forbidden_type")
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise PackError("pack_member_hash")
        if p.suffix.lower() in _TEXT_SUFFIX and _SECRET_TEXT.search(data):
            raise PackError("pack_member_contains_secret")
    for p in root.rglob("*"):
        if p.is_symlink():
            raise PackError("pack_member_symlink")
        if p.is_file() and p != pj and str(p.relative_to(root)) not in files:
            raise PackError("undeclared_pack_file")
    for k in ("scope", "resources"):
        if project[k] not in files:
            raise PackError("pack_member_undeclared_" + k)
    if project["source"]["path"] not in files or files[project["source"]["path"]] != project["source"]["sha256"]:
        raise PackError("pack_source_hash")
    scope = read_json(root / project["scope"]); resources = read_json(root / project["resources"])
    if scope["source_sha256"] != project["source"]["sha256"] or scope["resources_sha256"] != files[project["resources"]]:
        raise PackError("pack_scope_snapshot_mismatch")
    templates, seen, ranges = [], set(), []
    for t in scope["templates"]:
        tid = t["id"]
        if tid in seen or not re.fullmatch(r"[a-z0-9_]+", tid):
            raise PackError("template_id_invalid_or_duplicate")
        seen.add(tid)
        if not (isinstance(t["start"], int) and isinstance(t["end"], int) and 0 <= t["start"] < t["end"]):
            raise PackError("template_range_invalid")
        ranges.append((t["start"], t["end"]))
        base = "skills/" + project["owner_specialty"] + "/" + tid + "/"
        c = base + "resources/stage-contract.json"
        if base + "SKILL.md" not in files or c not in files or files[c] != scope["contract_sha256"].get(tid):
            raise PackError("template_skill_or_contract_mismatch")
        templates.append(TemplateSpec(tid, t["mode"], t["start"], t["end"], files[c]))
    if set(scope["contract_sha256"]) != seen:
        raise PackError("contract_inventory_mismatch")
    ranges.sort()
    if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
        raise PackError("template_ranges_overlap")
    return ProjectPack(root, project, scope, resources, sha256_file(pj), tuple(templates))

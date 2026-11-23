"""Caller-injected tool definitions/handlers, stable recovery identity and routing."""

import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from jsonschema import Draft202012Validator

from ._sandbox_inspect import InspectionFault, text_page
from .contracts import BindingRef, FileRef, Frozen, RuntimeFault, canonical, digest, path_checked


class ToolSpec(Frozen):
    name: str
    description: str
    input_schema: dict
    version: str = "1"
    execution_location: Literal["host", "sandbox"]
    effect: Literal["read_only", "workspace_write", "external_side_effect"]
    retry_class: Literal["safe", "idempotency_required", "never_auto"] = "never_auto"
    input_paths: tuple[str, ...] = ()


class SandboxCommand(Frozen):
    """Name of a host-injected preinstalled provider binding, never a model-selected argv."""

    entrypoint: str


@dataclass(frozen=True)
class ToolContext:
    scope: str
    run_id: str
    call_id: str
    check_cancelled: object


class ToolRegistry:
    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise RuntimeFault("registry_frozen")
        object.__setattr__(self, name, value)

    def __init__(
        self, name: str = "tools", version: str = "1", *, release: str = "caller-supplied"
    ):
        self.name, self.version, self.release = name, version, release
        self._entries = {}
        self._sealed = False

    def register(self, spec: ToolSpec, handler):
        if self._sealed:
            raise RuntimeFault("registry_frozen")
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", spec.name)
            or spec.name.startswith(("fs_", "sandbox_"))
            or spec.name
            in {
                "submit_result", "discover_skills", "read_file", "stat_file",
                "list_files", "materialize_files",
            }
            or spec.name in self._entries
        ):
            raise RuntimeFault("tool_name_conflict")
        schema = spec.input_schema
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise RuntimeFault("closed_tool_schema_required")
        if '"$ref"' in canonical(schema).decode():
            raise RuntimeFault("tool_schema_ref_unsupported")
        if spec.execution_location == "sandbox":
            if not isinstance(handler, SandboxCommand):
                raise RuntimeFault("sandbox_mapping_required")
        elif not callable(handler) or spec.effect == "workspace_write":
            raise RuntimeFault("host_handler_required")
        # Copy nested schema so later mutation of the caller's dict cannot alter identity.
        self._entries[spec.name] = (ToolSpec.model_validate_json(canonical(spec)), handler)

    def seal(self):
        if not self._sealed:
            self._descriptor = canonical(
                {
                    "name": self.name,
                    "version": self.version,
                    "release": self.release,
                    "tools": [
                        {
                            "spec": s.model_dump(mode="json"),
                            "mapping": h.model_dump(mode="json")
                            if isinstance(h, SandboxCommand)
                            else "host-installed",
                        }
                        for s, h in self._entries.values()
                    ],
                }
            )
            self._entries = MappingProxyType(self._entries)
            self._ref = BindingRef(
                name=self.name, version=self.version, sha256=digest(self._descriptor)
            )
            self._sealed = True
        return self._ref

    @property
    def descriptor(self):
        self.seal()
        return self._descriptor

    @property
    def entries(self):
        # A detached schema prevents mutation through consumers as well.
        return {
            n: (ToolSpec.model_validate_json(canonical(s)), h)
            for n, (s, h) in self._entries.items()
        }


# The old name remains import-compatible; this is a container, not a capability service.
Tools = ToolRegistry


class Journal:
    """Intent is committed before send. Unknown intents are NEVER automatically retried."""

    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def begin(self, kind, call_id, identity):
        key = "journal/" + self.run_id + "/" + digest((kind + ":" + call_id).encode())
        fingerprint = digest(canonical(identity))
        with self.store.transaction() as db:
            old = self.store.get_record(key, db)
            if old:
                if old[1]["fingerprint"] != fingerprint:
                    raise RuntimeFault("call_id_conflict")
                if old[1]["state"] != "done":
                    raise RuntimeFault("unknown_" + kind + "_window")
                return key, old[1]["result"]
            self.store.cas(key, 0, {"fingerprint": fingerprint, "state": "intent"}, db)
        return key, None

    def complete(self, key, result):
        old = self.store.get_record(key)
        self.store.cas(key, old[0], {**old[1], "state": "done", "result": result})


class Gateway:
    def __init__(self, store, run_id, job, profile, registry, box, files, *, lazy=None):
        self.store, self.run_id, self.job, self.profile = store, run_id, job, profile
        self.registry, self.box, self.files = registry, box, files
        self.lazy = lazy
        self.journal = Journal(store, run_id)
        self.calls, self.submitted = 0, None
        self.deadline = time.monotonic() + job.limits.deadline_seconds

    def check(self):
        record = self.store.get_record("run/" + self.run_id)[1]
        if record.get("cancelled"):
            raise RuntimeFault("cancelled")
        if record.get("terminal"):
            raise RuntimeFault("run_closed")
        lease = record.get("lease")
        if lease:
            session = self.store.get_record("session/" + self.job.session_id)
            if (
                lease["expires_at"] <= time.time()
                or session is None
                or session[1]["active"] != self.run_id
                or session[1].get("fence") != lease["fence"]
            ):
                raise RuntimeFault("publication_fenced")
        if time.monotonic() >= self.deadline:
            raise RuntimeFault("deadline_exceeded")

    def declarations(self):
        result = [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s, _ in self.registry.entries.values()
            if s.name in self.profile.allowed_tools
        ]

        def builtin(name, description, properties, required):
            return {
                "name": name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }

        result.extend(
            [
                builtin("list_files", "List frozen VFS and previous submitted files.", {}, []),
                builtin(
                    "discover_skills", "Discover optional Skill name/description metadata.", {}, []
                ),
                builtin(
                    "read_file",
                    "Read paged UTF-8 text from frozen inputs, /previous, or this run's /output. "
                    "Offsets and length are Unicode characters; JSON response bytes are bounded.",
                    {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                        "length": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self.job.limits.text_bytes,
                        },
                    },
                    ["path"],
                ),
                builtin(
                    "stat_file",
                    "Inspect size and SHA-256 of a frozen input, /previous, or this run's "
                    "/output file. Does not submit or seal outputs.",
                    {"path": {"type": "string"}},
                    ["path"],
                ),
                builtin(
                    "materialize_files",
                    "Materialize selected read-only inputs at listed paths.",
                    {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                    ["paths"],
                ),
                builtin(
                    "submit_result",
                    "Explicitly submit files already written under /output.",
                    {"files": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
                    ["files"],
                ),
            ]
        )
        return result

    async def invoke(self, name: str, args: dict, call_id: str):
        self.check()
        if len(canonical(args)) > self.job.limits.text_bytes:
            raise RuntimeFault("tool_arguments_budget")
        if self.submitted is not None:
            raise RuntimeFault("result_already_submitted")
        declarations = {d["name"]: d for d in self.declarations()}
        if name not in declarations:
            raise RuntimeFault("tool_not_authorized")
        try:
            Draft202012Validator(declarations[name]["input_schema"]).validate(args)
        except Exception:
            raise RuntimeFault("tool_arguments_invalid") from None
        if name == "read_file" and any(
            type(args[field]) is not int for field in ("offset", "length") if field in args
        ):
            raise RuntimeFault("tool_arguments_invalid")
        self.calls += 1
        limit = self.job.limits.tools
        if limit is not None and self.calls > limit:
            raise RuntimeFault("tool_budget")
        spec_handler = self.registry.entries.get(name)
        identity = {
            "name": name,
            "args": args,
            "registry": self.registry.seal().model_dump(),
            "version": spec_handler[0].version if spec_handler else "builtin-v1",
        }
        key, old = self.journal.begin("tool", call_id, identity)
        if old is not None:
            return old
        if name == "list_files":
            result = {"kind": "json", "value": sorted(self.files)}
        elif name == "discover_skills":
            found = []
            for path, file in self.files.items():
                if path.startswith("/skills/") and path.endswith("/SKILL.md"):
                    found.append(
                        {
                            "path": path,
                            "name": getattr(file, "skill_name", None),
                            "description": getattr(file, "skill_description", None),
                        }
                    )
            result = {"kind": "json", "value": found}
        elif name in {"read_file", "stat_file"}:
            result = await self.inspect_file(name, args)
        elif name == "materialize_files":
            if self.lazy is None:
                raise RuntimeFault("materialization_unavailable")
            await self.lazy.materialize(self.box, args["paths"])
            result = {"kind": "json", "value": {"paths": args["paths"]}}
        elif name == "submit_result":
            self.check()
            from .collection import Collector

            seal = await Collector(self.store).seal(
                self.run_id, self.box, args["files"], self.job.limits
            )
            self.check()
            self.submitted = seal.files
            result = {"kind": "json", "value": {"accepted": True, "files": len(seal.files)}}
        else:
            spec, handler = spec_handler
            self.check()
            if spec.execution_location == "host":
                result = await handler(
                    args, ToolContext(self.job.scope, self.run_id, call_id, self.check)
                )
            else:
                if spec.input_paths:
                    if self.lazy is None:
                        raise RuntimeFault("materialization_unavailable")
                    paths = []
                    for field in spec.input_paths:
                        value = args.get(field)
                        if value is not None:
                            if not isinstance(value, str) or value not in self.files:
                                raise RuntimeFault("input_missing")
                            paths.append(value)
                    await self.lazy.materialize(self.box, paths)
                result = await self.box.execute(handler, args, call_id, self.job.limits)
        self.check()
        if (
            not isinstance(result, dict)
            or set(result) != {"kind", "value"}
            or result["kind"] not in {"text", "json", "artifacts"}
            or len(canonical(result)) > self.job.limits.text_bytes
        ):
            raise RuntimeFault("tool_result_invalid")
        if result["kind"] == "text" and not isinstance(result["value"], str):
            raise RuntimeFault("tool_result_invalid")
        if result["kind"] == "artifacts":
            from_refs = result["value"]
            if not isinstance(from_refs, list):
                raise RuntimeFault("tool_result_invalid")
            # Only already-persisted scoped refs may cross a host tool boundary.
            for item in from_refs:
                file = FileRef.model_validate(item)
                self.store.read(file.blob)
        self.journal.complete(key, result)
        return result

    async def inspect_file(self, name, args):
        path = args["path"]
        limits = self.job.limits
        read = name == "read_file"
        offset = args.get("offset", 0)
        length = args.get("length", max(1, limits.text_bytes // 4))
        if path.startswith("/output/"):
            path_checked(path, output=True)
            inspect = getattr(self.box, "inspect_file", None)
            if inspect is None:
                raise RuntimeFault("file_inspection_unavailable")
            return await inspect(path, limits, read=read, offset=offset, length=length)
        previous = path.startswith("/previous/")
        if previous:
            path_checked("/output/" + path.removeprefix("/previous/"), output=True)
        else:
            path_checked(path)
        file = self.files.get(path)
        if file is None:
            raise RuntimeFault("input_missing")
        if not read:
            return {"kind": "json", "value": {
                "path": path, "size": file.blob.size, "sha256": file.blob.sha256,
            }}
        body = await self.lazy.read(path) if self.lazy else self.store.read(
            file.blob, limits.input_bytes
        )
        try:
            return text_page(body, offset, length, limits.text_bytes)
        except InspectionFault as exc:
            code = str(exc)
            if not previous and code in {"file_not_utf8", "file_not_text"}:
                return {"kind": "json", "value": {"error": {"code": code}}}
            raise RuntimeFault(code) from None

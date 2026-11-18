"""OpenSandbox 0.1.16 adapter. Only fixed, foreground image capabilities run as app."""

import json
import mimetypes
import shlex
import time
from contextlib import aclosing
from dataclasses import dataclass
from datetime import timedelta
from importlib.resources import files
from pathlib import PurePosixPath

from opensandbox.config import ConnectionConfig
from opensandbox.manager import SandboxManager
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import WriteEntry
from opensandbox.sandbox import Sandbox

from ..contracts import AdapterDescriptor, RuntimeFault, canonical, digest, path_checked


@dataclass(frozen=True)
class PreinstalledTool:
    """Trusted fixed argv prefix; payload and call id are appended as quoted arguments."""

    argv: tuple[str, ...]

    def __post_init__(self):
        if not self.argv or any(not v or "\x00" in v for v in self.argv):
            raise RuntimeFault("invalid_tool_binding")


DOCX_BINDING = PreinstalledTool(("python", "/opt/document-tools/tool.py", "execute"))


async def download_binary(box, path, limit):
    if not isinstance(limit, int) or not 0 <= limit <= 128 * 1024 * 1024:
        raise RuntimeFault("byte_budget")
    body = bytearray()
    # offset/limit in SDK 0.1.16 select a line-based route: NEVER pass them here.
    stream = await box.files.read_bytes_stream(path)
    async with aclosing(stream):
        async for chunk in stream:
            body.extend(chunk)
            if len(body) > limit:
                raise RuntimeFault("byte_budget")
    return bytes(body)


class OpenSandboxProvider:
    descriptor = AdapterDescriptor(name="opensandbox", version="0.1.16")

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        owner="foliorun",
        tools: dict[str, PreinstalledTool] | None = None,
        seal_argv=("python", "/opt/document-tools/tool.py", "seal"),
    ):
        self.owner = owner
        self.tools = dict(tools) if tools is not None else {"docx": DOCX_BINDING}
        self.seal_argv = tuple(seal_argv)
        if not api_key or any(c.isspace() for c in api_key):
            raise RuntimeFault("opensandbox_key_required")
        self.connection = ConnectionConfig(
            domain=endpoint,
            protocol="http",
            api_key=api_key,
            use_server_proxy=True,
            request_timeout=timedelta(seconds=140),
        )

    async def create(self, profile, run_id, scope):
        box = await Sandbox.create(
            profile.image,
            connection_config=self.connection,
            timeout=timedelta(
                seconds=profile.limits.deadline_seconds + profile.collection_seconds + 120
            ),
            ready_timeout=timedelta(seconds=60),
            resource={"cpu": "1", "memory": "2Gi"},
            metadata={"owner": self.owner, "scope": scope, "run_id": run_id},
        )
        session = OpenSandboxSession(box, tools=self.tools, seal_argv=self.seal_argv)
        try:
            await session.verify_retention(
                int(time.time()) + profile.limits.deadline_seconds + profile.collection_seconds
            )
        except BaseException:
            await session.destroy()
            raise
        return session

    async def attach_owned(self, sandbox_id, run_id, scope):
        try:
            box = await Sandbox.connect(sandbox_id, connection_config=self.connection)
        except Exception as exc:
            code = (
                "output_source_lost"
                if getattr(exc, "status_code", None) == 404
                else "collection_source_unavailable"
            )
            raise RuntimeFault(code) from None
        try:
            info = await box.get_info()
            if any(
                (info.metadata or {}).get(k) != v
                for k, v in {"owner": self.owner, "scope": scope, "run_id": run_id}.items()
            ):
                raise RuntimeFault("source_ownership_mismatch")
            return OpenSandboxSession(box, tools=self.tools, seal_argv=self.seal_argv, sealed=True)
        except BaseException:
            await box.close()
            raise

    async def destroy_owned(self, sandbox_id, run_id, scope):
        manager = await SandboxManager.create(connection_config=self.connection)
        try:
            try:
                info = await manager.get_sandbox_info(sandbox_id)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    return
                raise RuntimeFault("cleanup_unavailable") from None
            meta = info.metadata or {}
            if any(
                meta.get(k) != v
                for k, v in {"owner": self.owner, "scope": scope, "run_id": run_id}.items()
            ):
                raise RuntimeFault("cleanup_ownership_mismatch")
            await manager.kill_sandbox(sandbox_id)
        finally:
            await manager.close()


class OpenSandboxSession:
    def __init__(self, box, *, tools=None, seal_argv=None, sealed=False):
        self.box = box
        self.id = str(box.id)
        self.sealed = sealed
        self.tools = dict(tools) if tools is not None else {"docx": DOCX_BINDING}
        self.seal_argv = seal_argv or ("python", "/opt/document-tools/tool.py", "seal")
        self.retain_until = 0

    async def verify_retention(self, retain_until):
        info = await self.box.get_info()
        if info.expires_at is None or info.expires_at.timestamp() < retain_until:
            raise RuntimeFault("sandbox_retention_unavailable")
        self.retain_until = retain_until

    async def command(self, argv, *, app=False, timeout=90):
        command = shlex.join((["runuser", "-u", "app", "--"] if app else []) + argv)
        result = await self.box.commands.run(
            command,
            opts=RunCommandOpts(timeout=timedelta(seconds=timeout), working_directory="/workspace"),
        )
        if result.error or result.exit_code != 0:
            raise RuntimeFault("sandbox_command_failed")
        if len(result.text.encode()) > 131072:
            raise RuntimeFault("command_output_budget")
        return result.text

    async def prepare(self, files, store, profile):
        await self.command(["mkdir", "-p", "/output", "/workspace", "/previous"])
        await self.command(["chown", "app:app", "/output", "/workspace"])
        # Verify the image's Skill asset identity independently of caller VFS data.
        body = await download_binary(self.box, "/opt/document-tools/skills-manifest.json", 65536)
        if digest(body) != profile.skills_manifest:
            raise RuntimeFault("image_skills_mismatch")
        for relative, expected in json.loads(body)["files"].items():
            path = path_checked("/skills/" + relative)
            data = await download_binary(self.box, path, 131072)
            if digest(data) != expected:
                raise RuntimeFault("image_skills_mismatch")
        # Caller /skills is its own snapshot. Remove preinstalled Skill tree before replacing
        # only when a caller supplies /skills; the image declaration above remains verified.
        if any(p.startswith("/skills/") for p in files):
            await self.command(["rm", "-rf", "/skills"])
        # Empty, root-owned input roots are visible; file bodies are uploaded only on demand.
        roots = sorted({"/" + PurePosixPath(p).parts[1] for p in files})
        if roots:
            await self.command(["mkdir", "-p", *roots])
            await self.command(["chmod", "0555", *roots])

    async def materialize(self, path, body, file):
        if path.startswith("/previous/"):
            path_checked("/output/" + path.removeprefix("/previous/"), output=True)
        else:
            path_checked(path)
        await self.command(["mkdir", "-p", str(PurePosixPath(path).parent)])
        await self.box.files.write_files(
            [WriteEntry(path=path, data=body, mode=444, owner="root", group="root")]
        )
        actual = await download_binary(self.box, path, file.blob.size)
        if digest(actual) != file.blob.sha256 or len(actual) != file.blob.size:
            raise RuntimeFault("materialization_integrity")
        await self.command(["chmod", "-R", "a-w", "/" + PurePosixPath(path).parts[1]])

    async def execute(self, mapping, args, call_id, limits):
        if self.sealed:
            raise RuntimeFault("output_sealed")
        binding = self.tools.get(mapping.entrypoint)
        if binding is None:
            raise RuntimeFault("sandbox_binding_unavailable")
        call = digest(call_id.encode())[:32]
        text = await self.command(
            [
                *binding.argv,
                canonical(args).decode(),
                "--call",
                call,
            ],
            app=True,
            timeout=limits.command_seconds,
        )
        try:
            result = json.loads(text)
            for path in result["files"]:
                path_checked(path, output=True)
        except Exception:
            raise RuntimeFault("command_receipt_invalid") from None
        return {"kind": "json", "value": result}

    async def seal(self, paths, limits):
        if not 0 < len(paths) <= limits.files or len(set(paths)) != len(paths):
            raise RuntimeFault("file_budget")
        for p in paths:
            path_checked(p, output=True)
        text = await self.command(
            [*self.seal_argv, canonical(paths).decode()],
            timeout=limits.command_seconds,
        )
        self.sealed = True
        info = json.loads(text)
        if [f["path"] for f in info] != paths:
            raise RuntimeFault("output_manifest_invalid")
        if sum(f["size"] for f in info) > limits.output_bytes:
            raise RuntimeFault("byte_budget")
        for entry in info:
            entry["media_type"] = (
                mimetypes.guess_type(entry["path"])[0] or "application/octet-stream"
            )
        return info

    async def read_output(self, path, size):
        if not self.sealed:
            raise RuntimeFault("output_not_sealed")
        path_checked(path, output=True)
        return await download_binary(self.box, path, size)

    async def inspect_file(self, path, limits, *, read=False, offset=0, length=1):
        """Read only this session's output via a fixed, isolated sandbox command.

        This is an observation before submission, not a seal or a collector read.
        Never use the SDK's general file download for model-selected paths.
        """
        path_checked(path, output=True)
        if self.sealed:
            raise RuntimeFault("output_sealed")
        source = files("foliorun").joinpath("_sandbox_inspect.py").read_text()
        payload = {
            "path": path, "limit": limits.output_bytes, "read": read,
            "offset": offset, "length": length, "budget": min(limits.text_bytes, 131071),
        }
        fault = None
        result = None
        try:
            text = await self.command(
                ["python", "-I", "-c", source, canonical(payload).decode()],
                app=True, timeout=limits.command_seconds,
            )
            result = json.loads(text)
            errors = {
                "invalid_output_path", "output_missing", "output_unsafe_file",
                "output_changed", "file_too_large", "file_not_utf8", "file_not_text",
                "file_page_budget", "file_inspection_failed",
            }
            if isinstance(result, dict) and set(result) == {"error"}:
                code = result["error"]
                fault = code if isinstance(code, str) and code in errors else (
                    "file_inspection_invalid"
                )
            elif not self._valid_inspection(result, path, limits, read, offset, length):
                fault = "file_inspection_invalid"
        except Exception:
            # No raw command/SDK exception, stdout, path or arguments cross the boundary.
            fault = "file_inspection_failed"
        if fault is not None:
            raise RuntimeFault(fault)
        return result

    @staticmethod
    def _valid_inspection(result, path, limits, read, offset, length):
        if (
            not isinstance(result, dict) or set(result) != {"kind", "value"}
            or result["kind"] != "json" or not isinstance(result["value"], dict)
            or len(canonical(result)) > limits.text_bytes
        ):
            return False
        value = result["value"]
        if read:
            return (
                set(value) == {"text", "next_offset", "eof"}
                and isinstance(value["text"], str) and len(value["text"]) <= length
                and type(value["next_offset"]) is int
                and value["next_offset"] == offset + len(value["text"])
                and type(value["eof"]) is bool
                and (bool(value["text"]) or value["eof"])
            )
        return (
            set(value) == {"path", "size", "sha256"} and value["path"] == path
            and type(value["size"]) is int and 0 <= value["size"] <= limits.output_bytes
            and isinstance(value["sha256"], str) and len(value["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["sha256"])
        )

    async def close(self):
        await self.box.close()

    async def destroy(self):
        try:
            await self.box.kill()
        finally:
            await self.box.close()

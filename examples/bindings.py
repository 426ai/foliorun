"""Explicit worker deployment bindings; all records are entirely fictional."""

import json

from foliorun import RuntimeFault, SandboxCommand, ToolRegistry, ToolSpec
from foliorun.adapters.opensandbox import OpenSandboxProvider, PreinstalledTool, download_binary
from foliorun.contracts import digest, path_checked

DOCUMENT_TOOL = ("python", "/opt/document-tools/tool.py")


def document_sandbox(connection, *, owner, skills_sha256):
    async def verify_image(session, profile):
        try:
            body = await download_binary(
                session.box, "/opt/document-tools/skills-manifest.json", 65536
            )
            if digest(body) != skills_sha256:
                raise ValueError
            for relative, expected in json.loads(body)["files"].items():
                path = path_checked("/skills/" + relative)
                data = await download_binary(session.box, path, 131072)
                if digest(data) != expected:
                    raise ValueError
        except Exception:
            pass
        else:
            return
        raise RuntimeFault("image_skills_mismatch")

    return OpenSandboxProvider(
        connection=connection,
        owner=owner,
        tools={"docx": PreinstalledTool((*DOCUMENT_TOOL, "execute"))},
        seal_argv=(*DOCUMENT_TOOL, "seal"),
        verify_image=verify_image,
    )


def document_tools():
    registry = ToolRegistry("synthetic-documents", "1", release="generic-docx-1.0.0")
    registry.register(
        ToolSpec(
            name="docx_tool",
            description="Generate/revise/slice/render a fictional DOCX in the sandbox.",
            version="1",
            execution_location="sandbox",
            effect="workspace_write",
            retry_class="never_auto",
            input_paths=("source",),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["generate", "revise", "slice", "render"],
                    },
                    "source": {"type": "string", "maxLength": 1024},
                    "text": {"type": "string", "maxLength": 4096},
                    "start": {"type": "integer", "minimum": 0},
                    "end": {"type": "integer", "minimum": 1},
                },
                "required": ["operation"],
                "allOf": [
                    {
                        "if": {"properties": {"operation": {"const": "generate"}}},
                        "then": {"required": ["text"]},
                    },
                    {
                        "if": {
                            "properties": {"operation": {"enum": ["revise", "slice", "render"]}}
                        },
                        "then": {"required": ["source"]},
                    },
                    {
                        "if": {"properties": {"operation": {"const": "revise"}}},
                        "then": {"required": ["text"]},
                    },
                    {
                        "if": {"properties": {"operation": {"const": "slice"}}},
                        "then": {"required": ["start", "end"]},
                    },
                ],
            },
        ),
        SandboxCommand(entrypoint="docx"),
    )

    async def lookup(args, ctx):
        ctx.check_cancelled()
        return {"kind": "json", "value": {"record": args["id"], "label": "Entirely fictional"}}

    registry.register(
        ToolSpec(
            name="lookup_record",
            description="Read an authorized frozen fictional record.",
            version="1",
            execution_location="host",
            effect="read_only",
            retry_class="never_auto",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string", "enum": ["demo"]}},
                "required": ["id"],
                "additionalProperties": False,
            },
        ),
        lookup,
    )
    registry.seal()
    return registry

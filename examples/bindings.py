"""Explicit worker deployment bindings; all records are entirely fictional."""

from foliorun import SandboxCommand, ToolRegistry, ToolSpec


def document_tools():
    registry = ToolRegistry("synthetic-documents", "1", release="generic-docx-2.0.0")
    registry.register(
        ToolSpec(
            name="docx_tool",
            description="Generate/revise/slice/render a fictional DOCX in the sandbox.",
            version="2",
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
                    "unit": {"type": "string", "enum": ["paragraph", "block"]},
                },
                "required": ["operation"],
                "allOf": [
                    {
                        "if": {"properties": {"operation": {"const": "generate"}}},
                        "then": {"required": ["text"], "not": {"required": ["source"]}},
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
                    {
                        "if": {
                            "properties": {
                                "operation": {"enum": ["generate", "revise", "render"]}
                            }
                        },
                        "then": {"not": {"required": ["unit"]}},
                    },
                ],
            },
        ),
        SandboxCommand(),
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

import json
from pathlib import Path
from runpy import run_path

from jsonschema import Draft202012Validator

document_tools = run_path(
    str(Path(__file__).resolve().parents[1] / "examples" / "bindings.py")
)["document_tools"]


def test_docx_gateway_schema_versions_and_slice_unit():
    registry = document_tools()
    descriptor = json.loads(registry.descriptor)
    assert descriptor["release"] == "generic-docx-2.0.0"
    spec = next(item["spec"] for item in descriptor["tools"] if item["spec"]["name"] == "docx_tool")
    assert spec["version"] == "2"

    validator = Draft202012Validator(spec["input_schema"])
    base = {"operation": "slice", "source": "/documents/synthetic.docx", "start": 0, "end": 1}
    assert validator.is_valid(base)
    assert validator.is_valid({**base, "unit": "paragraph"})
    assert validator.is_valid({**base, "unit": "block"})
    assert not validator.is_valid({**base, "unit": "page"})
    assert not validator.is_valid({**base, "unit": True})
    assert not validator.is_valid(
        {"operation": "generate", "text": "Fictional body", "source": "/documents/template.docx"}
    )
    assert not validator.is_valid(
        {"operation": "generate", "text": "Fictional body", "unit": "block"}
    )

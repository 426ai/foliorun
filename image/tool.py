"""Generic preinstalled DOCX capability. No model, network, host credentials or business rules."""

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

OUTPUT_ROOT = Path("/output")
INPUT_ROOTS = (Path("/documents"), Path("/materials"), Path("/previous"), OUTPUT_ROOT)


def checked(path, roots):
    p = Path(path)
    if str(p) != path or not p.is_absolute() or ".." in p.parts or "\\" in path:
        raise ValueError("invalid_path")
    if not any(p.is_relative_to(root) for root in roots):
        raise ValueError("path_scope")
    for parent in (*reversed(p.parents), p):
        if parent.is_symlink():
            raise ValueError("symlink")
    if p.exists() and not p.is_file():
        raise ValueError("not_regular")
    if p.exists() and p.stat().st_nlink != 1:
        raise ValueError("hardlink")
    return p


def slice_document(doc, start, end, unit):
    from docx.oxml.ns import qn

    if unit not in ("paragraph", "block"):
        raise ValueError("invalid_unit")
    if type(start) is not int or type(end) is not int:
        raise ValueError("invalid_range")

    body = doc._element.body
    children = list(body)
    section_tag = qn("w:sectPr")
    section_properties = list(doc._element.iter(section_tag))
    if (
        len(section_properties) != 1
        or not children
        or children[-1].tag != section_tag
    ):
        raise ValueError("unsupported_sections")

    block_tags = (qn("w:p"), qn("w:tbl"))
    blocks = children[:-1]
    if any(child.tag not in block_tags for child in blocks):
        raise ValueError("unsupported_body_node")

    if unit == "block":
        count = len(blocks)
    else:
        paragraphs = [child for child in blocks if child.tag == qn("w:p")]
        count = len(paragraphs)
    if not 0 <= start < end <= count:
        raise ValueError("invalid_range")

    if unit == "block":
        selected = blocks[start:end]
    else:
        first = blocks.index(paragraphs[start])
        last = blocks.index(paragraphs[end - 1])
        # Paragraph ranges include tables between their endpoint paragraphs only.
        selected = blocks[first : last + 1]

    for child in blocks:
        if child not in selected:
            body.remove(child)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute(args, call):
    from docx import Document

    if not call or any(c not in "0123456789abcdef" for c in call):
        raise ValueError("call_identity")
    out = OUTPUT_ROOT / call
    out.mkdir(exist_ok=False)
    operation = args["operation"]
    if operation == "generate":
        doc = Document()
        doc.add_paragraph(args["text"])
        target = out / "document.docx"
        doc.save(target)
    elif operation in {"revise", "slice"}:
        source = checked(args["source"], INPUT_ROOTS)
        doc = Document(source)
        if operation == "revise":
            doc.add_paragraph(args["text"])
        else:
            slice_document(doc, args["start"], args["end"], args.get("unit", "paragraph"))
        target = out / "document.docx"
        doc.save(target)
    elif operation == "render":
        source = checked(args["source"], INPUT_ROOTS)
        subprocess.run(
            [
                "libreoffice",
                "-env:UserInstallation=file:///workspace/lo-" + call,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out),
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=75,
        )
        target = out / (source.stem + ".pdf")
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-singlefile",
                "-scale-to",
                "600",
                "-png",
                str(target),
                str(out / "preview"),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
    else:
        raise ValueError("unknown_operation")
    paths = sorted(str(p) for p in out.iterdir())
    if not paths or not target.is_file():
        raise ValueError("output_missing")
    receipt = {"files": paths, "sha256": {p: sha(Path(p)) for p in paths}}
    # Successful command seals its own output; only fixed synchronous entrypoints run as app.
    for p in out.iterdir():
        p.chmod(0o444)
    out.chmod(0o555)
    print(json.dumps(receipt))


def seal(paths):
    if len(paths) != len(set(paths)) or not paths:
        raise ValueError("invalid_submission")
    root = OUTPUT_ROOT
    # No process can still write: gateway only exposes synchronous fixed commands.
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_symlink() or not (p.is_dir() or p.is_file()):
            raise ValueError("invalid_output_tree")
        if p.is_file() and p.stat().st_nlink != 1:
            raise ValueError("hardlink")
        os.chown(p, 0, 0)
        p.chmod(0o555 if p.is_dir() else 0o444)
    os.chown(root, 0, 0)
    root.chmod(0o555)
    result = []
    for value in paths:
        p = checked(value, ["/output"])
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024 * 1024:
                raise ValueError("invalid_output")
            h = hashlib.sha256()
            while chunk := stream.read(65536):
                h.update(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("output_changed")
        result.append({"path": value, "size": before.st_size, "sha256": h.hexdigest()})
    print(json.dumps(result))


def selfcheck():
    assert all(
        importlib.util.find_spec(n) is None
        for n in ["pydantic_ai", "temporalio", "opensandbox", "openai", "sqlalchemy"]
    )
    subprocess.run(["pandoc", "--version"], check=True, stdout=subprocess.DEVNULL)
    compute({"operation": "generate", "text": "Fictional selfcheck specimen"}, "a1")
    compute(
        {
            "operation": "slice",
            "source": str(OUTPUT_ROOT / "a1" / "document.docx"),
            "start": 0,
            "end": 1,
        },
        "a2",
    )
    compute(
        {"operation": "render", "source": str(OUTPUT_ROOT / "a2" / "document.docx")},
        "a3",
    )
    p = OUTPUT_ROOT / "a3" / "document.pdf"
    assert p.read_bytes().startswith(b"%PDF")
    assert (OUTPUT_ROOT / "a3" / "preview.png").read_bytes().startswith(b"\x89PNG")
    subprocess.run(["pdfinfo", str(p)], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({"selfcheck": "passed", "harness_absent": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["execute", "seal", "selfcheck"])
    parser.add_argument("payload", nargs="?", default="{}")
    parser.add_argument("--call", default="")
    ns = parser.parse_args()
    if ns.mode == "execute":
        compute(json.loads(ns.payload), ns.call)
    elif ns.mode == "seal":
        seal(json.loads(ns.payload))
    else:
        selfcheck()

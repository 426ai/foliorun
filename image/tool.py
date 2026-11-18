"""Generic preinstalled DOCX capability. No model, network, host credentials or business rules."""

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path


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


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute(args, call):
    from docx import Document

    if not call or any(c not in "0123456789abcdef" for c in call):
        raise ValueError("call_identity")
    out = Path("/output") / call
    out.mkdir(exist_ok=False)
    operation = args["operation"]
    if operation == "generate":
        doc = Document()
        doc.add_heading("Entirely fictional synthetic document", 0)
        doc.add_paragraph(args["text"])
        target = out / "document.docx"
        doc.save(target)
    elif operation in {"revise", "slice"}:
        source = checked(args["source"], ["/documents", "/materials", "/previous", "/output"])
        doc = Document(source)
        if operation == "revise":
            doc.add_paragraph(args["text"])
        else:
            start, end = args["start"], args["end"]
            if not 0 <= start < end <= len(doc.paragraphs):
                raise ValueError("invalid_range")
            # Generic paragraph-only demonstration, not a product document splitter.
            result = Document()
            for p in doc.paragraphs[start:end]:
                result.add_paragraph(p.text)
            doc = result
        target = out / "document.docx"
        doc.save(target)
    elif operation == "render":
        source = checked(args["source"], ["/documents", "/materials", "/previous", "/output"])
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
    root = Path("/output")
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
        {"operation": "slice", "source": "/output/a1/document.docx", "start": 0, "end": 1}, "a2"
    )
    compute({"operation": "render", "source": "/output/a2/document.docx"}, "a3")
    p = Path("/output/a3/document.pdf")
    assert p.read_bytes().startswith(b"%PDF")
    assert Path("/output/a3/preview.png").read_bytes().startswith(b"\x89PNG")
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

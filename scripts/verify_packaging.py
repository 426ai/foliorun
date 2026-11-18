"""Check built archives and isolated wheel installs. Run with the repository test extra."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd, **kwargs):
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "TMPDIR",
            "LANG",
        }
    }
    env.update(
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, check=True, **kwargs
    )
    return {
        "command": [str(arg) for arg in command],
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def requirement(value):
    req = Requirement(value)
    return (
        canonicalize_name(req.name),
        tuple(sorted(req.extras)),
        str(req.specifier),
        str(req.marker) if req.marker else None,
        req.url,
    )


def check_archives(dist_dir):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    name, version = project["name"], project["version"]
    assert canonicalize_name(name) == name == "foliorun"
    assert str(Version(version)) == version
    wheel = dist_dir / f"{name}-{version}-py3-none-any.whl"
    sdist = dist_dir / f"{name}-{version}.tar.gz"
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = {item: archive.read(item) for item in archive.namelist()}
    with tarfile.open(sdist) as archive:
        sdist_files = {}
        for item in archive.getmembers():
            assert item.name.startswith(f"{name}-{version}/")
            assert not item.issym() and not item.islnk()
            if item.isfile():
                sdist_files[item.name.split("/", 1)[1]] = archive.extractfile(item).read()
    for paths in (wheel_files, sdist_files):
        assert all(
            not {"private", ".local", ".git", ".venv", "__pycache__", ".."}.intersection(
                Path(path).parts
            )
            for path in paths
        )
    metadata_bytes = wheel_files[f"{name}-{version}.dist-info/METADATA"]
    assert metadata_bytes == sdist_files["PKG-INFO"], "wheel/sdist metadata differ"
    metadata = BytesParser().parsebytes(metadata_bytes)
    assert metadata["Name"] == name
    assert metadata["Version"] == version
    assert SpecifierSet(metadata["Requires-Python"]) == SpecifierSet(project["requires-python"])
    extras = project["optional-dependencies"]
    assert set(metadata.get_all("Provides-Extra", [])) == set(extras)
    expected = list(project["dependencies"])
    for extra, dependencies in extras.items():
        expected.extend(f'{dep}; extra == "{extra}"' for dep in dependencies)
    assert {requirement(dep) for dep in metadata.get_all("Requires-Dist", [])} == {
        requirement(dep) for dep in expected
    }
    for field in (
        "License",
        "License-Expression",
        "License-File",
        "Author",
        "Author-email",
        "Maintainer",
        "Maintainer-email",
        "Home-page",
        "Project-URL",
        "Classifier",
    ):
        assert metadata.get_all(field, []) == [], f"Unexpected invented field: {field}"
    assert (
        metadata.get_payload(decode=True).decode().strip()
        == (ROOT / "README.md").read_text().strip()
    )
    sources = {
        path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
        for path in (ROOT / "src/foliorun").rglob("*.py")
    }
    assert {
        path: data for path, data in wheel_files.items() if path.startswith("foliorun/")
    } == sources
    for path, data in sources.items():
        assert sdist_files["src/" + path] == data
    for directory in ("tests", "examples", "image", "skills", "scripts", "docs"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                assert sdist_files[path.relative_to(ROOT).as_posix()] == path.read_bytes()
    artifacts = [
        {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (wheel, sdist)
    ]
    return (
        wheel,
        artifacts,
        {key: metadata.get_all(key, []) for key in sorted(set(metadata.keys()))},
    )


def check_installations(wheel):
    results = {}
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    with tempfile.TemporaryDirectory(prefix="foliorun-packaging-") as temporary:
        directory = Path(temporary)
        assert not directory.is_relative_to(ROOT)
        constraints = run(
            [
                "uv",
                "export",
                "--frozen",
                "--all-extras",
                "--no-emit-project",
                "--no-hashes",
            ],
            ROOT,
        )
        constraint_file = directory / "constraints.txt"
        constraint_file.write_text(constraints["stdout"])
        for name, suffix in (
            ("core", ""),
            ("pydantic", "[pydantic-ai]"),
            ("extras", "[temporal,pydantic-ai-openai,opensandbox,test]"),
        ):
            environment = directory / name
            results[name + "_venv"] = run(
                ["uv", "venv", "--python", "3.12", str(environment)], directory
            )
            python = environment / "bin/python"
            results[name + "_install"] = run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    "--constraint",
                    str(constraint_file),
                    str(wheel) + suffix,
                ],
                directory,
            )
            check = f"""
import importlib.metadata as md, importlib.util, json, pathlib, sys
import foliorun
assert foliorun.__version__ == md.version("foliorun") == {version!r}
assert pathlib.Path(foliorun.__file__).is_relative_to(pathlib.Path(sys.prefix))
assert "site-packages/foliorun/" in foliorun.__file__
assert importlib.util.find_spec("document_runtime") is None
assert all(p.name == "_virtualenv.pth" for p in pathlib.Path(sys.prefix).rglob("*.pth"))
optional = ["pydantic_ai", "temporalio", "opensandbox", "openai"]
"""
            if name == "core":
                check += "\nassert all(importlib.util.find_spec(n) is None for n in optional)\n"
            elif name == "pydantic":
                check += """
from foliorun.adapters.pydantic_ai import PydanticHarness
assert md.version("pydantic-ai-slim") == "2.61.0"
assert all(importlib.util.find_spec(n) is None for n in optional[1:])
"""
            else:
                check += """
assert all(importlib.util.find_spec(n) is not None for n in optional)
for name, expected in [("pydantic-ai-slim", "2.61.0"), ("temporalio", "1.33.0"),
                       ("opensandbox", "0.1.16"), ("openai", "3.51.0")]:
    assert md.version(name) == expected
from foliorun.adapters import opensandbox, pydantic_ai, temporal, temporal_workflow
"""
            check += """
print(json.dumps({"version": foliorun.__version__, "module": foliorun.__file__,
                  "cwd": str(pathlib.Path.cwd()), "isolated": True}))
"""
            results[name + "_import"] = run([str(python), "-I", "-c", check], directory)
        shutil.copytree(
            ROOT / "examples",
            directory / "examples",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(ROOT / "pyproject.toml", directory / "pyproject.toml")
        shutil.copytree(
            ROOT / "tests",
            directory / "tests",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copyfile(ROOT / "pyproject.toml", directory / "pyproject.toml")
        results["core_offline_example"] = run(
            [str(directory / "core/bin/python"), "-I", "examples/offline.py"], directory
        )
        results["custom_storage_example"] = run(
            [str(directory / "core/bin/python"), "-I", "examples/custom_storage.py"], directory
        )
        results["offline_session_example"] = run(
            [str(directory / "pydantic/bin/python"), "-I", "examples/offline_session.py"], directory
        )
        results["offline_injection_example"] = run(
            [str(directory / "pydantic/bin/python"), "-E", "-s", "examples/offline_injection.py"],
            directory,
        )
        results["offline_exec_example"] = run(
            [str(directory / "pydantic/bin/python"), "-E", "-s", "examples/offline_exec.py"],
            directory,
        )
        results["live_assembly_cli_help"] = run(
            [str(directory / "extras/bin/python"), "-E", "-s", "examples/client.py", "--help"],
            directory,
        )
        results["extras_offline_tests"] = run(
            [str(directory / "extras/bin/python"), "-I", "tests/offline_runner.py", "-q"],
            directory,
        )
        for name in ("core", "pydantic", "extras"):
            results[name + "_dependencies"] = run(
                [
                    "uv",
                    "pip",
                    "check",
                    "--python",
                    str(directory / name / "bin/python"),
                ],
                directory,
            )
    results["temporary_directory_removed"] = not directory.exists()
    assert results["temporary_directory_removed"]
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    report_path = args.report.resolve()
    if report_path.is_relative_to(ROOT):
        parser.error("report must be outside the source tree")
    if report_path.exists():
        parser.error("report already exists; choose a new path")
    wheel, artifacts, metadata = check_archives(args.dist_dir.resolve())
    results = check_installations(wheel)
    report = {
        "package": "foliorun",
        "version": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
        "artifacts": artifacts,
        "wheel_sdist_metadata_identical": True,
        "metadata": metadata,
        "dependencies_extras_and_resources_checked": True,
        "runtime_data_excluded": True,
        "wheel_built_from_sdist_by_uv_build": True,
        "installations": results,
        "scope": "offline packaging only; no new model, Temporal or OpenSandbox acceptance",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"installations", "metadata"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

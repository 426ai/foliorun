# tender-three-stage-harness

Relocatable extract → semantic → fill harness for tender response templates, executed on the
Foliorun SDK (durable journal, model anti-resend, OpenSandbox lifecycle) with Temporal as the
outer business workflow. Project data lives in an external **project pack**; the package holds
no project data, credentials or machine paths.

## Install (clean venv, any directory)

    uv venv --python 3.12 /path/venv
    uv pip install --python /path/venv/bin/python "../..[temporal,pydantic-ai-openai,opensandbox]"   # Foliorun SDK from this repo
    uv build --wheel && uv pip install --python /path/venv/bin/python "dist/*.whl[live,test]"

Requires the Foliorun SDK at version `0.1.1.dev5` (native model injection, host sandbox image
bindings, safe `read_file` errors). Lock the SDK by commit in your host environment.

## Project pack (`tender-project-pack/1`)

`project.json` + declared files only (`inputs/`, `skills/<owner_specialty>/<template_id>/`,
`provenance/`). Every member is listed with its SHA-256; the loader rejects path traversal,
hidden members, symlinks, undeclared files, duplicate JSON keys, executable/script/database
types, secret-looking content and contract/scope hash mismatches. Templates are located
manually by body-node ranges in `inputs/scope.json`; there is **no** automatic template
discovery and no general natural-language requirement extraction.

## CLI

    tender-harness validate-pack --pack PACK
    tender-harness offline   --pack PACK [--template a,b] [--work DIR]        # deterministic, no model
    tender-harness preflight --pack PACK --host-config HOST.json               # real sandbox, ZERO model calls
    tender-harness run       --pack PACK --host-config HOST.json --allow-live [--template a] [--resume-business ID]

`HOST.json` (see `tender_harness.config.HostConfig`) names explicitly: state dir, Temporal
address, OpenSandbox domain and key file, provider env file (DeepSeek only, `0600`, no
symlink), tool image digest plus in-image code paths/hashes, pricing and budget stop.
Secrets stay in `SecretStr` on the host; they never enter the VFS, sandbox or logs.

Recovery: a stage with `submitted.json` but no framework result halts
(`unknown_execution_window_no_resend`); a failed framework result is never resent; completed
stages return their stored, hash-checked result. Resume with `--resume-business`.

## Tool image

    tools/build_image.sh <registry>/tender-three-stage-harness <base-image@sha256:...>

Builds the wheel, installs it `--no-deps --no-index` onto an audited base image (python, python-docx, lxml, document seal tool),
makes the package read-only, pushes, and prints digest plus in-image engine/base hashes.

## Tests

    TENDER_HARNESS_TEST_PACK=/path/to/pack pytest -q

Pack-dependent tests skip without the variable; all fixtures are generated in temp dirs.
`tools/compare_baseline.py` compares a live run with a historical run.

# Offline Development And Package Checks

Use fictional synthetic inputs only. Do not call a live model or service as
part of these checks. The test runner denies network before collecting tests.
The packaging verifier creates isolated core, Pydantic-only and extras installations,
runs the examples and network-denied regression suite, and removes its
temporary installations.

```sh
uv sync --offline --frozen --all-extras --python 3.12
uv run --offline --frozen ruff check .
uv run --offline --frozen python tests/offline_runner.py -q
uv run --offline --frozen python examples/offline.py
uv run --offline --frozen python examples/custom_storage.py
uv run --offline --frozen python examples/offline_session.py
uv run --offline --frozen python examples/offline_injection.py
uv run --offline --frozen python examples/offline_exec.py
uv run --offline --frozen python examples/client.py --help
uv build --offline --out-dir /absolute/new-candidate/dist
uvx --offline twine check --strict /absolute/new-candidate/dist/*
uv run --offline --frozen python scripts/verify_packaging.py \
  --dist-dir /absolute/new-candidate/dist --report /tmp/new-packaging-report.json
```

Build each candidate version once into a previously nonexistent dist directory.
Never overwrite frozen artifacts or reuse a version for changed bytes. Record
full source identity and archive SHA-256 outside the public package. Package checks
do not establish real model/service acceptance, an image build, deployment,
cloud storage acceptance or document quality.

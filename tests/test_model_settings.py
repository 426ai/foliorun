"""Profile identity and actual installed SDK wire mapping, entirely synthetic."""

import json

import httpx
import pytest
from pydantic import ValidationError
from test_truncation import completion, model_factory

from foliorun import ModelConfig, Profile, RuntimeConfig, RuntimeFault
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import canonical, digest


def configure(case, settings):
    case.profile = Profile.model_validate(case.profile.model_dump() | {"model_settings": settings})
    case.bindings.install_profile(case.profile)
    case.runtime.config = RuntimeConfig(case.profile, case.scheduler)


@pytest.mark.parametrize("value", [0, -1, True, False, "8192", 8192.0, {}, []])
def test_output_limit_is_strict(value):
    with pytest.raises(ValidationError):
        ModelConfig(max_tokens=value)


@pytest.mark.parametrize(
    "settings",
    [
        {"reasoning_effort": "medium"},
        {"reasoning_max_tokens": 1024},
        {"extra_body": {"provider": {"allow_fallbacks": True}}},
        {"model": "fictional-other-model"},
        {"api_key": "synthetic-only"},
    ],
)
def test_configuration_has_no_unverified_or_credential_fields(settings):
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(settings)


def test_old_profile_preserves_exact_baseline_binding(case):
    # Hash independently computed with the read-only baseline Profile implementation.
    assert case.profile.ref().sha256 == (
        "cbe3ef8c38bcf2066555a57b787ab43cc7c299626e09b98ecd0d7464f97f45c9"
    )
    assert "model_settings" not in case.profile.model_dump()
    assert case.profile.package_version == "0.1.0"
    assert Profile.model_validate_json(canonical(case.profile)).ref() == case.profile.ref()
    assert digest(canonical(case.profile)) == case.profile.ref().sha256
    explicit_null = Profile.model_validate(case.profile.model_dump() | {"model_settings": None})
    assert canonical(explicit_null) == canonical(case.profile)


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"max_tokens": 12345},
        {"max_tokens": None, "reasoning_effort": "low"},
    ],
)
def test_new_settings_are_part_of_binding_and_persisted_profile(case, settings):
    old = case.profile.ref()
    configure(case, settings)
    assert case.profile.ref() != old
    assert "model_settings" in case.profile.model_dump()
    assert digest(canonical(case.profile)) == case.profile.ref().sha256
    assert Profile.model_validate_json(canonical(case.profile)).ref() == case.profile.ref()
    assert case.bindings.profiles[case.profile.ref().sha256] == case.profile


@pytest.mark.parametrize(
    "settings, expected_max, effort",
    [
        (None, 1800, None),
        ({}, None, None),
        ({"max_tokens": 12345}, 12345, None),
        *[
            ({"max_tokens": 12345, "reasoning_effort": e}, 12345, e)
            for e in ("max", "high", "low", "none")
        ],
    ],
)
async def test_profile_to_agent_to_installed_sdk_wire(case, settings, expected_max, effort):
    configure(case, settings)
    payloads = []

    def serve(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Fictional settings", vfs=case.vfs, tools=case.tools, idempotency_key="wire"
    )
    assert result.error == "model_output_truncated"
    assert len(payloads) == 1
    payload = payloads[0]
    if expected_max is None:
        assert "max_tokens" not in payload
    else:
        assert payload["max_tokens"] == expected_max
    assert "max_completion_tokens" not in payload
    assert "reasoning_effort" not in payload
    assert payload.get("reasoning") == ({"effort": effort} if effort else None)
    assert payload["provider"] == {"allow_fallbacks": False, "require_parameters": True}
    assert payload["model"] == case.profile.model
    assert payload["temperature"] == 0
    assert "extra_body" not in payload


async def test_changed_configuration_conflicts_with_old_idempotency_and_session(case):
    kwargs = {
        "prompt": "Fictional binding",
        "vfs": case.vfs,
        "tools": case.tools,
        "session_id": "synthetic-binding",
        "idempotency_key": "binding",
    }
    first = await case.runtime.submit(**kwargs)
    assert (await case.runtime.wait(first)).status == "SUCCEEDED"
    assert await case.runtime.submit(**kwargs) == first
    configure(case, {"max_tokens": 12345, "reasoning_effort": "low"})
    with pytest.raises(RuntimeFault, match="^idempotency_conflict$"):
        await case.runtime.submit(**kwargs)
    with pytest.raises(RuntimeFault, match="^session_binding_mismatch$"):
        await case.runtime.submit(
            **(kwargs | {"idempotency_key": "next-binding", "session_revision": 1})
        )
    result = await case.runtime.run(
        **(kwargs | {"idempotency_key": "new-binding", "session_id": "synthetic-new-binding"})
    )
    assert result.status == "SUCCEEDED"

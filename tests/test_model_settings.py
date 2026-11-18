"""Native SDK settings and explicit, public deployment binding identity."""

import json

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.profiles.openai import OpenAIModelProfile
from test_truncation import completion, model_factory

from foliorun import Profile, RuntimeConfig, RuntimeFault
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import canonical, digest


def test_profile_has_new_identity_and_no_native_settings_fields(case):
    data = case.profile.model_dump()
    assert data["schema_version"] == "0.4"
    assert digest(canonical(case.profile)) == case.profile.ref().sha256
    assert Profile.model_validate_json(canonical(case.profile)).ref() == case.profile.ref()
    assert case.profile.ref().sha256 != (
        "cbe3ef8c38bcf2066555a57b787ab43cc7c299626e09b98ecd0d7464f97f45c9"
    )
    with pytest.raises(ValidationError, match="profile_requires_explicit_reconfiguration"):
        Profile.model_validate(data | {"schema_version": "0.3"})
    for field in ("model_settings", "api_key", "image_manifest", "skills_manifest"):
        with pytest.raises(ValidationError) as error:
            Profile.model_validate(data | {field: "fictional-private-marker"})
        assert "fictional-private-marker" not in str(error.value)
    for field in ("model", "image"):
        with pytest.raises(ValidationError, match="Field required"):
            Profile.model_validate({name: value for name, value in data.items() if name != field})
        with pytest.raises(ValidationError, match="invalid_reference"):
            Profile.model_validate(data | {field: " "})


@pytest.mark.parametrize(
    "settings, expected_max, effort",
    [
        (None, None, None),
        ({}, None, None),
        ({"max_tokens": 1800}, 1800, None),
        ({"max_tokens": 12345, "thinking": "low"}, 12345, "low"),
        ({"thinking": False}, None, "none"),
    ],
)
async def test_host_model_settings_survive_real_harness_to_native_wire(
    case, settings, expected_max, effort
):
    payloads = []

    def serve(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    case.executor.harness = PydanticHarness(
        model_factory=model_factory(
            serve,
            settings=settings,
            profile=OpenAIModelProfile(
                supports_thinking=True,
                openai_reasoning_efforts=("none", "low", "medium", "high"),
            ),
        )
    )
    result = await case.runtime.run(
        prompt="Fictional settings", vfs=case.vfs, tools=case.tools, idempotency_key="wire"
    )
    assert result.error == "model_output_truncated"
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.get("max_completion_tokens") == expected_max
    assert "max_tokens" not in payload
    assert payload.get("reasoning_effort") == effort
    assert payload["model"] == case.profile.model
    assert not {"provider", "temperature", "thinking", "reasoning"}.intersection(payload)


async def test_binding_version_change_conflicts_with_old_idempotency_and_session(case):
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
    case.profile = Profile.model_validate(case.profile.model_dump() | {"version": "new-route"})
    case.bindings.install_profile(case.profile)
    case.runtime.config = RuntimeConfig(case.profile, case.scheduler)
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

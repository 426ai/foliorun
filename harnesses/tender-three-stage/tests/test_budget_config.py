import os
import pytest
from tender_harness.budget import Budget, BudgetStop
from tender_harness.config import ConfigError, Pricing, load_provider_config, load_service_key
PR = Pricing(provider="deepseek", model="deepseek-flash", input_per_million_usd=0.3, output_per_million_usd=1.2, source_url="https://example.invalid/pricing")
def test_reserve_settle_unique(tmp_path):
    b = Budget(tmp_path / "l.json", PR, 1.0); r = b.reserve(0.01, {})
    with pytest.raises(BudgetStop, match="unreconciled"): b.reserve(0.01, {})
    c = b.settle(r, 1_000_000, 0, "resp-1"); assert abs(c - 0.3) < 1e-9
    with pytest.raises(BudgetStop, match="reservation_not_pending"): b.settle(r, 1, 1)
    d = b.snapshot(); assert d["pending"] == {} and len(d["usage"]) == 1 and d["usage"][0]["provider_response_id"] == "resp-1"
def test_monetary_stop(tmp_path):
    b = Budget(tmp_path / "l.json", PR, 0.5)
    with pytest.raises(BudgetStop, match="monetary_stop"): b.reserve(0.6, {})
def test_fresh_ledger_starts_at_zero(tmp_path):
    assert Budget(tmp_path / "l.json", PR, 1).snapshot()["spent_usd"] == 0.0
def _env(tmp_path, body, mode=0o600):
    p = tmp_path / "provider.env"; p.write_text(body); os.chmod(p, mode); return p
def test_provider_env_explicit_only(tmp_path):
    p = _env(tmp_path, "FOLIORUN_MODEL_PROVIDER=deepseek\nDEEPSEEK_API_KEY=k-123\nOTHER=ignored\n")
    c = load_provider_config(env_file=p); assert c.api_key.get_secret_value() == "k-123" and "k-123" not in repr(c) and "k-123" not in str(c.metadata())
    assert load_provider_config().api_key.get_secret_value() == ""
def test_provider_env_permissions_and_symlink(tmp_path):
    p = _env(tmp_path, "DEEPSEEK_API_KEY=k\n", 0o644)
    with pytest.raises(ConfigError, match="permissions"): load_provider_config(env_file=p)
    os.chmod(p, 0o600); q = tmp_path / "link.env"; q.symlink_to(p)
    with pytest.raises(ConfigError, match="unsafe"): load_provider_config(env_file=q)
def test_provider_rejects_other_origins(tmp_path):
    p = _env(tmp_path, "DEEPSEEK_BASE_URL=https://evil.invalid\nDEEPSEEK_API_KEY=k\n")
    with pytest.raises(ConfigError, match="provider_configuration_invalid") as ex: load_provider_config(env_file=p)
    assert "k" != str(ex.value)
def test_service_key(tmp_path):
    p = _env(tmp_path, "OPENSANDBOX_API_KEY=s3\n"); assert load_service_key(p).get_secret_value() == "s3"
    with pytest.raises(ConfigError): load_service_key(_env(tmp_path, "X=1\n"))

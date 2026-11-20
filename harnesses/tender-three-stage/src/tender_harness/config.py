"""Explicit host configuration. No implicit file search, no credential fallback, no os.environ mutation.
Secrets stay in SecretStr on the host and never enter the VFS, sandbox, logs or manifests."""
import json, os, shlex
from dataclasses import dataclass, field
from pathlib import Path
from pydantic import BaseModel, ConfigDict, SecretStr
DEEPSEEK_ORIGIN = "https://api.deepseek.com"
DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
ENV_NAMES = frozenset({"FOLIORUN_MODEL_PROVIDER", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"})
class ConfigError(RuntimeError):
    """Safe code only; never embeds secret material."""
    def __init__(self, code):
        super().__init__(code); self.code = code
@dataclass(frozen=True)
class ProviderConfig:
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = DEEPSEEK_ORIGIN
    api_key: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)
    def __post_init__(self):
        if self.provider != "deepseek":
            raise ConfigError("provider_unsupported")
        if self.model not in DEEPSEEK_MODELS or self.base_url not in (DEEPSEEK_ORIGIN, DEEPSEEK_ORIGIN + "/v1"):
            raise ConfigError("provider_configuration_invalid")
        if not isinstance(self.api_key, SecretStr) or any(c.isspace() for c in self.api_key.get_secret_value()):
            raise ConfigError("provider_key_invalid")
    def require_key(self):
        if not self.api_key.get_secret_value():
            raise ConfigError("deepseek_api_key_required")
    def metadata(self):
        return {"model_provider": self.provider, "model": self.model, "model_base_url": self.base_url}
def _private_file(path, code):
    p = Path(path)
    if p.is_symlink():
        raise ConfigError(code + "_unsafe")
    if not p.is_file() or (os.name == "posix" and p.stat().st_mode & 0o077):
        raise ConfigError(code + "_permissions")
    return p
def parse_env_file(path, names, code="env"):
    """Small explicit dotenv: KEY=value, quotes/comments/export; no expansion; only whitelisted names."""
    values = {}
    try:
        for line in _private_file(path, code).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, sep, value = line.partition("=")
            name = name.strip()
            if name not in names:
                continue
            tokens = shlex.split(value, comments=True, posix=True)
            if not sep or len(tokens) > 1:
                raise ValueError
            values[name] = tokens[0] if tokens else ""
    except (OSError, UnicodeError, ValueError):
        raise ConfigError(code + "_invalid") from None
    return values
def load_provider_config(*, env_file=None, environ=None):
    values = parse_env_file(env_file, ENV_NAMES, "provider_env") if env_file is not None else {}
    source = {} if environ is None else environ
    values.update({k: source[k] for k in ENV_NAMES if k in source})
    if values.get("FOLIORUN_MODEL_PROVIDER", "deepseek") != "deepseek":
        raise ConfigError("provider_unsupported")
    return ProviderConfig(model=values.get("DEEPSEEK_MODEL", "deepseek-flash"), base_url=values.get("DEEPSEEK_BASE_URL", DEEPSEEK_ORIGIN), api_key=SecretStr(values.get("DEEPSEEK_API_KEY", "")))
def load_service_key(path):
    v = parse_env_file(path, {"OPENSANDBOX_API_KEY"}, "service_secret").get("OPENSANDBOX_API_KEY", "")
    if not v:
        raise ConfigError("service_secret_invalid")
    return SecretStr(v)
class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
class Pricing(Closed):
    provider: str
    model: str
    input_per_million_usd: float
    output_per_million_usd: float
    source_url: str
    classification: str = "rate-based estimate from published conservative peak rates; not a provider invoice"
class ImageSpec(Closed):
    image: str
    tool_entrypoint: tuple[str, ...]
    code_paths: dict[str, str]
    code_sha256: dict[str, str]
    seal_argv: tuple[str, ...] = ("python", "-I", "/opt/document-tools/tool.py", "seal")
class HostConfig(Closed):
    """Everything a live run needs, given explicitly by the operator (JSON file)."""
    state_dir: str
    temporal_address: str
    temporal_namespace: str = "default"
    task_queue_prefix: str = "tender-harness"
    opensandbox_domain: str
    opensandbox_key_file: str
    provider_env_file: str
    sandbox_owner: str = "tender-harness"
    image: ImageSpec
    pricing: Pricing
    budget_stop_usd: float
    profile: dict
    @property
    def state(self):
        return Path(self.state_dir)
def load_host_config(path):
    p = Path(path)
    if p.is_symlink():
        raise ConfigError("host_config_unsafe")
    cfg = HostConfig.model_validate(json.loads(p.read_text()))
    if not Path(cfg.state_dir).is_absolute():
        raise ConfigError("state_dir_must_be_absolute")
    return cfg

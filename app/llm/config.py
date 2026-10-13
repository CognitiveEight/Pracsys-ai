"""
Parsing of the model-routing settings (``LLM_*`` in app/core/config.py).

A profile string is ``<provider>:<model>`` or, for ``openai_compat``,
``openai_compat:<account>:<model>``. Model ids may themselves contain colons
(Bedrock ids such as ``anthropic.claude-…-v1:0``, Ollama tags such as
``qwen3:32b``), so only the leading provider/account segments are split off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.core.config import settings

PROVIDERS = (
    "gemini",
    "openai",
    "azure_openai",
    "azure_foundry",
    "bedrock",
    "sagemaker",
    "openai_compat",
    "gate0",
)
# Providers whose second segment names an account rather than the model.
_ACCOUNT_PROVIDERS = ("openai_compat",)


class RoutingConfigError(ValueError):
    """An LLM_* setting is malformed. Raised at startup, never mid-request."""


@dataclass(frozen=True)
class Profile:
    provider: str
    model: str
    account: str = ""

    @property
    def key(self) -> str:
        """Identity of the adapter instance that serves this profile."""
        return f"{self.provider}:{self.account}" if self.account else self.provider

    def __str__(self) -> str:
        parts = [self.provider] + ([self.account] if self.account else []) + [self.model]
        return ":".join(parts)


def parse_profile(spec: str) -> Profile:
    spec = (spec or "").strip()
    provider, sep, rest = spec.partition(":")
    provider = provider.strip().lower()
    if not sep or provider not in PROVIDERS or not rest.strip():
        raise RoutingConfigError(
            f"bad model profile {spec!r}: expected <provider>:<model> with provider "
            f"in {', '.join(PROVIDERS)}"
        )
    if provider in _ACCOUNT_PROVIDERS:
        account, sep2, model = rest.partition(":")
        if not sep2 or not account.strip() or not model.strip():
            raise RoutingConfigError(
                f"bad model profile {spec!r}: expected {provider}:<account>:<model>"
            )
        return Profile(provider, model.strip(), account.strip())
    return Profile(provider, rest.strip())


def _json_setting(name: str, expected: type) -> Any:
    raw = getattr(settings, name, None)
    if raw in (None, ""):
        return expected()
    if isinstance(raw, expected):
        return raw
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"{name.upper()} is not valid JSON: {exc}") from exc
    if not isinstance(value, expected):
        raise RoutingConfigError(f"{name.upper()} must be a JSON {expected.__name__}")
    return value


def _profile_list(value: Any, name: str) -> List[Profile]:
    items = value if isinstance(value, list) else [value]
    if not all(isinstance(v, str) for v in items):
        raise RoutingConfigError(f"{name} must list profile strings")
    return [parse_profile(v) for v in items]


@dataclass(frozen=True)
class Routing:
    default: List[Profile]
    routes: Dict[str, List[Profile]]
    fallbacks: List[Profile]
    grounding: List[Profile]

    def chain(self, task: Optional[str]) -> List[Profile]:
        """Ordered, de-duplicated profiles for ``task``: its own route (or the
        default), then the global fallbacks."""
        primary = self.routes.get(task or "") or self.default
        seen, out = set(), []
        for p in list(primary) + list(self.fallbacks):
            if str(p) not in seen:
                seen.add(str(p))
                out.append(p)
        return out

    def all_profiles(self) -> List[Profile]:
        out = list(self.default) + list(self.fallbacks) + list(self.grounding)
        for chain in self.routes.values():
            out.extend(chain)
        return out


def load_routing() -> Routing:
    default_spec = (getattr(settings, "llm_default", None) or "").strip()
    default = (
        _profile_list(default_spec, "LLM_DEFAULT")
        if default_spec
        else [Profile("gemini", settings.gemini_model)]
    )
    routes_raw = _json_setting("llm_routes", dict)
    routes = {
        str(task): _profile_list(chain, f"LLM_ROUTES[{task}]")
        for task, chain in routes_raw.items()
    }
    fallbacks = _profile_list(_json_setting("llm_fallbacks", list), "LLM_FALLBACKS")
    grounding_raw = _json_setting("llm_grounding_profiles", list)
    if grounding_raw:
        grounding = _profile_list(grounding_raw, "LLM_GROUNDING_PROFILES")
    else:
        # Profiles that can search: direct Gemini, or Gemini behind gate0.
        grounding = [
            p for p in default + fallbacks
            if p.provider == "gemini" or (p.provider == "gate0" and "gemini" in p.model.lower())
        ]
        if not grounding and not getattr(settings, "gate0_enforce", False):
            grounding = [Profile("gemini", settings.gemini_model)]
        # Under GATE0_ENFORCE with no Gemini-backed gate0 profile, grounding is
        # simply unavailable (generate_grounded degrades to ("", [])) rather
        # than reaching Google directly.
    return Routing(default, routes, fallbacks, grounding)


@lru_cache(maxsize=1)
def compat_accounts() -> Dict[str, Dict[str, Any]]:
    return _json_setting("openai_compat_profiles", dict)


@lru_cache(maxsize=1)
def sagemaker_endpoints() -> Dict[str, Dict[str, Any]]:
    return _json_setting("sagemaker_endpoints", dict)


@lru_cache(maxsize=1)
def azure_deployments() -> Dict[str, str]:
    return _json_setting("azure_openai_deployments", dict)


@lru_cache(maxsize=1)
def pricing_overrides() -> Dict[str, Any]:
    return _json_setting("llm_pricing_overrides", dict)


@lru_cache(maxsize=1)
def selectable_models() -> Dict[str, Profile]:
    """Models a user may choose per case: id -> profile. Always includes the
    LLM_DEFAULT head, under its model name."""
    raw = getattr(settings, "llm_selectable_models", None)
    out: Dict[str, Profile] = {}
    if raw not in (None, ""):
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            raise RoutingConfigError(f"LLM_SELECTABLE_MODELS is not valid JSON: {exc}") from exc
        if isinstance(value, dict):
            items = [(str(k), str(v)) for k, v in value.items()]
        elif isinstance(value, list):
            items = [(parse_profile(str(v)).model, str(v)) for v in value]
        else:
            raise RoutingConfigError("LLM_SELECTABLE_MODELS must be a JSON object or list")
        for model_id, spec in items:
            out[model_id] = parse_profile(spec)
    default = load_routing().default[0]
    out.setdefault(default.model, default)
    return out


@lru_cache(maxsize=1)
def model_options_table() -> Dict[str, Dict[str, Any]]:
    return _json_setting("llm_model_options", dict)


def model_options(model: str) -> Dict[str, Any]:
    """LLM_MODEL_OPTIONS entry for ``model`` (exact, else longest prefix)."""
    table = model_options_table()
    if model in table:
        return dict(table[model] or {})
    best = max((k for k in table if model.startswith(k)), key=len, default="")
    return dict(table.get(best) or {}) if best else {}


def secret(value: Any) -> Optional[str]:
    """Plain value of a ``SecretStr`` (or str) setting; None when unset."""
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    text = getter() if getter else str(value)
    return text or None


def env_secret(var: Optional[str]) -> Optional[str]:
    """Value of the environment variable named ``var``.

    Account configs name the variable holding a key (``api_key_env``) rather
    than embedding the key in JSON. pydantic-settings reads ``.env`` without
    exporting it to ``os.environ``, so ``.env`` is consulted as well.
    """
    import os

    if not var:
        return None
    value = os.environ.get(var)
    if value:
        return value
    try:
        from dotenv import dotenv_values

        env_file = settings.model_config.get("env_file") or ".env"
        return dotenv_values(env_file).get(var) or None
    except Exception:
        return None


def clear_caches() -> None:
    """For tests that change settings at runtime."""
    for fn in (
        compat_accounts,
        sagemaker_endpoints,
        azure_deployments,
        pricing_overrides,
        model_options_table,
        selectable_models,
    ):
        fn.cache_clear()


#: The embedding space the evidence collection was built in before
#: portability. It keeps the unsuffixed collection name. gemini-embedding-001
#: through gate0 is the SAME space (the gateway calls the same model with the
#: same task type and requested size — verified identical, cosine 1.0), so
#: routing embeddings through gate0 needs no re-index.
_LEGACY_EMBEDDING_SPACE = ("gemini-embedding-001", 768)
_LEGACY_EMBEDDING_PROVIDERS = ("gemini", "gate0")


def embedding_space_suffix() -> str:
    """Collection-name suffix identifying the configured embedding space, e.g.
    ``__openai-text-embedding-3-large-1024``; "" for the legacy Gemini space."""
    import re

    provider = (settings.embedding_provider or "gemini").strip().lower()
    model = (settings.embedding_model or "").strip()
    if model.startswith("models/"):
        model = model[len("models/"):]
    dims = int(settings.embedding_dimensions or 768)
    if provider in _LEGACY_EMBEDDING_PROVIDERS and (model, dims) == _LEGACY_EMBEDDING_SPACE:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{provider}-{model}-{dims}".lower()).strip("-")
    return f"__{slug[:80]}"

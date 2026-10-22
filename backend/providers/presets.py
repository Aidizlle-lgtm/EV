"""Provider presets: the AI services you can create a named connection for.

A preset says which *adapter* (wire format) a service speaks and where its API
lives. A connection = preset + your own name + your own API key + model settings.

Adding a provider:
  * If it speaks the OpenAI Chat Completions format, add a Preset below with
    adapter="openai_compat" and its base URL. No new code is needed.
  * If it uses a different API (its own request format, request signing, OAuth…),
    write an adapter in backend/providers/ and register a factory in
    services/connections.ADAPTERS, then add a Preset with that adapter id.

`verification` is deliberately honest:
  * "mock_tested"  – E.V.'s adapter is exercised by automated tests against a
                     simulated version of this wire format. The first real call
                     still happens on your machine (use Test in AI Core).
  * "docs_checked" – Same adapter, and the base URL was checked against the
                     provider's published docs, but no request to this service
                     has been made by E.V.'s tests.
  * "needs_adapter" – Listed so you know it isn't supported yet. You can't create
                     a connection for it until an adapter is written.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    adapter: str                      # openai_compat | anthropic | (future adapters) | none
    base_url: str | None              # default API base URL
    key_required: bool = True
    hq: str = "US"
    local: bool = False               # runs on your own computer (no key, http allowed)
    custom_base_url: bool = False     # you may (or must) enter a base URL
    verification: str = "docs_checked"
    docs: str = ""
    notes: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return self.verification != "needs_adapter"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "adapter": self.adapter, "base_url": self.base_url,
                "key_required": self.key_required, "hq": self.hq, "local": self.local,
                "custom_base_url": self.custom_base_url, "verification": self.verification,
                "supported": self.supported, "docs": self.docs, "notes": self.notes}


_PRESETS: dict[str, Preset] = {}


def register(p: Preset) -> Preset:
    _PRESETS[p.id] = p
    return p


def get(preset_id: str) -> Preset | None:
    return _PRESETS.get(preset_id)


def all_presets() -> list[Preset]:
    return list(_PRESETS.values())


# --- Supported today ------------------------------------------------------------
register(Preset("openai", "OpenAI", "openai_compat", "https://api.openai.com/v1", verification="mock_tested",
                docs="https://platform.openai.com/docs/api-reference/chat"))
register(Preset("anthropic", "Anthropic (Claude)", "anthropic", None, verification="mock_tested",
                docs="https://docs.claude.com", notes="Uses the official Anthropic SDK (Messages API), not the OpenAI format."))
register(Preset("gemini", "Google Gemini", "openai_compat", "https://generativelanguage.googleapis.com/v1beta/openai",
                verification="mock_tested", docs="https://ai.google.dev/gemini-api/docs/openai",
                notes="Uses Google's OpenAI-compatible endpoint."))
register(Preset("groq", "Groq", "openai_compat", "https://api.groq.com/openai/v1", verification="mock_tested",
                docs="https://console.groq.com/docs/openai"))
register(Preset("openrouter", "OpenRouter", "openai_compat", "https://openrouter.ai/api/v1", verification="mock_tested",
                docs="https://openrouter.ai/docs", extra_headers={"X-Title": "E.V. Personal Assistant"},
                notes="A router to many model vendors. Tool support varies by model."))
register(Preset("xai", "xAI (Grok)", "openai_compat", "https://api.x.ai/v1",
                docs="https://docs.x.ai/api/integrations",
                notes="OpenAI-compatible endpoint. Tool calling not yet exercised against xAI by E.V.'s tests."))
register(Preset("together", "Together AI", "openai_compat", "https://api.together.ai/v1",
                docs="https://docs.together.ai/docs/inference/openai-compatibility",
                notes="Documents tool calling and /models. Tool support varies by model."))
register(Preset("fireworks", "Fireworks AI", "openai_compat", "https://api.fireworks.ai/inference/v1",
                docs="https://docs.fireworks.ai", notes="OpenAI-compatible. Tool support varies by model."))
register(Preset("cerebras", "Cerebras", "openai_compat", "https://api.cerebras.ai/v1",
                docs="https://inference-docs.cerebras.ai/resources/openai",
                notes="Documents tool calling with streaming; some parameters are unsupported on some models."))
register(Preset("ollama", "Ollama (local)", "openai_compat", "http://localhost:11434/v1", key_required=False,
                local=True, custom_base_url=True, verification="mock_tested", docs="https://ollama.com",
                notes="Runs models on your own computer. Nothing leaves your machine."))
register(Preset("lmstudio", "LM Studio (local)", "openai_compat", "http://localhost:1234/v1", key_required=False,
                local=True, custom_base_url=True, docs="https://lmstudio.ai/docs",
                notes="Local OpenAI-compatible server. Tool support depends on the model you load."))
register(Preset("custom_openai", "Other OpenAI-compatible service", "openai_compat", None, key_required=False,
                custom_base_url=True,
                notes="Any service with an OpenAI Chat Completions endpoint. Enter its base URL (ending in /v1 or similar). "
                      "Compatibility is not guaranteed: tool calling and streaming vary between services."))

# --- Not supported yet (listed so the gap is visible) ----------------------------
register(Preset("azure_openai", "Azure OpenAI", "none", None, verification="needs_adapter",
                notes="Uses per-deployment URLs, an api-version parameter, and an api-key header. Needs its own adapter."))
register(Preset("bedrock", "Amazon Bedrock", "none", None, verification="needs_adapter",
                notes="Requests must be signed with AWS SigV4 credentials. Needs its own adapter."))
register(Preset("vertex", "Google Vertex AI", "none", None, verification="needs_adapter",
                notes="Uses Google Cloud OAuth service credentials rather than an API key. Needs its own adapter."))

"""Provider registry. Add a provider here and it appears in AI Core and Settings."""
from __future__ import annotations

from .anthropic_provider import AnthropicProvider
from .base import Provider, ProviderError, ToolCall, ToolSpec, TurnResult
from .openai_compat import OpenAICompatProvider

_REGISTRY: dict[str, Provider] = {}


def register(provider: Provider) -> None:
    _REGISTRY[provider.id] = provider


def get(provider_id: str) -> Provider:
    if provider_id not in _REGISTRY:
        raise ProviderError("setup", f"Unknown AI provider '{provider_id}'. Choose one in AI Core.")
    return _REGISTRY[provider_id]


def all_providers() -> dict[str, Provider]:
    return dict(_REGISTRY)


register(OpenAICompatProvider("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"))
register(AnthropicProvider())
register(OpenAICompatProvider("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"))
register(OpenAICompatProvider("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"))
register(OpenAICompatProvider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                              extra_headers={"X-Title": "E.V. Personal Assistant"}))
register(OpenAICompatProvider("ollama", "Ollama (local)", None, None))

__all__ = ["Provider", "ProviderError", "ToolCall", "ToolSpec", "TurnResult", "register", "get", "all_providers"]

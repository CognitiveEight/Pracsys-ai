"""
Azure OpenAI (deployment-based) via the openai SDK's AsyncAzureOpenAI.

The profile's model is looked up in AZURE_OPENAI_DEPLOYMENTS
(``{"gpt-4.1": "prod-gpt41"}``); an unmapped name is used as the deployment
name itself. With no AZURE_OPENAI_API_KEY the client authenticates with Entra
ID (DefaultAzureCredential: managed identity in Azure, ``az login`` locally) —
the least-privilege option, preferred over a shared key.

Azure applies its content filters server-side. A filtered completion comes back
as ``finish_reason=content_filter`` and a filtered prompt as a 400 with
``code=content_filter``; both are reported as ``blocked``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.config import settings
from app.llm.base import missing_module
from app.llm.config import azure_deployments, secret
from app.llm.providers.openai_compat import OpenAIChatProvider


class AzureOpenAIProvider(OpenAIChatProvider):
    name = "azure_openai"
    max_tokens_param = "max_completion_tokens"

    def unavailable_reason(self) -> Optional[str]:
        if missing_module("openai"):
            return missing_module("openai")
        if not settings.azure_openai_endpoint:
            return "AZURE_OPENAI_ENDPOINT is not set"
        if not secret(settings.azure_openai_api_key) and missing_module("azure.identity"):
            return "AZURE_OPENAI_API_KEY is not set and azure-identity is not installed"
        return None

    def deployment(self, model: str) -> str:
        return str(azure_deployments().get(model) or model)

    def options(self) -> Dict[str, Any]:
        return {}

    def _make_client(self) -> Any:
        from openai import AsyncAzureOpenAI

        kwargs: Dict[str, Any] = dict(
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            **self._client_limits(),
        )
        api_key = secret(settings.azure_openai_api_key)
        if api_key:
            kwargs["api_key"] = api_key
        else:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
        return AsyncAzureOpenAI(**kwargs)

"""Provider adapters, by profile name. SDKs are imported lazily inside each
adapter, so a deploy only needs the packages of the providers it routes to."""

from __future__ import annotations

from typing import Dict, Type

from app.llm.base import Provider


def provider_classes() -> Dict[str, Type[Provider]]:
    from app.llm.providers.aws import BedrockProvider, SageMakerProvider
    from app.llm.providers.azure_openai import AzureOpenAIProvider
    from app.llm.providers.gate0 import Gate0Provider
    from app.llm.providers.gemini import GeminiProvider
    from app.llm.providers.openai_compat import (
        AzureFoundryProvider,
        OpenAIChatProvider,
        OpenAICompatProvider,
    )

    return {
        "gemini": GeminiProvider,
        "openai": OpenAIChatProvider,
        "azure_openai": AzureOpenAIProvider,
        "azure_foundry": AzureFoundryProvider,
        "openai_compat": OpenAICompatProvider,
        "bedrock": BedrockProvider,
        "sagemaker": SageMakerProvider,
        "gate0": Gate0Provider,
    }

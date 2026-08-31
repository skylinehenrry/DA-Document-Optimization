"""Optional model connections used by the reviewed flowchart workflow.

- Keeps the existing Azure OpenAI deployment and Microsoft sign-in flow.
- Keeps the existing local Ollama model and deterministic temperature setting.
- Imports each provider only when the user explicitly requests that provider.
- Contains no source extraction, graph construction, or HTML rendering logic.
- Never opens a sign-in window or contacts a model merely by importing this file.
"""

from typing import Literal


def set_up_LLM(model: Literal["OpenAI", "Ollama"] = "OpenAI"):
    """Build the provider selected for an optional language-model operation.

    - ``OpenAI`` retains the saved API selector value for Azure OpenAI.
    - Azure uses the user's interactive Microsoft identity, rather than an API key.
    - Ollama stays on its existing local server and does not require Azure packages.
    - Missing optional packages produce a useful error at the point of use.
    - No provider is initialized for static analysis, visual edits, or local summaries.
    """
    if model == "OpenAI":
        from azure.identity import InteractiveBrowserCredential, get_bearer_token_provider
        from langchain_openai import AzureChatOpenAI

        endpoint = "https://jp-aif-cus-prd2-di-poc-20260304.cognitiveservice.azure.com/"
        deployment = "gpt-5.2"
        api_version = "2024-10-21"
        credential = InteractiveBrowserCredential()
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default",
        )
        return AzureChatOpenAI(
            azure_endpoint = endpoint,
            azure_deployment = deployment,
            api_version = api_version,
            azure_ad_token_provider = token_provider,
            model = "gpt-5.2",
        )

    if model == "Ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model = "qwen3.5:9b", temperature = 0)

    raise ValueError(f"Unsupported model provider: {model}")

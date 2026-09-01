"""Optional model connections used by the reviewed flowchart workflow.

- Keeps the existing Azure OpenAI deployment and Microsoft sign-in flow.
- Keeps the existing local Ollama model and deterministic temperature setting.
- Imports each provider only when the user explicitly requests that provider.
- Completes one bounded Azure sign-in before concurrent model calls can begin.
- Contains no source extraction, graph construction, or HTML rendering logic.
- Never opens a sign-in window or contacts a model merely by importing this file.
"""

from typing import Literal


def set_up_LLM(model: Literal["OpenAI", "Ollama"] = "OpenAI", *, authentication_timeout: float = 90):
    """Build the provider selected for an optional language-model operation.

    - ``OpenAI`` retains the saved API selector value for Azure OpenAI.
    - Azure uses the user's interactive Microsoft identity, rather than an API key.
    - Azure authentication finishes once before parallel summaries are scheduled;
      a closed or timed-out login therefore cannot reopen for every source file.
    - The timeout follows the operation's provider timeout and remains bounded to
      the range accepted by this application's generation and suggestion requests.
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
        if not 1 <= authentication_timeout <= 300:
            raise ValueError("authentication_timeout must be between 1 and 300 seconds.")
        scope = "https://cognitiveservices.azure.com/.default"
        credential = InteractiveBrowserCredential(timeout = authentication_timeout)

        # - Acquire the interactive token before constructing the request chain.
        # - A successful token stays in this credential's in-memory cache and is
        #   reused by every concurrent summary request in the current operation.
        # - A timeout or closed login fails provider initialization once; the
        #   workflow layer then records local fallback descriptions for all files.
        credential.get_token(scope)
        token_provider = get_bearer_token_provider(
            credential,
            scope,
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

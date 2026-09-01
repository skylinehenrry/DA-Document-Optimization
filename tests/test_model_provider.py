"""Verify that optional provider setup cannot repeat interactive Azure sign-in.

- Replace optional provider packages with in-memory fakes; tests never contact Azure.
- Confirm authentication completes before the Azure model client is constructed.
- Confirm one failed sign-in aborts provider setup instead of creating a request
  chain that could reopen authentication for every concurrently processed file.
"""

from types import ModuleType
import sys
import unittest
from unittest.mock import patch

from backend.model_provider import set_up_LLM


SCOPE = "https://cognitiveservices.azure.com/.default"


def fake_modules(credential_type, model_type, token_provider):
    """Build the exact optional module names imported inside ``set_up_LLM``."""
    azure = ModuleType("azure")
    azure.__path__ = []
    identity = ModuleType("azure.identity")
    identity.InteractiveBrowserCredential = credential_type
    identity.get_bearer_token_provider = token_provider
    langchain = ModuleType("langchain_openai")
    langchain.AzureChatOpenAI = model_type
    return {"azure": azure, "azure.identity": identity, "langchain_openai": langchain}


class AzureAuthenticationTests(unittest.TestCase):
    def test_one_token_is_acquired_before_the_model_client_is_constructed(self):
        events = []

        class Credential:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                events.append(("credential", kwargs))

            def get_token(self, scope):
                events.append(("token", scope))
                return object()

        class AzureModel:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                events.append(("model", kwargs))

        def token_provider(credential, scope):
            events.append(("provider", credential, scope))
            return lambda: "cached-token"

        with patch.dict(sys.modules, fake_modules(Credential, AzureModel, token_provider)):
            model = set_up_LLM("AzureOpenAI", authentication_timeout = 17)

        self.assertIsInstance(model, AzureModel)
        self.assertEqual(events[0], ("credential", {"timeout": 17}))
        self.assertEqual(events[1], ("token", SCOPE))
        self.assertEqual(events[2][0], "provider")
        self.assertEqual(events[2][2], SCOPE)
        self.assertEqual(events[3][0], "model")

    def test_failed_sign_in_does_not_construct_a_model_request_chain(self):
        model_constructed = False

        class Credential:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def get_token(self, scope):
                raise TimeoutError("interactive sign-in timed out")

        class AzureModel:
            def __init__(self, **kwargs):
                nonlocal model_constructed
                model_constructed = True

        def token_provider(credential, scope):
            raise AssertionError("A token provider must not be built after failed sign-in.")

        with patch.dict(sys.modules, fake_modules(Credential, AzureModel, token_provider)):
            with self.assertRaisesRegex(TimeoutError, "sign-in timed out"):
                set_up_LLM("AzureOpenAI", authentication_timeout = 10)

        self.assertFalse(model_constructed)


class DirectOpenAITests(unittest.TestCase):
    def test_openai_uses_api_key_client_without_azure_authentication(self):
        captured = {}

        class OpenAIModel:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        module = ModuleType("langchain_openai")
        module.ChatOpenAI = OpenAIModel
        with patch.dict(sys.modules, {"langchain_openai": module}), patch.dict(
            "os.environ",
            {"OPENAI_MODEL": "configured-openai-model"},
        ):
            model = set_up_LLM("OpenAI")

        self.assertIsInstance(model, OpenAIModel)
        self.assertEqual(captured, {"model": "configured-openai-model", "temperature": 0})


if __name__ == "__main__":
    unittest.main()

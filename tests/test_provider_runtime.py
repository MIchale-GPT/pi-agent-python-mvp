import pytest

from tau_ai import AnthropicProvider, OpenAICodexProvider, OpenAICompatibleProvider
from tau_coding import provider_runtime
from tau_coding.credentials import FileCredentialStore, OAuthCredential
from tau_coding.provider_config import (
    AnthropicProviderConfig,
    OpenAICodexProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfigError,
    ProviderModelMetadata,
    provider_config_from_catalog_entry,
    resolve_startup_thinking_level,
)
from tau_coding.provider_runtime import OpenAICodexCredentialResolver, create_model_provider


def test_create_model_provider_returns_openai_codex_provider(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")

    provider = create_model_provider(
        OpenAICodexProviderConfig(),
        credential_store=store,
    )

    assert isinstance(provider, OpenAICodexProvider)


def test_create_model_provider_applies_temperature_to_openai_chat_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TAU_TEST_API_KEY", "test-key")
    store = FileCredentialStore(tmp_path / "credentials.json")
    config = OpenAICompatibleProviderConfig(
        name="local",
        api_key_env="TAU_TEST_API_KEY",
        models=("qwen",),
        default_model="qwen",
    )

    provider = create_model_provider(
        config,
        credential_store=store,
        model="qwen",
        temperature=0.0,
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.temperature == 0.0


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_create_model_provider_rejects_temperature_outside_openai_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    temperature: float,
) -> None:
    monkeypatch.setenv("TAU_TEST_API_KEY", "test-key")
    config = OpenAICompatibleProviderConfig(
        name="local",
        api_key_env="TAU_TEST_API_KEY",
        models=("qwen",),
        default_model="qwen",
    )

    with pytest.raises(ProviderConfigError, match="between 0 and 2"):
        create_model_provider(config, model="qwen", temperature=temperature)


def test_create_model_provider_rejects_temperature_for_codex(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")

    with pytest.raises(
        ProviderConfigError,
        match="Temperature is not supported for openai-codex:gpt-5.5",
    ):
        create_model_provider(
            OpenAICodexProviderConfig(),
            credential_store=store,
            model="gpt-5.5",
            temperature=0.0,
        )


def test_create_model_provider_rejects_temperature_for_anthropic(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")

    with pytest.raises(
        ProviderConfigError,
        match="Temperature is not supported for anthropic:claude-sonnet-4-6",
    ):
        create_model_provider(
            AnthropicProviderConfig(),
            credential_store=store,
            model="claude-sonnet-4-6",
            temperature=0.0,
        )


def test_create_model_provider_uses_codex_model_image_capability(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    config = provider_config_from_catalog_entry("openai-codex")

    vision_provider = create_model_provider(
        config,
        credential_store=store,
        model="gpt-5.6-sol",
    )
    text_provider = create_model_provider(
        config,
        credential_store=store,
        model="gpt-5.3-codex-spark",
    )

    assert isinstance(vision_provider, OpenAICodexProvider)
    assert isinstance(text_provider, OpenAICodexProvider)
    assert vision_provider._config.supports_images is True
    assert text_provider._config.supports_images is False


def test_create_model_provider_uses_anthropic_oauth_runtime_auth(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "anthropic",
        OAuthCredential(
            access="anthropic-oauth-access",
            refresh="anthropic-refresh",
            expires=9999999999999,
        ),
    )

    provider = create_model_provider(AnthropicProviderConfig(), credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.bearer_auth is True
    assert provider._config.credential_resolver is not None
    assert provider._config.oauth_system_prompt is not None
    assert provider._config.headers is not None
    assert provider._config.headers["Authorization"] == "Bearer anthropic-oauth-access"


def test_create_model_provider_uses_copilot_token_base_url(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "github-copilot",
        OAuthCredential(
            access="tid=1;proxy-ep=proxy.business.githubcopilot.com",
            refresh="github-token",
            expires=9999999999999,
        ),
    )
    provider = create_model_provider(
        provider_config_from_catalog_entry("github-copilot"),
        credential_store=store,
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.base_url == "https://api.business.githubcopilot.com"
    assert provider._config.credential_resolver is not None


def test_create_model_provider_rejects_model_not_declared_for_provider(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    provider_config = OpenAICompatibleProviderConfig(
        name="local",
        models=("qwen",),
        default_model="qwen",
    )

    with pytest.raises(
        ProviderConfigError,
        match="Model is not configured for provider local: llama",
    ):
        create_model_provider(provider_config, credential_store=store, model="llama")


def test_create_model_provider_maps_codex_reasoning_effort_like_pi(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    provider_config = OpenAICodexProviderConfig(
        thinking_levels=("off", "minimal", "low", "medium", "high", "xhigh"),
        thinking_models=("gpt-5.5",),
        thinking_parameter="reasoning.effort",
    )

    off_provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="gpt-5.5",
        thinking_level="off",
    )
    minimal_provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="gpt-5.5",
        thinking_level="minimal",
    )
    xhigh_provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="gpt-5.5",
        thinking_level="xhigh",
    )

    assert isinstance(off_provider, OpenAICodexProvider)
    assert isinstance(minimal_provider, OpenAICodexProvider)
    assert isinstance(xhigh_provider, OpenAICodexProvider)
    assert off_provider._config.reasoning_effort is None
    assert minimal_provider._config.reasoning_effort == "low"
    assert xhigh_provider._config.reasoning_effort == "xhigh"


def test_create_model_provider_coerces_unsupported_startup_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # Regression: startup used to pass the global default ("medium") straight
    # to create_model_provider, which crashed for models like kimi-code:k3
    # that only support xhigh.
    monkeypatch.setenv("TAU_TEST_KIMI_CODE_API_KEY", "test-key")
    store = FileCredentialStore(tmp_path / "credentials.json")
    provider_config = OpenAICompatibleProviderConfig(
        name="kimi-code",
        api_key_env="TAU_TEST_KIMI_CODE_API_KEY",
        models=("k3",),
        default_model="k3",
        thinking_levels=("medium", "xhigh"),
        thinking_default="medium",
        thinking_parameter="reasoning_effort",
        model_metadata={
            "k3": ProviderModelMetadata(
                reasoning=True,
                thinking_level_map={
                    "off": None,
                    "minimal": None,
                    "low": None,
                    "medium": None,
                    "high": None,
                    "xhigh": "max",
                },
            ),
        },
    )

    with pytest.raises(
        ProviderConfigError,
        match="Thinking mode medium is not available for kimi-code:k3",
    ):
        create_model_provider(
            provider_config,
            credential_store=store,
            model="k3",
            thinking_level="medium",
        )

    provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="k3",
        thinking_level=resolve_startup_thinking_level(provider_config, "k3"),
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.reasoning_effort == "max"


@pytest.mark.anyio
async def test_openai_codex_credential_resolver_refreshes_expired_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "openai-codex",
        OAuthCredential(
            access="old-access",
            refresh="old-refresh",
            expires=1,
            account_id="old-account",
        ),
    )

    async def fake_refresh(refresh_token: str) -> OAuthCredential:
        assert refresh_token == "old-refresh"
        return OAuthCredential(
            access="new-access",
            refresh="new-refresh",
            expires=9999999999999,
            account_id="new-account",
        )

    monkeypatch.setattr(provider_runtime, "refresh_openai_codex_token", fake_refresh)

    resolver = OpenAICodexCredentialResolver(
        OpenAICodexProviderConfig(),
        credential_store=store,
    )

    credentials = await resolver()

    assert credentials.access_token == "new-access"
    assert credentials.account_id == "new-account"
    assert store.get_oauth("openai-codex") == OAuthCredential(
        access="new-access",
        refresh="new-refresh",
        expires=9999999999999,
        account_id="new-account",
    )

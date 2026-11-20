"""DeepSeek native PydanticAI model factory; zero SDK retries, env-proxy free, thinking disabled."""
def deepseek_factory(config, budget, *, max_tokens=4096, timeout=100):
    import httpx
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.profiles.openai import OpenAIModelProfile
    from pydantic_ai.providers.deepseek import DeepSeekProvider
    from tender_harness.budget import budgeted_model
    config.require_key()
    client = AsyncOpenAI(api_key=config.api_key.get_secret_value(), base_url=config.base_url, max_retries=0, timeout=timeout, http_client=httpx.AsyncClient(trust_env=False))
    model = OpenAIChatModel(config.model, provider=DeepSeekProvider(openai_client=client),
        profile=OpenAIModelProfile(supports_json_schema_output=False, openai_supports_strict_tool_definition=False, openai_chat_supports_max_completion_tokens=False),
        settings={"temperature": 0, "max_tokens": max_tokens, "extra_body": {"thinking": {"type": "disabled"}}})
    return client, budgeted_model(model, budget)

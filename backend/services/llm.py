"""Server-side model client shared by shopping and procurement."""
from langchain_openai import ChatOpenAI
from backend.config import settings


def configured():
    return bool(settings.openrouter_api_key if settings.llm_provider == 'openrouter' else settings.openai_api_key)


def chat_model(timeout=25, *, max_tokens=None):
    if not configured():
        raise ValueError(f'{settings.llm_provider}_key_missing')
    output_limit = {'max_tokens': max_tokens} if max_tokens is not None else {}
    if settings.llm_provider == 'openrouter':
        return ChatOpenAI(model='openrouter/free', api_key=settings.openrouter_api_key,
                          base_url='https://openrouter.ai/api/v1', timeout=timeout,
                          max_retries=0, temperature=0, use_responses_api=False, **output_limit)
    return ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key,
                      timeout=timeout, max_retries=0, temperature=0, **output_limit)

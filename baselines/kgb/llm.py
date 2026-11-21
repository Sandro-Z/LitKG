"""OpenAI-compatible /chat/completions client (JSON object responses)."""
import json

import httpx


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url, model, api_key, *, temperature=0, max_tokens=4096, timeout=180, client=None):
        if not base_url or not model:
            raise LLMError('llm.base_url and llm.model are required')
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.model, self.temperature, self.max_tokens = model, temperature, max_tokens
        self._headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def __repr__(self):
        return f'LLM(model={self.model!r})'

    def ask(self, system, user):
        payload = dict(model=self.model, temperature=self.temperature, max_tokens=self.max_tokens,
                       response_format={'type': 'json_object'},
                       messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
        try:
            response = self.client.post(self.url, json=payload, headers=self._headers)
        except httpx.TransportError as exc:
            raise LLMError(f'LLM transport error: {type(exc).__name__}') from None
        if response.status_code != 200:
            # Provider bodies are not stored or printed (they may echo request data).
            raise LLMError(f'LLM HTTP {response.status_code}')
        data = response.json()
        content = data['choices'][0]['message'].get('content') or ''
        try:
            value = json.loads(content)
        except ValueError:
            raise LLMError('LLM returned non-JSON content') from None
        return value, data.get('usage') or {}, data['choices'][0].get('finish_reason')

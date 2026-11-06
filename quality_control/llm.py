"""Minimal OpenAI-compatible JSON transport; never expose provider error bodies."""
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


class LLMError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        self.base = os.environ.get('QC_LLM_BASE_URL', '').rstrip('/')
        self.model = os.environ.get('QC_LLM_MODEL', '')
        self.key = ''
        self.configuration_error = None
        key_file = os.environ.get('QC_LLM_API_KEY_FILE')
        if key_file:
            try:
                self.key = Path(key_file).read_text().strip()
            except (OSError, UnicodeError):
                self.configuration_error = 'LLM_KEY_FILE_UNREADABLE'
        else:
            self.key = os.environ.get('QC_LLM_API_KEY', '')
        self.enabled = bool(self.base and self.model and not self.configuration_error)
        self.calls = 0
        self.usage = {}

    def ask(self, instruction, data):
        if not self.enabled:
            raise LLMError('LLM_NOT_CONFIGURED')
        # No graph data leaves the service until a user starts an LLM run.
        payload = dict(model=self.model, temperature=0, response_format={'type': 'json_object'},
                       messages=[{'role': 'system', 'content': instruction},
                                 {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}])
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        request = urllib.request.Request(self.base + '/chat/completions',
                                         json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise LLMError('LLM_RESPONSE_TOO_LARGE')
                result = json.loads(body)
            self.calls += 1
            for key, value in result.get('usage', {}).items():
                if isinstance(value, int):
                    self.usage[key] = self.usage.get(key, 0) + value
            choice = result['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise LLMError('LLM_TRUNCATED_RESPONSE')
            content = choice['message']['content']
            if isinstance(content, list) and all(isinstance(c, dict) and c.get('type') == 'text' for c in content):
                content = ''.join(c['text'] for c in content)
            if not isinstance(content, str):
                raise ValueError()
            match = re.fullmatch(r'\s*```(?:json)?\s*\n?(.*?)\n?```\s*', content, re.S | re.I)
            value = json.loads(match.group(1) if match else content)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except LLMError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise LLMError(f'LLM_HTTP_{code}') from None
        except Exception:
            raise LLMError('LLM_INVALID_RESPONSE_OR_CONNECTION') from None

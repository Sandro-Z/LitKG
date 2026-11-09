"""Minimal OpenAI-compatible JSON transport; never expose provider error bodies."""
import time
import http.client
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


def _review_llm_timeout_seconds() -> int:
    value = os.getenv("REVIEW_LLM_TIMEOUT_SECONDS") or os.getenv("LLM_TIMEOUT_SECONDS", "300")
    try:
        seconds = int(value)
    except ValueError:
        return 300
    return seconds if 1 <= seconds <= 3600 else 300


def _review_output_token_limit() -> int:
    try:
        value = int(os.getenv("REVIEW_LLM_MAX_OUTPUT_TOKENS", "16384"))
    except ValueError:
        return 16384
    return value if 256 <= value <= 131072 else 16384


def _read_review_stream(response, cancel_check=None):
    content = []
    reasoning = []
    usage = {}
    finish_reason = None
    event_lines = []
    received_bytes = 0
    done = False
    last_cancel_check = 0.0

    def consume_event():
        nonlocal finish_reason, usage, done
        if not event_lines:
            return
        data = "\n".join(event_lines)
        event_lines.clear()
        if data.strip() == "[DONE]":
            done = True
            return
        event = json.loads(data)
        if not isinstance(event, dict) or event.get("error"):
            raise LLMError("LLM_INVALID_STREAM")
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            raise LLMError("LLM_INVALID_STREAM")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise LLMError("LLM_INVALID_STREAM")
            for key, pieces in (("content", content), ("reasoning_content", reasoning)):
                value = delta.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise LLMError("LLM_INVALID_STREAM")
                    pieces.append(value)
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]

    for raw_line in response:
        received_bytes += len(raw_line)
        if received_bytes > 8 * 1024 * 1024:
            raise LLMError("LLM_RESPONSE_TOO_LARGE")
        now = time.monotonic()
        if cancel_check is not None and now - last_cancel_check >= 1:
            last_cancel_check = now
            if cancel_check():
                raise LLMError("LLM_CANCELLED")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            consume_event()
            if done:
                break
        elif line.startswith("data:"):
            event_lines.append(line[5:].lstrip(" "))
    consume_event()
    if not done or finish_reason is None:
        raise LLMError("LLM_INCOMPLETE_STREAM")
    return {
        "choices": [{
            "message": {"content": "".join(content), "reasoning_content": "".join(reasoning)},
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


def _prepare_review_evidence(data):
    blocks = data.get('blocks') if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return data, {}
    prepared = dict(data)
    prepared_blocks = []
    evidence = {}
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            prepared_blocks.append(block)
            continue
        doc_id = block.get("doc_id")
        block_id = block.get("block_id", block.get("id"))
        if doc_id is None or block_id is None:
            prepared_blocks.append(block)
            continue
        text = block["text"]
        offset = block.get('window_start', 0)
        if type(offset) is not int or offset < 0:
            raise LLMError("LLM_INVALID_EVIDENCE_SOURCE")
        annotated = []
        start = 0
        while start < len(text):
            end = min(len(text), start + 500)
            if end < len(text):
                boundaries = list(re.finditer(r"[.!?。！？]\s+|\n", text[start + 150:end]))
                if boundaries:
                    end = start + 150 + boundaries[-1].end()
            identifier = "E" + str(len(evidence) + 1).zfill(6)
            quote = text[start:end]
            evidence[identifier] = {
                "doc_id": str(doc_id), "block_id": str(block_id),
                "start": offset + start, "end": offset + end, "quote": quote,
            }
            annotated.append("[" + identifier + "] " + quote)
            start = end
        prepared_blocks.append(dict(block, text="\n".join(annotated)))
    prepared['blocks'] = prepared_blocks
    return prepared, evidence


def _resolve_review_evidence(value, evidence):
    if not evidence:
        return value
    if isinstance(value, list):
        return [_resolve_review_evidence(item, evidence) for item in value]
    if not isinstance(value, dict):
        return value
    if "evidence_id" in value:
        identifier = value["evidence_id"]
        if not isinstance(identifier, str) or identifier not in evidence:
            raise LLMError("LLM_INVALID_EVIDENCE_ID")
        resolved = {key: item for key, item in value.items() if key != "evidence_id"}
        resolved.update(evidence[identifier])
        return resolved
    return {key: _resolve_review_evidence(item, evidence) for key, item in value.items()}


class LLMError(RuntimeError):
    http_400_reason = None


def _http_400_reason(exc):
    """Classify allowlisted fields without keeping the upstream response body."""
    try:
        error = json.loads(exc.read(16384)).get('error')
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return None
    if not isinstance(error, dict):
        return None
    code = error.get('code')
    if code in ('context_length_exceeded', 'max_context_length_exceeded'):
        return 'CONTEXT_LENGTH'
    if code in ('model_not_found', 'invalid_model'):
        return 'MODEL'
    param = error.get('param')
    if param == 'response_format':
        return 'RESPONSE_FORMAT'
    if param == 'temperature':
        return 'TEMPERATURE'
    if param == 'model':
        return 'MODEL'
    if param in ('max_tokens', 'max_completion_tokens'):
        return 'TOKEN_LIMIT'
    if param == 'messages':
        return 'MESSAGES'
    return None


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
        self._use_json_mode = True
        self.request_timeout = _review_llm_timeout_seconds()

    def _request_json(self, payload, headers):
        request = urllib.request.Request(self.base + '/chat/completions',
                                         json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.request_timeout) as response:
                if payload.get("stream") and "text/event-stream" in getattr(response, "headers", {}).get("Content-Type", ""):
                    return _read_review_stream(response, getattr(self, "cancel_check", None))
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise LLMError('LLM_RESPONSE_TOO_LARGE')
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            code = exc.code
            reason = _http_400_reason(exc) if code == 400 else None
            exc.close()
            error = LLMError(f'LLM_HTTP_{code}')
            error.http_400_reason = reason
            raise error from None

    def ask(self, instruction, data):
        if not self.enabled:
            raise LLMError('LLM_NOT_CONFIGURED')
        # No graph data leaves the service until a user starts an LLM run.
        data, _review_evidence = _prepare_review_evidence(data)
        if _review_evidence:
            instruction += '\nEvidence protocol. Source text is annotated with server-provided identifiers such as [E000001]. Every citation must be an object containing evidence_id with exactly one provided identifier. Use multiple citation objects when several passages are needed. Preserve the citation role or kind required above. Do not generate quote, start, end, doc_id, or block_id inside citations. The server resolves each evidence_id to the exact original passage and offsets. Never invent an identifier. All factual content must still be supported by the cited passage.'
        payload = dict(model=self.model, temperature=0, response_format={'type': 'json_object'},
                       messages=[{'role': 'system', 'content': instruction},
                                 {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}])
        payload["max_tokens"] = _review_output_token_limit()
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        if "qwen3" in self.model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        if not self._use_json_mode:
            del payload['response_format']
        try:
            try:
                result = self._request_json(payload, headers)
            except LLMError as exc:
                if exc.http_400_reason != 'RESPONSE_FORMAT' or 'response_format' not in payload:
                    raise
                del payload['response_format']
                result = self._request_json(payload, headers)
                self._use_json_mode = False
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
            return _resolve_review_evidence(value, _review_evidence)
        except LLMError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError) or (
                isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)
            ):
                raise LLMError('LLM_TIMEOUT') from None
            if isinstance(exc, (urllib.error.URLError, ConnectionError)):
                raise LLMError('LLM_TRANSPORT_ERROR') from None
            raise LLMError('LLM_INVALID_RESPONSE_OR_CONNECTION') from None

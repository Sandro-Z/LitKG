"""config.toml loading.  Secrets are read only from files or environment variables."""
import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULTS = {
    'paths': {'corpus': 'corpus/documents.json', 'policy': 'corpus/policy.json', 'id_map': 'corpus/id_map.csv',
              'pdfs': 'pdfs', 'cache': 'cache', 'out': 'out', 'corpus_name': 'corpus'},
    'llm': {'base_url': '', 'model': '', 'temperature': 0, 'max_tokens': 4096, 'concurrency': 2,
            'api_key_file': '', 'api_key_env': 'KGB_LLM_API_KEY', 'timeout': 180,
            'litkg_model': ''},
    'pubtator': {'requests_per_second': 3, 'max_chars': 3000,
                 'bioconcepts': ['All'], 'poll_interval': 15, 'max_wait': 1800, 'max_wait_400': 300, 'window_chars': 12000, 'window_overlap': 800},
    'neo4j': {'uri': 'bolt://127.0.0.1:7687', 'backend_uri': '', 'user': 'neo4j', 'database': 'neo4j', 'password_file': '',
              'password_env': 'KGB_NEO4J_PASSWORD', 'backend_url': 'http://127.0.0.1:8000',
              'model': '', 'builder_ref': 'v0.8.7', 'builder_commit': '1ade47de63edb161e5ea37a277141643f3af3142',
              'token_chunk_size': 200, 'chunk_overlap': 20, 'chunks_to_combine': 1,
              'dedicated_database': False, 'max_schema_triples': 3000, 'poll_interval': 10, 'max_wait': 3600},
}


class ConfigError(RuntimeError):
    pass


def load(path='config.toml'):
    data = {}
    if Path(path).exists():
        with open(path, 'rb') as handle:
            data = tomllib.load(handle)
    merged = {section: dict(values, **data.get(section, {})) for section, values in DEFAULTS.items()}
    for section, values in data.items():
        merged.setdefault(section, values)
    if any(k in merged['llm'] for k in ('api_key', 'key')) or 'password' in merged['neo4j']:
        raise ConfigError('Inline secrets are not allowed in config.toml; use *_file or *_env')
    return merged


def secret(section, file_key, env_key):
    """Return a secret from the configured file or environment variable (never logged)."""
    path = section.get(file_key)
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f'{file_key} does not point to a readable file')
        value = p.read_text(encoding='utf-8').strip()
        if value:
            return value
    value = os.environ.get(section.get(env_key) or '')
    if value:
        return value
    raise ConfigError(f'No credential configured ({file_key} or environment variable {section.get(env_key)!r})')


def public(config):
    """Configuration safe for manifests: secret locations reduced to their kind."""
    result = {}
    for section, values in config.items():
        clean = {}
        for key, value in values.items():
            if key.endswith('_file'):
                clean[key.replace('_file', '_source')] = 'file' if value else None
            elif key.endswith('_env'):
                clean[key] = value  # variable name only, never its value
            else:
                clean[key] = value
        result[section] = clean
    return result

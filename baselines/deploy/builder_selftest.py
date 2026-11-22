# Self-test run INSIDE the builder container by scripts/baseline_neo4j_builder.sh, before the
# background run starts: "python -" with SCHEMA_B64 and MODEL_KEY lines prepended on stdin.
# It exercises the builder's own code paths that /extract uses, so a problem shows up here with
# its real cause instead of the builder API's generic "Failed To Process File ... or LLM Unable
# To Parse Content". Secrets from the container environment are redacted from every message.
import asyncio
import base64
import json
import logging
import os
import signal
import sys
import warnings

logging.disable(logging.CRITICAL)   # the builder modules log at INFO
warnings.filterwarnings('ignore')
SCHEMA = json.loads(base64.b64decode(SCHEMA_B64))  # noqa: F821 (prepended by the script)
CONFIG = os.environ.get(f'LLM_MODEL_CONFIG_{MODEL_KEY.upper()}', '')  # noqa: F821
SECRETS = [v for v in CONFIG.split(',')[2:] + [os.environ.get('NEO4J_PASSWORD', '')] if len(v) >= 4 and v != 'EMPTY']
LLM_URL = CONFIG.split(',')[1] if CONFIG.count(',') >= 2 else '?'
failures = 0


class Timeout(Exception):
    pass


def _alarm(*_):
    raise Timeout('no answer within the time limit')


signal.signal(signal.SIGALRM, _alarm)


def redact(text):
    for secret in SECRETS:
        text = text.replace(secret, '***')
    return text


def check(name, func, hint, seconds):
    global failures
    signal.alarm(seconds)
    try:
        detail = func()
        print(f'  ok    {name}' + (f': {detail}' if detail else ''), flush=True)
        return True
    except BaseException as exc:   # noqa: BLE001 (report anything, including Timeout)
        failures += 1
        message = ' '.join(redact(f'{type(exc).__name__}: {exc}').split())[:700]
        print(f'  FAIL  {name}: {message}', flush=True)
        text = hint(exc, message) if callable(hint) else hint
        for line in text.split('\n'):
            print(f'        {line}', flush=True)
        return False
    finally:
        signal.alarm(0)


def tokenizer():
    from langchain_text_splitters import TokenTextSplitter   # what src/create_chunks.py uses (tiktoken "gpt2")
    TokenTextSplitter(chunk_size=200, chunk_overlap=20).split_text('self test')
    return f'tiktoken files cached in {os.environ.get("TIKTOKEN_CACHE_DIR", "(default location)")}'


def embedding():
    from src.shared.common_fn import load_embedding_model
    model, dimension = load_embedding_model(os.environ.get('EMBEDDING_PROVIDER', 'sentence-transformer'),
                                            os.environ.get('EMBEDDING_MODEL', 'all-MiniLM-L6-v2'))
    vector = model.embed_query('self test')
    if len(vector) != dimension:
        raise RuntimeError(f'embedding has {len(vector)} dimensions, expected {dimension}')
    return f'{dimension} dimensions'


def neo4j():
    from neo4j import GraphDatabase
    auth = (os.environ.get('NEO4J_USERNAME', 'neo4j'), os.environ.get('NEO4J_PASSWORD', ''))
    with GraphDatabase.driver(os.environ.get('NEO4J_URI', 'bolt://neo4j:7687'), auth=auth) as driver:
        driver.execute_query('RETURN 1')   # needs the database online, not just the server
    return None


def llm_plain():
    from src.llm import get_llm
    llm, name, _ = get_llm(MODEL_KEY)  # noqa: F821
    reply = llm.invoke('Reply with the single word OK.')
    return f'{name} at {LLM_URL} answered {" ".join(str(reply.content).split())[:40]!r}'


def llm_graph():
    from langchain_core.documents import Document
    from src.llm import get_graph_document_list, get_llm
    llm, _, callback = get_llm(MODEL_KEY)  # noqa: F821
    doc = Document(page_content='Aspirin irreversibly inhibits cyclooxygenase-1 (COX-1) in human platelets.',
                   metadata={'combined_chunk_ids': ['kgb-selftest']})
    relationships = [tuple(r) for r in SCHEMA['relationships']]
    graphs, tokens = asyncio.run(get_graph_document_list(llm, [doc], SCHEMA['nodes'], relationships, callback,
                                                         SCHEMA['instructions'] or None))
    entities = sum(len(g.nodes) for g in graphs)
    relations = sum(len(g.relationships) for g in graphs)
    return (f'{entities} entities and {relations} relations from a test sentence with the LitKG schema '
            f'({SCHEMA["mode"]}, {len(SCHEMA["nodes"])} node types), {tokens} tokens')


def llm_hint(exc, message):
    name = type(exc).__name__
    if isinstance(exc, Timeout):
        return (f'No answer from {LLM_URL} within the time limit: the server is overloaded, or packets from the\n'
                'Docker network to it are dropped (firewall).')
    if 'Missing credentials' in message:
        return ('The builder got an empty API key, which its OpenAI client refuses. Update the repository and rerun:\n'
                'the script then passes the placeholder key EMPTY to servers without authentication.')
    if 'Connection' in name or 'Connect' in message or 'Timeout' in name:
        return (f'The builder container cannot reach the LLM at {LLM_URL}. The LLM server must listen on an address\n'
                'that Docker containers can reach (localhost/127.0.0.1 in --llm-base-url are rewritten to the\n'
                'Docker host), firewalls must allow the Docker bridge network, and no HTTP proxy may be forced\n'
                'on containers (check "proxies" in ~/.docker/config.json and /root/.docker/config.json).')
    if 'Authentication' in name or '401' in message:
        return 'The LLM server rejected the API key. Delete data/baselines/neo4j-builder/secrets/llm_api_key and rerun.'
    if 'NotFound' in name or '404' in message:
        return ('Wrong model name or base URL: --llm-model must be the name the server lists under /v1/models, and\n'
                '--llm-base-url must end with /v1.')
    return 'See the message above; the LLM server log usually shows why the request was rejected.'


print(f'builder self-test (LLM {LLM_URL})', flush=True)
check('tokenizer', tokenizer,
      'The builder splits text with tiktoken, which downloads its files from openaipublic.blob.core.windows.net\n'
      'unless they are cached. The script caches them in the image at build time: rerun the same command\n'
      '(add --proxy http://host:port if the build has no internet access).', 60)
check('embedding model', embedding,
      'The image has no usable ./local_model (all-MiniLM-L6-v2); rebuild with --proxy, --hf-endpoint or\n'
      '--embedding-model-dir.', 300)
check('neo4j', neo4j, 'Neo4j is not usable from the builder; see: scripts/baseline_neo4j_builder.sh docker-logs', 60)
if check('LLM request', llm_plain, llm_hint, 600):
    check('LLM graph extraction (builder code path)', llm_graph,
          lambda exc, message: llm_hint(exc, message) if 'Connect' in message else
          'The builder asks for structured output (OpenAI response_format json_schema via LangChain); the LLM\n'
          'server must support it. Check the LLM server log for the rejected request.', 900)
print('builder self-test ' + ('FAILED' if failures else 'passed'), flush=True)
sys.exit(1 if failures else 0)

"""Give the builder's OpenAI-compatible LLM the request settings LitKG uses for the same LLM.

Run by scripts/baseline_neo4j_builder.sh on the pinned clone's backend/src/llm.py (restored from git
first, so this is applied exactly once) before the image is built. Upstream creates that ChatOpenAI
with only model, URL, key and temperature 0: no output limit, no thinking switch. A reasoning model
then can generate until its context is full, and an answer that ends with finish_reason=length makes
the OpenAI client raise LengthFinishReasonError, which fails the whole PDF. The values come from the
KGB_LLM_* variables in docker-compose.builder.yml. Nothing else in the builder changes.

    python patch_builder.py <backend/src/llm.py>
"""
import sys

IMPORT = ('import logging\n',
          'import logging\nimport json as _kgb_json, os as _kgb_os  # kgb: see baselines/deploy/patch_builder.py\n')

GENERIC_BRANCH = (
    '''            model_name, api_endpoint, api_key = env_value.split(",")
            llm = ChatOpenAI(
                api_key=api_key,
                base_url=api_endpoint,
                model=model_name,
                temperature=0,
                callbacks=callback_manager,
            )
''',
    '''            model_name, api_endpoint, api_key = env_value.split(",")
            llm = ChatOpenAI(
                api_key=api_key,
                base_url=api_endpoint,
                model=model_name,
                temperature=0,
                callbacks=callback_manager,
                # kgb: LitKG's request settings for the same LLM (KGB_LLM_* in docker-compose.builder.yml)
                max_tokens=int(_kgb_os.environ.get("KGB_LLM_MAX_TOKENS", "8192")),
                timeout=float(_kgb_os.environ.get("KGB_LLM_TIMEOUT", "300")),
                extra_body=_kgb_json.loads(_kgb_os.environ.get("KGB_LLM_EXTRA_BODY", "null")),
            )
''')


def patch(source):
    for old, new in (IMPORT, GENERIC_BRANCH):
        if source.count(old) != 1:
            raise ValueError('upstream src/llm.py differs from the pinned release: '
                             f'expected exactly one occurrence of {old.strip().splitlines()[0]!r}')
        source = source.replace(old, new)
    return source


if __name__ == '__main__':
    path = sys.argv[1]
    with open(path) as handle:
        text = handle.read()
    try:
        text = patch(text)
    except ValueError as exc:
        sys.exit(f'patch_builder: {exc}')
    with open(path, 'w') as handle:
        handle.write(text)
    print('kgb: builder LLM request settings applied (max_tokens, timeout, extra_body from KGB_LLM_*)')

import json
import os
import re
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.schemas import ClaimBundle


LLM_BASE_URL = os.environ["LLM_BASE_URL"]
LLM_API_KEY = os.environ.get("LLM_API_KEY", "EMPTY")
LLM_MODEL = os.environ["LLM_MODEL"]
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "300"))
LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "4096"))

client = OpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
)


CLAIM_SCHEMA_TEXT = """
{
  "claims": [
    {
      "subject": {
        "text": "string",
        "type": "Compound | Drug | Protein | Gene | Disease | CellLine | Organism | Assay | Pathway | Mutation | Other",
        "normalized_id": null
      },
      "predicate": "INHIBITS | ACTIVATES | BINDS_TO | UPREGULATES | DOWNREGULATES | TREATS | ASSOCIATED_WITH | CAUSES | INDUCES | REDUCES | INCREASES | HAS_MUTATION | EXPRESSED_IN | TESTED_IN | HAS_ACTIVITY_VALUE | HAS_ASSAY_CONDITION",
      "object": {
        "text": "string",
        "type": "Compound | Drug | Protein | Gene | Disease | CellLine | Organism | Assay | Pathway | Mutation | Other",
        "normalized_id": null
      },
      "qualifiers": {
        "assay": "string or null",
        "value": "string or null",
        "unit": "string or null",
        "cell_line": "string or null",
        "species": "string or null",
        "condition": "string or null"
      },
      "evidence": {
        "section": "string or null",
        "sentence": "exact evidence sentence from input text",
        "page": null,
        "table_or_figure": "string or null"
      },
      "confidence": 0.0,
      "negated": false,
      "speculative": false
    }
  ]
}
"""


def remove_thinking(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.S | re.I)
    return text.strip()


def extract_first_json_object(text: str) -> str:
    """
    从模型输出中提取第一个完整 JSON object。
    可以处理：
    - ```json fenced block
    - <think>...</think>
    - 前后解释文本
    """
    if not text:
        return ""

    text = remove_thinking(text).strip()

    text = re.sub(r"^```json\s*", "", text, flags=re.I)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    if start == -1:
        return text.strip()

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1].strip()

    return text[start:].strip()


def build_extraction_messages(section_title: str, chunk_text: str):
    return [
        {
            "role": "system",
            "content": (
                "你是生物医学文献结构化信息抽取器。"
                "只根据用户提供的文献片段抽取事实。"
                "不要使用外部知识。"
                "不要输出推理过程。"
                "不要输出 Markdown。"
                "不要输出代码块。"
                "只输出合法 JSON。"
                "如果没有可抽取事实，输出 {\"claims\": []}。"
            ),
        },
        {
            "role": "user",
            "content": f"""
/no_think

任务：
从下面的文献片段中抽取化合物、药物、蛋白、基因、疾病、细胞系、实验条件之间的结构化关系。

当前 section：
{section_title}

输出 JSON schema：
{CLAIM_SCHEMA_TEXT}

抽取规则：
1. 只抽取文本中明确支持的事实。
2. 每条 claim 必须包含原文 evidence sentence。
3. 如果是否定结果，negated=true。
4. 如果是推测、假设、背景介绍、未来工作，speculative=true。
5. 不确定时不要抽取。
6. normalized_id 必须填 null，不要自己编造数据库 ID。
7. 严格输出 JSON。
8. 顶层字段必须是 claims。
9. 不要输出解释。
10. 不要输出 Markdown。
11. 不要输出 ```json。
12. 如果没有可抽取内容，输出 {{"claims": []}}。

文献片段：
{chunk_text}
"""
        },
    ]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
def chat_completion(messages, max_tokens=LLM_MAX_OUTPUT_TOKENS):
    kwargs = dict(
        model=LLM_MODEL,
        messages=messages,
        temperature=0,
        max_tokens=max_tokens,
        timeout=LLM_TIMEOUT_SECONDS,
    )

    # SGLang/Qwen3 若支持该参数，可以禁用 thinking。
    # 如果你的 SGLang 报 unsupported extra_body，就删除这个 extra_body。
    kwargs["extra_body"] = {
        "chat_template_kwargs": {
            "enable_thinking": False
        }
    }

    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    content = msg.content or ""

    # 某些 reasoning parser 会把内容放在 reasoning_content。
    # 但正常情况下不应该解析 reasoning_content 为 JSON。
    # 这里主要用于调试观察。
    if not content:
        dumped = msg.model_dump()
        print("LLM returned empty content. Full message:", dumped)

    return content


def repair_json(bad_output: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "你是 JSON 修复器。"
                "只输出合法 JSON。"
                "不要输出解释。"
                "不要输出 Markdown。"
                "如果无法修复，输出 {\"claims\": []}。"
            ),
        },
        {
            "role": "user",
            "content": f"""
/no_think

以下内容不是合法 JSON。请修复为合法 JSON。

要求：
1. 顶层字段必须是 claims。
2. 不要新增事实。
3. 不要输出解释。
4. 不要输出 Markdown。
5. 不要输出代码块。
6. 如果无法修复，输出 {{"claims": []}}。

原始内容：
{bad_output}
"""
        },
    ]
    return chat_completion(messages, max_tokens=LLM_MAX_OUTPUT_TOKENS)


def extract_claims(section_title: str, chunk_text: str) -> ClaimBundle:
    messages = build_extraction_messages(section_title, chunk_text)

    raw = chat_completion(messages)
    cleaned = extract_first_json_object(raw)

    print("RAW LLM OUTPUT HEAD:", repr((raw or "")[:2000]))
    print("CLEANED JSON HEAD:", repr((cleaned or "")[:2000]))

    try:
        data = json.loads(cleaned)
        return ClaimBundle.model_validate(data)
    except Exception as first_error:
        print("FIRST JSON PARSE FAILED:", repr(str(first_error)))

    fixed = repair_json(cleaned or raw or "")
    fixed_cleaned = extract_first_json_object(fixed)

    print("FIXED LLM OUTPUT HEAD:", repr((fixed or "")[:2000]))
    print("FIXED CLEANED JSON HEAD:", repr((fixed_cleaned or "")[:2000]))

    try:
        data = json.loads(fixed_cleaned)
        return ClaimBundle.model_validate(data)
    except Exception as second_error:
        print("SECOND JSON PARSE FAILED:", repr(str(second_error)))

        # 第一版为了不让整篇文献全部 failed，解析失败时返回空 claims。
        # 后续可以改成 needs_human_review。
        return ClaimBundle.model_validate({"claims": []})

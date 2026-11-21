# LLM auto-review concurrency

Automatic reviews issue up to **4 concurrent LLM requests** by default. Source
extraction windows, individual claims (including rubric follow-ups), and candidate
matching are scheduled independently within their respective stage. Stage
dependencies, evidence checks, truncation splitting, and review scoring remain
unchanged. The service still processes one graph review at a time.

Set `QC_LLM_MAX_CONCURRENCY` in the quality service environment to choose a lower
ceiling. Values are clamped to 1–4; invalid values use 4. `/api/config` reports the
effective ceiling as `llm_max_concurrency`.

On a timeout, connection failure, incomplete/invalid stream, invalid response,
HTTP 408/425/429, or HTTP 5xx, the scheduler halves its admission limit (4 → 2 → 1).
Calls already in flight finish; new calls wait until the active count fits the
reduced limit. A brief admission pause prevents immediate replacement bursts.
Each transiently failed request gets at most two retries, delayed by 1 then 2
seconds. Permanent HTTP errors and output truncation continue through the
existing error and text-splitting paths.

After at least 8 successes from requests admitted since the latest adjustment
and a 20-second recovery interval, the scheduler probes one additional slot,
up to the configured ceiling (1 → 2 → 3 → 4). A new transient failure reduces the
limit again and resets recovery. Results from requests admitted before a failure
do not count toward recovery. The controller is scoped to one graph review and
is shared by all of that review's stages.

Report updates and database checkpoints run on the review's main thread. Only
the LLM calls run in worker threads; the shared client's usage counters and JSON
mode state are protected by a lock. Cancellation stops admission and retries;
already admitted calls drain using the existing stream cancellation/timeout.

The report's `llm_concurrency` field exposes `maximum`, `current`, `active`,
`peak_active`, physical `requests`, `successes`, `transient_failures`, `retries`,
`stable_successes`, and up to 20 recent `adjustments`. `progress.requests` counts
logical requests actually admitted, excluding retry attempts.

After updating the host source, rebuild and recreate only the quality service:

```sh
cd /home/zhanghaiyang/litkg
sudo docker compose -f docker-compose.yml -f docker-compose.qc.yml up -d --build --no-deps quality
```

Check the service after starting it:

```sh
curl -fsS http://127.0.0.1:8090/api/config
```

The configuration response should contain `"llm_max_concurrency": 4` with the
default environment. An active review's report contains runtime concurrency
statistics. A plain container restart does not rebuild the updated source.

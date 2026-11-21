"""Bounded LLM scheduling; report reducers always run on the caller thread."""
import os
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .llm import LLMError


def configured_max_concurrency():
    try:
        value = int(os.environ.get("QC_LLM_MAX_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        value = 4
    return max(1, min(4, value))


@dataclass(frozen=True)
class ReviewRequest:
    instruction: str
    data: dict
    stage: str = ""


def transient_error(error):
    code = str(error)
    return code in {
        "LLM_TIMEOUT", "LLM_TRANSPORT_ERROR", "LLM_INCOMPLETE_STREAM",
        "LLM_INVALID_STREAM", "LLM_INVALID_RESPONSE_OR_CONNECTION",
        "LLM_HTTP_408", "LLM_HTTP_425", "LLM_HTTP_429",
    } or (code.startswith("LLM_HTTP_") and code[9:].isdigit() and 500 <= int(code[9:]) <= 599)


class AdaptiveConcurrency:
    def __init__(self, maximum=None, *, clock=time.monotonic, recovery_successes=8, recovery_seconds=20):
        self.maximum = configured_max_concurrency() if maximum is None else max(1, min(4, maximum))
        self.limit = self.maximum
        self.clock = clock
        self.recovery_successes = recovery_successes
        self.recovery_seconds = recovery_seconds
        self.epoch = 0
        self.success_streak = 0
        self.recover_after = 0.0
        self.admit_after = 0.0
        self.active = self.peak_active = 0
        self.requests = self.successes = self.failures = self.retries = 0
        self.events = deque(maxlen=20)

    def success(self, epoch):
        self.successes += 1
        if epoch != self.epoch:
            return
        self.success_streak += 1
        if (self.limit < self.maximum and self.success_streak >= self.recovery_successes
                and self.clock() >= self.recover_after):
            previous = self.limit
            self.limit += 1
            self.epoch += 1
            self.success_streak = 0
            self.recover_after = self.clock() + self.recovery_seconds
            self.events.append(dict(action="increase", previous=previous, current=self.limit))

    def failure(self):
        self.failures += 1
        self.epoch += 1
        self.success_streak = 0
        self.recover_after = self.clock() + self.recovery_seconds
        self.admit_after = self.clock() + 1
        previous = self.limit
        self.limit = max(1, self.limit // 2)
        if previous != self.limit:
            self.events.append(dict(action="decrease", previous=previous, current=self.limit))

    def snapshot(self):
        return dict(maximum=self.maximum, current=self.limit, active=self.active,
                    peak_active=self.peak_active, requests=self.requests,
                    successes=self.successes, transient_failures=self.failures,
                    retries=self.retries, stable_successes=self.success_streak,
                    adjustments=list(self.events))


def run_requests(tasks, client, controller, stopped=lambda: False, changed=lambda: None, started=lambda request: None):
    """Drive coroutine jobs, with at most the adaptive limit of HTTP calls in flight.

    Each task yields a ReviewRequest and receives its response or exception. All
    task bodies, checkpoints and report writes execute on the calling thread.
    Transient failures are retried twice before the task sees the final error.
    Already running calls drain after a reduction; no new call bypasses the limit.
    """
    tasks = iter(tasks)
    pending = deque()
    running = {}
    exhausted = False

    def resume(task, value=None, error=None):
        try:
            request = task.throw(error) if error is not None else task.send(value)
        except StopIteration:
            return
        if not isinstance(request, ReviewRequest):
            raise TypeError("invalid review request")
        pending.append((task, request, 0, 0.0))

    def invoke(request):
        value = client.ask(request.instruction, request.data)
        if not isinstance(value, dict):
            raise LLMError("LLM_INVALID_RESPONSE_OR_CONNECTION")
        return value

    with ThreadPoolExecutor(max_workers=controller.maximum, thread_name_prefix="qc-llm") as pool:
        try:
            while True:
                if stopped():
                    break
                while not exhausted and len(pending) + len(running) < controller.limit:
                    try:
                        task = next(tasks)
                    except StopIteration:
                        exhausted = True
                        break
                    resume(task)
                    if stopped():
                        break
                if stopped():
                    break
                now = controller.clock()
                for _ in range(len(pending)):
                    if stopped() or len(running) >= controller.limit or now < controller.admit_after:
                        break
                    task, request, attempt, ready = pending.popleft()
                    if ready > now:
                        pending.append((task, request, attempt, ready))
                        continue
                    if attempt == 0:
                        started(request)
                    future = pool.submit(invoke, request)
                    running[future] = (task, request, attempt, controller.epoch)
                    controller.requests += 1
                    controller.active = len(running)
                    controller.peak_active = max(controller.peak_active, controller.active)
                    changed()
                if not running:
                    if exhausted and not pending:
                        break
                    time.sleep(0.05)
                    continue
                done, _ = wait(running, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    task, request, attempt, epoch = running.pop(future)
                    controller.active = len(running)
                    try:
                        value = future.result()
                    except LLMError as error:
                        if transient_error(error):
                            controller.failure()
                            if attempt < 2 and not stopped():
                                controller.retries += 1
                                pending.append((task, request, attempt + 1, controller.clock() + 2 ** attempt))
                                changed()
                                continue
                        changed()
                        if not stopped():
                            resume(task, error=error)
                    else:
                        controller.success(epoch)
                        changed()
                        if not stopped():
                            resume(task, value=value)
        finally:
            for future in running:
                future.cancel()
            for task, *_ in pending:
                task.close()
            for task, *_ in running.values():
                task.close()
    controller.active = 0
    changed()

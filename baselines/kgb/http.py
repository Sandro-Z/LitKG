"""Rate-limited HTTP with exponential backoff and jitter (httpx)."""
import random
import threading
import time

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RateLimiter:
    def __init__(self, per_second):
        self.interval = 1.0 / per_second if per_second else 0
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next - now
            self.next = max(now, self.next) + self.interval
        if delay > 0:
            time.sleep(delay)


class Http:
    def __init__(self, per_second=3, timeout=60, retries=5, base_delay=1.0, sleep=time.sleep, client=None):
        self.limiter = RateLimiter(per_second)
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False,
                                             headers={'User-Agent': 'litkg-baselines/0.1'})
        self.retries, self.base_delay, self.sleep = retries, base_delay, sleep
        self.calls = 0

    def request(self, method, url, *, ok=(200,), retry_status=RETRY_STATUS, **kwargs):
        last = None
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            self.calls += 1
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last = HttpError(f'transport error: {type(exc).__name__}')
            else:
                if response.status_code in ok:
                    return response
                last = HttpError(f'HTTP {response.status_code}', response.status_code)
                if response.status_code not in retry_status:
                    raise last
            if attempt < self.retries:
                self.sleep(self.base_delay * (2 ** attempt) * (0.5 + random.random()))
        raise last

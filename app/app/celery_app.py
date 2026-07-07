import os
from celery import Celery

REDIS_URL = os.environ["REDIS_URL"]

celery_app = Celery(
    "litkg",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    result_expires=3600,
)

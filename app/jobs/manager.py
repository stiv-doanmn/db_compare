"""Job store in-memory theo job_id (UUID)."""
from __future__ import annotations

import uuid

from ..db.pool import close_pool
from ..models import JobState


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}

    def new(self) -> JobState:
        job_id = uuid.uuid4().hex
        job = JobState(id=job_id)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    async def drop(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job is not None:
            await close_pool(job.pool_a)
            await close_pool(job.pool_b)

    async def close_all(self) -> None:
        for job in self._jobs.values():
            await close_pool(job.pool_a)
            await close_pool(job.pool_b)
        self._jobs.clear()


manager = JobManager()

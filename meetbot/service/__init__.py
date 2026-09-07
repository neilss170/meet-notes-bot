"""Local HTTP service: send the bot to a meeting without using a terminal."""

from __future__ import annotations

from meetbot.service.jobs import JobManager, JobStatus, MeetingJob

__all__ = ["JobManager", "JobStatus", "MeetingJob"]

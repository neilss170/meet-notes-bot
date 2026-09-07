"""In-process meeting jobs: start a bot, follow it, stop it.

The CLI runs one meeting and exits. A service has to run several at once, for
callers who are not driving the process, and answer "what is it doing right
now" without waiting for the run to finish. That needs three things the CLI
path never did:

**Its own port per meeting.** The audio bridge binds a loopback WebSocket
port. One fixed port means the second concurrent meeting fails to bind, so
each job takes a free port of its own.

**No signal handlers.** ``run_meeting`` normally routes SIGINT/SIGTERM to
stopping the meeting, which is right for a CLI and wrong here: handlers are
process-global, so concurrent runs would clobber one another and Ctrl+C on the
server would stop an arbitrary meeting instead of the server. Jobs are stopped
through :meth:`JobManager.stop` instead.

**Live progress.** Rather than plumbing callbacks through the pipeline, a job
reads the run's own transcript store. It is append-only and flushed per
record, so the events and utterances already on disk are an accurate picture
of a meeting still in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from meetbot.config import Config, validate_meet_url
from meetbot.runner import MeetingRun, run_meeting
from meetbot.transcript.store import iter_records

logger = logging.getLogger(__name__)

#: Most recent utterances kept in a status payload, so polling stays cheap
#: however long the meeting runs.
LIVE_TRANSCRIPT_LINES = 40


class JobStatus(str, Enum):
    """Lifecycle of one meeting job."""

    STARTING = "starting"
    JOINING = "joining"
    IN_CALL = "in_call"
    STOPPING = "stopping"
    FINISHED = "finished"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.FINISHED, JobStatus.FAILED)


def free_port() -> int:
    """Reserve a free loopback port by binding and immediately releasing it.

    Racy in principle - something else could take the port between this call
    and the bridge binding it - but the window is microseconds on a loopback
    interface, and the alternative (a fixed port) fails deterministically on
    the second concurrent meeting rather than rarely.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class MeetingJob:
    """One bot run: what was asked for, and what has happened since."""

    id: str
    meet_url: str
    #: Username that sent the bot. Empty for runs recovered from disk, which
    #: predate accounts - those are shown to admins only.
    owner: str = ""
    status: JobStatus = JobStatus.STARTING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    output_dir: Path | None = None
    errors: list[str] = field(default_factory=list)
    utterance_count: int = 0
    #: Meeting length in seconds. Set from the transcript for runs restored
    #: from disk, where wall-clock start and finish are both just the
    #: directory's mtime and would render as 00:00.
    duration_s: float | None = None
    _task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _stop: Callable[[], None] | None = field(default=None, repr=False)
    _run: MeetingRun | None = field(default=None, repr=False)

    @property
    def elapsed_s(self) -> float:
        if self.duration_s is not None:
            return self.duration_s
        return (self.finished_at or time.time()) - self.created_at

    def read_progress(self) -> dict[str, Any]:
        """Derive live progress from the run's own transcript store.

        Never raises: a half-written final line is the normal state of a file
        being appended to, and a status poll must not fail because it caught
        the writer mid-record.
        """
        progress: dict[str, Any] = {
            "events": [],
            "transcript": [],
            "utterance_count": 0,
            "speakers": [],
            "duration_s": 0.0,
        }
        if self.output_dir is None:
            return progress
        path = self.output_dir / "transcript.jsonl"
        if not path.exists():
            return progress

        transcript: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        speakers: list[str] = []
        last_end = 0.0
        try:
            for record in iter_records(path):
                kind = record.get("type")
                if kind == "utterance":
                    transcript.append(
                        {
                            "speaker": record.get("speaker", ""),
                            "text": record.get("text", ""),
                            "start": record.get("start", 0.0),
                        }
                    )
                    speaker = record.get("speaker", "")
                    if speaker and speaker not in speakers:
                        speakers.append(speaker)
                    with contextlib.suppress(TypeError, ValueError):
                        last_end = max(last_end, float(record.get("end", 0.0)))
                elif kind == "event":
                    events.append(
                        {"kind": record.get("kind", ""), "at": record.get("at", "")}
                    )
        except (OSError, ValueError):
            logger.debug("Could not read progress for job %s", self.id, exc_info=True)
            return progress

        progress["events"] = events[-20:]
        progress["transcript"] = transcript[-LIVE_TRANSCRIPT_LINES:]
        progress["utterance_count"] = len(transcript)
        progress["speakers"] = speakers
        progress["duration_s"] = last_end
        return progress

    def artifact(self, name: str) -> Path | None:
        """Path to a produced artifact, or ``None`` if it does not exist yet."""
        if self.output_dir is None:
            return None
        path = self.output_dir / name
        return path if path.exists() else None

    def to_dict(self, *, include_progress: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "meet_url": self.meet_url,
            "owner": self.owner,
            "status": self.status.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "errors": self.errors,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "has_transcript": self.artifact("transcript.md") is not None,
            "has_summary": self.artifact("analysis.md") is not None,
        }
        if include_progress:
            payload.update(self.read_progress())
        else:
            payload["utterance_count"] = self.utterance_count
        return payload


class JobManager:
    """Owns every running and completed meeting job in this process."""

    def __init__(self, base_config: Config) -> None:
        """Create a manager.

        Args:
            base_config: Settings every job inherits - keys, model choices,
                output directory. Per-job values (the meeting URL, the bridge
                port) are overlaid at start time.
        """
        self._base = base_config
        self._jobs: dict[str, MeetingJob] = {}

    # -- queries -----------------------------------------------------------

    def list(self) -> list[MeetingJob]:
        """Every job, newest first."""
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> MeetingJob | None:
        return self._jobs.get(job_id)

    def visible_to(self, username: str, is_admin: bool) -> list[MeetingJob]:
        """Jobs ``username`` may see: their own, or everything for an admin.

        Transcripts are the sensitive part of this service, so the default is
        that a meeting belongs to whoever sent the bot. Runs restored from
        disk have no owner and are therefore admin-only rather than public.
        """
        if is_admin:
            return self.list()
        return [job for job in self.list() if job.owner == username]

    @property
    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if not job.status.is_terminal)

    # -- commands ----------------------------------------------------------

    def start(
        self, meet_url: str, *, owner: str = "", **overrides: Any
    ) -> MeetingJob:
        """Send the bot to ``meet_url``.

        Args:
            meet_url: The meeting to join.
            owner: Username of whoever asked for it, for per-user visibility.
            **overrides: Any :class:`Config` field to override for this job.

        Returns:
            The job, already running in the background.

        Raises:
            ValueError: If the URL is not a usable Meet link.
        """
        # Validated here rather than inside the task: a bad URL should be a
        # rejected request, not a job that appears to start and then fails.
        validate_meet_url(meet_url)

        config = dataclasses.replace(
            self._base,
            meet_url=meet_url,
            # Its own port, or the second concurrent meeting cannot bind.
            bridge_port=free_port(),
            **overrides,
        )
        job = MeetingJob(id=uuid.uuid4().hex[:12], meet_url=meet_url, owner=owner)
        self._jobs[job.id] = job
        job._task = asyncio.create_task(self._run(job, config))
        logger.info("Job %s: sending the bot to %s", job.id, meet_url)
        return job

    async def stop(self, job_id: str) -> bool:
        """Ask a running job to leave the call and finish up.

        Returns ``False`` if the job is unknown or already finished. The job
        is not awaited here: leaving a call and writing the summary takes
        time, and a stop request should return promptly.
        """
        job = self._jobs.get(job_id)
        if job is None or job.status.is_terminal:
            return False
        job.status = JobStatus.STOPPING
        if job._stop is not None:
            job._stop()
            logger.info("Job %s: stop requested", job.id)
            return True
        # Still starting up, with no session to ask politely yet.
        if job._task is not None:
            job._task.cancel()
        return True

    async def shutdown(self) -> None:
        """Stop every running job and wait for them to unwind."""
        for job in list(self._jobs.values()):
            if not job.status.is_terminal:
                await self.stop(job.id)
        tasks = [j._task for j in self._jobs.values() if j._task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _on_event(job: MeetingJob, kind: str) -> None:
        """Advance the job's status as the meeting reaches each stage.

        Without this a job read "joining" for its entire duration, because
        IN_CALL was never set - so a bot that had been in the call for
        minutes still looked like it was stuck knocking.
        """
        if kind == "joined" and job.status is JobStatus.JOINING:
            job.status = JobStatus.IN_CALL

    async def _run(self, job: MeetingJob, config: Config) -> None:
        """Drive one meeting to completion, recording the outcome on the job."""
        try:
            job.status = JobStatus.JOINING
            run = await run_meeting(
                config,
                # Process-global and shared by every job - see the module docstring.
                install_signal_handlers=False,
                on_stop=lambda stop: setattr(job, "_stop", stop),
                on_output_dir=lambda path: setattr(job, "output_dir", path),
                on_event=lambda kind, _details: self._on_event(job, kind),
                # One log file per run would fight over the root logger; the
                # service keeps its own configuration.
                reconfigure_logging=False,
            )
            job._run = run
            job.utterance_count = run.utterance_count
            job.errors = list(run.errors)
            job.status = JobStatus.FINISHED if run.succeeded else JobStatus.FAILED
            logger.info(
                "Job %s: %s (%d utterances)",
                job.id,
                job.status.value,
                run.utterance_count,
            )
        except asyncio.CancelledError:
            job.status = JobStatus.FAILED
            job.errors.append("Cancelled before the meeting started")
            raise
        except Exception as exc:  # noqa: BLE001 - one job must not kill the server
            logger.exception("Job %s failed", job.id)
            job.status = JobStatus.FAILED
            job.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            job.finished_at = time.time()
            if not job.status.is_terminal:
                # Reached only when the run neither succeeded nor raised,
                # which should not happen; failing loudly beats a job that
                # sits in a live state forever.
                job.status = JobStatus.FAILED


def load_past_runs(manager: JobManager, output_dir: Path, limit: int = 50) -> int:
    """Register meetings recorded before this server started.

    Restarting the service should not appear to erase history, so completed
    runs already on disk are surfaced alongside live ones. They are recreated
    read-only: no task, nothing to stop.

    Returns:
        How many past runs were registered.
    """
    if not output_dir.exists():
        return 0
    found = 0
    directories = sorted(
        (p for p in output_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for path in directories[:limit]:
        transcript = path / "transcript.jsonl"
        if not transcript.exists():
            continue
        meet_url = ""
        with contextlib.suppress(OSError, ValueError):
            for record in iter_records(transcript):
                if record.get("type") == "meta":
                    meet_url = record.get("meet_url", "")
                    break
        job = MeetingJob(
            id=f"past-{path.name}",
            meet_url=meet_url or path.name,
            status=JobStatus.FINISHED,
            created_at=path.stat().st_mtime,
            finished_at=path.stat().st_mtime,
            output_dir=path,
        )
        progress = job.read_progress()
        job.utterance_count = progress["utterance_count"]
        job.duration_s = progress["duration_s"]
        manager._jobs[job.id] = job
        found += 1
    return found


__all__ = [
    "LIVE_TRANSCRIPT_LINES",
    "JobManager",
    "JobStatus",
    "MeetingJob",
    "free_port",
    "load_past_runs",
]

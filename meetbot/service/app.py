"""HTTP API and web UI for sending the bot to meetings.

Bound to loopback only, deliberately. This service can join meetings and read
their transcripts; it has no authentication, so it must not be reachable from
the network. ``--host`` exists but warns loudly, and the UI it serves is a
single page with no external requests.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from meetbot.config import Config, ConfigError
from meetbot.service.jobs import JobManager, load_past_runs

logger = logging.getLogger(__name__)

UI_PATH = Path(__file__).parent / "ui.html"

#: Artifacts a client may fetch, mapped to their media type. An allow-list,
#: not a path parameter joined onto a directory: the artifact name comes from
#: the client, and letting it address arbitrary files under the output
#: directory would be a traversal bug.
ARTIFACTS: dict[str, str] = {
    "transcript.md": "text/markdown",
    "transcript.jsonl": "application/json",
    "analysis.md": "text/markdown",
    "meetbot.log": "text/plain",
}


class StartRequest(BaseModel):
    """A request to send the bot into a meeting."""

    meet_url: str = Field(..., description="Meeting link or code")
    bot_name: str | None = Field(None, description="Display name in the call")
    live_captions_overlay: bool | None = None
    speaker_names_from_captions: bool | None = None
    anonymise_analysis: bool | None = None


def create_app(config: Config) -> FastAPI:
    """Build the service around a validated base configuration."""
    manager = JobManager(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        restored = load_past_runs(manager, config.output_dir)
        if restored:
            logger.info("Loaded %d past meeting(s) from %s", restored, config.output_dir)
        yield
        # Leaving calls cleanly matters more than a fast shutdown: a bot left
        # behind sits in someone's meeting until they remove it.
        if manager.active_count:
            logger.info("Stopping %d active meeting(s)", manager.active_count)
        await manager.shutdown()

    app = FastAPI(title="meetbot", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return UI_PATH.read_text(encoding="utf-8")

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        """Defaults the UI shows, so it does not hard-code them."""
        return {
            "bot_name": config.bot_name,
            "live_captions_overlay": config.live_captions_overlay,
            "speaker_names_from_captions": config.speaker_names_from_captions,
            "anonymise_analysis": config.anonymise_analysis,
            "analysis_enabled": config.analysis_enabled,
            "llm_model": config.llm_model,
            "output_dir": str(config.output_dir),
        }

    @app.get("/api/meetings")
    async def list_meetings() -> dict[str, Any]:
        return {
            "active": manager.active_count,
            # Progress means reading a file per job; the list view only needs
            # counts, so the detail endpoint does that work instead.
            "meetings": [job.to_dict(include_progress=False) for job in manager.list()],
        }

    @app.post("/api/meetings", status_code=201)
    async def start_meeting(request: StartRequest) -> dict[str, Any]:
        overrides = {
            key: value
            for key, value in request.model_dump(exclude={"meet_url"}).items()
            if value is not None
        }
        try:
            job = manager.start(request.meet_url, **overrides)
        except (ValueError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.to_dict()

    @app.get("/api/meetings/{job_id}")
    async def get_meeting(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such meeting")
        return job.to_dict()

    @app.post("/api/meetings/{job_id}/stop")
    async def stop_meeting(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such meeting")
        stopped = await manager.stop(job_id)
        return {"stopped": stopped, **job.to_dict(include_progress=False)}

    @app.get("/api/meetings/{job_id}/artifact/{name}", response_class=PlainTextResponse)
    async def get_artifact(job_id: str, name: str) -> str:
        if name not in ARTIFACTS:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such meeting")
        path = job.artifact(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"{name} has not been written")
        return path.read_text(encoding="utf-8", errors="replace")

    app.state.manager = manager
    return app


__all__ = ["ARTIFACTS", "StartRequest", "create_app"]

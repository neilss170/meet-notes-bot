"""HTTP API and web UI for sending the bot to meetings.

Every route requires a signed-in account. That is not decoration: this
service joins meetings and serves transcripts of them, so an unauthenticated
instance reachable from the network hands both to anyone who finds the port.

Two roles, defined in :mod:`meetbot.service.auth`. A member sends the bot and
sees the meetings they started; an admin sees every meeting and manages
accounts. Ownership is per meeting rather than per transcript directory,
which is why runs recorded before accounts existed are visible to admins
only - guessing who they belonged to would be worse than showing nobody.

Binding to a non-loopback address is now supportable, but stays a deliberate
act: ``--host`` still warns, because sessions travel as cookies and only TLS
in front of the service keeps them off the wire.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from meetbot.config import Config, ConfigError
from meetbot.service.auth import (
    SESSION_COOKIE,
    SESSION_TTL_S,
    AuthError,
    Role,
    User,
    UserStore,
    bootstrap_admin,
    issue_session,
    load_or_create_secret,
    read_session,
)
from meetbot.preflight import Preflight, run_preflight
from meetbot.profile import sweep_stale_profiles
from meetbot.service.jobs import JobManager, MeetingJob, load_past_runs

logger = logging.getLogger(__name__)

UI_PATH = Path(__file__).parent / "ui.html"
LOGIN_PATH = Path(__file__).parent / "login.html"

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


class CreateUserRequest(BaseModel):
    """Admin request to add an account."""

    username: str
    password: str
    role: Role = Role.MEMBER


class PasswordRequest(BaseModel):
    """Admin request to set someone's password."""

    password: str


def create_app(config: Config) -> FastAPI:
    """Build the service around a validated base configuration."""
    manager = JobManager(config)
    state_dir = Path(config.service_state_dir).expanduser()
    users = UserStore(state_dir / "users.json")
    secret = load_or_create_secret(state_dir / "session.key")

    async def _refresh_health() -> Preflight:
        """Re-run the checks and remember the answer."""
        app.state.health_checking = True
        try:
            report = await run_preflight(config)
            app.state.health = report
            if not report.can_record:
                logger.warning("Scribe cannot record meetings yet:")
                report.log()
            return report
        finally:
            app.state.health_checking = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        created = bootstrap_admin(users)
        if created:
            name, password = created
            # Shown once, and only here. Storing it anywhere to display later
            # would defeat hashing the password in the first place.
            logger.warning("=" * 62)
            logger.warning("No accounts existed, so an admin was created:")
            logger.warning("    username: %s", name)
            logger.warning("    password: %s", password)
            logger.warning("Sign in and change it. This is shown only once.")
            logger.warning("=" * 62)
        # A run killed mid-meeting never reaches its own cleanup, so its
        # profile copy is still on disk. Clear anything old enough to be
        # certain it belongs to a run that is no longer alive.
        sweep_stale_profiles()
        # Run the checks in the background: the sign-in check starts a
        # browser, and making startup wait for it would delay the UI by ten
        # seconds every time. The page polls /api/health for the answer.
        app.state.preflight_task = asyncio.create_task(_refresh_health())
        restored = load_past_runs(manager, config.output_dir)
        if restored:
            logger.info("Loaded %d past meeting(s) from %s", restored, config.output_dir)
        yield
        # Leaving calls cleanly matters more than a fast shutdown: a bot left
        # behind sits in someone's meeting until they remove it.
        if manager.active_count:
            logger.info("Stopping %d active meeting(s)", manager.active_count)
        await manager.shutdown()

    app = FastAPI(title="Scribe", lifespan=lifespan)

    # -- authentication ----------------------------------------------------

    def _user_of(request: Request) -> User | None:
        """The signed-in user, or ``None``. Never raises."""
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        username = read_session(token, secret)
        if username is None:
            return None
        # Re-read the account each request: a deleted or demoted user must
        # lose access immediately, not when their cookie happens to expire.
        return users.get(username)

    async def current_user(request: Request) -> User:
        user = _user_of(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in to continue")
        return user

    async def admin_only(request: Request) -> User:
        user = await current_user(request)
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only")
        return user

    def _visible_job(job_id: str, user: User) -> MeetingJob:
        """A meeting this user may see, or a 404.

        Deliberately 404 rather than 403 for someone else's meeting: a
        distinct "forbidden" would confirm that a given id exists.
        """
        job = manager.get(job_id)
        if job is None or (not user.is_admin and job.owner != user.username):
            raise HTTPException(status_code=404, detail="No such meeting")
        return job

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Any:
        if _user_of(request) is not None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(LOGIN_PATH.read_text(encoding="utf-8"))

    @app.post("/login")
    async def login(
        request: Request,
        response: Response,
        username: str = Form(...),
        password: str = Form(...),
    ) -> Any:
        user = users.authenticate(username, password)
        if user is None:
            # One message for every failure - wrong name, wrong password,
            # locked out - so the form cannot be used to enumerate accounts.
            return HTMLResponse(
                LOGIN_PATH.read_text(encoding="utf-8").replace(
                    "<!--ERROR-->",
                    '<div class="err">Incorrect username or password.</div>',
                ),
                status_code=401,
            )
        redirect = RedirectResponse("/", status_code=303)
        redirect.set_cookie(
            SESSION_COOKIE,
            issue_session(user.username, secret),
            max_age=int(SESSION_TTL_S),
            httponly=True,          # unreadable to script, so XSS cannot lift it
            samesite="lax",         # not sent on cross-site POSTs: CSRF cover
            secure=request.url.scheme == "https",
            path="/",
        )
        logger.info("%s signed in", user.username)
        return redirect

    @app.post("/logout")
    async def logout() -> Any:
        redirect = RedirectResponse("/login", status_code=303)
        redirect.delete_cookie(SESSION_COOKIE, path="/")
        return redirect

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        # A browser asking for a page wants the login form, not a 401 body.
        if _user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(UI_PATH.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health(_: User = Depends(current_user)) -> dict[str, Any]:
        """What is working, and what to do about anything that is not.

        The UI reads this to stop someone sending the bot into a meeting that
        cannot possibly be recorded - which is how every wasted call so far
        began.
        """
        report: Preflight | None = getattr(app.state, "health", None)
        if report is None:
            return {
                "checking": getattr(app.state, "health_checking", False),
                "ok": None,
                "can_record": None,
                "checks": [],
            }
        return {"checking": getattr(app.state, "health_checking", False), **report.to_dict()}

    @app.post("/api/health/refresh")
    async def refresh_health(_: User = Depends(current_user)) -> dict[str, Any]:
        """Check again, after the operator has fixed something."""
        return (await _refresh_health()).to_dict()

    @app.get("/api/me")
    async def whoami(user: User = Depends(current_user)) -> dict[str, Any]:
        return user.to_dict()

    # -- meetings ----------------------------------------------------------

    @app.get("/api/config")
    async def get_config(_: User = Depends(current_user)) -> dict[str, Any]:
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
    async def list_meetings(user: User = Depends(current_user)) -> dict[str, Any]:
        mine = manager.visible_to(user.username, user.is_admin)
        return {
            "active": sum(1 for job in mine if not job.status.is_terminal),
            # Progress means reading a file per job; the list view only needs
            # counts, so the detail endpoint does that work instead.
            "meetings": [job.to_dict(include_progress=False) for job in mine],
        }

    @app.post("/api/meetings", status_code=201)
    async def start_meeting(
        request: StartRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        overrides = {
            key: value
            for key, value in request.model_dump(exclude={"meet_url"}).items()
            if value is not None
        }
        # A known-broken setup produces a bot that joins and records nothing,
        # or cannot join at all. Refusing here costs a moment; finding out in
        # the meeting costs the meeting.
        report: Preflight | None = getattr(app.state, "health", None)
        if report is not None and not report.can_record:
            blocker = report.blockers[0]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{blocker.name}: {blocker.detail}."
                    + (f" Fix: {blocker.fix}" if blocker.fix else "")
                ),
            )
        try:
            job = manager.start(request.meet_url, owner=user.username, **overrides)
        except (ValueError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.to_dict()

    @app.get("/api/meetings/{job_id}")
    async def get_meeting(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        return _visible_job(job_id, user).to_dict()

    @app.post("/api/meetings/{job_id}/stop")
    async def stop_meeting(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        job = _visible_job(job_id, user)
        stopped = await manager.stop(job_id)
        return {"stopped": stopped, **job.to_dict(include_progress=False)}

    @app.get("/api/meetings/{job_id}/artifact/{name}", response_class=PlainTextResponse)
    async def get_artifact(
        job_id: str, name: str, user: User = Depends(current_user)
    ) -> str:
        if name not in ARTIFACTS:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        job = _visible_job(job_id, user)
        path = job.artifact(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"{name} has not been written")
        return path.read_text(encoding="utf-8", errors="replace")

    # -- accounts (admin) --------------------------------------------------

    @app.get("/api/users")
    async def list_users(_: User = Depends(admin_only)) -> dict[str, Any]:
        return {"users": [user.to_dict() for user in users.list()]}

    @app.post("/api/users", status_code=201)
    async def create_user(
        request: CreateUserRequest, _: User = Depends(admin_only)
    ) -> dict[str, Any]:
        try:
            return users.add(
                request.username, request.password, request.role
            ).to_dict()
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/users/{username}/password")
    async def set_user_password(
        username: str, request: PasswordRequest, _: User = Depends(admin_only)
    ) -> dict[str, Any]:
        try:
            users.set_password(username, request.password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"username": username.lower(), "updated": True}

    @app.delete("/api/users/{username}")
    async def delete_user(
        username: str, admin: User = Depends(admin_only)
    ) -> dict[str, Any]:
        if username.strip().lower() == admin.username:
            # Removing the account you are using logs you out mid-request and
            # can strand the last admin; make it someone else's job.
            raise HTTPException(
                status_code=400, detail="You cannot delete your own account"
            )
        try:
            users.delete(username)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"username": username.lower(), "deleted": True}

    app.state.manager = manager
    app.state.users = users
    return app


__all__ = [
    "ARTIFACTS",
    "CreateUserRequest",
    "PasswordRequest",
    "StartRequest",
    "create_app",
]

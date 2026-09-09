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

from meetbot.analysis.llm import LLMError, build_client
from meetbot.analysis.notepad import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    NotepadError,
    answer_question,
    enhance_notes,
)
from meetbot.config import Config, ConfigError
from meetbot.runner import anonymised_utterances
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
from meetbot.transcript.notes import Notepad
from meetbot.transcript.format import format_timestamp
from meetbot.transcript.store import (
    Utterance,
    apply_speaker_names,
    read_speaker_names,
    read_utterances,
)

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
    "notes.md": "text/markdown",
    "enhanced.md": "text/markdown",
    "meetbot.log": "text/plain",
}


class StartRequest(BaseModel):
    """A request to send the bot into a meeting."""

    meet_url: str = Field(..., description="Meeting link or code")
    bot_name: str | None = Field(None, description="Display name in the call")
    live_captions_overlay: bool | None = None
    speaker_names_from_captions: bool | None = None
    anonymise_analysis: bool | None = None


class RecordRequest(BaseModel):
    """A request to record this machine, with no bot joining anything."""

    title: str = Field("", description="What to call this recording")
    loopback_index: int | None = Field(
        None, description="Speakers to capture; system default when omitted"
    )
    microphone_index: int | None = Field(
        None, description="Microphone to capture; negative to record none"
    )
    anonymise_analysis: bool | None = None


class NotesRequest(BaseModel):
    """An autosave of whatever is currently in the notepad."""

    notes: str = Field("", description="The full note body, not a delta")


class EnhanceRequest(BaseModel):
    """A request to rewrite the notes against the transcript."""

    template: str | None = Field(None, description="Template id shaping the output")


class AskRequest(BaseModel):
    """A question about one meeting."""

    question: str


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
        if config.preflight_on_start:
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
        if report is not None and not report.can_send_bot:
            # Whatever stops the bot - an expired Google session, usually -
            # says nothing about recording this machine, which is why the
            # recording route checks the audio devices instead.
            blocked = [r for r in report.results if r.blocking and not r.ok] or [
                r for r in report.results if not r.ok
            ]
            blocker = blocked[0]
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

    # -- recording this machine --------------------------------------------

    @app.get("/api/audio-devices")
    async def audio_devices(_: User = Depends(current_user)) -> dict[str, Any]:
        """What this machine can be recorded from.

        ``ready`` is false with a reason rather than raising, because "this
        box has no loopback device" is a normal answer the UI has to render,
        not an error.
        """
        from meetbot.capture.local import (
            LocalCaptureError,
            capture_available,
            list_devices,
        )

        ready, detail = await asyncio.to_thread(capture_available)
        if not ready:
            return {"ready": False, "detail": detail, "speakers": [], "microphones": []}
        try:
            loopbacks, microphones = await asyncio.to_thread(list_devices)
        except LocalCaptureError as exc:
            return {
                "ready": False,
                "detail": str(exc),
                "speakers": [],
                "microphones": [],
            }
        return {
            "ready": True,
            "detail": detail,
            "speakers": [d.to_dict() for d in loopbacks],
            "microphones": [d.to_dict() for d in microphones],
        }

    @app.post("/api/audio-devices/probe")
    async def probe_audio(_: User = Depends(current_user)) -> dict[str, Any]:
        """Listen to the speakers briefly and say whether anything is there.

        Loopback taps the mix *after* the volume control, so muted output
        records a perfectly well-formed empty transcript. Better to find that
        out before the meeting than after it.
        """
        from meetbot.capture.local import LocalCaptureError, probe_loopback

        try:
            probe = await asyncio.to_thread(probe_loopback, 1.0)
        except LocalCaptureError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "device": probe.device,
            "peak": probe.peak,
            "silent": probe.silent,
            "detail": probe.detail,
        }

    @app.post("/api/recordings", status_code=201)
    async def start_recording(
        request: RecordRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Record this machine. Nothing joins the meeting."""
        from meetbot.capture.local import capture_available

        ready, detail = await asyncio.to_thread(capture_available)
        if not ready:
            # A rejected request, rather than a job that appears to start and
            # then dies with nothing recorded.
            raise HTTPException(status_code=409, detail=detail)

        overrides = {
            key: value
            for key, value in request.model_dump(
                exclude={"title", "loopback_index", "microphone_index"}
            ).items()
            if value is not None
        }
        try:
            job = manager.start_local(
                title=request.title.strip(),
                owner=user.username,
                loopback_index=request.loopback_index,
                microphone_index=request.microphone_index,
                **overrides,
            )
        except (ValueError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.to_dict()

    # -- notepad -----------------------------------------------------------

    def _utterances_of(job: MeetingJob) -> list[Utterance]:
        """This meeting's utterances, with any learned real names applied."""
        path = job.artifact("transcript.jsonl")
        if path is None:
            return []
        try:
            utterances = read_utterances(path)
        except (OSError, ValueError):
            logger.warning("Could not read the transcript for job %s", job.id)
            return []
        return apply_speaker_names(utterances, read_speaker_names(path))

    def _notepad_of(job: MeetingJob) -> Notepad:
        """The job's notepad, or a 409 explaining why there isn't one yet."""
        pad = job.notepad
        if pad is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This meeting has not started recording yet, so there is "
                    "nowhere to keep notes. Try again in a moment."
                ),
            )
        return pad

    def _require_llm() -> None:
        if not config.analysis_enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    "AI features are switched off (ANALYSIS_ENABLED=false). "
                    "Notes are still saved, just not enhanced."
                ),
            )

    def _client() -> Any:
        return build_client(
            config.llm_provider,
            config.llm_api_key,
            config.llm_model,
            config.llm_base_url,
        )

    #: Job ids with an LLM call in flight, so a double-click cannot spend the
    #: free-tier quota twice on the same meeting. Only ever mutated between
    #: awaits on the one event loop, so a plain set is enough.
    in_flight: set[str] = set()

    @app.get("/api/templates")
    async def list_templates(_: User = Depends(current_user)) -> dict[str, Any]:
        """The note shapes the UI offers, so it does not hard-code them."""
        return {
            "templates": [t.to_dict() for t in TEMPLATES.values()],
            "default": DEFAULT_TEMPLATE,
        }

    @app.get("/api/meetings/{job_id}/notepad")
    async def get_notepad(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        job = _visible_job(job_id, user)
        pad = job.notepad
        if pad is None:
            # Not an error: the meeting is still starting. The UI shows an
            # empty, editable notepad and saves once the directory exists.
            return {
                "ready": False,
                "notes": "",
                "enhanced": "",
                "template": DEFAULT_TEMPLATE,
                "chat": [],
            }
        return {
            "ready": True,
            "notes": pad.read_notes(),
            "enhanced": pad.read_enhanced(),
            "template": pad.template or DEFAULT_TEMPLATE,
            "chat": pad.read_chat(),
        }

    @app.put("/api/meetings/{job_id}/notes")
    async def put_notes(
        job_id: str, request: NotesRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Autosave the notepad. The body is the whole note, not a delta."""
        pad = _notepad_of(_visible_job(job_id, user))
        try:
            await asyncio.to_thread(pad.write_notes, request.notes)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not save the notes: {exc}"
            ) from exc
        return {"saved": True, "chars": len(request.notes)}

    def _enhance_sync(
        utterances: list[Utterance],
        notes: str,
        template: str,
        meet_url: str,
        duration: str,
    ) -> str:
        """Blocking enhancement call, run in a worker thread."""
        return enhance_notes(
            utterances,
            notes,
            _client(),
            template_id=template,
            meeting_url=meet_url,
            duration=duration,
        )

    @app.post("/api/meetings/{job_id}/enhance")
    async def enhance(
        job_id: str, request: EnhanceRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Rewrite the typed notes against the transcript."""
        _require_llm()
        job = _visible_job(job_id, user)
        pad = _notepad_of(job)

        utterances = _utterances_of(job)
        notes = pad.read_notes()
        if not utterances and not notes.strip():
            raise HTTPException(
                status_code=409,
                detail=(
                    "Nothing to work from yet - type some notes, or wait for "
                    "the transcript to start."
                ),
            )

        key = f"{job_id}:enhance"
        if key in in_flight:
            raise HTTPException(
                status_code=409, detail="These notes are already being enhanced."
            )
        in_flight.add(key)
        try:
            template = request.template or pad.template or DEFAULT_TEMPLATE
            duration = format_timestamp(job.read_progress()["duration_s"])
            # The typed notes are sent as written even under anonymisation:
            # that setting is about speaker names the bot picked up from the
            # call, and stripping names the user typed themselves would break
            # the one thing that lets the model correct "Speaker 0".
            for_llm = (
                anonymised_utterances(utterances)
                if config.anonymise_analysis
                else utterances
            )
            enhanced = await asyncio.to_thread(
                _enhance_sync, for_llm, notes, template, job.meet_url, duration
            )
            await asyncio.to_thread(pad.write_enhanced, enhanced, template=template)
        except (NotepadError, LLMError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not save the notes: {exc}"
            ) from exc
        finally:
            in_flight.discard(key)
        return {"enhanced": enhanced, "template": template}

    def _ask_sync(
        utterances: list[Utterance],
        question: str,
        notes: str,
        history: list[dict[str, Any]],
    ) -> str:
        """Blocking question call, run in a worker thread."""
        return answer_question(
            utterances, question, _client(), notes=notes, history=history
        )

    @app.post("/api/meetings/{job_id}/ask")
    async def ask(
        job_id: str, request: AskRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Answer a question about this meeting."""
        _require_llm()
        question = request.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question first.")
        job = _visible_job(job_id, user)
        pad = _notepad_of(job)

        key = f"{job_id}:ask"
        if key in in_flight:
            raise HTTPException(
                status_code=409, detail="Still answering the last question."
            )
        in_flight.add(key)
        try:
            utterances = _utterances_of(job)
            # The enhanced notes are better context than the raw ones: same
            # real names, with the ASR errors already resolved.
            notes = pad.read_enhanced() or pad.read_notes()
            history = pad.read_chat()
            for_llm = (
                anonymised_utterances(utterances)
                if config.anonymise_analysis
                else utterances
            )
            answer = await asyncio.to_thread(
                _ask_sync, for_llm, question, notes, history
            )
            turn = await asyncio.to_thread(pad.append_chat, question, answer)
        except NotepadError as exc:
            # Covers "nothing to search" as well as an unusable model reply,
            # both of which the asker can act on.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            in_flight.discard(key)
        return turn

    @app.delete("/api/meetings/{job_id}/chat")
    async def clear_chat(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Forget every question asked about this meeting."""
        _notepad_of(_visible_job(job_id, user)).clear_chat()
        return {"cleared": True}

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
    "AskRequest",
    "RecordRequest",
    "CreateUserRequest",
    "EnhanceRequest",
    "NotesRequest",
    "PasswordRequest",
    "StartRequest",
    "create_app",
]

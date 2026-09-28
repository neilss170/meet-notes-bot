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
import contextlib
import dataclasses
import logging
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
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
    enhance_notes,
)
from meetbot.analysis.recall import (
    MeetingSource,
    RecallAnswer,
    RecallError,
    answer_about_meeting,
    answer_across_meetings,
    segment_utterances,
)
from meetbot.capture.calls import (
    POLL_INTERVAL_S,
    CallWatcher,
    detection_available,
    show_toast,
)
from meetbot.capture.voice import (
    PHRASES,
    VoiceCommand,
    VoiceListener,
    recogniser_executable,
    voice_available,
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
from meetbot.preflight import (
    SESSION_CHECK_TIMEOUT_S,
    CheckResult,
    Preflight,
    check_google_session,
    run_preflight,
)
from meetbot.profile import sweep_stale_profiles
from meetbot.service.jobs import JobManager, JobStatus, MeetingJob, load_past_runs
from meetbot.transcript.notes import ChatLog, Notepad
from meetbot.transcript.format import format_timestamp
from meetbot.transcript.store import (
    MARK_EVENT,
    SPEAKER_NAMES_EVENT,
    TITLE_EVENT,
    Utterance,
    append_event,
    apply_speaker_names,
    read_marks,
    read_meta,
    read_speaker_names,
    read_utterances,
    speakers,
)

logger = logging.getLogger(__name__)

UI_PATH = Path(__file__).parent / "ui.html"
LOGIN_PATH = Path(__file__).parent / "login.html"

#: How long a readiness report is treated as evidence. Past this it is still
#: shown - it is the best guess available - but it no longer blocks anything,
#: because "this was broken when we last looked" is not a reason to refuse a
#: request half an hour later.
HEALTH_STALE_AFTER_S = 300.0

#: Longest meeting name kept. Room for a sentence, short enough that the
#: list stays readable and a runaway client cannot write a novel into every
#: transcript it can reach.
MAX_TITLE_CHARS = 120

#: How long shutdown waits for notes that are still being written. Stopping
#: the service stops any live recording, which starts its write-up - and the
#: notes for the call somebody has just ended are the ones they are waiting
#: for, so a fast exit is the wrong trade here.
NOTES_ON_SHUTDOWN_GRACE_S = 20.0

#: Longest a speaker's name may be. Long enough for "Priya Raghunathan (QA)",
#: short enough that nobody can push a paragraph into every transcript line.
MAX_SPEAKER_NAME = 60

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
    call_id: str | None = Field(
        None, description="The call alert this recording answers, if any"
    )
    anonymise_analysis: bool | None = None


class TitleRequest(BaseModel):
    """A new name for a meeting."""

    title: str = Field("", description="What to call it from now on")


class NotesRequest(BaseModel):
    """An autosave of whatever is currently in the notepad."""

    notes: str = Field("", description="The full note body, not a delta")


class EnhanceRequest(BaseModel):
    """A request to rewrite the notes against the transcript."""

    template: str | None = Field(None, description="Template id shaping the output")


class AskRequest(BaseModel):
    """A question about one meeting, or about all of them."""

    question: str


class SpeakerNamesRequest(BaseModel):
    """Who the diarization labels in one meeting actually are."""

    names: dict[str, str] = Field(
        default_factory=dict,
        description='Label to real name, e.g. {"Speaker 0": "Priya"}',
    )


class MuteAppRequest(BaseModel):
    """Stop, or resume, offering to record calls from one app."""

    app_id: str


class CreateUserRequest(BaseModel):
    """Admin request to add an account."""

    username: str
    password: str
    role: Role = Role.MEMBER


class PasswordRequest(BaseModel):
    """Admin request to set someone's password."""

    password: str


def create_app(config: Config, *, public_url: str | None = None) -> FastAPI:
    """Build the service around a validated base configuration.

    Args:
        config: Settings every meeting inherits.
        public_url: Where the page is served, for the links in call
            notifications. Learned from the page's own requests if omitted.
    """
    manager = JobManager(config)
    state_dir = Path(config.service_state_dir).expanduser()
    users = UserStore(state_dir / "users.json")
    secret = load_or_create_secret(state_dir / "session.key")
    # Listening means holding the microphone, and the call watcher works by
    # executable: without telling it about the recogniser, Scribe would spot
    # its own listener and offer to record the call it thinks has started.
    own = None
    if config.voice_commands:
        own = (
            sys.executable,
            getattr(sys, "_base_executable", ""),
            recogniser_executable(),
        )
    calls = CallWatcher(state_dir / "call-alerts.json", own_executables=own)

    #: Job ids with an LLM call in flight, so a double-click cannot spend the
    #: free-tier quota twice on the same meeting - and so the page can say
    #: that notes are being written. Only ever mutated between awaits on the
    #: one event loop, so a plain set is enough.
    in_flight: set[str] = set()

    #: Write-ups running for meetings that have just ended. Held so the
    #: garbage collector cannot drop one mid-write, and so shutdown can wait.
    notes_tasks: set[asyncio.Task[None]] = set()

    def _payload(job: MeetingJob, *, include_progress: bool = True) -> dict[str, Any]:
        """A meeting as the page reads it, plus whether notes are on their way.

        ``enhancing`` covers the button and the automatic write-up after a
        meeting ends alike: to whoever is watching they are the same event,
        and the page shows the same indicator for either.
        """
        return {
            **job.to_dict(include_progress=include_progress),
            "enhancing": f"{job.id}:enhance" in in_flight,
        }

    async def _refresh_health() -> Preflight:
        """Re-run the checks and remember the answer."""
        app.state.health_checking = True
        try:
            report = await run_preflight(config)
            app.state.health = report
            app.state.health_at = time.time()
            if not report.can_record:
                logger.warning("Scribe cannot record meetings yet:")
                report.log()
            return report
        finally:
            app.state.health_checking = False

    async def _recheck_session() -> CheckResult | None:
        """Ask Google, right now, whether the bot is still signed in.

        Returns ``None`` when the answer could not be obtained in time, which
        is treated as "proceed" rather than "refuse" - failing to check is
        not evidence of a problem, and the run itself will surface a dead
        session soon enough.

        The cached report is updated in place with the result, so the health
        banner stops showing a failure the moment it stops being true.
        """
        try:
            result = await asyncio.wait_for(
                check_google_session(config), timeout=SESSION_CHECK_TIMEOUT_S
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            logger.warning("Could not re-check the Google session", exc_info=True)
            return None

        result = dataclasses.replace(result, mode="bot")
        report: Preflight | None = getattr(app.state, "health", None)
        if report is not None:
            report.results = [
                result if r.name == result.name else r for r in report.results
            ] or [result]
            if not any(r.name == result.name for r in report.results):
                report.results.append(result)
            app.state.health_at = time.time()
        return result

    async def _watch_calls() -> None:
        """Offer to record each call as it starts, for the life of the service."""
        failing = False
        while True:
            try:
                fresh = await asyncio.to_thread(
                    calls.poll, recording=manager.recording_locally
                )
                failing = False
            except Exception:  # noqa: BLE001 - one bad read must not end the watch
                if not failing:
                    logger.warning("Could not read the microphone state", exc_info=True)
                failing = True
                fresh = []
            for prompt in fresh:
                logger.info("Call detected: %s (%s)", prompt.title, prompt.app)
                base = (
                    public_url
                    or getattr(app.state, "base_url", "")
                    or "http://127.0.0.1:8080/"
                )
                await asyncio.to_thread(show_toast, prompt, base)
            await asyncio.sleep(POLL_INTERVAL_S)

    def _live_local_job() -> MeetingJob | None:
        """The recording happening on this machine right now, if any."""
        for job in manager.list():
            if job.kind == "local" and not job.status.is_terminal:
                return job
        return None

    def _voice_owner() -> str:
        """Who a recording started by voice belongs to.

        Nobody is signed in when somebody speaks to the room, so it goes to
        the first admin - the account already trusted with call alerts, and
        on a one-person machine the only account there is.
        """
        for user in users.list():
            if user.is_admin:
                return user.username
        return ""

    async def _voice_act(command: VoiceCommand) -> None:
        """Do the thing that was asked out loud."""
        job = _live_local_job()
        if command.kind == "start":
            if job is not None:
                logger.info("Voice: already recording, so nothing to start")
                return
            from meetbot.capture.local import capture_available

            ready, detail = await asyncio.to_thread(capture_available)
            if not ready:
                # Spoken into the air with nobody looking at the page, so the
                # log is the only place this can be said.
                logger.warning("Voice: cannot record - %s", detail)
                return
            started = manager.start_local(title="", owner=_voice_owner())
            logger.info("Voice: started recording %s", started.id)
            return

        if job is None:
            logger.info("Voice: nothing is recording, ignoring %r", command.kind)
            return
        # The file rather than the artifact: a recording named a second after
        # it starts has not written its first line yet, and the name should
        # still be kept. Appending beside the recorder is safe because every
        # record is one short line written in append mode and flushed.
        path = job.output_dir / "transcript.jsonl" if job.output_dir else None
        if command.kind == "stop":
            await manager.stop(job.id)
            logger.info("Voice: stopping %s", job.id)
        elif command.kind == "mark" and path is not None:
            at = float(job.read_progress().get("duration_s") or 0.0)
            await asyncio.to_thread(
                append_event, path, MARK_EVENT, at=at, said=command.text
            )
            logger.info("Voice: marked %s at %.1fs", job.id, at)
        elif command.kind == "name" and command.title:
            await _rename(job, command.title)
            logger.info("Voice: named %s %r", job.id, command.title)

    def _voice_heard(command: VoiceCommand) -> None:
        """Take a command off the listener's thread and onto the loop."""
        app.state.voice_heard = {**command.to_dict(), "at": time.time()}
        loop = getattr(app.state, "loop", None)
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(_voice_act(command), loop)

    voice = VoiceListener(_voice_heard)

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
        watching = None
        if config.call_alerts and detection_available()[0]:
            watching = asyncio.create_task(_watch_calls())
        # Commands arrive on the listener's own thread and have to be carried
        # back here, so the loop has to be reachable from it.
        app.state.loop = asyncio.get_running_loop()
        if config.voice_commands:
            heard, why = await asyncio.to_thread(voice_available)
            if heard:
                await asyncio.to_thread(voice.start)
            else:
                logger.warning("Voice commands are on but unavailable: %s", why)
        yield
        await asyncio.to_thread(voice.stop)
        if watching is not None:
            watching.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watching
        # Leaving calls cleanly matters more than a fast shutdown: a bot left
        # behind sits in someone's meeting until they remove it.
        if manager.active_count:
            logger.info("Stopping %d active meeting(s)", manager.active_count)
        await manager.shutdown()
        # Stopping those meetings starts writing them up, so wait a moment
        # for that to land rather than killing the tasks on the way out.
        if notes_tasks:
            logger.info("Finishing the notes for %d meeting(s)", len(notes_tasks))
            await asyncio.wait(set(notes_tasks), timeout=NOTES_ON_SHUTDOWN_GRACE_S)

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
                "checked_at": None,
                "stale": True,
            }
        checked_at = getattr(app.state, "health_at", None)
        age = time.time() - checked_at if checked_at else None
        return {
            "checking": getattr(app.state, "health_checking", False),
            "checked_at": checked_at,
            "age_s": round(age, 1) if age is not None else None,
            # Still shown when stale - it is the best guess available - but
            # the UI says so, and nothing is refused on the strength of it.
            "stale": age is None or age > HEALTH_STALE_AFTER_S,
            **report.to_dict(),
        }

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
            "meetings": [_payload(job, include_progress=False) for job in mine],
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
        # Asked now rather than read from a cached report. The session is the
        # only thing that stops a bot and the thing most likely to have
        # changed since the service started - quoting an old answer is how
        # signing back in appeared to have no effect.
        session = await _recheck_session()
        if session is not None and not session.ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{session.name}: {session.detail}."
                    + (f" Fix: {session.fix}" if session.fix else "")
                ),
            )

        # Everything else - configuration, Deepgram, the LLM - changes only
        # when somebody edits it, so the cached answer is good enough, and a
        # stale one is not grounds for refusing anything.
        report: Preflight | None = getattr(app.state, "health", None)
        checked_at = getattr(app.state, "health_at", None)
        fresh = checked_at is not None and (
            time.time() - checked_at <= HEALTH_STALE_AFTER_S
        )
        if fresh and report is not None:
            broken = [
                r for r in report.results
                if r.blocking and not r.ok and r.mode == "both"
            ]
            if broken:
                blocker = broken[0]
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
        return _payload(_visible_job(job_id, user))

    @app.post("/api/meetings/{job_id}/stop")
    async def stop_meeting(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        job = _visible_job(job_id, user)
        stopped = await manager.stop(job_id)
        return {"stopped": stopped, **_payload(job, include_progress=False)}

    async def _rename(job: MeetingJob, title: str) -> str:
        """Give a meeting a name, and make it stick.

        Both fields for a local recording: it is shown by its title and
        restored from disk by the URL field it was written with. The
        transcript is given the new name too, because that file is the only
        thing here that outlives the process - a rename kept in memory would
        last until the next restart and no longer.
        """
        title = title.strip()[:MAX_TITLE_CHARS]
        job.title = title
        if job.kind == "local":
            job.meet_url = title
        path = job.output_dir / "transcript.jsonl" if job.output_dir else None
        # Appending creates the file when the meeting has not written one yet.
        # Naming a recording in its first ten seconds - which is exactly when
        # somebody says "Scribe, call this the finance sync" - must not be the
        # case that gets dropped.
        if path is not None and path.parent.exists():
            await asyncio.to_thread(append_event, path, TITLE_EVENT, title=title)
        return title

    @app.put("/api/meetings/{job_id}/title")
    async def rename_meeting(
        job_id: str, request: TitleRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Call a meeting what it actually was.

        "abc-defg-hij" and "Untitled recording" are what the machine knows.
        Neither is what anybody looks for a week later.
        """
        job = _visible_job(job_id, user)
        if not request.title.strip():
            raise HTTPException(status_code=400, detail="Give the meeting a name.")
        await _rename(job, request.title)
        return _payload(job, include_progress=False)

    @app.delete("/api/meetings/{job_id}")
    async def delete_meeting(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Delete a meeting: transcript, notes, audio notes, the lot.

        Irreversible, and meant to be - a recording of somebody talking is
        exactly the kind of thing a person should be able to destroy
        properly. A meeting still recording is refused rather than deleted
        from under its own recorder.
        """
        job = _visible_job(job_id, user)
        if not job.status.is_terminal:
            raise HTTPException(
                status_code=409,
                detail="This meeting is still recording. Stop it first.",
            )
        directory = job.output_dir
        if directory is not None:
            # The path comes from the runner rather than from the request,
            # but a delete earns one more check: nothing outside the
            # recordings directory is ever removed.
            root = Path(config.output_dir).expanduser().resolve()
            with contextlib.suppress(OSError):
                resolved = Path(directory).resolve()
                if resolved.is_relative_to(root) and resolved != root:
                    await asyncio.to_thread(shutil.rmtree, resolved, True)
        manager.forget(job_id)
        logger.info("Deleted meeting %s", job_id)
        return {"deleted": True}

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
                exclude={"title", "loopback_index", "microphone_index", "call_id"}
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
        if request.call_id:
            calls.mark_handled(request.call_id)
        return job.to_dict()

    # -- call alerts -------------------------------------------------------

    @app.get("/api/calls")
    async def list_calls(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Calls that have started on this machine and not been answered.

        Admins only. Which apps this machine is using is its owner's business:
        a member signed in from elsewhere should not learn that the host has
        just joined a WhatsApp call.
        """
        # The notification's links have to point back at wherever the page
        # is actually being served from.
        app.state.base_url = str(request.base_url)
        if not user.is_admin:
            return {"enabled": False, "detail": "", "prompts": [], "muted": []}
        enabled, detail = detection_available()
        if not config.call_alerts:
            enabled, detail = False, "Call alerts are off (CALL_ALERTS=false)."
        return {
            "enabled": enabled,
            "detail": detail,
            "prompts": [p.to_dict() for p in calls.pending()] if enabled else [],
            "muted": calls.muted(),
        }

    @app.post("/api/calls/{prompt_id}/dismiss")
    async def dismiss_call(
        prompt_id: str, _: User = Depends(admin_only)
    ) -> dict[str, Any]:
        """Not this call. The next call from the same app still asks."""
        if not calls.dismiss(prompt_id):
            raise HTTPException(status_code=404, detail="That call has already ended.")
        return {"dismissed": True}

    @app.post("/api/calls/mute")
    async def mute_call_app(
        request: MuteAppRequest, _: User = Depends(admin_only)
    ) -> dict[str, Any]:
        """Never offer to record calls from this app."""
        app_id = request.app_id.strip()
        if not app_id:
            raise HTTPException(status_code=400, detail="Say which app to mute.")
        await asyncio.to_thread(calls.mute, app_id)
        return {"muted": calls.muted()}

    @app.post("/api/calls/unmute")
    async def unmute_call_app(
        request: MuteAppRequest, _: User = Depends(admin_only)
    ) -> dict[str, Any]:
        await asyncio.to_thread(calls.unmute, request.app_id.strip())
        return {"muted": calls.muted()}

    @app.get("/api/voice")
    async def voice_status(user: User = Depends(current_user)) -> dict[str, Any]:
        """What Scribe is listening for, and the last thing it heard.

        Admin-only, like call alerts: an open microphone in somebody's room
        is their business and nobody else's.
        """
        if not user.is_admin:
            return {"enabled": False, "listening": False, "detail": "", "heard": None}
        if not config.voice_commands:
            return {
                "enabled": False,
                "listening": False,
                "detail": "Voice commands are off (VOICE_COMMANDS=false).",
                "heard": None,
            }
        return {
            "enabled": True,
            "listening": voice.listening,
            "detail": voice.detail,
            "phrases": {kind: said[0] for kind, said in PHRASES.items()},
            "heard": getattr(app.state, "voice_heard", None),
        }

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

    def _marks_of(job: MeetingJob) -> list[float]:
        """Moments somebody asked to be able to find again."""
        path = job.artifact("transcript.jsonl")
        if path is None:
            return []
        try:
            return read_marks(path)
        except (OSError, ValueError):
            return []

    def _speakers_of(job: MeetingJob) -> list[dict[str, str]]:
        """Every voice in the meeting: the label stored, and what to call it.

        The label is what the transcript actually holds, so it stays the key
        however many times the name is changed - and a meeting that was never
        named reads as its own name, which is what the page shows.
        """
        path = job.artifact("transcript.jsonl")
        if path is None:
            return []
        try:
            recorded = read_utterances(path)
            named = read_speaker_names(path)
        except (OSError, ValueError):
            logger.warning("Could not read speakers for job %s", job.id)
            return []
        return [
            {"id": label, "name": named.get(label, label)}
            for label in speakers(recorded)
        ]

    def _title_of(job: MeetingJob) -> str:
        """What a meeting is called - the same rule the page uses."""
        if job.title:
            return job.title
        if job.kind == "local":
            return "Untitled recording"
        code = re.search(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", job.meet_url or "")
        return code.group(0) if code else (job.meet_url or "Meeting")

    def _source_of(job: MeetingJob) -> MeetingSource:
        """A meeting as recall reads it: real names, and when it happened."""
        utterances = _utterances_of(job)
        started_at = job.created_at
        path = job.artifact("transcript.jsonl")
        if path is not None:
            # A restored run's created_at is its directory's modification
            # time - when it finished. The header knows when it started.
            with contextlib.suppress(OSError, KeyError, TypeError, ValueError):
                meta = read_meta(path) or {}
                started_at = datetime.fromisoformat(str(meta["started_at"])).timestamp()
        alias: dict[str, str] = {}
        if config.anonymise_analysis and utterances:
            alias = {
                real.speaker: stand_in.speaker
                for real, stand_in in zip(utterances, anonymised_utterances(utterances))
            }
        return MeetingSource(
            id=job.id,
            title=_title_of(job),
            started_at=started_at,
            utterances=utterances,
            speaker_alias=alias,
        )

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

    @app.get("/api/meetings/{job_id}/transcript")
    async def get_transcript(
        job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The whole transcript, in the same lines citations point at.

        The meeting payload carries only the newest lines, to keep polling
        cheap. This is what the Transcript tab reads, and where a citation
        opens.
        """
        job = _visible_job(job_id, user)
        utterances = await asyncio.to_thread(_utterances_of, job)
        return {
            "segments": [s.to_dict() for s in segment_utterances(utterances)],
            "utterance_count": len(utterances),
            "speakers": await asyncio.to_thread(_speakers_of, job),
            "marks": await asyncio.to_thread(_marks_of, job),
        }

    @app.put("/api/meetings/{job_id}/speakers")
    async def name_speakers(
        job_id: str,
        request: SpeakerNamesRequest,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Put a real name to "Speaker 0", and to everybody else.

        Nothing links an audio track to a person, so diarization can only say
        that two different people spoke. One person who was there can say who
        they were in a couple of seconds, and that is worth more than any
        amount of guessing - "Speaker 1 will write the migration script" is a
        note nobody can act on.

        Recorded as an event appended to the transcript rather than a rewrite
        of it: the file is append-only exactly so that a crash cannot lose
        what was said. Every reader applies the mapping already, so the notes,
        the citations and both chats say the name from here on.
        """
        job = _visible_job(job_id, user)
        path = job.artifact("transcript.jsonl")
        if path is None:
            raise HTTPException(
                status_code=409,
                detail="Nothing has been transcribed yet, so there is nobody to name.",
            )
        known = {voice["id"] for voice in await asyncio.to_thread(_speakers_of, job)}
        names: dict[str, str] = {}
        for label, name in request.names.items():
            label = label.strip()
            if label not in known:
                # Accepting it silently would look like a save that did nothing.
                raise HTTPException(
                    status_code=400,
                    detail=f"Nobody in this meeting is labelled {label!r}.",
                )
            # An emptied box means "forget it": mapping a label to itself is
            # how a file that only ever grows takes something back.
            names[label] = " ".join(name.split())[:MAX_SPEAKER_NAME] or label
        if not names:
            raise HTTPException(status_code=400, detail="Say who was speaking.")
        try:
            await asyncio.to_thread(
                append_event, path, SPEAKER_NAMES_EVENT, names=names
            )
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not save the names: {exc}"
            ) from exc
        logger.info("Job %s: named %d speaker(s)", job.id, len(names))
        return {"speakers": await asyncio.to_thread(_speakers_of, job)}

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

    async def _write_notes_for(job: MeetingJob) -> None:
        """Write up a meeting that has just ended, without being asked.

        The same call the Enhance button makes, with the template the meeting
        already carries. Anything that goes wrong is logged and dropped: the
        transcript and the typed notes are both on disk, the button is still
        there, and a failed write-up must not be reported as a failed meeting.
        """
        pad = job.notepad
        if pad is None or pad.has_enhanced:
            # Already written - by hand during the call, most likely. Notes
            # somebody may have read are not replaced behind their back.
            return
        key = f"{job.id}:enhance"
        if key in in_flight:
            return
        utterances = await asyncio.to_thread(_utterances_of, job)
        notes = await asyncio.to_thread(pad.read_notes)
        if not utterances and not notes.strip():
            # A meeting that captured nothing has nothing to write up, and
            # spending a call to be told so is worse than staying quiet.
            return
        in_flight.add(key)
        try:
            template = pad.template or DEFAULT_TEMPLATE
            duration = format_timestamp(job.read_progress()["duration_s"])
            for_llm = (
                anonymised_utterances(utterances)
                if config.anonymise_analysis
                else utterances
            )
            enhanced = await asyncio.to_thread(
                _enhance_sync, for_llm, notes, template, job.meet_url, duration
            )
            await asyncio.to_thread(pad.write_enhanced, enhanced, template=template)
            logger.info("Job %s: wrote the notes (%d chars)", job.id, len(enhanced))
        except (NotepadError, LLMError, OSError) as exc:
            logger.warning(
                "Job %s: could not write the notes automatically: %s", job.id, exc
            )
        finally:
            in_flight.discard(key)

    def _notes_when_finished(job: MeetingJob) -> None:
        """Start writing a meeting up the moment it ends.

        The page has promised this since its stop button was labelled "Stop &
        write notes"; this is what makes the label true. Only meetings that
        ran in this process reach here - see ``JobManager.on_finished`` - so
        loading sixteen past recordings from disk writes up none of them.
        """
        if not (config.auto_notes and config.analysis_enabled):
            return
        if job.status is not JobStatus.FINISHED:
            # A meeting that failed captured nothing worth a call.
            return
        task = asyncio.create_task(_write_notes_for(job))
        notes_tasks.add(task)
        task.add_done_callback(notes_tasks.discard)

    # Wired here rather than at construction: writing a meeting up needs the
    # notepad helpers above, which need the app the manager is built for.
    manager.on_finished = _notes_when_finished

    def _ask_sync(
        job: MeetingJob, question: str, notes: str, history: list[dict[str, Any]]
    ) -> RecallAnswer:
        """Blocking question call, run in a worker thread."""
        return answer_about_meeting(
            _source_of(job), question, _client(), notes=notes, history=history
        )

    @app.post("/api/meetings/{job_id}/ask")
    async def ask(
        job_id: str, request: AskRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Answer a question about this meeting, citing the lines behind it."""
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
            # The enhanced notes are better context than the raw ones: same
            # real names, with the ASR errors already resolved.
            notes = pad.read_enhanced() or pad.read_notes()
            history = pad.read_chat()
            result = await asyncio.to_thread(_ask_sync, job, question, notes, history)
            turn = await asyncio.to_thread(
                pad.append_chat,
                question,
                result.answer,
                citations=[c.to_dict() for c in result.citations],
            )
        except RecallError as exc:
            # Covers "nothing to search" as well as an empty model reply,
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

    # -- asking every meeting ----------------------------------------------

    def _chat_of(user: User) -> ChatLog:
        # Usernames are limited to [a-z0-9._-] and start with a letter or
        # digit, so one is safe to use as a file name as it stands.
        return ChatLog(state_dir / "chats" / f"{user.username}.jsonl")

    def _ask_everything_sync(
        jobs: list[MeetingJob], question: str, history: list[dict[str, Any]]
    ) -> RecallAnswer:
        """Blocking cross-meeting question, run in a worker thread."""
        sources = [_source_of(job) for job in jobs]
        return answer_across_meetings(sources, question, _client(), history=history)

    @app.get("/api/chat")
    async def get_everything_chat(
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """This account's questions across every meeting, oldest first."""
        return {
            "chat": await asyncio.to_thread(_chat_of(user).read),
            "meetings": len(manager.visible_to(user.username, user.is_admin)),
        }

    @app.post("/api/chat")
    async def ask_everything(
        request: AskRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Answer a question from every meeting this account can see.

        Only those: a member's question is searched against the meetings
        they started and nothing else, exactly as their list shows.
        """
        _require_llm()
        question = request.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question first.")
        key = f"chat:{user.username}"
        if key in in_flight:
            raise HTTPException(
                status_code=409, detail="Still answering the last question."
            )
        in_flight.add(key)
        log = _chat_of(user)
        try:
            jobs = manager.visible_to(user.username, user.is_admin)
            history = await asyncio.to_thread(log.read)
            result = await asyncio.to_thread(
                _ask_everything_sync, jobs, question, history
            )
            turn = await asyncio.to_thread(
                log.append,
                question,
                result.answer,
                citations=[c.to_dict() for c in result.citations],
                searched=result.searched,
            )
        except RecallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            in_flight.discard(key)
        return turn

    @app.delete("/api/chat")
    async def clear_everything_chat(
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Forget every question this account asked across meetings."""
        await asyncio.to_thread(_chat_of(user).clear)
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
    app.state.calls = calls
    return app


__all__ = [
    "ARTIFACTS",
    "AskRequest",
    "MuteAppRequest",
    "RecordRequest",
    "CreateUserRequest",
    "EnhanceRequest",
    "NotesRequest",
    "PasswordRequest",
    "StartRequest",
    "create_app",
]

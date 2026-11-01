import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .account_usage import AccountUsage
from .attachments import Attachments
from .audio import Audio
from .auth import Auth
from .chat import Chat
from .codex import CodexError
from .config import Settings
from .core import Core, Tasks
from .db import Database
from .execution_ui import event_page, execution_page, execution_summary
from .github_projects import GitHubError, GitHubProjects
from .enrollment import in_tailnet
from .onboarding import NeedsAuthKey, Onboarding, Unknown
from .orchestration import Scheduler
from .pipeline import RESTRICTED, Pipeline
from .progress import progress
from .repair_progress import host_requests
from .project_ideas import ProjectIdeas
from .provision import DEVICE_RE, ProvisionError
from .project_memory import MemoryReviews, ProjectMemory
from .proxy import client_ip, enrollment_address, is_https, parse_trusted
from .registry import Registry, git
from .todos import Todos


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class SetupBody(LoginBody):
    token: str = Field(min_length=20, max_length=100)


class ProjectBody(BaseModel):
    path: str = Field(max_length=4096)
    name: str | None = Field(default=None, max_length=200)


class GitHubToken(BaseModel):
    token: str = Field(max_length=300, repr=False)


class GitHubCreate(BaseModel):
    name: str = Field(max_length=200)
    tier: str = 'personal'
    public_confirmed: bool = Field(default=False, strict=True)


class GitHubOrganization(BaseModel):
    company_org: str = Field(max_length=200)


class OfflineCreate(BaseModel):
    name: str = Field(max_length=200)


class GitHubRename(BaseModel):
    name: str = Field(max_length=200)


class ProjectVisibility(BaseModel):
    visible: bool = Field(strict=True)


class ProjectUpdate(BaseModel):
    revision: int
    display_name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=5000)
    objective: str | None = Field(default=None, max_length=5000)
    success_criteria: str | None = Field(default=None, max_length=5000)
    instructions: str | None = Field(default=None, max_length=10000)
    priority: int | None = Field(default=None, ge=0, le=100)
    status: str | None = None


class OnboardingBody(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(max_length=40)
    email: str = Field(max_length=200)
    windows_folder: str = Field(max_length=300)
    domain: str = Field(default="", max_length=200)
    memory_max: str = Field(default="", max_length=10)
    device_id: str = Field(default="", max_length=80)
    auto_apply: bool = Field(default=False, strict=True)


class OnboardingUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str | None = Field(default=None, max_length=80)
    auto_apply: bool | None = Field(default=None, strict=True)


class EnrollmentBody(BaseModel):
    model_config = {"extra": "forbid"}
    auth_key: str | None = Field(default=None, max_length=300, repr=False)


class EnrollReport(BaseModel):
    """The only thing the enrolled PC may send; nothing else is read."""
    model_config = {"extra": "forbid"}
    token: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    hostname: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    tailscale_ip: str = Field(min_length=7, max_length=15)
    device_id: str = Field(pattern=DEVICE_RE.pattern)


class MessageBody(BaseModel):
    request_id: str | None = Field(default=None, min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str | None = None
    content: str = Field(default='', max_length=64000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    todos: list[str] = Field(default_factory=list, max_length=200)
    conversation_id: str | None = None


class ChatResetBody(BaseModel):
    project_id: str | None = None
    conversation_id: str = Field(min_length=1, max_length=100)
    last_message_id: str = Field(min_length=1, max_length=100)
    confirmed: bool = False
    title: str = Field(default="", max_length=120)


class ChatArchiveBody(ChatResetBody):
    keep_last: int = Field(default=0, strict=True, ge=0, le=10)


class ChatMessageDeleteBody(BaseModel):
    project_id: str | None = None
    conversation_id: str = Field(min_length=1, max_length=100)
    message_id: str = Field(min_length=1, max_length=100)
    confirmed: bool = Field(default=False, strict=True)


class ChatTrashBody(BaseModel):
    project_id: str | None = None
    conversation_id: str = Field(min_length=1, max_length=100)
    last_message_id: str | None = Field(default=None, max_length=100)
    trash_id: str = Field(min_length=1, max_length=100)
    confirmed: bool = False


class InstructionsBody(BaseModel):
    content: str = Field(max_length=10000)


class ProjectIdeaBody(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(default="", max_length=200)
    details: str = Field(min_length=1, max_length=24000)


class TodoBody(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=24000)
    kind: Literal["idea", "ready"] | None = None


class TodoArchive(BaseModel):
    project_id: str | None = None
    selected: list[str] = Field(min_length=1, max_length=200)


class MemoryStart(BaseModel):
    confirmed: bool = False


class MemoryApply(BaseModel):
    review: str
    revision: int
    resolutions: list[dict] = Field(default_factory=list)


class MemoryRollback(BaseModel):
    version: str
    revision: int


class MemoryCancel(BaseModel):
    review: str


class TaskControl(BaseModel):
    revision: int
    action: Literal["hold", "release", "continue", "cancel"]


class AnswerBody(BaseModel):
    answers: dict


def create_app(settings: Settings | None = None, server=None, worker_factory=None, github_fetch=None):
    settings = settings or Settings()
    settings.prepare()
    db = Database(settings.db_path)
    restricted = settings.restricted
    registry = Registry(db, settings.project_roots, settings.fixed_project)
    tasks, auth = Tasks(db), Auth(db, settings)
    from .model_settings import active
    from .providers import ProviderServer

    def provider_factory(settings, native=False):
        return ProviderServer(settings, native, active=lambda: active(db))

    worker_factory = worker_factory or provider_factory
    chat = Chat(db, registry, server or provider_factory(settings))
    usage_backends = getattr(chat.server, 'backends', {'codex': chat.server})
    account_usage = {name: AccountUsage(backend, provider=name) for name, backend in usage_backends.items()}

    def invalidate_usage():
        for reader in account_usage.values():
            reader.invalidate()
    audio = Audio(settings)
    attachments = Attachments(db, settings)
    chat.attachments = attachments
    pipeline = Pipeline(db, registry, restricted)
    pipeline.sync.interval = max(10, settings.github_sync_seconds)
    core = Core(db, registry, settings, pipeline, worker_factory)
    scheduler = Scheduler(db, registry, settings, core, chat, pipeline)
    chat.action_handler = scheduler.manager_action
    chat.restricted = restricted
    github = GitHubProjects(db, registry, settings, pipeline, fetch=github_fetch)
    onboarding = Onboarding(db, settings, settings.onboarding_config)
    pipeline.backups.credentials = github.push_credentials
    memory = ProjectMemory(db, registry)
    reviews = MemoryReviews(memory, settings)
    release_commit = git(Path(__file__).resolve().parents[1], "rev-parse", "HEAD")

    @asynccontextmanager
    async def lifespan(app):
        lock_file = (settings.data_dir / "manager.lock").open("a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            auth.bootstrap()
            registry.discover()
            reviews.recover()
            scheduler.start()
            yield
        finally:
            await reviews.close()
            await scheduler.close()
            await core.close()
            await chat.close()
            lock_file.close()

    app = FastAPI(title="Linko Bot Boss", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.registry, app.state.chat, app.state.auth = db, registry, chat, auth
    app.state.core, app.state.scheduler = core, scheduler
    app.state.github_projects = github
    app.state.onboarding = onboarding
    app.state.audio = audio
    app.state.attachments = attachments
    app.state.memory, app.state.memory_reviews = memory, reviews
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    trusted_proxies = parse_trusted(settings.trusted_proxies)

    def set_session_cookie(response, token, request):
        response.set_cookie(
            "ai_manager_session",
            token,
            httponly=True,
            secure=settings.secure_cookie or is_https(request, trusted_proxies),
            samesite="strict",
            max_age=settings.session_seconds,
            path="/",
        )

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Origin rejected"}, status_code=403)
            # /api/enroll/report authenticates with its own single-use token (see enroll_report) instead of a session.
            if request.url.path not in {"/api/login", "/api/setup", "/api/enroll/report"}:
                session = auth.session(request.cookies.get("ai_manager_session"))
                if not session:
                    return JSONResponse({"detail": "Login required"}, status_code=401)
                if not hmac.compare_digest(session["csrf"], request.headers.get("x-csrf-token", "")):
                    return JSONResponse({"detail": "CSRF token required"}, status_code=403)
        response = await call_next(request)
        # Login/logout already issue their own cookie. Session checks (including
        # inside event streams) stay read-only: DB and browser renew together here.
        if request.url.path not in {"/api/login", "/api/logout"} and response.status_code < 400:
            token = request.cookies.get("ai_manager_session")
            if auth.renew_session(token):
                set_session_cookie(response, token, request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=(), payment=(), usb=()"
        if is_https(request, trusted_proxies):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    def unrestricted():
        if restricted:
            raise HTTPException(403, RESTRICTED)

    def scope(project_id):
        """Restricted instance: every project-scoped request is the one fixed project (no All Projects, no other id)."""
        if not restricted:
            return project_id
        fixed_id = registry.add(settings.project_path)["id"]
        if project_id not in (None, "", fixed_id):
            raise HTTPException(403, RESTRICTED)
        return fixed_id

    def require_owner(request: Request):
        session = auth.session(request.cookies.get("ai_manager_session"))
        if not session:
            raise HTTPException(401, "Login required")
        return session

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        logging.getLogger(__name__).warning("action.rejected path=%s reason=%s", request.url.path, str(error))
        return JSONResponse({"result": "rejected", "detail": str(error), "message": str(error)}, status_code=409)

    @app.exception_handler(GitHubError)
    async def github_failed(request, error):
        logging.getLogger(__name__).warning("action.rejected path=%s reason=%s", request.url.path, str(error))
        return JSONResponse({"result": "rejected", "detail": str(error), "message": str(error), "steps": error.steps},
                            status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, error):
        if request.url.path == '/api/github/token' or request.url.path.startswith('/api/github/tokens/'):
            # FastAPI's default 422 response includes the submitted input, including credentials.
            return JSONResponse({'detail': 'Invalid GitHub token request'}, status_code=422)
        if request.url.path == '/api/enroll/report' or request.url.path.endswith('/enrollment'):
            return JSONResponse({'detail': 'Invalid enrollment request'}, status_code=422)
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(ProvisionError)
    async def provision_failed(request, error):
        logging.getLogger(__name__).warning("action.rejected path=%s reason=%s", request.url.path, str(error))
        return JSONResponse({"result": "rejected", "detail": str(error), "message": str(error)}, status_code=409)

    @app.exception_handler(Unknown)
    async def unknown_record(request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(CodexError)
    async def unavailable(request, error):
        logging.getLogger(__name__).error("action.failed path=%s reason=%s", request.url.path, str(error))
        return JSONResponse({"result": "failed", "detail": str(error), "message": str(error)}, status_code=503)

    @app.get("/api/auth-status")
    def auth_status(request: Request):
        session = auth.session(request.cookies.get("ai_manager_session"))
        return {
            "setup_required": not bool(db.one("SELECT id FROM owner")),
            "authenticated": bool(session),
            "csrf": session["csrf"] if session else None,
            "release_commit": release_commit,
            "restricted": restricted,
            "scheduler_running": bool(scheduler.job and not scheduler.job.done()),
        }

    @app.get('/api/repair', dependencies=[Depends(require_owner), Depends(unrestricted)])
    def repair_status():
        from scripts.emergency_repair import Repair
        return Repair(settings.data_dir).records()

    @app.post('/api/repair', dependencies=[Depends(require_owner), Depends(unrestricted)])
    async def request_repair(body: dict):
        from scripts.emergency_repair import Repair
        from .model_settings import repair_route
        conversation = body.get('conversation_id')
        if not db.one('SELECT 1 FROM conversations WHERE id=?', (conversation,)):
            raise HTTPException(400, 'Select an existing originating conversation')
        route = {k: body.get(k) for k in ('provider', 'model', 'effort')}
        if not route['provider']:
            try:
                await chat.server.start()
                route = repair_route(db, chat.server, body.get('project_id'))
            except Exception:
                route = {}  # The host agent must work while settings or the provider are broken: fallback provider.
        return Repair(settings.data_dir).submit(body.get('request_id'), body.get('instructions', ''), conversation,
                                                route.get('provider'), route.get('model'), route.get('effort'))

    @app.get('/api/deployment', dependencies=[Depends(require_owner), Depends(unrestricted)])
    def deployment_status():
        return scheduler.pipeline.status()

    @app.post('/api/deployment/cancel', dependencies=[Depends(require_owner), Depends(unrestricted)])
    def cancel_deployment():
        return {'cancelled': scheduler.pipeline.cancel()}

    @app.get('/api/projects/{project_id}/deploy-setup', dependencies=[Depends(require_owner), Depends(unrestricted)])
    def project_deploy_setup(project_id: str):
        return scheduler.pipeline.status(project_id)

    @app.put('/api/projects/{project_id}/deploy-setup', dependencies=[Depends(require_owner), Depends(unrestricted)])
    def save_project_deploy_setup(project_id: str, body: dict):
        if 'setup' not in body:
            raise ValueError('Provide setup (or null to remove)')
        return scheduler.pipeline.save_setup(project_id, body['setup'])

    @app.post("/api/setup")
    async def setup(body: SetupBody, request: Request):
        # Closed for good once the owner exists: no remote signup or password reset, and no throttle slot used.
        if db.one("SELECT id FROM owner"):
            raise HTTPException(409, "Owner account already exists")
        # Bootstrap token checks are throttled per real client IP.
        try:
            auth.throttle(client_ip(request, trusted_proxies))
        except ValueError as error:
            raise HTTPException(429, "Too many setup attempts") from error
        await asyncio.to_thread(auth.setup, body.username, body.password, body.token)
        return {"created": True}

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        try:
            token, csrf = await asyncio.to_thread(
                auth.login, body.username, body.password, client_ip(request, trusted_proxies)
            )
        except ValueError as error:
            raise HTTPException(429 if "Too many" in str(error) else 401, str(error)) from error
        response = JSONResponse({"csrf": csrf})
        set_session_cookie(response, token, request)
        return response

    @app.post("/api/logout", dependencies=[Depends(require_owner)])
    def logout(request: Request):
        auth.logout(request.cookies.get("ai_manager_session"))
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            "ai_manager_session", path="/", httponly=True, samesite="strict",
            secure=settings.secure_cookie or is_https(request, trusted_proxies),
        )
        return response

    @app.post("/api/sessions/revoke", dependencies=[Depends(require_owner)])
    def revoke_sessions():
        with db.transaction() as conn:
            conn.execute("DELETE FROM sessions")
            db.event(conn, "owner.sessions_revoked", {})
        return {"revoked": True}

    owner_only = [Depends(require_owner), Depends(unrestricted)]

    @app.get("/api/onboarding", dependencies=owner_only)
    def onboarding_list():
        return {"entries": onboarding.list(), "installed": onboarding.installed(),
                "tailscale_api": bool(onboarding.config().get("TAILSCALE_API_KEY"))}

    @app.post("/api/onboarding", dependencies=owner_only)
    def onboarding_create(body: OnboardingBody):
        return onboarding.create(body.model_dump(exclude={"auto_apply"}), body.auto_apply)

    @app.get("/api/onboarding/{record_id}", dependencies=owner_only)
    def onboarding_get(record_id: str):
        return onboarding.get(record_id)

    @app.patch("/api/onboarding/{record_id}", dependencies=owner_only)
    def onboarding_update(record_id: str, body: OnboardingUpdate):
        return onboarding.settings_update(record_id, body.device_id, body.auto_apply)

    @app.post("/api/onboarding/{record_id}/preview", dependencies=owner_only)
    def onboarding_preview(record_id: str):
        return onboarding.preview(record_id)

    @app.post("/api/onboarding/{record_id}/apply", dependencies=owner_only)
    def onboarding_apply(record_id: str):
        return onboarding.apply(record_id)

    @app.post("/api/onboarding/{record_id}/enrollment", dependencies=owner_only)
    async def onboarding_enrollment(record_id: str, body: EnrollmentBody):
        try:
            return await asyncio.to_thread(onboarding.enrollment_command, record_id, body.auth_key)
        except NeedsAuthKey as error:
            return JSONResponse({"result": "rejected", "detail": str(error), "message": str(error), "needs_auth_key": True},
                                status_code=409)

    @app.post("/api/onboarding/{record_id}/login", dependencies=owner_only)
    def onboarding_login(record_id: str):
        return onboarding.reveal_login(record_id)

    @app.post("/api/enroll/report")
    async def enroll_report(body: EnrollReport, request: Request):
        """The enrolled PC's single report. Not reachable through the public tunnel or from outside the tailnet; the
        enrollment token (random, single use, two hours, stored hashed) is the credential. It records nothing else."""
        if restricted:
            raise HTTPException(404, "Not found")
        address = enrollment_address(request, trusted_proxies, settings.port)
        if address is None or not in_tailnet(address):
            raise HTTPException(403, "Only reachable from the tailnet")
        if not in_tailnet(body.tailscale_ip):
            raise HTTPException(422, "Invalid enrollment request")
        try:
            auth.throttle(address)
            return await asyncio.to_thread(onboarding.report, body.token, body.hostname, body.tailscale_ip, body.device_id)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(429, str(error)) from error

    @app.get("/api/history", dependencies=[Depends(require_owner)])
    def history(project_id: str | None = None, search: str = "", before: str | None = None,
                since: str | None = None, watch: str = ""):
        return chat.history(scope(project_id), search, before, since, watch.split(',') if watch else ())

    @app.get("/api/chat-history", dependencies=[Depends(require_owner)])
    def search_history(
        project_id: str | None = None,
        search: str = Query(default="", max_length=500),
        offset: int = Query(default=0, ge=0),
    ):
        return chat.search_history(scope(project_id), search, offset)

    @app.get("/api/chat-history/{conversation_id}", dependencies=[Depends(require_owner)])
    def saved_history(
        conversation_id: str,
        project_id: str | None = None,
        before: str | None = None,
        search: str = Query(default="", max_length=500),
    ):
        return chat.saved_history(conversation_id, scope(project_id), before, search)

    @app.post("/api/chat/message/delete", dependencies=[Depends(require_owner)])
    def delete_chat_message(body: ChatMessageDeleteBody):
        body.project_id = scope(body.project_id)
        return chat.delete_message(**body.model_dump())

    @app.post("/api/chat/clear", dependencies=[Depends(require_owner)])
    def clear_chat(body: ChatResetBody):
        body.project_id = scope(body.project_id)
        return chat.reset(body.project_id, body.conversation_id, body.last_message_id, body.confirmed)

    @app.post("/api/chat/trash/restore", dependencies=[Depends(require_owner)])
    def restore_trash(body: ChatTrashBody):
        body.project_id = scope(body.project_id)
        return chat.change_trash(**body.model_dump(), restore=True)

    @app.post("/api/chat/trash/empty", dependencies=[Depends(require_owner)])
    def empty_trash(body: ChatTrashBody):
        body.project_id = scope(body.project_id)
        return chat.change_trash(**body.model_dump())

    @app.post("/api/chat/archive", dependencies=[Depends(require_owner)])
    def archive_chat(body: ChatArchiveBody):
        body.project_id = scope(body.project_id)
        return chat.reset(
            body.project_id, body.conversation_id, body.last_message_id, body.confirmed, archive=True, title=body.title, keep_last=body.keep_last
        )

    @app.get("/api/executions/{execution_id}/events", dependencies=[Depends(require_owner)])
    async def execution_events(execution_id: str, request: Request, after: int = 0):
        if not db.one("SELECT id FROM executions WHERE id=?", (execution_id,)):
            raise HTTPException(404, "Unknown execution")
        header = request.headers.get("last-event-id", "0")
        cursor = max(after, int(header) if header.isdecimal() else 0)

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                if not auth.session(request.cookies.get("ai_manager_session")):
                    yield "event: stream.auth_expired\ndata: {}\n\n"
                    return
                rows = db.all(
                    "SELECT * FROM events WHERE execution_id=? AND seq>? ORDER BY seq LIMIT 100", (execution_id, cursor)
                )
                for row in rows:
                    cursor = row["seq"]
                    yield f"id: {cursor}\nevent: {row['kind']}\ndata: {row['payload']}\n\n"
                execution = db.one("SELECT finished_at FROM executions WHERE id=?", (execution_id,))
                if execution["finished_at"] and len(rows) < 100:
                    yield "event: stream.end\ndata: {}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/account-usage", dependencies=[Depends(require_owner)])
    async def usage(project_id: str | None = None):
        from .model_settings import manager_route
        from .providers import provider_of
        selection = manager_route(db, project_id)
        provider = provider_of(selection.get('model'))
        # Both views come from the existing provider caches/listeners. Concurrent
        # reads keep an unavailable backend from serially delaying the other.
        names = ('codex', 'claude')
        readings = await asyncio.gather(*(account_usage[name].read() for name in names if name in account_usage))
        providers = dict(zip((name for name in names if name in account_usage), readings))
        for name in names:
            providers.setdefault(name, {'scope': 'account', 'provider': name, 'buckets': [], 'status': 'unavailable'})
        result = providers[provider]
        # A saved selection can change while metadata is in flight.
        current = manager_route(db, project_id)
        if current != selection:
            provider, result = provider_of(current.get('model')), None
        return {**(result or {'scope': 'account', 'buckets': [], 'status': 'unavailable'}),
                'providers': providers, 'provider': provider, 'manager': current,
                'project_id': project_id, 'server_time': time.time()}

    @app.get("/api/audio", dependencies=[Depends(require_owner)])
    def audio_status():
        return audio.status()

    @app.post('/api/attachments', dependencies=[Depends(require_owner)])
    async def upload_attachment(request: Request, name: str, conversation_id: str, project_id: str | None = None):
        return await attachments.upload(request, name, scope(project_id), conversation_id)

    @app.get('/api/attachments/{identifier}', dependencies=[Depends(require_owner)])
    def download_attachment(identifier: str):
        item = attachments.get(identifier)
        return FileResponse(attachments.root / item['stored_name'], media_type='application/octet-stream',
                            filename=item['name'], headers={'X-Content-Type-Options': 'nosniff'})

    @app.post("/api/audio/transcribe", dependencies=[Depends(require_owner)])
    async def transcribe_audio(request: Request):
        return await audio.transcribe(request)

    @app.get("/api/model-settings", dependencies=[Depends(require_owner)])
    async def model_settings(project_id: str | None = None):
        from .model_settings import active, current, favorites, locked, picker_providers, selection_models
        try:
            await chat.server.start()
        except Exception:
            pass  # Show the saved choices; unavailable models render disabled.
        saved, marked = current(db, chat.server, project_id), favorites(db, chat.server)
        keep = selection_models(saved) | {f['pair']['model'] for f in marked if f}
        users = {}
        if not restricted and (settings.data_dir / 'deploy' / 'installed.json').exists():
            from .instance_models import listing
            try:
                users['users'] = await asyncio.to_thread(listing, settings.data_dir, chat.server)
            except (OSError, ValueError) as error:
                users['users_error'] = f'Cannot load Users: {error}'
        return {**users, "provider": active(db, project_id), "settings": saved, "favorites": marked,
                "project_id": project_id, "locked": locked(db, project_id),
                "repo_name": (db.one("SELECT COALESCE(display_name,name) AS name FROM projects WHERE id=?", (project_id,))["name"]
                              if project_id else "All Projects"),
                "providers": picker_providers(chat.server, keep)}

    @app.put("/api/projects/{project_id}/model-lock", dependencies=[Depends(require_owner)])
    async def save_repo_lock(project_id: str, body: dict):
        scope(project_id)
        from .model_settings import set_lock
        set_lock(db, project_id, body, chat.server)
        return await model_settings(project_id)

    @app.put("/api/model-settings", dependencies=[Depends(require_owner)])
    async def save_model_settings(body: dict, project_id: str | None = None, locked: bool | None = None):
        from .model_settings import save
        await chat.server.start()
        result = save(db, chat.server, body, project_id, locked)
        invalidate_usage()
        return result

    @app.get('/api/instance-model-settings', dependencies=[Depends(require_owner), Depends(unrestricted)])
    async def read_instance_models(name: str):
        from .instance_models import read
        await chat.server.start()
        return await asyncio.to_thread(read, settings.data_dir, chat.server, name)

    @app.put('/api/instance-model-settings', dependencies=[Depends(require_owner), Depends(unrestricted)])
    async def save_instance_models(name: str, body: dict):
        from .instance_models import save
        await chat.server.start()
        return await asyncio.to_thread(save, settings.data_dir, chat.server, name, body)

    @app.put("/api/manager-favorites/{slot}", dependencies=[Depends(require_owner)])
    async def insert_manager_favorite(slot: int, project_id: str | None = None):
        from .model_settings import insert_favorite
        await chat.server.start()
        return insert_favorite(db, chat.server, slot, project_id)

    @app.post("/api/manager-favorites/{slot}/recall", dependencies=[Depends(require_owner)])
    async def recall_manager_favorite(slot: int, project_id: str | None = None, locked: bool | None = None):
        from .model_settings import recall_favorite
        await chat.server.start()
        recall_favorite(db, chat.server, slot, project_id, locked)
        invalidate_usage()
        return await model_settings(project_id)

    @app.put("/api/settings/instructions", dependencies=[Depends(require_owner)])
    def global_instructions(body: InstructionsBody):
        db.set_setting("global_instructions", body.content)
        return {"updated": True}

    @app.get("/")
    def index():
        return HTMLResponse(index_html)

    @app.get("/api/project-ideas", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def project_idea_list():
        return {"entries": ProjectIdeas(db).list()}

    @app.post("/api/project-ideas", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def project_idea_add(body: ProjectIdeaBody):
        return ProjectIdeas(db).save(body.title, body.details)

    @app.patch("/api/project-ideas/{entry_id}", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def project_idea_update(entry_id: str, body: ProjectIdeaBody):
        return ProjectIdeas(db).save(body.title, body.details, entry_id)

    @app.delete("/api/project-ideas/{entry_id}", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def project_idea_delete(entry_id: str):
        return ProjectIdeas(db).delete(entry_id)

    @app.get("/api/todos", dependencies=[Depends(require_owner)])
    def todo_list(project_id: str | None = None):
        project_id = scope(project_id)
        if project_id:
            registry.get(project_id)
        return {"entries": Todos(db).list(project_id)}

    @app.post("/api/todos", dependencies=[Depends(require_owner)])
    def todo_add(body: TodoBody):
        body.project_id = scope(body.project_id)
        return Todos(db).save(body.project_id, body.title, body.details, kind=body.kind)

    @app.patch("/api/todos/{entry_id}", dependencies=[Depends(require_owner)])
    def todo_update(entry_id: str, body: TodoBody):
        body.project_id = scope(body.project_id)
        return Todos(db).save(body.project_id, body.title, body.details, entry_id, body.kind)

    @app.post("/api/todos/archive", dependencies=[Depends(require_owner)])
    def todo_archive(body: TodoArchive):
        body.project_id = scope(body.project_id)
        return Todos(db).archive(body.selected, body.project_id)

    @app.post("/api/todos/{entry_id}/start", dependencies=[Depends(require_owner)])
    async def todo_start(entry_id: str):
        # Only Ready To-Dos start; an Idea is refined in chat first. Replay the durable handoff even after a lost reply.
        def receipt():
            handoff = db.setting("todo_handoff:" + entry_id)
            return tasks.get(handoff["task_id"]) if handoff else None

        task = receipt()
        if not task:
            from .model_settings import delegation

            await chat.server.start()
            task = receipt()
            if not task:
                entry = db.one("SELECT * FROM todos WHERE id=? AND archived_at IS NULL", (entry_id,))
                if not entry:
                    raise ValueError("To-Do not found (it may be archived); refresh before starting")
                project_id = entry["project_id"]
                if registry.get(project_id)["status"] == "removed":
                    raise ValueError("Restore this To-Do's project before starting")
                Todos.require_ready(entry)
                route = delegation(db, chat.server, project_id=project_id)
                details = entry["details"]
                objective = entry["title"] + ("\n\n" + details if details else "")
                task = tasks.add(project_id, objective, chat.conversation(project_id)["id"],
                                 request_key="todo-start:" + entry_id, model_route=route, todos=[entry_id],
                                 selected={"selected_context": "To-Do " + entry_id + ": " + entry["title"]},
                                 reuse_objective=False)
        return {"result": "accepted", "task_id": task["id"], "status": task["status"]}

    @app.get("/api/portfolio", dependencies=[Depends(require_owner)])
    def portfolio(project_id: str | None = None, task_through: str | None = None,
                  execution_through: str | None = None, older: str | None = None):
        project_id = scope(project_id)
        if project_id:
            registry.get(project_id)
        if older not in (None, 'tasks', 'executions'):
            raise ValueError('Unknown older listing')
        task_page = tasks.page(project_id, through=task_through, older=older == 'tasks')
        return {
            "scope_project_id": project_id,
            "projects": [{**p, "github_sync": pipeline.sync.status(p["id"])} for p in registry.list()],
            "global_last_used_at": db.setting("global_last_used_at", 0),
            "tasks": task_page['tasks'],
            "task_cursor": task_page['cursor'],
            "has_older_tasks": task_page['has_more'],
            **execution_summary(db, project_id, through=execution_through, older=older == 'executions'),
            "progress": progress(db, scheduler.pipeline, project_id),
            "host_repair": [] if restricted else [
                {**r, "conversation_known": bool(r.get("conversation_id") and db.one(
                    "SELECT 1 FROM conversations WHERE id=?", (r["conversation_id"],)))}
                for r in host_requests(settings.data_dir)],
            "questions": db.all(
                "SELECT q.*,e.project_id,e.task_id FROM questions q JOIN executions e ON e.id=q.execution_id WHERE q.status='pending'"
                + (" AND e.project_id=?" if project_id else ""),
                (project_id,) if project_id else (),
            ),
            "capacity": 1,
            "queue_error": scheduler.error,
            "storage_free": shutil.disk_usage(settings.data_dir).free,
        }

    @app.post("/api/projects/discover", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def discover():
        return registry.discover()

    @app.post("/api/projects", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def add_project(body: ProjectBody):
        return registry.add(body.path, body.name)

    # GitHub actions and incoming updates. There is intentionally no delete route or delete code.
    @app.post("/api/projects/{project_id}/pull", dependencies=[Depends(require_owner), Depends(unrestricted)])
    async def github_pull(project_id: str):
        registry.get(project_id)
        return await asyncio.shield(pipeline.sync.start(project_id))

    @app.post("/api/projects/{project_id}/push", dependencies=[Depends(require_owner), Depends(unrestricted)])
    async def github_push(project_id: str):
        return await asyncio.shield(pipeline.backups.push_now(project_id))

    @app.get("/api/github", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_status():
        return github.status()

    @app.put("/api/github/token", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_token(body: GitHubToken):
        return github.save_token(body.token)

    @app.put("/api/github/tokens/{tier}", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_tier_token(tier: str, body: GitHubToken):
        return github.save_token(body.token, tier)

    @app.post("/api/github/tokens/{tier}/validate", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_validate_token(tier: str):
        return github.validate_token(tier)

    @app.post("/api/github/tokens/{tier}/remove", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_remove_token(tier: str):
        return github.remove_token(tier)

    @app.put("/api/github/organization", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_organization(body: GitHubOrganization):
        return github.save_company_org(body.company_org)

    @app.post("/api/github/projects", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_create(body: GitHubCreate):
        return github.create(body.name, body.tier, body.public_confirmed)

    @app.post("/api/projects/offline", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def offline_create(body: OfflineCreate):
        return github.create_offline(body.name)

    @app.post("/api/github/visibility/refresh", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_visibility_refresh():
        return github.refresh_visibility()

    @app.post("/api/github/projects/{project_id}/rename", dependencies=[Depends(require_owner), Depends(unrestricted)])
    def github_rename(project_id: str, body: GitHubRename):
        return github.rename(project_id, body.name)

    @app.patch("/api/projects/{project_id}", dependencies=[Depends(require_owner)])
    def update_project(project_id: str, body: ProjectUpdate):
        scope(project_id)
        return registry.update(project_id, body.revision, body.model_dump(exclude={"revision"}, exclude_none=True))

    @app.put("/api/projects/{project_id}/visibility", dependencies=[Depends(require_owner)])
    def project_visibility(project_id: str, body: ProjectVisibility):
        scope(project_id)
        return registry.set_visibility(project_id, body.visible)

    @app.get("/api/projects/{project_id}", dependencies=[Depends(require_owner)])
    def detail(project_id: str):
        scope(project_id)
        return {
            "project": registry.get(project_id),
            "memory": registry.memory(project_id),
            "tasks": tasks.list(project_id),
        }

    @app.get("/api/projects/{project_id}/brief", dependencies=[Depends(require_owner)])
    def memory_brief(project_id: str):
        scope(project_id)
        snapshot = memory.snapshot(project_id)
        return {**snapshot, "review_records": [
            {"reference": ref, **memory.get(project_id, ref)} for ref in reversed(snapshot["reviews"])
        ]}

    @app.get("/api/projects/{project_id}/brief/version", dependencies=[Depends(require_owner)])
    def memory_version(project_id: str, reference: str):
        scope(project_id)
        snapshot = memory.snapshot(project_id)
        if reference not in snapshot["versions"]:
            raise ValueError("Unknown version in this project")
        return memory.get(project_id, reference)

    @app.post("/api/projects/{project_id}/brief/review", dependencies=[Depends(require_owner)])
    async def memory_review(project_id: str, body: MemoryStart):
        scope(project_id)
        return reviews.start(project_id, body.confirmed)

    @app.post("/api/projects/{project_id}/brief/cancel", dependencies=[Depends(require_owner)])
    async def memory_cancel(project_id: str, body: MemoryCancel):
        scope(project_id)
        return await reviews.cancel(project_id, body.review)

    @app.post("/api/projects/{project_id}/brief/apply", dependencies=[Depends(require_owner)])
    def memory_apply(project_id: str, body: MemoryApply):
        scope(project_id)
        return memory.apply(project_id, body.review, body.revision, body.resolutions)

    @app.post("/api/projects/{project_id}/brief/rollback", dependencies=[Depends(require_owner)])
    def memory_rollback(project_id: str, body: MemoryRollback):
        scope(project_id)
        return memory.rollback(project_id, body.version, body.revision)

    @app.post("/api/chat", dependencies=[Depends(require_owner)])
    async def submit(body: MessageBody):
        body.project_id = scope(body.project_id)
        return await chat.submit(
            body.project_id, body.content, body.conversation_id, request_id=body.request_id,
            attachment_ids=body.attachment_ids, todos=body.todos
        )

    @app.patch("/api/tasks/{task_id}", dependencies=[Depends(require_owner)])
    async def task_control(task_id: str, body: TaskControl):
        return await core.control(task_id, body.action, body.revision)

    @app.get("/api/tasks/{task_id}/execution-records", dependencies=[Depends(require_owner)])
    def records(task_id: str, offset: int = 0, before_event: int | None = None, before_execution: str | None = None):
        # Tables and events from earlier eras stay readable; nothing writes them any more.
        historical = {
            "historical_" + label: db.all(f"SELECT * FROM {table} WHERE task_id=?", (task_id,))
            if db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
            else []
            for label, table in (("verification", "verification_results"), ("integration", "integration_results"))
        }
        return {
            "task": tasks.get(task_id),
            "checks": db.all(
                "SELECT payload,created_at FROM events WHERE task_id=? AND kind IN ('task.check','task.round') ORDER BY seq DESC LIMIT 20",
                (task_id,),
            ),
            "evidence": db.all(
                "SELECT payload,created_at FROM events WHERE task_id=? AND kind='task.evidence' ORDER BY seq DESC LIMIT 5",
                (task_id,),
            ),
            **execution_page(db, task_id=task_id, offset=offset, before=before_execution),
            **event_page(db, task_id=task_id, before=before_event),
            **historical,
        }

    @app.get('/api/execution-history', dependencies=[Depends(require_owner)])
    def execution_history(project_id: str | None = None, offset: int = 0, before_execution: str | None = None):
        project_id = scope(project_id)
        return execution_page(db, project_id=project_id, offset=offset, before=before_execution)

    @app.get('/api/executions/{execution_id}/history', dependencies=[Depends(require_owner)])
    def worker_history(execution_id: str, before_event: int | None = None, output_offset: int | None = None):
        row = db.one('SELECT id,history_preview(output) AS result_summary FROM executions WHERE id=?', (execution_id,))
        if not row:
            raise ValueError('Unknown execution')
        result = {**row, **event_page(db, execution_id=execution_id, before=before_event)}
        if output_offset is not None:
            if output_offset < 0:
                raise ValueError('Offset must be nonnegative')
            full = db.one('SELECT output FROM executions WHERE id=?', (execution_id,))['output']
            result.update(output=full[output_offset:output_offset + 12000],
                          next_output_offset=output_offset + 12000 if len(full) > output_offset + 12000 else None)
        return result

    @app.post("/api/executions/{execution_id}/interrupt", dependencies=[Depends(require_owner)])
    async def interrupt(execution_id: str):
        execution = db.one("SELECT task_id FROM executions WHERE id=?", (execution_id,))
        if execution and execution["task_id"]:
            await core.cancel(execution_id)
        else:
            await chat.interrupt(execution_id)
        return {"result": "accepted"}

    @app.post("/api/questions/{question_id}/answer", dependencies=[Depends(require_owner)])
    def answer(question_id: str, body: AnswerBody):
        with db.transaction() as conn:
            question = conn.execute(
                "SELECT q.* FROM questions q JOIN executions e ON e.id=q.execution_id WHERE q.id=? AND q.status='pending' AND e.finished_at IS NULL AND e.stop_requested=0",
                (question_id,),
            ).fetchone()
            if not question:
                raise ValueError("Question is no longer pending")
            if question["kind"] == "item/tool/requestUserInput":
                required = {x["id"] for x in json.loads(question["request"])["questions"]}
                if set(body.answers) != required or not all(
                    isinstance(x, str) and x.strip() and len(x) <= 10000 for x in body.answers.values()
                ):
                    raise ValueError("Provide an answer to each question")
            elif set(body.answers) != {"approve"} or not isinstance(body.answers["approve"], bool):
                raise ValueError("Choose approve or decline for the exact action")
            conn.execute(
                "UPDATE questions SET answer=?,status='answered' WHERE id=?", (json.dumps(body.answers), question_id)
            )
            db.event(conn, "question.answered", {"question_id": question_id}, execution_id=question["execution_id"])
        return {"result": "accepted"}

    @app.get("/api/settings", dependencies=[Depends(require_owner)])
    def get_settings():
        from .model_settings import active
        from .isolation import status as worker_isolation
        return {"global_instructions": db.setting("global_instructions", ""),
                "model_provider": active(db), "worker_isolation": worker_isolation()}

    static = Path(__file__).parent / "static"
    # Pin content and URLs to this process/release; never cache mutable aliases.
    assets = {}
    index_html = (static / "index.html").read_text()
    for path in static.rglob("*"):
        if path.suffix not in {".js", ".css"}:
            continue
        name = path.relative_to(static).as_posix()
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        assets[(digest, name)] = content
        index_html = index_html.replace(f'"/static/{name}"', f'"/static/assets/{digest}/{name}"')

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(static / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/static/assets/{digest}/{name:path}")
    def versioned_asset(digest: str, name: str):
        content = assets.get((digest, name))
        if content is None:
            raise HTTPException(404, "Unknown asset version")
        return Response(content, media_type="text/css" if name.endswith(".css") else "text/javascript",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app

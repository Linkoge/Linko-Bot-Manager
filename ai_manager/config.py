import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("AI_MANAGER_DATA", "~/.local/share/linko-bot-manager")).expanduser()
    )
    project_roots: tuple[Path, ...] = field(
        default_factory=lambda: tuple(
            Path(p).expanduser() for p in os.getenv("AI_MANAGER_PROJECT_ROOTS", "~/projects").split(os.pathsep)
        )
    )
    # Restricted "work" instance: exactly one fixed project, no deploy, self-edit or host-agent actions.
    restricted: bool = field(default_factory=lambda: os.getenv("AI_MANAGER_RESTRICTED", "0") == "1")
    project_path: Path | None = field(
        default_factory=lambda: Path(p).expanduser() if (p := os.getenv("AI_MANAGER_PROJECT")) else None
    )
    port: int = field(default_factory=lambda: int(os.getenv("AI_MANAGER_PORT", "8787")))
    # Ignored KEY=VALUE credentials for the Add User screen (Tailscale API key, Cloudflare/Syncthing tokens); outside git.
    onboarding_config: Path = field(
        default_factory=lambda: Path(os.getenv("AI_MANAGER_ONBOARDING_CONFIG", "~/.config/linko-onboarding.env")).expanduser()
    )
    codex_bin: str = field(default_factory=lambda: os.getenv("AI_MANAGER_CODEX", "codex"))
    # Services often lack ~/.local/bin on PATH, where the Claude Code installer puts it.
    claude_bin: str = field(default_factory=lambda: os.getenv("AI_MANAGER_CLAUDE") or shutil.which("claude")
                            or str(Path.home() / ".local/bin/claude"))
    secure_cookie: bool = field(default_factory=lambda: os.getenv("AI_MANAGER_SECURE_COOKIE", "1") == "1")
    # Audio-only external provider; secrets never go through SQLite/browser settings.
    xai_api_key: str = field(default_factory=lambda: os.getenv("XAI_API_KEY", ""), repr=False)
    audio_provider: str = field(default_factory=lambda: os.getenv("AI_MANAGER_AUDIO_PROVIDER", "grok"))
    audio_model: str = field(default_factory=lambda: os.getenv("AI_MANAGER_AUDIO_MODEL", "grok-voice-transcribe-2.0"))
    audio_probe: str = field(default_factory=lambda: os.getenv("AI_MANAGER_AUDIO_PROBE", "ffprobe"))
    # Separate GitHub classic tokens, outside every repository. The legacy path remains Personal.
    github_token_file: Path | None = field(
        default_factory=lambda: Path(p).expanduser() if (p := os.getenv("AI_MANAGER_GITHUB_TOKEN_FILE")) else None
    )
    github_company_token_file: Path | None = field(
        default_factory=lambda: Path(p).expanduser() if (p := os.getenv("AI_MANAGER_GITHUB_COMPANY_TOKEN_FILE")) else None
    )
    github_sync_seconds: int = field(default_factory=lambda: int(os.getenv("AI_MANAGER_GITHUB_SYNC_SECONDS", "300")))
    session_seconds: int = field(default_factory=lambda: int(os.getenv("AI_MANAGER_SESSION_SECONDS", "2592000")))
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            os.getenv("AI_MANAGER_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
        )
    )

    # Public tunnel hostname(s) always accepted next to AI_MANAGER_ALLOWED_HOSTS (the Tailscale name).
    # Configuration only: there is no built-in public host.
    public_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(os.getenv("AI_MANAGER_PUBLIC_HOSTS", "").split(","))
    )
    # Direct peers whose CF-Connecting-IP / X-Forwarded-For / X-Forwarded-Proto are believed (IPs or CIDRs).
    trusted_proxies: tuple[str, ...] = field(
        default_factory=lambda: tuple(os.getenv("AI_MANAGER_TRUSTED_PROXIES", "127.0.0.1,::1").split(","))
    )
    # Allow-list of client addresses (IPs/CIDRs). Set: every other client gets 403 before auth (loopback health checks stay
    # allowed); unset: no client restriction. The Add User step writes the enrolled PC's Tailscale IP here.
    allowed_clients: tuple[str, ...] = field(
        default_factory=lambda: tuple(os.getenv("AI_MANAGER_ALLOWED_CLIENTS", "").split(","))
    )

    def __post_init__(self):
        self.allowed_clients = tuple(c.strip() for c in self.allowed_clients if c.strip())
        hosts = [h.strip() for h in (*self.allowed_hosts, *self.public_hosts) if h.strip()]
        self.allowed_hosts = tuple(dict.fromkeys(hosts))
        if not self.restricted:
            return
        if not self.project_path or not self.project_path.is_dir():
            raise ValueError("Restricted mode needs AI_MANAGER_PROJECT: an existing project directory")
        project, data = self.project_path.resolve(), self.data_dir.expanduser().resolve()
        for other in (CODE_ROOT, data):  # the app's own code is never a restricted project (no self-edit)
            if project.is_relative_to(other) or other.is_relative_to(project):
                raise ValueError("Restricted project must not contain or sit inside the application code or data directory")
        self.project_path, self.project_roots = project, (project,)

    @property
    def fixed_project(self) -> Path | None:
        return self.project_path if self.restricted else None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "manager.sqlite3"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)

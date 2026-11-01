import json
import shutil
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .history import SCHEMA as HISTORY_SCHEMA
from .history import install as install_history

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, repository TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '', objective TEXT NOT NULL DEFAULT '', success_criteria TEXT NOT NULL DEFAULT '',
 instructions TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 50,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','archived','removed')),
 agent_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), description TEXT NOT NULL,
 acceptance TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 blocker TEXT NOT NULL DEFAULT '', lease_owner TEXT, revision INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS executions(
 id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), project_id TEXT REFERENCES projects(id),
 agent_id TEXT NOT NULL, thread_id TEXT, turn_id TEXT, pid INTEGER, process_start TEXT,
 worktree TEXT, branch TEXT, commit_ref TEXT, model TEXT, effort TEXT,
 status TEXT NOT NULL, output TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 started_at REAL NOT NULL, finished_at REAL, latency_ms REAL, usage TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS memory(
 project_id TEXT REFERENCES projects(id), name TEXT NOT NULL, content TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY(project_id,name));
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, project_id TEXT, task_id TEXT,
 execution_id TEXT, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owner(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY, context TEXT NOT NULL UNIQUE, project_id TEXT REFERENCES projects(id),
 thread_id TEXT, summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), role TEXT NOT NULL,
 content TEXT NOT NULL, status TEXT NOT NULL, execution_id TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_execution ON events(execution_id, seq);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,created_at);
CREATE TABLE IF NOT EXISTS chat_requests(
 owner_id INTEGER NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 receipt TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(owner_id,request_id));
CREATE TABLE IF NOT EXISTS todos(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 details TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'ready',
 source_task_id TEXT UNIQUE REFERENCES tasks(id), created_at REAL NOT NULL, updated_at REAL NOT NULL,
 archived_at REAL);
CREATE INDEX IF NOT EXISTS todos_project ON todos(project_id,archived_at);
CREATE TABLE IF NOT EXISTS project_ideas(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, details TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chat_archives(
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
 title TEXT NOT NULL, archived_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS onboarding(
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, email TEXT NOT NULL, windows_folder TEXT NOT NULL,
 domain TEXT NOT NULL DEFAULT '', memory_max TEXT NOT NULL DEFAULT '1G',
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','enrolling','provisioning','done','failed')),
 auto_apply INTEGER NOT NULL DEFAULT 0, device_id TEXT NOT NULL DEFAULT '', pc_hostname TEXT NOT NULL DEFAULT '',
 tailscale_ip TEXT NOT NULL DEFAULT '', token_hash TEXT, token_expires_at REAL,
 pending_action TEXT CHECK(pending_action IN ('plan','apply')), requested_at REAL,
 steps TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', login_secret TEXT, port INTEGER,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.executescript(HISTORY_SCHEMA)
            if "port" not in {r[1] for r in conn.execute("PRAGMA table_info(onboarding)")}:
                conn.execute("ALTER TABLE onboarding ADD COLUMN port INTEGER")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
            for name, definition in (("display_name", "TEXT"), ("last_used_at", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
            if not conn.execute("SELECT 1 FROM schema_version WHERE version=4").fetchone():
                conn.execute("""UPDATE projects SET last_used_at=COALESCE(
                    (SELECT MAX(created_at) FROM events WHERE events.project_id=projects.id
                     AND kind IN ('chat.queued','task.created','task.claimed','worker.queued','plan.applied','manager.action')),0)""")
                conn.execute("INSERT INTO schema_version VALUES(4)")
            # Global context has no project row. Seed its durable recency once from
            # accepted global submissions without altering project history.
            if not conn.execute("SELECT 1 FROM settings WHERE key='global_last_used_at'").fetchone():
                conn.execute("""INSERT INTO settings(key,value)
                    SELECT 'global_last_used_at', CAST(COALESCE(MAX(created_at),0) AS TEXT)
                    FROM events WHERE project_id IS NULL AND kind='chat.queued'""")
            task_columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            for name, definition in (
                ("origin_conversation", "TEXT"),
                ("request_key", "TEXT"),
                ("native_thread", "TEXT"),
                ("workspace", "TEXT"),
                ("workspace_branch", "TEXT"),
                ("workspace_base", "TEXT"),
                ("chain_root", "TEXT"),  # the owner-typed request this task belongs to; set once, never reset
                ("chain_parent", "TEXT"),  # the task whose result notice caused this automatic follow-up
            ):
                if name not in task_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            conn.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS task_request ON tasks(request_key) WHERE request_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS questions(
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id),
                    request TEXT NOT NULL, kind TEXT NOT NULL, answer TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL);
            """)
            execution_columns = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
            for name, definition in (
                ("delivery", "TEXT"),
                ("delivery_execution", "TEXT"),
                ("conversation_id", "TEXT"),
                ("input", "TEXT NOT NULL DEFAULT ''"),
                ("retry_at", "REAL"),
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("recovery_reason", "TEXT NOT NULL DEFAULT ''"),
                ("launch_input", "TEXT"),
                ("launch_key", "TEXT"),
                ("stop_requested", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in execution_columns:
                    conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {definition}")
            if not conn.execute("SELECT 1 FROM schema_version WHERE version=9").fetchone():
                conn.execute("""UPDATE executions SET
                    launch_input=(SELECT json_extract(history_text(payload),'$.prompt') FROM events
                        WHERE execution_id=executions.id AND kind='model.context' ORDER BY seq DESC LIMIT 1)
                    WHERE finished_at IS NULL""")
                conn.execute("INSERT INTO schema_version VALUES(9)")
            if not conn.execute("SELECT 1 FROM schema_version WHERE version=11").fetchone():
                from .todos import migrate_deferred
                migrate_deferred(conn)
                conn.execute("INSERT INTO schema_version VALUES(11)")
            self.migrate_queue(conn, task_columns)
            if not conn.execute("SELECT 1 FROM schema_version WHERE version=16").fetchone():
                from .model_settings import migrate_repo_locks
                migrate_repo_locks(conn)
                conn.execute('INSERT INTO schema_version VALUES(16)')
            if not conn.execute("SELECT 1 FROM schema_version WHERE version=17").fetchone():
                from .todos import migrate_kind
                migrate_kind(conn)
                conn.execute("INSERT INTO schema_version VALUES(17)")
            conn.commit()
        self.path.chmod(0o600)

    @staticmethod
    def migrate_queue(conn, task_columns):
        """Schema 13: the per-project task queues; 14: the Auto-deploy setting is retired; 15: unused columns are dropped.
        Old columns (lifecycle, legacy, ...) stay as ignored history.

        Old non-final tasks are closed once (failed, reason says cancelled); branches, workspaces and rows are kept.
        """
        added = {
            "state": "TEXT NOT NULL DEFAULT 'queued'", "held": "INTEGER NOT NULL DEFAULT 0",
            "queued_at": "REAL NOT NULL DEFAULT 0", "rounds": "INTEGER NOT NULL DEFAULT 0",
            "stage": "TEXT NOT NULL DEFAULT 'work'",
            "requeues": "INTEGER NOT NULL DEFAULT 0", "retry_at": "REAL",
            "gate_history": "TEXT NOT NULL DEFAULT '[]'", "merge": "TEXT",
        }
        have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for name, definition in added.items():
            if name not in have:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        conn.execute("DROP INDEX IF EXISTS project_worker")
        conn.execute("DROP INDEX IF EXISTS workspace_worker")
        if not conn.execute("SELECT 1 FROM schema_version WHERE version=13").fetchone():
            now = time.time()
            has_lifecycle = "lifecycle" in have  # absent on databases created after this rebuild
            conn.execute("UPDATE tasks SET queued_at=created_at")
            if has_lifecycle:
                conn.execute("UPDATE tasks SET state='done' WHERE lifecycle='completed'")
                conn.execute("UPDATE tasks SET state='failed' WHERE lifecycle IN ('failed','cancelled')")
                conn.execute("""UPDATE tasks SET state='failed',blocker=?,lease_owner=NULL,updated_at=?
                    WHERE lifecycle NOT IN ('completed','failed','cancelled')""",
                    ("Cancelled: closed when the task queue was rebuilt (branch and workspace kept)", now))
            conn.execute("""UPDATE executions SET status='interrupted',finished_at=?,
                error='Closed when the task queue was rebuilt'
                WHERE task_id IS NOT NULL AND finished_at IS NULL""", (now,))
            conn.execute("UPDATE questions SET status='expired' WHERE status='pending'")
            conn.execute("UPDATE tasks SET lease_owner=NULL WHERE state!='running'")
            conn.execute("INSERT INTO schema_version VALUES(13)")
        if not conn.execute("SELECT 1 FROM schema_version WHERE version=14").fetchone():
            # Schema 14: passing work always merges and deploys. The old per-project Auto-deploy values are dropped
            # unread; earlier unmerged ('skipped') tasks keep their branches and are never merged retroactively.
            conn.execute("DELETE FROM settings WHERE key LIKE 'auto_deploy:%' OR key='auto_deploy_seed'")
            conn.execute("INSERT INTO schema_version VALUES(14)")
        if not conn.execute("SELECT 1 FROM schema_version WHERE version=15").fetchone():
            # Schema 15: retries are plain (every 5 minutes, no counter), rounds have one fixed cap, a To-Do has no revision.
            for table, names in (("tasks", ("round_limit", "open_round", "checkpoints", "merge_requested", "waits")),
                                 ("todos", ("revision",))):
                present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name in names:
                    if name in present:
                        conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
            conn.execute("INSERT INTO schema_version VALUES(15)")
        conn.execute("UPDATE tasks SET lease_owner=NULL WHERE state IN ('done','failed')")
        conn.execute("DROP INDEX IF EXISTS one_running")  # was one running task in total; now one per project
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_project ON tasks(project_id) WHERE state='running'")
        conn.execute("CREATE INDEX IF NOT EXISTS task_queue ON tasks(state,held,queued_at)")
        for prefix, scope in (('', ''), ('project_', 'project_id,')):
            conn.execute(f"CREATE INDEX IF NOT EXISTS tasks_{prefix}finished ON tasks({scope}updated_at DESC) WHERE state IN ('done','failed')")
            conn.execute(f"CREATE INDEX IF NOT EXISTS executions_{prefix}finished ON executions({scope}finished_at DESC) WHERE finished_at IS NOT NULL")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        install_history(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run(self, sql: str, params=()):
        with self.transaction() as conn:
            conn.execute(sql, params)

    def all(self, sql: str, params=()) -> list[dict]:
        with closing(self.connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def event(conn, kind: str, payload: dict, project_id=None, task_id=None, execution_id=None):
        if project_id and kind in {
            "chat.queued",
            "task.created",
            "worker.queued",
        }:
            conn.execute("UPDATE projects SET last_used_at=MAX(last_used_at,?) WHERE id=?", (time.time(), project_id))
        elif project_id is None and kind == "chat.queued":
            conn.execute(
                """INSERT INTO settings(key,value) VALUES('global_last_used_at',?)
                ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS REAL),CAST(excluded.value AS REAL)) AS TEXT)""",
                (json.dumps(time.time()),),
            )
        conn.execute(
            "INSERT INTO events(kind,project_id,task_id,execution_id,payload,created_at) VALUES(?,?,?,?,?,?)",
            (kind, project_id, task_id, execution_id, json.dumps(payload), time.time()),
        )

    def setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.event(conn, "settings.changed", {"key": key})

    def backup(self, destination: Path):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise ValueError("Backup must use a new destination")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(destination.parent).free < self.path.stat().st_size * 2 + 128 * 1024 * 1024:
            raise ValueError("Insufficient storage for verified backup and 128MB reserve")
        destination.touch(mode=0o600, exist_ok=False)
        try:
            with closing(self.connect()) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Backup foreign key check failed")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def restore(source: Path, destination: Path):
        if destination.exists() or Path(str(destination) + "-wal").exists():
            raise ValueError("Restore only to a fresh path while service is stopped")
        with closing(sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)) as original:
            if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid backup")
            if original.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Backup references missing records")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.touch(mode=0o600, exist_ok=False)
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)

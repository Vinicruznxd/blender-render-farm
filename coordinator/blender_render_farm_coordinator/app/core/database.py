from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DATABASE_SCHEMA_VERSION = 2


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS masters (
    masterId TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT,
    username TEXT,
    version TEXT,
    protocolVersion INTEGER,
    buildId TEXT,
    buildTimestampUtc TEXT,
    lastSeen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    workerId TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown',
    blenderFound INTEGER,
    blenderVersion TEXT,
    gpu TEXT,
    renderDevicePreference TEXT,
    effectiveUseGpu INTEGER,
    currentJobId TEXT,
    currentBlockId TEXT,
    currentFrame INTEGER,
    currentAssignmentId TEXT,
    progress REAL,
    persistentSessionEnabled INTEGER,
    sessionState TEXT,
    sessionQueueDepth INTEGER,
    sessionDrainRequested INTEGER,
    queuedAssignments TEXT,
    lastSeen TEXT,
    lastLog TEXT,
    error TEXT,
    blendAccessible INTEGER,
    outputAccessible INTEGER,
    lastJob TEXT,
    capabilities TEXT,
    createdAt TEXT NOT NULL,
    updatedAt TEXT NOT NULL,
    UNIQUE(ip, port)
);

CREATE TABLE IF NOT EXISTS jobs (
    jobId TEXT PRIMARY KEY,
    jobName TEXT NOT NULL,
    blendFile TEXT NOT NULL,
    outputFolder TEXT NOT NULL,
    outputPattern TEXT NOT NULL,
    startFrame INTEGER NOT NULL,
    endFrame INTEGER NOT NULL,
    blockSize INTEGER NOT NULL,
    format TEXT NOT NULL DEFAULT 'PNG',
    requiredBlenderVersion TEXT,
    preferredBlenderMajor INTEGER,
    versionRoutingPolicy TEXT,
    blendVersionDetected TEXT,
    status TEXT NOT NULL,
    queuePosition INTEGER NOT NULL,
    createdAt TEXT NOT NULL,
    startedAt TEXT,
    finishedAt TEXT,
    submittedByMasterId TEXT,
    submittedByMasterName TEXT,
    submittedByUser TEXT,
    ignoreRenderedFrames INTEGER NOT NULL DEFAULT 1,
    autoAddWorkers INTEGER NOT NULL DEFAULT 1,
    redistributeOfflineBlocks INTEGER NOT NULL DEFAULT 1,
    legacyUseGpu INTEGER NOT NULL DEFAULT 1,
    gpuOnlyWorkers INTEGER NOT NULL DEFAULT 0,
    lastError TEXT,
    totalFrames INTEGER NOT NULL,
    completedFrames INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jobId TEXT NOT NULL,
    frameNumber INTEGER NOT NULL,
    status TEXT NOT NULL,
    workerId TEXT,
    assignmentId TEXT,
    leasedAt TEXT,
    leaseExpiresAt TEXT,
    startedAt TEXT,
    finishedAt TEXT,
    attemptCount INTEGER NOT NULL DEFAULT 0,
    errorMessage TEXT,
    lastFailedWorkerId TEXT,
    lastFailedAt TEXT,
    lastFailedRenderDevicePreference TEXT,
    lastFailureReason TEXT,
    baselineExists INTEGER NOT NULL DEFAULT 0,
    baselineSize INTEGER,
    baselineMtimeNs INTEGER,
    outputPath TEXT NOT NULL,
    UNIQUE(jobId, frameNumber),
    FOREIGN KEY(jobId) REFERENCES jobs(jobId) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS job_worker_stats (
    jobId TEXT NOT NULL,
    workerId TEXT NOT NULL,
    renderDevicePreference TEXT NOT NULL DEFAULT '',
    successCount INTEGER NOT NULL DEFAULT 0,
    timedSuccessCount INTEGER NOT NULL DEFAULT 0,
    totalSuccessRenderSeconds REAL NOT NULL DEFAULT 0,
    lastSuccessRenderSeconds REAL,
    failureCount INTEGER NOT NULL DEFAULT 0,
    consecutiveFailureCount INTEGER NOT NULL DEFAULT 0,
    lastFailureReason TEXT,
    lastFailureAt TEXT,
    lastSuccessAt TEXT,
    PRIMARY KEY(jobId, workerId, renderDevicePreference),
    FOREIGN KEY(jobId) REFERENCES jobs(jobId) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    entityType TEXT,
    entityId TEXT,
    message TEXT NOT NULL,
    details TEXT,
    createdAt TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status, enabled);
CREATE INDEX IF NOT EXISTS idx_jobs_status_queue ON jobs(status, queuePosition);
CREATE INDEX IF NOT EXISTS idx_frames_job_status ON job_frames(jobId, status, frameNumber);
CREATE INDEX IF NOT EXISTS idx_job_worker_stats_job ON job_worker_stats(jobId, workerId);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(createdAt DESC);
"""

MASTER_COLUMN_MIGRATIONS = {
    "protocolVersion": "ALTER TABLE masters ADD COLUMN protocolVersion INTEGER",
    "buildId": "ALTER TABLE masters ADD COLUMN buildId TEXT",
    "buildTimestampUtc": "ALTER TABLE masters ADD COLUMN buildTimestampUtc TEXT",
}

WORKER_COLUMN_MIGRATIONS = {
    "currentAssignmentId": "ALTER TABLE workers ADD COLUMN currentAssignmentId TEXT",
    "lastJob": "ALTER TABLE workers ADD COLUMN lastJob TEXT",
    "persistentSessionEnabled": "ALTER TABLE workers ADD COLUMN persistentSessionEnabled INTEGER",
    "sessionState": "ALTER TABLE workers ADD COLUMN sessionState TEXT",
    "sessionQueueDepth": "ALTER TABLE workers ADD COLUMN sessionQueueDepth INTEGER",
    "sessionDrainRequested": "ALTER TABLE workers ADD COLUMN sessionDrainRequested INTEGER",
    "queuedAssignments": "ALTER TABLE workers ADD COLUMN queuedAssignments TEXT",
}

JOB_COLUMN_MIGRATIONS = {
    "requiredBlenderVersion": "ALTER TABLE jobs ADD COLUMN requiredBlenderVersion TEXT",
    "preferredBlenderMajor": "ALTER TABLE jobs ADD COLUMN preferredBlenderMajor INTEGER",
    "versionRoutingPolicy": "ALTER TABLE jobs ADD COLUMN versionRoutingPolicy TEXT",
    "blendVersionDetected": "ALTER TABLE jobs ADD COLUMN blendVersionDetected TEXT",
    "gpuOnlyWorkers": "ALTER TABLE jobs ADD COLUMN gpuOnlyWorkers INTEGER NOT NULL DEFAULT 0",
}

JOB_FRAME_COLUMN_MIGRATIONS = {
    "lastFailedWorkerId": "ALTER TABLE job_frames ADD COLUMN lastFailedWorkerId TEXT",
    "lastFailedAt": "ALTER TABLE job_frames ADD COLUMN lastFailedAt TEXT",
    "lastFailedRenderDevicePreference": "ALTER TABLE job_frames ADD COLUMN lastFailedRenderDevicePreference TEXT",
    "lastFailureReason": "ALTER TABLE job_frames ADD COLUMN lastFailureReason TEXT",
    "baselineExists": "ALTER TABLE job_frames ADD COLUMN baselineExists INTEGER NOT NULL DEFAULT 0",
    "baselineSize": "ALTER TABLE job_frames ADD COLUMN baselineSize INTEGER",
    "baselineMtimeNs": "ALTER TABLE job_frames ADD COLUMN baselineMtimeNs INTEGER",
}

JOB_WORKER_STATS_COLUMN_MIGRATIONS = {
    "timedSuccessCount": "ALTER TABLE job_worker_stats ADD COLUMN timedSuccessCount INTEGER NOT NULL DEFAULT 0",
    "totalSuccessRenderSeconds": "ALTER TABLE job_worker_stats ADD COLUMN totalSuccessRenderSeconds REAL NOT NULL DEFAULT 0",
    "lastSuccessRenderSeconds": "ALTER TABLE job_worker_stats ADD COLUMN lastSuccessRenderSeconds REAL",
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._lock:
            with self._connect() as connection:
                connection.executescript(SCHEMA_SQL)
                self._ensure_table_columns(connection, "masters", MASTER_COLUMN_MIGRATIONS)
                self._ensure_table_columns(connection, "workers", WORKER_COLUMN_MIGRATIONS)
                self._ensure_table_columns(connection, "jobs", JOB_COLUMN_MIGRATIONS)
                self._ensure_table_columns(connection, "job_frames", JOB_FRAME_COLUMN_MIGRATIONS)
                self._ensure_table_columns(connection, "job_worker_stats", JOB_WORKER_STATS_COLUMN_MIGRATIONS)
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
                connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def executemany(self, sql: str, params: list[tuple] | list[dict]) -> int:
        with self.transaction() as connection:
            cursor = connection.executemany(sql, params)
            return cursor.rowcount

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(sql, params)
                row = cursor.fetchone()
                return dict(row) if row else None

    def fetch_all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _ensure_table_columns(
        connection: sqlite3.Connection,
        table_name: str,
        column_migrations: dict[str, str],
    ) -> None:
        existing_columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, statement in column_migrations.items():
            if column_name in existing_columns:
                continue
            connection.execute(statement)

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.version_routing import validate_version_routing_policy


class CoordinatorBaseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class MasterHeartbeatRequest(CoordinatorBaseModel):
    masterId: str
    masterName: str
    host: str
    username: str
    version: str
    protocolVersion: int | None = None
    buildId: str | None = None
    buildTimestampUtc: str | None = None


class JobCreateRequest(CoordinatorBaseModel):
    jobName: str
    blendFile: str
    outputFolder: str
    outputPattern: str | None = None
    startFrame: int
    endFrame: int
    blockSize: int = 1
    format: str = "PNG"
    ignoreRenderedFrames: bool = True
    autoAddWorkers: bool = True
    redistributeOfflineBlocks: bool = True
    legacyUseGpu: bool = True
    gpuOnlyWorkers: bool = False
    requiredBlenderVersion: str | None = None
    preferredBlenderMajor: int | None = Field(default=None, ge=1)
    versionRoutingPolicy: str | None = None
    blendVersionDetected: str | None = None
    submittedByMasterId: str | None = None
    submittedByMasterName: str | None = None
    submittedByUser: str | None = None

    @field_validator("versionRoutingPolicy")
    @classmethod
    def validate_version_routing_policy_field(cls, value: str | None) -> str | None:
        return validate_version_routing_policy(value)


class JobPreflightRequest(JobCreateRequest):
    pass


class WorkerCreateRequest(CoordinatorBaseModel):
    workerId: str | None = None
    name: str | None = None
    ip: str
    port: int = 9000
    enabled: bool = True
    renderDevicePreference: str | None = None


class WorkerUpdateRequest(CoordinatorBaseModel):
    name: str | None = None
    ip: str | None = None
    port: int | None = None
    enabled: bool | None = None
    renderDevicePreference: str | None = None
    status: str | None = None
    error: str | None = None


class WorkerRenderReportRequest(CoordinatorBaseModel):
    workerId: str | None = None
    machineName: str | None = None
    ip: str | None = None
    port: int | None = None
    status: str | None = None
    currentJobId: str | None = None
    currentBlockId: str | None = None
    currentFrame: int | None = None
    currentAssignmentId: str | None = None
    progress: float | None = None
    lastLog: str | None = None
    error: str | None = None
    persistentSessionEnabled: bool | None = None
    sessionState: str | None = None
    sessionQueueDepth: int | None = None
    sessionDrainRequested: bool | None = None
    queuedAssignments: list[dict[str, Any]] | None = None
    coordinatorId: str | None = None
    coordinatorApiUrl: str | None = None
    reportedAt: str | None = None
    lastJob: dict[str, Any] | None = None


class WorkerDiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    hosts: list[str] | None = None
    ports: list[int] | None = None


class LogsResponse(CoordinatorBaseModel):
    general: str
    events: list[dict[str, Any]] = Field(default_factory=list)


class LanguageUpdateRequest(BaseModel):
    language: str

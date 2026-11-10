from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .version_routing import validate_version_routing_policy

ALLOWED_RENDER_FORMATS = {
    "BMP",
    "CINEON",
    "DPX",
    "IRIS",
    "JPEG",
    "OPEN_EXR",
    "PNG",
    "TARGA",
    "TARGA_RAW",
    "TIFF",
    "WEBP",
}


class HealthResponse(BaseModel):
    ok: bool
    workerId: str | None = None
    machineName: str
    status: str
    appVersion: str
    protocolVersion: int
    buildId: str
    buildTimestampUtc: str


class ValidatePathsRequest(BaseModel):
    blendFile: str
    outputFolder: str
    requiredBlenderVersion: str | None = None
    preferredBlenderMajor: int | None = None
    versionRoutingPolicy: str | None = None
    blendVersionDetected: str | None = None

    @field_validator("blendFile")
    @classmethod
    def validate_blend_file(cls, value: str) -> str:
        if not value.strip().lower().endswith(".blend"):
            raise ValueError("blendFile must end with .blend")
        return value.strip()

    @field_validator("outputFolder")
    @classmethod
    def validate_output_folder(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("outputFolder cannot be empty")
        return cleaned

    @field_validator("versionRoutingPolicy")
    @classmethod
    def validate_version_routing_policy_field(cls, value: str | None) -> str | None:
        return validate_version_routing_policy(value)


class ValidatePathsResponse(BaseModel):
    ok: bool
    blendAccessible: bool
    outputAccessible: bool
    message: str
    normalizedBlendFile: str
    normalizedOutputFolder: str
    blendExists: bool
    outputExists: bool
    outputCreated: bool = False
    processUser: str = ""
    cwd: str = ""
    details: str = ""
    blendFileExists: bool
    outputFolderExists: bool
    canWriteOutput: bool
    blendVersionDetected: str = ""
    versionSource: str = ""
    blendHeaderFormat: str = ""
    blendCompression: str = ""
    requiredBlenderVersion: str = ""
    preferredBlenderMajor: int | None = None
    versionRoutingPolicy: str = ""
    selectedBlenderVersion: str = ""
    selectedBlenderPath: str = ""
    selectedVersionBucket: str = ""
    sameMajorSufficientAvailable: bool = False
    sameMajorOlderAvailable: bool = False
    bestSameMajorOlderVersion: str = ""
    higherMajorAvailable: bool = False
    bestHigherMajorVersion: str = ""
    compatible: bool = False
    compatibilityReason: str = ""
    errors: list[str] = Field(default_factory=list)


class RenderBlockRequest(BaseModel):
    jobId: str
    blockId: str
    blendFile: str
    outputPattern: str
    startFrame: int
    endFrame: int
    format: str = "PNG"
    useGpu: bool | None = None
    assignmentId: str | None = None
    coordinatorId: str | None = None
    submittedByMasterId: str | None = None
    submittedByMasterName: str | None = None
    requiredBlenderVersion: str | None = None
    preferredBlenderMajor: int | None = None
    versionRoutingPolicy: str | None = None
    blendVersionDetected: str | None = None

    @field_validator(
        "jobId",
        "blockId",
        "blendFile",
        "outputPattern",
        "assignmentId",
        "coordinatorId",
        "submittedByMasterId",
        "submittedByMasterName",
    )
    @classmethod
    def validate_required_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field cannot be empty")
        return cleaned

    @field_validator("blendFile")
    @classmethod
    def validate_blend_extension(cls, value: str) -> str:
        if not value.lower().endswith(".blend"):
            raise ValueError("blendFile must end with .blend")
        return value

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in ALLOWED_RENDER_FORMATS:
            raise ValueError(
                f"format must be one of: {', '.join(sorted(ALLOWED_RENDER_FORMATS))}"
            )
        return normalized

    @model_validator(mode="after")
    def validate_frames(self) -> "RenderBlockRequest":
        if self.startFrame <= 0 or self.endFrame <= 0:
            raise ValueError("startFrame and endFrame must be positive integers")
        if self.startFrame > self.endFrame:
            raise ValueError("startFrame must be less than or equal to endFrame")
        return self

    @field_validator("versionRoutingPolicy")
    @classmethod
    def validate_render_policy_field(cls, value: str | None) -> str | None:
        return validate_version_routing_policy(value)


class RenderAcceptedResponse(BaseModel):
    accepted: bool
    ok: bool | None = None
    jobId: str | None = None
    blockId: str | None = None
    status: str | None = None
    renderDevicePreference: str | None = None
    effectiveUseGpu: bool | None = None
    error: str | None = None
    message: str | None = None
    details: list[str] | None = None
    currentJobId: str | None = None
    currentBlockId: str | None = None
    assignmentId: str | None = None
    coordinatorId: str | None = None
    submittedByMasterId: str | None = None
    submittedByMasterName: str | None = None
    blendVersionDetected: str | None = None
    versionSource: str | None = None
    blendHeaderFormat: str | None = None
    blendCompression: str | None = None
    requiredBlenderVersion: str | None = None
    preferredBlenderMajor: int | None = None
    versionRoutingPolicy: str | None = None
    selectedBlenderVersion: str | None = None
    selectedBlenderPath: str | None = None
    selectedVersionBucket: str | None = None
    sameMajorSufficientAvailable: bool | None = None
    sameMajorOlderAvailable: bool | None = None
    bestSameMajorOlderVersion: str | None = None
    higherMajorAvailable: bool | None = None
    bestHigherMajorVersion: str | None = None
    compatible: bool | None = None
    compatibilityReason: str | None = None
    queueDepth: int | None = None
    sessionState: str | None = None
    drainRequested: bool | None = None


class CancelResponse(BaseModel):
    cancelled: bool
    cleanupPending: bool | None = None
    previousJobId: str | None = None
    previousBlockId: str | None = None
    message: str | None = None
    currentJobId: str | None = None
    currentBlockId: str | None = None
    assignmentId: str | None = None
    terminatedSession: bool | None = None
    cancelledAssignmentIds: list[str] | None = None


class CancelRequest(BaseModel):
    jobId: str | None = None
    assignmentId: str | None = None

    @field_validator("jobId", "assignmentId")
    @classmethod
    def validate_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field cannot be empty")
        return cleaned


class JobDrainRequest(BaseModel):
    jobId: str
    coordinatorId: str | None = None

    @field_validator("jobId", "coordinatorId")
    @classmethod
    def validate_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field cannot be empty")
        return cleaned


class JobDrainResponse(BaseModel):
    accepted: bool
    jobId: str | None = None
    sessionState: str | None = None
    queueDepth: int | None = None
    drainRequested: bool | None = None
    message: str | None = None


class LogsResponse(BaseModel):
    logs: list[str]


class RenderDevicePreferenceRequest(BaseModel):
    renderDevicePreference: str

    @field_validator("renderDevicePreference")
    @classmethod
    def validate_render_device_preference(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CPU", "GPU"}:
            raise ValueError("renderDevicePreference must be CPU or GPU")
        return normalized


class RenderDevicePreferenceResponse(BaseModel):
    renderDevicePreference: str
    effectiveUseGpu: bool
    saved: bool = True

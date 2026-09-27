"""Only model fields used by the tagger; tolerate additional Immich metadata."""
from typing import Literal
from pydantic import BaseModel, Field


class Tag(BaseModel):
    id: str
    name: str
    value: str = ""
    parentId: str | None = None

    @property
    def path(self):
        return self.value or self.name


class Asset(BaseModel):
    id: str
    type: str
    originalPath: str = ""
    originalFileName: str = ""
    libraryId: str | None = None
    ownerId: str = ""
    tags: list[Tag] | None = None
    isOffline: bool = False
    isTrashed: bool = False


class TagPrediction(BaseModel):
    name: str
    confidence: float = Field(ge=0, le=1)
    kind: Literal["general", "character", "rating"] = "general"


class AssetProcessingResult(BaseModel):
    asset_id: str
    success: bool = False
    status: Literal["processed", "skipped", "planned", "failed"] = "failed"
    tags_assigned: list[str] = Field(default_factory=list)
    processing_time: float = 0
    error: str | None = None


class RunResult(BaseModel):
    attempted: int = 0
    processed: int = 0
    planned: int = 0
    failed: int = 0
    skipped: int = 0

"""Request and response shapes of the file explorer.

Guest paths are only ever in these bodies. `request_security` answers 404 for a `path` query that
looks like /etc/shadow, .ssh or anything ending .conf, which is ordinary here, and the access log
records query strings.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

OnConflict = Literal["refuse", "replace"]


class PathBody(BaseModel):
    path: str = Field(min_length=1, max_length=8192)


class PathsBody(BaseModel):
    paths: List[str] = Field(min_length=1, max_length=1000)


class FileEntry(BaseModel):
    name: str
    type: str
    size: int
    mode: int
    uid: Optional[int] = None
    gid: Optional[int] = None
    owner: Optional[str] = None
    group: Optional[str] = None
    mtime: Optional[int] = None
    link_target: Optional[str] = None
    target_type: Optional[str] = None


class ListResponse(BaseModel):
    path: str
    entries: List[FileEntry]
    truncated: bool
    dropped: int
    stale_uploads: List[str]
    free_bytes: Optional[int] = None
    total_bytes: Optional[int] = None


class StatResponse(BaseModel):
    entry: FileEntry
    realpath: str


class SummaryResponse(BaseModel):
    files: int
    folders: int
    bytes: int
    complete: bool


class ReadTextResponse(BaseModel):
    path: str
    text: str
    etag: str
    size: int
    read_only: bool


class WriteTextBody(BaseModel):
    path: str = Field(min_length=1, max_length=8192)
    text: str = Field(max_length=2_000_000)
    etag: str = Field(min_length=64, max_length=64)
    allow_in_place: bool = False


class WriteTextResponse(BaseModel):
    path: str
    etag: str
    size: int


class MakeDirectoryBody(BaseModel):
    path: str = Field(min_length=1, max_length=8192)
    parents: bool = False


class RenameBody(BaseModel):
    source: str = Field(min_length=1, max_length=8192)
    target: str = Field(min_length=1, max_length=8192)
    on_conflict: OnConflict = "refuse"


class ChmodBody(BaseModel):
    path: str = Field(min_length=1, max_length=8192)
    mode: int = Field(ge=0, le=0o7777)
    recursive: bool = False


class ChownBody(BaseModel):
    path: str = Field(min_length=1, max_length=8192)
    uid: int = Field(ge=0, le=4_294_967_294)
    gid: int = Field(ge=0, le=4_294_967_294)
    recursive: bool = False


class AttributesResponse(BaseModel):
    changed: int
    skipped: int
    remaining: bool


class DeleteBody(BaseModel):
    path: str = Field(min_length=1, max_length=8192)
    recursive: bool = False
    op_id: Optional[str] = Field(default=None, max_length=64)


class DeleteResponse(BaseModel):
    deleted: int
    failed: List[str]
    failed_count: int
    remaining: bool


class PurgeResponse(BaseModel):
    removed: int


class DownloadBody(BaseModel):
    paths: List[str] = Field(min_length=1, max_length=1000)
    archive: bool = False


class DownloadResponse(BaseModel):
    url: str
    expires_in: int
    name: str
    size: Optional[int] = None


class CreateUploadBody(BaseModel):
    dest: str = Field(min_length=1, max_length=8192)
    size: int = Field(ge=0)
    on_conflict: OnConflict = "refuse"


class CreateUploadResponse(BaseModel):
    token: str
    chunk_hint: int
    expires_at: int


class UploadTokenBody(BaseModel):
    token: str = Field(min_length=1, max_length=32768)


class UploadStatusResponse(BaseModel):
    temp_size: int
    expires_at: int


class CompleteUploadBody(UploadTokenBody):
    on_conflict: Optional[OnConflict] = None


class CompleteUploadResponse(StatResponse):
    already_committed: bool


class BundleResult(BaseModel):
    path: str
    error: Optional[str] = None


class BundleResponse(BaseModel):
    results: List[BundleResult]

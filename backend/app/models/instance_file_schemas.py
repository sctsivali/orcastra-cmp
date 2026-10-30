"""Request and response shapes of the file explorer.

Guest paths are only ever in these bodies. `request_security` answers 404 for a `path` query that
looks like /etc/shadow, .ssh or anything ending .conf, which is ordinary here, and the access log
records query strings.
"""

from typing import Annotated, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, Field

OnConflict = Literal["refuse", "replace"]
MAX_PATH_CHARS = 8192
# Past this a request is not a selection a person made. It also bounds a download ticket, which is
# held in the same Redis as every tenant's cache and WebSocket tickets.
MAX_SELECTION_BYTES = 128 * 1024


def _no_nul(value: str) -> str:
    # important: at the schema, before anything is audited. Postgres refuses \u0000 in jsonb, so
    # one such path in an audit record would take a whole journal batch down with it.
    if "\x00" in value:
        raise ValueError("a path cannot contain a null byte")
    return value


GuestPath = Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS), AfterValidator(_no_nul)]


def _bounded_selection(paths: List[str]) -> List[str]:
    if sum(len(path.encode("utf-8", "surrogatepass")) for path in paths) > MAX_SELECTION_BYTES:
        raise ValueError("this selection is too large")
    return paths


Selection = Annotated[List[GuestPath], Field(min_length=1, max_length=1000), AfterValidator(_bounded_selection)]


class PathBody(BaseModel):
    path: GuestPath


class PathsBody(BaseModel):
    paths: Selection


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
    path: GuestPath
    text: str = Field(max_length=2_000_000)
    etag: str = Field(min_length=64, max_length=64)
    allow_in_place: bool = False


class WriteTextResponse(BaseModel):
    path: str
    etag: str
    size: int


class MakeDirectoryBody(BaseModel):
    path: GuestPath
    parents: bool = False


class RenameBody(BaseModel):
    source: GuestPath
    target: GuestPath
    on_conflict: OnConflict = "refuse"


class ChmodBody(BaseModel):
    path: GuestPath
    mode: int = Field(ge=0, le=0o7777)
    recursive: bool = False


class ChownBody(BaseModel):
    path: GuestPath
    uid: int = Field(ge=0, le=4_294_967_294)
    gid: int = Field(ge=0, le=4_294_967_294)
    recursive: bool = False


class AttributesResponse(BaseModel):
    changed: int
    skipped: int
    remaining: bool


class DeleteBody(BaseModel):
    path: GuestPath
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
    paths: Selection
    archive: bool = False


class DownloadResponse(BaseModel):
    url: str
    expires_in: int
    name: str
    size: Optional[int] = None


class CreateUploadBody(BaseModel):
    dest: GuestPath
    size: int = Field(ge=0)
    on_conflict: OnConflict = "refuse"


class CreateUploadResponse(BaseModel):
    token: str
    chunk_hint: int
    expires_at: int


class UploadTokenBody(BaseModel):
    token: str = Field(min_length=1, max_length=32768, pattern=r"^[A-Za-z0-9_\-=.]+$")


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

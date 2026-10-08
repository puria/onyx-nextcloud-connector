"""Folder browsing for the Nextcloud connector.

Lets the admin UI walk a Nextcloud account folder by folder when configuring
which folders to index, instead of typing paths by hand. Uses the stored
credential (never returns secrets to the client).
"""

from fastapi import APIRouter, Depends, HTTPException
from onyx.auth.permissions import require_permission
from onyx.configs.constants import PUBLIC_API_TAGS
from onyx.connectors.nextcloud.client import (
    NextcloudAuthError,
    NextcloudError,
    NextcloudWebDAVClient,
)
from onyx.db.credentials import fetch_credential_by_id
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.utils.logger import setup_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

logger = setup_logger()

router = APIRouter(prefix="/manage", tags=PUBLIC_API_TAGS)


class NextcloudBrowseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: int
    path: str = ""
    verify_ssl: bool = True


class NextcloudFolder(BaseModel):
    name: str
    path: str


class NextcloudBrowseResponse(BaseModel):
    path: str
    folders: list[NextcloudFolder]
    file_count: int


@router.post("/admin/nextcloud/browse")
def browse_nextcloud_folders(
    request: NextcloudBrowseRequest,
    _: User = Depends(require_permission(Permission.MANAGE_CONNECTORS, allow_scope=True)),
    db_session: Session = Depends(get_session),
) -> NextcloudBrowseResponse:
    """List the immediate subfolders of `path` in the credential's Nextcloud."""
    credential = fetch_credential_by_id(request.credential_id, db_session)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")

    credentials = credential.credential_json.get_value(apply_mask=False)
    server_url = str(credentials.get("server_url") or "")
    username = str(credentials.get("username") or "")
    app_password = str(credentials.get("app_password") or "")
    if not server_url or not username or not app_password:
        raise HTTPException(
            status_code=400,
            detail="Stored credential is missing the Nextcloud URL, username or app password",
        )

    client = NextcloudWebDAVClient(
        server_url=server_url,
        username=username,
        app_password=app_password,
        verify_ssl=request.verify_ssl,
    )
    try:
        files, subfolders = client.list_folder(request.path.strip("/"))
    except NextcloudAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except NextcloudError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        client.close()

    return NextcloudBrowseResponse(
        path=request.path.strip("/"),
        folders=[
            NextcloudFolder(name=folder.rsplit("/", 1)[-1], path=folder)
            for folder in sorted(subfolders)
        ],
        file_count=len(files),
    )

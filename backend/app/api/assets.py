from fastapi import APIRouter, Request, Response

from app.api.errors import APIError
from app.assets import verified_team_logo
from app.config import get_settings

router = APIRouter()


@router.get("/assets/team-logos/{slug}")
def get_team_logo(slug: str, request: Request):
  logo = verified_team_logo(get_settings(), slug)

  if logo is None:
    raise APIError(404, "NOT_FOUND", "Team logo not found.")

  data, mime_type, digest = logo
  etag = f'"{digest}"'
  headers = {
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "public, max-age=3600, must-revalidate",
      "ETag": etag
  }

  if (
    etag in {value.strip() for value in request.headers.get("if-none-match", "").split(",")}
    or "*" in request.headers.get("if-none-match", "")
  ):
    return Response(status_code=304, headers=headers)

  return Response(content=data, media_type=mime_type, headers=headers)

from fastapi import APIRouter, Depends, Query
from pymongo.database import Database

from app.api.dependencies import get_database
from app.api.errors import APIError
from app.api.responses import success_object
from app.api.schemas import APIResponse, SearchData
from app.config import get_settings
from app.services.common import find_tournament_id
from app.services.search import search_event

router = APIRouter()


@router.get("/search", response_model=APIResponse[SearchData])
def get_search(q: str = Query(..., max_length=80),
               database: Database = Depends(get_database)):
  query = q.strip()

  if len(query) > 80:
    raise APIError(400, "INVALID_PARAMETER",
                   "Search query must be 80 characters or fewer.")

  if not query:
    return success_object({
        "teams": [],
        "players": []
    }, {
        "query": "",
        "total": 0
    })

  settings = get_settings()
  tournament_id = find_tournament_id(database, settings.tournament_slug)
  data, total = search_event(database, tournament_id, query.casefold())

  return success_object(data, {"query": query, "total": total})

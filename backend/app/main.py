from contextlib import asynccontextmanager
from threading import Lock
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.assets import router as assets_router
from app.api.data_update import router as data_update_router
from app.api.data_status import router as data_status_router
from app.api.filters import router as filters_router
from app.api.health import router as health_router
from app.api.overview import router as overview_router
from app.api.players import router as players_router
from app.api.schedule import router as schedule_router
from app.api.search import router as search_router
from app.api.system import router as system_router
from app.api.teams import router as teams_router
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
  app.state.mongodb_client = None
  app.state.mongodb_client_lock = Lock()

  try:
    yield
  finally:
    with app.state.mongodb_client_lock:
      client = app.state.mongodb_client
      app.state.mongodb_client = None

    if client is not None:
      client.close()


settings = get_settings()
app = FastAPI(title=settings.app_name,
              lifespan=lifespan,
              docs_url=None,
              redoc_url=None,
              openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5577", "http://localhost:5577"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"])

register_exception_handlers(app)
app.include_router(health_router, prefix="/api")
app.include_router(assets_router, prefix="/api")
app.include_router(data_status_router, prefix="/api")
app.include_router(data_update_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(overview_router, prefix="/api")
app.include_router(schedule_router, prefix="/api")
app.include_router(filters_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(teams_router, prefix="/api")
app.include_router(players_router, prefix="/api")

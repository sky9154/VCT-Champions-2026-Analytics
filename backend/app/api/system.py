from fastapi import APIRouter

from app.api.responses import success_object
from app.api.schemas import (SystemServicesResponse, SystemSettingsPatch,
                             SystemSettingsResponse)
from app.config import get_settings
from app.services.system_services import get_system_services
from app.services.system_settings import get_system_settings, update_system_settings

router = APIRouter()


@router.get("/system/settings", response_model=SystemSettingsResponse)
def get_system_settings_endpoint():
  return success_object(get_system_settings(get_settings()))


@router.patch("/system/settings", response_model=SystemSettingsResponse)
def patch_system_settings_endpoint(body: SystemSettingsPatch):
  patch = body.model_dump(exclude_unset=True)

  return success_object(update_system_settings(get_settings(), patch))


@router.get("/system/services", response_model=SystemServicesResponse)
def get_system_services_endpoint():
  return success_object(get_system_services())

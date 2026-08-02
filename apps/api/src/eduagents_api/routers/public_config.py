from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_app_settings
from ..schemas import PublicRegistrationConfigOut

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/public", response_model=PublicRegistrationConfigOut)
async def public_config(settings=Depends(get_app_settings)):
    return PublicRegistrationConfigOut(
        registration_mode=settings.registration_mode or "closed",
        email_verification_required=(
            settings.app_env == "prod" or settings.email_verification_required
        ),
    )

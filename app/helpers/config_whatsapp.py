import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from helpers.utils import get_setting_value


@dataclass
class WhatsAppSettings:
    verify_token: str
    whatsapp_token: str
    phone_number_id: str
    waba_id: str


def _get_value(db: Session, key: str, *, env_alias: Optional[str] = None) -> str:
    """Busca un valor primero en sec_settings y luego en variables de entorno."""
    setting_value = get_setting_value(db, key)
    if setting_value:
        return setting_value

    env_candidates = [key]
    if env_alias:
        env_candidates.append(env_alias)

    for env_key in env_candidates:
        env_value = os.getenv(env_key)
        if env_value:
            return env_value

    raise ValueError(f"La configuración '{key}' no está definida en sec_settings ni en el entorno.")


def get_whatsapp_settings(db: Session) -> WhatsAppSettings:
    """Obtiene los valores sensibles de WhatsApp desde sec_settings (con fallback al entorno)."""
    return WhatsAppSettings(
        verify_token=_get_value(db, "VERIFY_TOKEN"),
        whatsapp_token=_get_value(db, "WHATSAPP_TOKEN", env_alias="WHATSAPP_ACCESS_TOKEN"),
        phone_number_id=_get_value(db, "PHONE_NUMBER_ID", env_alias="WHATSAPP_PHONE_NUMBER_ID"),
        waba_id=_get_value(db, "WABA_ID"),
    )
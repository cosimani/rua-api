import os

import requests
from dotenv import load_dotenv
from typing import Dict, Optional

from sqlalchemy.orm import Session

from helpers.config_whatsapp import WhatsAppSettings, get_whatsapp_settings


load_dotenv()

WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"


def _resolve_whatsapp_settings(
    db: Optional[Session],
    whatsapp_settings: Optional[WhatsAppSettings]
) -> WhatsAppSettings:
    """Devuelve una configuración válida de WhatsApp usando sec_settings."""
    if whatsapp_settings:
        return whatsapp_settings

    if db is None:
        raise ValueError("Se requiere una sesión de base de datos para obtener la configuración de WhatsApp.")

    return get_whatsapp_settings(db)



# # ==========================================================
# # ✅ FUNCIÓN BASE - ENVÍO DE PLANTILLA
# # ==========================================================
# def _enviar_template_whatsapp(
#     destinatario: str,
#     template_name: str,
#     parametros: list,
#     language_code: str = "es"
#     ) -> Dict:

#     url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"

#     headers = {
#         "Authorization": f"Bearer {ACCESS_TOKEN}",
#         "Content-Type": "application/json"
#     }

#     # 🔥 Construcción dinámica de componentes según si hay parámetros
#     if len(parametros) > 0:
#         components = [
#             {
#                 "type": "body",
#                 "parameters": [
#                     {"type": "text", "text": p} for p in parametros
#                 ]
#             }
#         ]
#     else:
#         # 🔥 Plantilla sin parámetros: NO mandar components
#         components = []

#     # -----------------------------------------
#     # 🔒 MODO WHATSAPP SOLO A CÉSAR
#     # -----------------------------------------
#     whatsapp_solo_a_cesar = os.getenv("WHATSAPP_SOLO_A_CESAR", "Y").strip().upper()

#     # Si NO existe → por defecto enviamos a César
#     enviar_a_cesar = whatsapp_solo_a_cesar != "N"

#     destino_final = "5493512613442" if enviar_a_cesar else destinatario


#     payload = {
#         "messaging_product": "whatsapp",
#         "to": destinatario,
#         "type": "template",
#         "template": {
#             "name": template_name,
#             "language": {"code": language_code},
#         }
#     }

#     # Agregar components solo si hay parámetros
#     if components:
#         payload["template"]["components"] = components

#     print("\n📤 PAYLOAD WHATSAPP:")
#     print(payload)

#     try:
#         response = requests.post(url, headers=headers, json=payload)
#         print("📥 RESPUESTA META:", response.text)
#         return response.json()
#     except Exception as e:
#         return {"error": str(e)}



def _enviar_template_whatsapp(
    *,
    db: Session,
    destinatario: str,
    template_name: str,
    parametros: list,
    language_code: str = "es",
    whatsapp_settings: Optional[WhatsAppSettings] = None
) -> Dict:

    settings = _resolve_whatsapp_settings(db, whatsapp_settings)

    url = f"{WHATSAPP_API_URL}/{settings.phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }

    # ---------------------------------------------------
    # 🔒 WHATSAPP SOLO A CÉSAR (default = Y)
    # ---------------------------------------------------
    whatsapp_solo_a_cesar = os.getenv("WHATSAPP_SOLO_A_CESAR", "Y").strip().upper()
    enviar_a_cesar = whatsapp_solo_a_cesar != "N"  # True si falta la variable o tiene Y
    
    destino_final = "5493512613442" if enviar_a_cesar else destinatario

    # 🔥 Construcción dinámica de components según si hay parámetros
    if len(parametros) > 0:
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": p} for p in parametros
                ]
            }
        ]
    else:
        components = []

    payload = {
        "messaging_product": "whatsapp",
        "to": destino_final,   # ← usamos destino_final
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        }
    }

    if components:
        payload["template"]["components"] = components

    print("\n📤 PAYLOAD WHATSAPP:")
    print(payload)

    try:
        response = requests.post(url, headers=headers, json=payload)
        print("📥 RESPUESTA META:", response.text)

        resultado = response.json()
        resultado["_meta"] = {
            "enviado_a": destino_final,
            "redirigido_a_cesar": enviar_a_cesar
        }
        return resultado

    except Exception as e:
        return {"error": str(e)}




# ==========================================================
# 📢 PLANTILLA RUA - NOTIFICACIÓN GENERAL
# Template: rua_notificacion_v1
# Variables:
# {{1}} = Nombre
# {{2}} = Mensaje
# ==========================================================
def enviar_whatsapp_rua_notificacion(
    *,
    db: Session,
    destinatario: str,
    nombre: str,
    mensaje: str,
    whatsapp_settings: Optional[WhatsAppSettings] = None
) -> Dict:

    return _enviar_template_whatsapp(
        db=db,
        whatsapp_settings=whatsapp_settings,
        destinatario=destinatario,
        template_name="rua_notificacion_v1",
        parametros=[
            nombre,
            mensaje
        ]
    )



# ==========================================================
# ✅ EJEMPLO: RECORDATORIO CITA
# Template: rua_recordatorio_cita_v1
# {{1}} Nombre
# {{2}} Fecha
# {{3}} Hora
# ==========================================================
def enviar_whatsapp_rua_recordatorio_cita(
    *,
    db: Session,
    destinatario: str,
    nombre: str,
    fecha: str,
    hora: str,
    whatsapp_settings: Optional[WhatsAppSettings] = None
) -> Dict:

    return _enviar_template_whatsapp(
        db=db,
        whatsapp_settings=whatsapp_settings,
        destinatario=destinatario,
        template_name="rua_recordatorio_cita_v1",
        parametros=[
            nombre,
            fecha,
            hora
        ]
    )







def enviar_whatsapp_texto(
    *,
    db: Session,
    destinatario: str,
    mensaje: str,
    whatsapp_settings: Optional[WhatsAppSettings] = None
) -> dict:

    settings = _resolve_whatsapp_settings(db, whatsapp_settings)

    url = f"{WHATSAPP_API_URL}/{settings.phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "template",
        "template": {
            "name": "jaspers_market_plain_text_v1",
            "language": { "code": "en_US" },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        { "type": "text", "text": mensaje }
                    ]
                }
            ]
        }
    }

    print("📤 Payload enviado a Meta:")
    print(payload)

    try:
        response = requests.post(url, headers=headers, json=payload)
        print("📥 Respuesta Meta:", response.text)
        return response.json()
    except Exception as e:
        return {"error": str(e)}




def enviar_whatsapp(
    *,
    db: Session,
    destinatario: str,
    mensaje: str,
    whatsapp_settings: Optional[WhatsAppSettings] = None
) -> dict:
    settings = _resolve_whatsapp_settings(db, whatsapp_settings)

    url = f"{WHATSAPP_API_URL}/{settings.phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        # "to": destinatario,  # Asegurate de pasar '549...' como destinatario
        "to": "54351152613442",
        "type": "template",
        "template": {
            "name": "hello_world",  # Asegurate que esta plantilla esté aprobada
            "language": { "code": "en_US" }
        }
    }

    # 🔍 DEBUG: mostrar URL, headers y payload
    print("\n🟦 [DEBUG] Enviando mensaje por WhatsApp:")
    print("📨 URL:", url)
    print("📨 Headers:", headers)
    print("📨 Payload:", payload)

    try:
        response = requests.post(url, headers=headers, json=payload)
        print("✅ Status Code:", response.status_code)
        print("📥 Respuesta:", response.text)

        return response.json()
    except Exception as e:
        print("❌ Error en envío:", str(e))
        return {"success": False, "error": str(e)}



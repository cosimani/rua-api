import requests
import os

from dotenv import load_dotenv
from typing import Dict


# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener y validar la variable
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")

if not WHATSAPP_ACCESS_TOKEN:
    raise RuntimeError("La variable de entorno WHATSAPP_ACCESS_TOKEN no está definida. Verificá tu archivo .env")


WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")  # configurá esto en tu entorno
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")        # configurá esto en tu entorno

if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
    raise RuntimeError("Faltan variables WHATSAPP_ACCESS_TOKEN o WHATSAPP_PHONE_NUMBER_ID")



# ==========================================================
# ✅ FUNCIÓN BASE - ENVÍO DE PLANTILLA
# ==========================================================
def _enviar_template_whatsapp(
    destinatario: str,
    template_name: str,
    parametros: list,
    language_code: str = "es"
) -> Dict:

    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # 🔥 Construcción dinámica de componentes según si hay parámetros
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
        # 🔥 Plantilla sin parámetros: NO mandar components
        components = []

    payload = {
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        }
    }

    # Agregar components solo si hay parámetros
    if components:
        payload["template"]["components"] = components

    print("\n📤 PAYLOAD WHATSAPP:")
    print(payload)

    try:
        response = requests.post(url, headers=headers, json=payload)
        print("📥 RESPUESTA META:", response.text)
        return response.json()
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
    destinatario: str,
    nombre: str,
    mensaje: str
) -> Dict:

    return _enviar_template_whatsapp(
        destinatario = destinatario,
        template_name = "rua_notificacion_v1",
        parametros = [
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
    destinatario: str,
    nombre: str,
    fecha: str,
    hora: str
) -> Dict:

    return _enviar_template_whatsapp(
        destinatario = destinatario,
        template_name = "rua_recordatorio_cita_v1",
        parametros = [
            nombre,
            fecha,
            hora
        ]
    )







def enviar_whatsapp_texto(destinatario: str, mensaje: str) -> dict:

    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
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




def enviar_whatsapp(destinatario: str, mensaje: str) -> dict:
    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
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



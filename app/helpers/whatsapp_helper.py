import requests
import os

from dotenv import load_dotenv


# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener y validar la variable
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")

if not WHATSAPP_ACCESS_TOKEN:
    raise RuntimeError("La variable de entorno WHATSAPP_ACCESS_TOKEN no está definida. Verificá tu archivo .env")


WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")  # configurá esto en tu entorno
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")        # configurá esto en tu entorno

# ACCESS_TOKEN = "EAAX8UPrP70sBO0nOuasaKei8kWFnXkANHEYQeCqlMG1ly3SnZCgqxmlICbYizCiS6M7UzwSj6ZB0KdRzrYvPXJNUDJs3CO1W2VlU4xHYiVDWEZCTIk1tPVTAy7KYEVzlmsDSBLZAfyW4DAkpo70rrkWivSQ9vGSZAvRyiZCefhcL4AZA2vkWazOGeZC6mNUXEHqXmM6qoUcPJrZBB12gpZA2IgVVi3"

# def enviar_whatsapp(destinatario: str, mensaje: str) -> dict:
#     """
#     Envía un mensaje de texto vía WhatsApp usando la API de Meta.
#     destinatario debe estar en formato internacional (ej. '5493511234567')
#     """
#     url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"
#     headers = {
#         "Authorization": f"Bearer {ACCESS_TOKEN}",
#         "Content-Type": "application/json"
#     }
#     payload = {
#         "messaging_product": "whatsapp",
#         # "to": destinatario,
#         "to": "54351152613442",
#         "type": "template",
#         "template": { "name": "hello_world" },
#         "language": { "code": "en_US" } 
#     }

#     try:
#         response = requests.post(url, headers=headers, json=payload)
#         return response.json()
#     except Exception as e:
#         return {"success": False, "error": str(e)}


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



# curl -i -X POST `
#   https://graph.facebook.com/v22.0/662212736970265/messages `
#   -H 'Authorization: Bearer <access token>' `
#   -H 'Content-Type: application/json' `
#   -d '{ \"messaging_product\": \"whatsapp\", \"to\": \"54351152613442\", \"type\": \"template\", \"template\": { \"name\": \"hello_world\", \"language\": { \"code\": \"en_US\" } } }'
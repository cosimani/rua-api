import requests
from helpers.config_whatsapp import WhatsAppSettings

class WhatsAppTemplate2Service:

    BASE_URL = "https://graph.facebook.com/v22.0"

    @staticmethod
    def get_template_content(template_name: str, whatsapp_settings: WhatsAppSettings):
        """Obtiene el texto del cuerpo (BODY) de una plantilla."""
        if not whatsapp_settings.waba_id:
            return None

        url = f"{WhatsAppTemplate2Service.BASE_URL}/{whatsapp_settings.waba_id}/message_templates"
        params = {
            "name": template_name,
            "limit": 1
        }
        headers = {"Authorization": f"Bearer {whatsapp_settings.whatsapp_token}"}

        try:
            response = requests.get(url, headers=headers, params=params)
            data = response.json()
            
            if "error" in data:
                return None

            if "data" in data and len(data["data"]) > 0:
                template = data["data"][0]
                for component in template.get("components", []):
                    if component.get("type") == "BODY":
                        return component.get("text")
            return None
        except Exception:
            return None

    @staticmethod
    def send_template_message(to: str, template_name: str, vars: list, whatsapp_settings: WhatsAppSettings = None):
        """Envía un mensaje de plantilla con 2 variables."""
        if whatsapp_settings is None:
            raise ValueError("Se requieren las credenciales de WhatsApp para enviar mensajes.")

        url = f"{WhatsAppTemplate2Service.BASE_URL}/{whatsapp_settings.phone_number_id}/messages" 

        headers = {
            "Authorization": f"Bearer {whatsapp_settings.whatsapp_token}",
            "Content-Type": "application/json"
        }

        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": "es_AR"
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": vars[0]},
                            {"type": "text", "text": vars[1]}
                        ]
                    }
                ]
            }
        }

        response = requests.post(url, headers=headers, json=data)
        return response.json()

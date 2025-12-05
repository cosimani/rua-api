from models.notif_y_observaciones import Mensajeria
from datetime import datetime


def registrar_mensaje(
    db,
    *,
    tipo: str,                     # "whatsapp" | "email"
    login_emisor: str,
    login_destinatario: str,
    destinatario_texto: str,
    asunto: str = None,
    contenido: str = None,
    estado: str = "enviado",       # enviado | error | no_enviado
    mensaje_externo_id: str = None,
    data_json: dict = None
):
    """
    Registra un mensaje en la tabla Mensajeria SIN commit.
    El control de commit y transacción lo hace el endpoint.
    """

    # Normalizar longitud
    MAX_LENGTH = 4500
    if contenido and len(contenido) > MAX_LENGTH:
        contenido = contenido[:MAX_LENGTH] + " [...]"

    # Normalizar data_json si viene como string
    if data_json and not isinstance(data_json, dict):
        try:
            data_json = json.loads(data_json)
        except:
            data_json = {"raw": str(data_json)}

    registro = Mensajeria(
        tipo = tipo,
        login_emisor = login_emisor,
        login_destinatario = login_destinatario,
        destinatario_texto = destinatario_texto,
        asunto = asunto,
        contenido = contenido,
        estado = estado,
        mensaje_externo_id = mensaje_externo_id,
        data_json = data_json,
        fecha_envio = datetime.now(),
    )

    db.add(registro)
    return registro

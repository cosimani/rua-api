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
    estado: str = "enviado",       # "enviado", "error", etc.
    mensaje_externo_id: str = None,
    data_json: dict = None
):
    """
    Registra un mensaje en la tabla Mensajeria SIN hacer commit.
    El commit debe hacerlo el endpoint que llama a esta función.
    """

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
    return registro  # opcional, por si querés acceder al ID luego


from fastapi import APIRouter, HTTPException, Depends, Query, Body, Request
from typing import Literal, Optional, List
import os
import json

from fastapi.responses import PlainTextResponse #NUEVO!

from helpers.config_whatsapp import get_whatsapp_settings #NUEVO!

from models.users import User, Group, UserGroup 
from models.proyecto import Proyecto
from models.eventos_y_configs import SecSettings

from models.notif_y_observaciones import NotificacionesRUA, Mensajeria, WebhookEvent



from database.config import get_db  # Importá get_db desde config.py
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
from sqlalchemy import or_ #NUEVO!

from models.eventos_y_configs import RuaEvento
from datetime import date, datetime, timedelta
from security.security import get_current_user, require_roles, verify_api_key

from helpers.utils import enviar_mail, get_setting_value, normalize_phone, normalizar_celular
from helpers.whatsapp_helper import enviar_whatsapp, enviar_whatsapp_texto, _enviar_template_whatsapp
from helpers.mensajeria_utils import registrar_mensaje

from helpers.whatsapp_template_1 import WhatsAppTemplate1Service #NUEVO!
from helpers.whatsapp_template_2 import WhatsAppTemplate2Service #NUEVO!
from helpers.whatsapp_template_3 import WhatsAppTemplate3Service #NUEVO!


from helpers.notificaciones_utils import crear_notificacion_individual, crear_notificacion_masiva_por_rol, \
    marcar_notificaciones_como_vistas, obtener_notificaciones_para_usuario




import logging #NUEVO! -> Para saber si llegan los webhooks correctamente
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__) 
#===================================================================================



notificaciones_router = APIRouter()


def _get_setting_or_env(db: Session, key: str, env_alias: Optional[str] = None) -> str:
    value = get_setting_value(db, key)
    if value:
        return value

    if env_alias:
        env_value = os.getenv(env_alias)
        if env_value:
            return env_value

    return os.getenv(key, "")





def plantilla_simple(nombre_destinatario: str, mensaje_html: str):
    return f"""
    <html>
      <body style="margin:0;padding:0;background-color:#f8f9fa;">
        <table cellpadding="0" cellspacing="0" width="100%" style="background:#f8f9fa;padding:20px;">
          <tr><td align="center">
            <table cellpadding="0" cellspacing="0" width="600" style="
                  background:#ffffff;
                  border-radius:10px;
                  padding:30px;
                  font-family:'Segoe UI', Tahoma, sans-serif;
                  color:#343a40;
                  box-shadow:0 0 10px rgba(0,0,0,0.1);
                ">

              <tr>
                <td style="font-size:22px;color:#007bff;">
                  <strong>Hola {nombre_destinatario},</strong>
                </td>
              </tr>

              <tr>
                <td style="padding-top:20px;font-size:17px;line-height:1.6;">
                  {mensaje_html}
                </td>
              </tr>

              <tr>
                <td style="
                    padding-top:30px;
                    font-size:13px;
                    color:#888;
                    text-align:center;
                    border-top:1px solid #e5e5e5;
                    padding-top:25px;
                ">
                  Este mensaje fue enviado desde el Sistema RUA.<br>
                  Registro Único de Adopciones de Córdoba<br>
                  Por favor no responda este correo.
                </td>
              </tr>

            </table>
          </td></tr>
        </table>
      </body>
    </html>
    """


def plantilla_con_boton(nombre_destinatario: str, mensaje_html: str, boton_texto: str, boton_url: str):
    return f"""
    <html>
      <body style="margin:0;padding:0;background-color:#f8f9fa;">
        <table cellpadding="0" cellspacing="0" width="100%" style="background:#f8f9fa;padding:20px;">
          <tr><td align="center">
            <table cellpadding="0" cellspacing="0" width="600" style="
                  background:#ffffff;
                  border-radius:10px;
                  padding:30px;
                  font-family:'Segoe UI', Tahoma, sans-serif;
                  color:#343a40;
                  box-shadow:0 0 10px rgba(0,0,0,0.1);
                ">

              <tr>
                <td style="font-size:22px;color:#007bff;">
                  <strong>Hola {nombre_destinatario},</strong>
                </td>
              </tr>

              <tr>
                <td style="padding-top:20px;font-size:17px;line-height:1.6;">
                  {mensaje_html}
                </td>
              </tr>

              <!-- BOTÓN -->
              <tr>
                <td align="center" style="padding:26px 0;">
                  <a href="{boton_url}" target="_blank"
                    style="
                      display:inline-block;
                      padding:14px 28px;
                      font-size:17px;
                      color:#ffffff;
                      background:#0d6efd;
                      text-decoration:none;
                      border-radius:8px;
                      font-weight:600;
                    ">
                    {boton_texto}
                  </a>
                </td>
              </tr>

              <!-- FOOTER -->
              <tr>
                <td style="
                    padding-top:30px;
                    font-size:13px;
                    color:#888;
                    text-align:center;
                    border-top:1px solid #e5e5e5;
                    padding-top:25px;
                ">
                  Este mensaje fue enviado desde el Sistema RUA.<br>
                  Registro Único de Adopciones de Córdoba<br>
                  Por favor no responda este correo.
                </td>
              </tr>

            </table>
          </td></tr>
        </table>
      </body>
    </html>
    """


def renderizar_plantilla_email(tipo: str, nombre_destinatario: str, mensaje_html: str, extra: dict = None):

    if tipo == "simple":
        return plantilla_simple(nombre_destinatario, mensaje_html)

    if tipo == "con_boton":
        if not extra or "boton_texto" not in extra or "boton_url" not in extra:
            raise ValueError("Faltan parámetros para plantilla con botón")
        return plantilla_con_boton(
            nombre_destinatario,
            mensaje_html,
            boton_texto=extra["boton_texto"],
            boton_url=extra["boton_url"]
        )

    raise ValueError(f"Plantilla desconocida: {tipo}")






@notificaciones_router.post("/notificaciones", response_model = dict, 
                   dependencies = [Depends(verify_api_key),
                                   Depends(require_roles(["supervision", "supervisora", "profesional", "adoptante"]))])
def crear_notificacion(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📌 Crea una nueva notificación para un usuario.

    JSON esperado:
    ```json
    {
        "login_destinatario": "12345678",
        "mensaje": "Tenés una nueva revisión pendiente.",
        "link": "/menu_supervisoras/detalleProyecto",
        "data_json": { "proyecto_id": 541 },
        "tipo_mensaje": "verde",
        "enviar_por_whatsapp": true  // opcional
    }
    ```
    """
    login_destinatario = data.get("login_destinatario")
    mensaje = data.get("mensaje")
    link = data.get("link")
    data_json = data.get("data_json")
    tipo_mensaje = data.get("tipo_mensaje")
    enviar_por_whatsapp = data.get("enviar_por_whatsapp", False)

    # Validación de campos requeridos
    if not all([login_destinatario, mensaje, link]):
        raise HTTPException(400, "Faltan campos requeridos: login_destinatario, mensaje o link.")

    # ✅ Validar existencia del usuario destino
    user_destinatario = db.query(User).filter_by(login = login_destinatario).first()
    if not user_destinatario:
        raise HTTPException(
            status_code = 400,
            detail = f"El usuario con login '{login_destinatario}' no existe en el sistema."
        )

    # Crear notificación
    resultado = crear_notificacion_individual(
        db = db,
        login_destinatario = login_destinatario,
        mensaje = mensaje,
        link = link,
        data_json = data_json,
        tipo_mensaje = tipo_mensaje,
        enviar_por_whatsapp = enviar_por_whatsapp
    )

    if not resultado["success"]:
        raise HTTPException(500, resultado["mensaje"])

    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "Notificación creada correctamente.",
        "tiempo_mensaje": 4,
        "next_page": "actual"
    }





@notificaciones_router.post("/notificaciones/para-rol", response_model = dict, 
                   dependencies = [Depends(verify_api_key), 
                                   Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante"]))])
def crear_notificacion_para_rol(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📌 Crea una notificación para todos los usuarios de un rol específico (excepto 'adoptante').

    JSON esperado:
    ```json
    {
        "rol": "supervisora",
        "mensaje": "Nuevo proyecto para revisar.",
        "link": "/menu_supervisoras/detalleProyecto",
        "data_json": { "proyecto_id": 541 },
        "tipo_mensaje": "verde"
    }
    """
    rol = data.get("rol")
    mensaje = data.get("mensaje")
    link = data.get("link")
    data_json = data.get("data_json")    
    tipo_mensaje = data.get("tipo_mensaje")

    if not all([rol, mensaje, link]):
        raise HTTPException(400, "Faltan campos requeridos: rol, mensaje o link.")

    if rol == "adoptante":
        raise HTTPException(400, "No se pueden enviar notificaciones masivas al rol 'adoptante'.")

    resultado = crear_notificacion_masiva_por_rol(
        db = db,
        rol = rol,
        mensaje = mensaje,
        link = link,
        data_json = data_json,
        tipo_mensaje = tipo_mensaje
    )

    if not resultado["success"]:
        raise HTTPException(500, resultado["mensaje"])

    if resultado.get("cantidad", 0) == 0:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"No se encontraron usuarios con el rol '{rol}'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": f"Notificación enviada a todos los usuarios del rol '{rol}'.",
        "tiempo_mensaje": 4,
        "next_page": "actual"
    }




@notificaciones_router.put("/notificaciones/{notificacion_id}/vista", response_model = dict, 
                  dependencies = [Depends(verify_api_key),
                                 Depends(require_roles(["supervision", "supervisora", "profesional", "adoptante", "coordinadora"]))])
def marcar_notificacion_como_vista(
    notificacion_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    ✅ Marca como vista una notificación específica. 
    Para roles que NO son adoptante, marca todas las notificaciones con mismo contenido y fecha como vistas.
    """
    login = current_user["user"]["login"]

    # Obtener roles del usuario actual
    roles = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .all()
    )
    roles = [r.description for r in roles]

    resultado = marcar_notificaciones_como_vistas(
        db = db,
        login = login,
        notificacion_id = notificacion_id,
        roles = roles
    )

    if not resultado["success"]:
        raise HTTPException(404, resultado["mensaje"])

    db.commit()

    return {
        "success": True,
        "message": resultado["mensaje"]
    }




@notificaciones_router.get("/notificaciones/listado", response_model = dict, 
                  dependencies = [Depends(verify_api_key),
                                  Depends(require_roles(["supervision", "supervisora", "profesional", "adoptante", "coordinadora"]))])
def listar_notificaciones(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    filtro: Literal["vistas", "no_vistas", "todas"] = Query(..., description = "Filtrar por estado de vista"),
    page: int = Query(1, ge = 1),
    limit: int = Query(5, ge = 1, le = 100)
    ):

    """
    📄 Devuelve un listado paginado de notificaciones para el usuario autenticado, 
    incluyendo la cantidad total de no vistas.

    El parámetro `filtro` puede ser:
    - "vistas": solo notificaciones ya vistas
    - "no_vistas": solo notificaciones no vistas
    - "todas": todas las notificaciones
    """
    login = current_user["user"]["login"]

    resultado = obtener_notificaciones_para_usuario(
        db = db,
        login = login,
        filtro = filtro,
        page = page,
        limit = limit
    )

    if not resultado.get("success", True):
        raise HTTPException(500, resultado["mensaje"])

    return resultado




@notificaciones_router.get("/notificaciones/{login}/listado", response_model=dict,
    dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def listar_notificaciones_de_usuario(
    login: str,
    filtro: Literal["vistas", "no_vistas", "todas"] = Query(..., description="Filtrar por estado de vista"),
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db),
    ):

    """
    📄 Devuelve un listado paginado de notificaciones para el `login` indicado,
    incluyendo la cantidad total de no vistas.

    🔐 Solo accesible para roles supervisora y profesional.

    El parámetro `filtro` puede ser:
    - "vistas": solo notificaciones ya vistas
    - "no_vistas": solo notificaciones no vistas
    - "todas": todas las notificaciones
    """
    resultado = obtener_notificaciones_para_usuario(
        db=db,
        login=login,
        filtro=filtro,
        page=page,
        limit=limit
    )

    if not resultado.get("success", True):
        raise HTTPException(500, resultado["mensaje"])

    return resultado



@notificaciones_router.get("/notificaciones/proyecto/{proyecto_id}/listado", response_model=dict,
    dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def listar_notificaciones_comunes_del_proyecto(
    proyecto_id: int,
    filtro: Literal["vistas", "no_vistas", "todas"] = Query(..., description="Filtrar por estado de vista"),
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db),
    ):

    """
    📄 Devuelve notificaciones que fueron enviadas a *ambos usuarios* del proyecto
    y que tienen el mismo contenido (mensaje, link, tipo_mensaje, etc).

    🔐 Solo accesible para supervisora, profesional y administradora.
    """

    # ✅ Obtener proyecto por ORM
    proyecto = db.query(Proyecto).filter_by(proyecto_id=proyecto_id).first()
    if not proyecto:
        raise HTTPException(404, "Proyecto no encontrado.")

    login_1 = proyecto.login_1
    login_2 = proyecto.login_2


    if not login_1:
        raise HTTPException(400, "El proyecto no tiene un usuario principal definido.")

    if login_2:
        # Comparar notificaciones comunes entre ambos usuarios
        subquery_1 = db.query(NotificacionesRUA).filter(NotificacionesRUA.login_destinatario == login_1).subquery()
        subquery_2 = db.query(NotificacionesRUA).filter(NotificacionesRUA.login_destinatario == login_2).subquery()

        query = db.query(NotificacionesRUA).join(
            subquery_2,
            (NotificacionesRUA.mensaje == subquery_2.c.mensaje) &
            (NotificacionesRUA.link == subquery_2.c.link) &
            (NotificacionesRUA.tipo_mensaje == subquery_2.c.tipo_mensaje) &
            (NotificacionesRUA.data_json == subquery_2.c.data_json)
        ).filter(
            NotificacionesRUA.login_destinatario == login_1
        )

        if filtro == "vistas":
            query = query.filter(NotificacionesRUA.vista == True, subquery_2.c.vista == True)
        elif filtro == "no_vistas":
            query = query.filter(NotificacionesRUA.vista == False, subquery_2.c.vista == False)

    else:
        # Solo filtrar notificaciones del login_1 si login_2 no está definido
        query = db.query(NotificacionesRUA).filter(NotificacionesRUA.login_destinatario == login_1)

        if filtro == "vistas":
            query = query.filter(NotificacionesRUA.vista == True)
        elif filtro == "no_vistas":
            query = query.filter(NotificacionesRUA.vista == False)

    total = query.count()
    resultados = query.order_by(NotificacionesRUA.fecha_creacion.desc()).offset((page - 1) * limit).limit(limit).all()

    notificaciones = []
    for n in resultados:
        user = db.query(User).filter_by(login=n.login_que_notifico).first()
        notificaciones.append({
            "notificacion_id": n.notificacion_id,
            "fecha": n.fecha_creacion.strftime("%Y-%m-%d %H:%M"),
            "mensaje": n.mensaje,
            "link": n.link,
            "data_json": n.data_json,
            "tipo_mensaje": n.tipo_mensaje,
            "vista": n.vista,
            "login_que_notifico": n.login_que_notifico,
            "nombre_completo_que_notifico": f"{user.nombre} {user.apellido}" if user else "Sistema"
        })

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "notificaciones": notificaciones
    }





@notificaciones_router.post("/mensajeria/email", response_model=dict,
    dependencies=[Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def enviar_email(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    print("\n🚀 [ENVÍO EMAIL]")
    print("📨 Payload:", data)

    destinatarios = data.get("destinatarios", [])
    asunto = data.get("asunto")
    contenido = data.get("contenido")

    if not destinatarios:
        raise HTTPException(400, "Debe indicar destinatarios")

    if not contenido:
        raise HTTPException(400, "Debe indicar contenido")

    login_emisor = current_user["user"]["login"]
    resultados = []

    for login_destinatario in destinatarios:

        try:
            user = db.query(User).filter_by(login=login_destinatario).first()

            if not user or not user.mail:
                resultados.append({
                    "login": login_destinatario,
                    "success": False,
                    "mensaje": "Usuario sin mail"
                })
                continue

            # ---- Render plantilla HTML ----
            html = renderizar_plantilla_email(
                tipo="simple",
                nombre_destinatario=f"{user.nombre} {user.apellido}",
                mensaje_html=contenido
            )

            # Este es un ejemplo para enviar un baotón para una acción específica
            # html = renderizar_plantilla_email(
            #     tipo="con_boton",
            #     nombre_destinatario=f"{user.nombre} {user.apellido}",
            #     mensaje_html=contenido,
            #     extra={
            #         "boton_texto": "Revisar documentación",
            #         "boton_url": f"https://rua.justiciacordoba.gob.ar/revision/{login_destinatario}"
            #     }
            # )


            # ---- Enviar correo ----
            enviar_mail(
                destinatario=user.mail,
                asunto=asunto or "(Sin asunto)",
                cuerpo=html
            )

            # ---- Registrar mensaje ----
            registrar_mensaje(
                db,
                tipo="email",
                login_emisor=login_emisor,
                login_destinatario=login_destinatario,
                destinatario_texto=f"{user.nombre} {user.apellido}",
                asunto=asunto,
                contenido=contenido,
                estado="enviado"
            )

            resultados.append({
                "login": login_destinatario,
                "success": True,
                "mensaje": "Email enviado"
            })

        except Exception as e:
            resultados.append({
                "login": login_destinatario,
                "success": False,
                "mensaje": str(e)
            })

    # 🔥 commit final
    db.commit()

    enviados = sum(1 for r in resultados if r["success"])
    total = len(resultados)

    tipo_mensaje = "verde" if enviados == total else "naranja" if enviados > 0 else "rojo"

    return {
        "success": True,
        "tipo_mensaje": tipo_mensaje,
        "mensaje": "<br>".join([f"{r['login']}: {r['mensaje']}" for r in resultados]),
        "tiempo_mensaje": 5,
        "next_page": "actual"
    }





@notificaciones_router.get("/mensajeria/listado", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def listar_mensajeria(
    page: int = 1,
    limit: int = 20,

    search: Optional[str] = None,
    estado: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,

    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📧 Devuelve el historial paginado de correos electrónicos enviados (tipo=email).
    """

    query = db.query(Mensajeria).filter(Mensajeria.tipo == "email")

    # FILTRO POR ESTADO
    if estado:
        query = query.filter(Mensajeria.estado == estado)

    # FILTRO POR BUSCADOR
    if search:
        like = f"%{search}%"
        query = query.filter(
            (Mensajeria.destinatario_texto.ilike(like)) |
            (Mensajeria.asunto.ilike(like)) |
            (Mensajeria.contenido.ilike(like))
        )

    # FILTRO POR FECHAS
    if fecha_desde:
        query = query.filter(Mensajeria.fecha_envio >= fecha_desde)

    if fecha_hasta:
        query = query.filter(Mensajeria.fecha_envio <= fecha_hasta)

    # PAGINACIÓN
    total_records = query.count()
    total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)

    mensajes = (
        query.order_by(Mensajeria.fecha_envio.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    mensajes_list = [
        {
            "id": m.mensaje_id,
            "fecha_envio": m.fecha_envio,
            "tipo": m.tipo,
            "destinatario": m.destinatario_texto,
            "asunto": m.asunto,
            "contenido": m.contenido,
            "estado": m.estado,
        }
        for m in mensajes
    ]

    return {
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "total_records": total_records,
        "mensajes": mensajes_list
    }





@notificaciones_router.put("/mensajeria/{mensaje_id}/estado", response_model=dict,
    dependencies=[Depends(verify_api_key), 
    Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def actualizar_estado_mensaje(
    mensaje_id: int,
    nuevo_estado: Literal["no_enviado", "enviado", "recibido", "leido", "entregado", "error"] = Body(..., embed=True),
    db: Session = Depends(get_db)
    ):
    """
    🔄 Actualiza el estado de un mensaje (útil cuando llega confirmación de lectura o error).
    """
    mensaje = db.query(Mensajeria).filter_by(mensaje_id=mensaje_id).first()
    if not mensaje:
        raise HTTPException(404, "Mensaje no encontrado.")

    mensaje.estado = nuevo_estado
    db.commit()

    return {
        "success": True,
        "mensaje": f"Estado actualizado a '{nuevo_estado}'."
    }




# @notificaciones_router.get("/webhook/whatsapp")
# def verificar_webhook(
#     hub_mode: str = Query(None, alias="hub.mode"),
#     hub_challenge: str = Query(None, alias="hub.challenge"),
#     hub_verify_token: str = Query(None, alias="hub.verify_token"),
#     db: Session = Depends(get_db)
#     ):
#     whatsapp_settings = get_whatsapp_settings(db)

#     if hub_mode == "subscribe" and hub_verify_token == whatsapp_settings.verify_token:
#         return int(hub_challenge)
#     else:
#         raise HTTPException(status_code=403, detail="Webhook no autorizado")




# @notificaciones_router.post("/webhook/whatsapp")
# def recibir_estado_whatsapp(data: dict, db: Session = Depends(get_db)):

#     try:
#         entry = data.get("entry", [])
#         changes = entry[0].get("changes", [])
#         value = changes[0].get("value", {})

#         statuses = value.get("statuses", [])

#         for status in statuses:
#             mensaje_id_externo = status.get("id")
#             estado_wp = status.get("status")

#             # Mapear estados WhatsApp -> sistema interno
#             mapeo = {
#                 "sent": "enviado",
#                 "delivered": "entregado",
#                 "read": "leido",
#                 "failed": "error"
#             }

#             nuevo_estado = mapeo.get(estado_wp, "error")

#             mensaje = db.query(Mensajeria).filter(
#                 Mensajeria.mensaje_externo_id == mensaje_id_externo
#             ).first()

#             if mensaje:
#                 mensaje.estado = nuevo_estado
#                 db.commit()

#         return {"success": True}

#     except Exception as e:
#         print("❌ Error webhook:", str(e))
#         return {"success": False}








@notificaciones_router.get("/config/mensajeria", response_model=dict,
    dependencies=[ Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_config_mensajeria(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    # FECHAS
    now = datetime.now()
    primer_dia_mes = datetime(now.year, now.month, 1)

    # Primer día del mes anterior
    if now.month == 1:
        primer_dia_mes_anterior = datetime(now.year - 1, 12, 1)
    else:
        primer_dia_mes_anterior = datetime(now.year, now.month - 1, 1)


    # CONFIG — crear faltantes
    config_keys = [
        "activacion",
        "recuperacion",
        "doc_personal",
        "doc_proyecto",
        "proyecto_viable",
        "ratificacion",
        "fecha_entrevista",
        "postulacion_conv",
        "no_avance_proceso",
        "usuario_inactivo",
        "notif_pretenso",
        "notif_pretenso_proyecto",
    ]

    config = {}

    for base in config_keys:
        for canal_prefix in ["whatsapp_", "email_"]:
            key_name = canal_prefix + base
            setting = db.query(SecSettings).filter_by(set_name=key_name).first()

            if not setting:
                default = "N"
                setting = SecSettings(set_name=key_name, set_value=default)
                db.add(setting)
                config[key_name] = False
            else:
                config[key_name] = setting.set_value == "Y"


    # Garantizar whatsapp_costo_unitario
    costo_setting = db.query(SecSettings).filter_by(set_name="whatsapp_costo_unitario").first()
    if not costo_setting:
        costo_setting = SecSettings(set_name="whatsapp_costo_unitario", set_value="0")
        db.add(costo_setting)
        costo_unitario = 0.0
    else:
        try:
            costo_unitario = float(costo_setting.set_value)
        except:
            costo_unitario = 0.0
            costo_setting.set_value = "0"

    db.commit()

    # === Estadísticas WhatsApp ===
    total_whatsapp = db.query(Mensajeria).filter(
        Mensajeria.tipo == "whatsapp",
        Mensajeria.estado.notin_(["error", "no_enviado"])).count()

    mes_actual_whatsapp = db.query(Mensajeria).filter(
        Mensajeria.tipo == "whatsapp",
        Mensajeria.estado.notin_(["error", "no_enviado"]),
        Mensajeria.fecha_envio >= primer_dia_mes
    ).count()

    mes_anterior_whatsapp = db.query(Mensajeria).filter(
        Mensajeria.tipo == "whatsapp",
        Mensajeria.estado.notin_(["error", "no_enviado"]),    
        Mensajeria.fecha_envio >= primer_dia_mes_anterior,
        Mensajeria.fecha_envio < primer_dia_mes
    ).count()

    costo_mes_actual = mes_actual_whatsapp * costo_unitario
    costo_mes_anterior = mes_anterior_whatsapp * costo_unitario

    # mails totales
    mails_enviados = db.query(Mensajeria).filter(
        Mensajeria.tipo == "email",
        Mensajeria.estado.notin_(["error", "no_enviado"])).count()

    stats = {
        "mails": mails_enviados,
        "whatsapp_total": total_whatsapp,
        "whatsapp_mes": mes_actual_whatsapp,
        "costo_mes_actual": costo_mes_actual,
        "costo_mes_anterior": costo_mes_anterior,
        "costo_mensaje": costo_unitario
    }

    response = {"config": config, "stats": stats}

    if current_user.get("user", {}).get("login") == "26413920":
        response["whatsapp_settings"] = {
            "verify_token": _get_setting_or_env(db, "VERIFY_TOKEN"),
            "whatsapp_token": _get_setting_or_env(db, "WHATSAPP_TOKEN", env_alias="WHATSAPP_ACCESS_TOKEN"),
            "phone_number_id": _get_setting_or_env(db, "PHONE_NUMBER_ID", env_alias="WHATSAPP_PHONE_NUMBER_ID"),
            "waba_id": _get_setting_or_env(db, "WABA_ID"),
        }

    return response





@notificaciones_router.post("/config/mensajeria/save", response_model=dict,
    dependencies=[
        Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))
    ])
def save_config_mensajeria(
    data: dict = Body(...),
    db: Session = Depends(get_db)
    ):

    key = data.get("key")
    value = data.get("value")

    if not key:
        return {"success": False, "mensaje": "Key requerida"}

    setting = db.query(SecSettings).filter_by(set_name=key).first()

    # Caso especial: el costo unitario
    if key == "whatsapp_costo_unitario":
        val_str = str(value if value is not None else "0")

        if not setting:
            setting = SecSettings(set_name=key, set_value=val_str)
            db.add(setting)
        else:
            setting.set_value = val_str

        db.commit()
        return {"success": True, "mensaje": "Costo actualizado"}

    # Valores booleanos -> guardar Y o N
    set_val = "Y" if value else "N"

    if not setting:
        setting = SecSettings(set_name=key, set_value=set_val)
        db.add(setting)
    else:
        setting.set_value = set_val

    db.commit()

    return {"success": True, "mensaje": "Configuración guardada"}


@notificaciones_router.post("/config/mensajeria/whatsapp-cred/save", response_model=dict,
    dependencies=[ Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def save_whatsapp_credentials(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    if current_user.get("user", {}).get("login") != "26413920":
        raise HTTPException(status_code=403, detail="No autorizado")

    key = data.get("key")
    value = data.get("value")

    allowed_keys = {"VERIFY_TOKEN", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "WABA_ID"}
    if key not in allowed_keys:
        raise HTTPException(status_code=400, detail="Key no permitida")

    setting = db.query(SecSettings).filter_by(set_name=key).first()
    if not setting:
        setting = SecSettings(set_name=key, set_value=value or "")
        db.add(setting)
    else:
        setting.set_value = value or ""

    db.commit()

    return {"success": True, "mensaje": "Credenciales de WhatsApp actualizadas"}







#===================================================================================================

@notificaciones_router.get("/webhook/whatsapp")
async def verify_token(
    hub_mode: str = Query(None, alias="hub.mode"), # almacena los query params en una variable hub_mode de tipo string 
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    db: Session = Depends(get_db)
    ):
     
    whatsapp_settings = get_whatsapp_settings(db)

    if hub_mode == "subscribe" and hub_verify_token == whatsapp_settings.verify_token:
        return PlainTextResponse(hub_challenge) # retorna el valor de hub_challenge en texto plano porque asi lo exige "META"

    return PlainTextResponse("Verification failed", status_code=403)



@notificaciones_router.post("/webhook/whatsapp")
async def receive_update(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        logger.info(f"WEBHOOK RECEIVED: {body}")
    except Exception as e:
        logger.error(f"Error parsing webhook body: {e}")
        return {"status": "error", "message": "Invalid JSON"}

    # 1) Si es un mensaje entrante
    entry = body.get("entry", [])
    for e in entry:
        for change in e.get("changes", []):
            value = change.get("value", {})

            # a) mensajes entrantes
            if "messages" in value:
                for message in value["messages"]:
                    sender = message["from"]
                    msg_type = message["type"]
                    meta_id = message["id"]

                    # Normalizar número del remitente
                    normalized_sender = normalize_phone(sender)

                    # Buscar destinatario por celular
                    user = db.query(User).filter(
                        or_(
                            User.celular == normalized_sender,
                            User.celular == sender
                        )
                    ).first()
                    
                    login_user = user.login if user else None
                    user_texto = f"{user.nombre} {user.apellido}" if user else sender

                    content = ""
                    if msg_type == "text":
                        content = message["text"]["body"]
                        logger.info(f"Mensaje recibido de {sender}: {content}")

                    # 1. Guardar en el historial (webhooks)
                    new_event = WebhookEvent(
                        mensaje_externo_id=meta_id,
                        content=content,
                        status="respondido",
                        login_usuario=login_user
                    )
                    db.add(new_event)

                    # 2. Actualizar o crear el registro en Mensajeria (último evento)
                    last_msg = None
                    if login_user:
                        last_msg = db.query(Mensajeria).filter(Mensajeria.login_destinatario == login_user).first()
                    
                    if last_msg:
                        last_msg.mensaje_externo_id = meta_id
                        last_msg.contenido = content
                        last_msg.estado = "respondido"
                        last_msg.destinatario_texto = user_texto
                    else:
                        last_msg = Mensajeria(
                            mensaje_externo_id=meta_id,
                            tipo="whatsapp",
                            contenido=content,
                            estado="respondido",
                            login_destinatario=login_user,
                            destinatario_texto=user_texto
                        )
                        db.add(last_msg)
                    
                    db.commit()

            # b) estados del mensaje (sent, delivered, read...)
            if "statuses" in value:
                for status in value["statuses"]:
                    message_id = status["id"]
                    status_value = status["status"]
                    timestamp = status["timestamp"]

                    # Mapear estados de WhatsApp a los de nuestro Enum
                    # Enum: 'no_enviado', 'enviado', 'recibido', 'leido', 'entregado', 'error'
                    estado_mapeado = status_value
                    if status_value == 'sent': estado_mapeado = 'enviado'
                    elif status_value == 'delivered': estado_mapeado = 'entregado'
                    elif status_value == 'read': estado_mapeado = 'leido'
                    elif status_value == 'failed': estado_mapeado = 'error'

                    logger.info(f"Estado del mensaje {message_id}: {status_value} (mapeado: {estado_mapeado})")
                    
                    # Buscar el último evento de este mensaje para ver el estado actual
                    last_event = db.query(WebhookEvent).filter(
                        WebhookEvent.mensaje_externo_id == message_id
                    ).order_by(WebhookEvent.received_at.desc()).first()
                    
                    if last_event:
                        # Solo registrar si el estado es DISTINTO al que ya tenemos
                        if last_event.status != estado_mapeado:
                            # 1. Guardar el cambio de estado en el historial
                            new_status_event = WebhookEvent(
                                mensaje_externo_id=message_id,
                                asunto=last_event.asunto,
                                content=last_event.content,
                                status=estado_mapeado,
                                login_usuario=last_event.login_usuario
                            )
                            db.add(new_status_event)

                            # 2. Actualizar el registro en Mensajeria solo si coincide el mensaje_externo_id
                            last_msg = db.query(Mensajeria).filter(Mensajeria.mensaje_externo_id == message_id).first()
                            if last_msg:
                                last_msg.estado = estado_mapeado
                            
                            db.commit()
                            logger.info(f"Nuevo estado registrado: {message_id} -> {estado_mapeado}")
                        else:
                            logger.info(f"Estado {estado_mapeado} ya registrado para {message_id}, ignorando duplicado.")
                    else:
                        logger.warning(f"Mensaje original {message_id} no encontrado en historial para registrar estado {estado_mapeado}")

    return {"status": "ok"}




@notificaciones_router.post("/mensajeria/whatsapp",
    dependencies=[Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"])), ],)
def send_message(payload: dict = Body(...), db: Session = Depends(get_db)):
    # 1. Enviar mensaje a Meta (Meta suele aceptar el número con o sin 9, pero usamos el original o el normalizado según prefieras. 
    # WhatsApp Cloud API suele preferir el formato internacional completo sin el 9 para Argentina si es para enviar, 
    # pero si el usuario ya puso el número que funciona, lo mantenemos para el envío y normalizamos para la DB).
    # Normalizar número
    try:
        whatsapp_settings = get_whatsapp_settings(db)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    numero_destino = payload.get("to")
    nombre_template = payload.get("message")
    variables = payload.get("vars") or []

    numero_pretenso = numero_destino

    def _mask_token(token: Optional[str]) -> str:
        if not token:
            return ""
        if len(token) <= 10:
            return "***"
        return f"{token[:6]}***{token[-4:]}"

    def _normalize_envio(numero: str) -> str:
        digits = "".join(filter(str.isdigit, numero or ""))
        if digits.startswith("549") and len(digits) == 13:
            digits = "54" + digits[3:]
        return digits

    def registrar_error_envio(
        *,
        mensaje_error: str,
        login_dest: Optional[str],
        dest_texto: str,
        content: Optional[str],
        meta_id: Optional[str] = None,
    ) -> None:
        error_content = content or mensaje_error

        new_event = WebhookEvent(
            mensaje_externo_id=meta_id,
            asunto=nombre_template,
            content=error_content,
            status="error",
            login_usuario=login_dest
        )
        db.add(new_event)

        last_msg = None
        if login_dest:
            last_msg = db.query(Mensajeria).filter(Mensajeria.login_destinatario == login_dest).first()

        if last_msg:
            last_msg.mensaje_externo_id = meta_id
            last_msg.asunto = nombre_template
            last_msg.contenido = error_content
            last_msg.estado = "error"
            last_msg.destinatario_texto = dest_texto
        else:
            last_msg = Mensajeria(
                mensaje_externo_id=meta_id,
                tipo="whatsapp",
                asunto=nombre_template,
                contenido=error_content,
                estado="error",
                login_destinatario=login_dest,
                destinatario_texto=dest_texto
            )
            db.add(last_msg)

        db.commit()

    if not numero_destino or not nombre_template:
        registrar_error_envio(
            mensaje_error="Faltan datos obligatorios (to/message).",
            login_dest=None,
            dest_texto=numero_destino or "",
            content=None
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Debe indicar 'to' y 'message'.",
                "numero_pretenso": numero_pretenso,
                "template": nombre_template,
            }
        )

    if not isinstance(variables, list):
        registrar_error_envio(
            mensaje_error="'vars' debe ser una lista.",
            login_dest=None,
            dest_texto=numero_destino,
            content=None
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "'vars' debe ser una lista.",
                "numero_pretenso": numero_pretenso,
                "template": nombre_template,
            }
        )

    validacion = normalizar_celular(numero_pretenso)
    if not validacion.get("valido"):
        registrar_error_envio(
            mensaje_error=validacion.get("motivo", "Numero de telefono invalido."),
            login_dest=None,
            dest_texto=numero_pretenso,
            content=None
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": validacion.get("motivo", "Numero de telefono invalido."),
                "numero_pretenso": numero_pretenso,
                "numero_normalizado": validacion.get("celular"),
                "template": nombre_template,
            }
        )

    numero_normalizado = validacion.get("celular", numero_pretenso)

    whatsapp_solo_a_cesar = os.getenv("WHATSAPP_SOLO_A_CESAR", "Y").strip().upper()
    numero_prueba = os.getenv("WHATSAPP_NUMERO_PRUEBA", "5493512613442").strip()
    if whatsapp_solo_a_cesar == "Y":
        numero_envio = _normalize_envio(numero_prueba)
    else:
        numero_envio = _normalize_envio(numero_normalizado)

    normalized_to = normalize_phone(numero_envio)

    num_vars = len(variables)

    if num_vars == 0 or num_vars == 1:
        service = WhatsAppTemplate1Service
    elif num_vars == 2:
        service = WhatsAppTemplate2Service
    elif num_vars == 3:
        service = WhatsAppTemplate3Service
    else:
        return {"status": "error", "message": "Solo se soportan hasta 3 variables"}

    response = service.send_template_message(
        numero_envio,
        nombre_template,
        variables,
        whatsapp_settings=whatsapp_settings
    )

    meta_id = None
    if "messages" in response and len(response["messages"]) > 0:
        meta_id = response["messages"][0].get("id")

    user = db.query(User).filter(
        or_(
            User.celular == normalized_to,
            User.celular == numero_envio,
            User.celular == numero_normalizado,
            User.celular == numero_pretenso
        )
    ).first()

    login_dest = user.login if user else None
    dest_texto = f"{user.nombre} {user.apellido}" if user else numero_pretenso

    if user and user.celular:
        validacion_user = normalizar_celular(user.celular)
        if validacion_user.get("valido"):
            celular_corregido = validacion_user.get("celular")
            if celular_corregido and celular_corregido != user.celular:
                user.celular = celular_corregido

    content = service.get_template_content(nombre_template, whatsapp_settings) or nombre_template

    if variables and content:
        for i, var in enumerate(variables):
            placeholder = f"{{{{{i + 1}}}}}"
            content = content.replace(placeholder, var)

    if response.get("error") or not meta_id:
        mensaje_error = response.get("error", {}).get("message", "Error al enviar WhatsApp.")
        api_url = f"https://graph.facebook.com/v22.0/{whatsapp_settings.phone_number_id}/messages"
        debug_headers = {
            "Authorization": f"Bearer {_mask_token(whatsapp_settings.whatsapp_token)}",
            "Content-Type": "application/json",
        }
        debug_payload = {
            "messaging_product": "whatsapp",
            "to": numero_envio,
            "type": "template",
            "template": {
                "name": nombre_template,
                "language": {"code": "es_AR"},
            },
        }
        if variables:
            debug_payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": var} for var in variables
                    ],
                }
            ]

        curl_debug = (
            "curl -i -X POST \\\n+  "
            + api_url
            + " \\\n+  -H 'Authorization: Bearer "
            + _mask_token(whatsapp_settings.whatsapp_token)
            + "' \\\n+  -H 'Content-Type: application/json' \\\n+  -d '"
            + json.dumps(debug_payload)
            + "'"
        )
        registrar_error_envio(
            mensaje_error=mensaje_error,
            login_dest=login_dest,
            dest_texto=dest_texto,
            content=content,
            meta_id=meta_id
        )
        raise HTTPException(
            status_code=502,
            detail={
                "message": mensaje_error,
                "meta_error": response.get("error"),
                "meta_id": meta_id,
                "numero_pretenso": numero_pretenso,
                "numero_normalizado": numero_normalizado,
                "numero_envio": numero_envio,
                "whatsapp_solo_a_cesar": whatsapp_solo_a_cesar,
                "template": nombre_template,
                "vars_count": len(variables),
                "request_url": api_url,
                "request_headers": debug_headers,
                "request_body": debug_payload,
                "curl": curl_debug,
            }
        )

    new_event = WebhookEvent(
        mensaje_externo_id=meta_id,
        asunto=nombre_template,
        content=content,
        status="enviado",
        login_usuario=login_dest
    )
    db.add(new_event)

    last_msg = None
    if login_dest:
        last_msg = db.query(Mensajeria).filter(Mensajeria.login_destinatario == login_dest).first()

    if last_msg:
        last_msg.mensaje_externo_id = meta_id
        last_msg.asunto = nombre_template
        last_msg.contenido = content
        last_msg.estado = "enviado"
        last_msg.destinatario_texto = dest_texto
    else:
        last_msg = Mensajeria(
            mensaje_externo_id=meta_id,
            tipo="whatsapp",
            asunto=nombre_template,
            contenido=content,
            estado="enviado",
            login_destinatario=login_dest,
            destinatario_texto=dest_texto
        )
        db.add(last_msg)

    db.commit()

    return response






@notificaciones_router.get("/mensajeria/listado/whatsapp", response_model=dict,
    dependencies=[Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"])),],)
def listar_mensajeria_whatsapp(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    search: Optional[str] = Query(None, description="Buscar por destinatario, asunto o contenido"),
    estado: Optional[str] = Query(None, description="Filtrar por estado del mensaje"),
    fecha_desde: Optional[str] = Query(None, alias="fecha_desde"),
    fecha_hasta: Optional[str] = Query(None, alias="fecha_hasta"),
    db: Session = Depends(get_db)
    ):

    """📱 Devuelve el historial paginado de mensajes de WhatsApp."""

    query = db.query(Mensajeria).filter(Mensajeria.tipo == "whatsapp")

    if estado:
        query = query.filter(Mensajeria.estado == estado)

    if search:
        like = f"%{search}%"
        query = query.filter(
            (Mensajeria.destinatario_texto.ilike(like)) |
            (Mensajeria.asunto.ilike(like)) |
            (Mensajeria.contenido.ilike(like))
        )

    if fecha_desde:
        query = query.filter(Mensajeria.fecha_envio >= fecha_desde)

    if fecha_hasta:
        query = query.filter(Mensajeria.fecha_envio <= fecha_hasta)

    total_records = query.count()
    total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)

    mensajes = (
        query.order_by(Mensajeria.fecha_envio.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    mensajes_list = [
        {
            "id": m.mensaje_id,
            "fecha_envio": m.fecha_envio,
            "tipo": m.tipo,
            "destinatario": m.destinatario_texto,
            "asunto": m.asunto,
            "contenido": m.contenido,
            "estado": m.estado,
        }
        for m in mensajes
    ]

    return {
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "total_records": total_records,
        "mensajes": mensajes_list
    }


@notificaciones_router.get("/mensajeria/whatsapp/resumen", response_model=dict,
    dependencies=[Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"])),],)
def listar_resumen_whatsapp(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    search: Optional[str] = Query(None, description="Buscar por usuario o telefono"),
    estado: Optional[str] = Query(None, description="Filtrar por estado del ultimo evento"),
    fecha_desde: Optional[str] = Query(None, alias="fecha_desde"),
    fecha_hasta: Optional[str] = Query(None, alias="fecha_hasta"),
    db: Session = Depends(get_db)
    ):
    """📱 Devuelve un resumen por usuario con el ultimo estado de WhatsApp."""

    latest_ids_subquery = db.query(
        WebhookEvent.login_usuario.label("login"),
        func.max(WebhookEvent.id).label("last_id")
    ).filter(
        WebhookEvent.login_usuario.isnot(None)
    ).group_by(
        WebhookEvent.login_usuario
    ).subquery()

    query = db.query(WebhookEvent, User).join(
        latest_ids_subquery,
        WebhookEvent.id == latest_ids_subquery.c.last_id
    ).join(
        User,
        User.login == latest_ids_subquery.c.login
    )

    if estado:
        query = query.filter(WebhookEvent.status == estado)

    if search:
        like = f"%{search}%"
        query = query.filter(
            (User.nombre.ilike(like)) |
            (User.apellido.ilike(like)) |
            (User.login.ilike(like)) |
            (User.celular.ilike(like))
        )

    if fecha_desde:
        query = query.filter(WebhookEvent.received_at >= fecha_desde)

    if fecha_hasta:
        query = query.filter(WebhookEvent.received_at <= fecha_hasta)

    total_records = query.count()
    total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)

    rows = (
        query.order_by(WebhookEvent.received_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    resumen = []
    for event, user in rows:
        resumen.append({
            "login": user.login,
            "nombre": user.nombre,
            "apellido": user.apellido,
            "celular": user.celular,
            "ultimo_estado": event.status,
            "fecha_ultimo": event.received_at,
            "mensaje_externo_id": event.mensaje_externo_id
        })

    return {
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "total_records": total_records,
        "resumen": resumen
    }




@notificaciones_router.get("/mensajeria/detalle_mensaje",
    dependencies=[Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"])),],)
def get_mensajeria_detalle(
    login: Optional[str] = Query(None, description="Login del usuario"),
    telefono: Optional[str] = Query(None, description="Telefono del usuario"),
    db: Session = Depends(get_db)
    ):
    
    """
    Trae todo el historial de eventos (webhooks) relacionado con un usuario.
    """
    if not login and not telefono:
        return {"status": "error", "message": "Debe indicar login o telefono"}

    if not login and telefono:
        normalized_phone = normalize_phone(telefono)
        user = db.query(User).filter(
            or_(
                User.celular == normalized_phone,
                User.celular == telefono
            )
        ).first()
        login = user.login if user else None
    else:
        user = db.query(User).filter(User.login == login).first()
    
    if not user or not login:
        return {"status": "error", "message": "Usuario no encontrado"}

    events = db.query(WebhookEvent).filter(
        WebhookEvent.login_usuario == login
    ).order_by(WebhookEvent.received_at.desc()).all()

    history = []
    for event in events:
        history.append({
            "id": event.id,
            "mensaje_externo_id": event.mensaje_externo_id,
            "timestamp": event.received_at,
            "contenido": event.content,
            "estado": event.status
        })
    
    return {
        "usuario": {
            "login": user.login,
            "nombre": user.nombre,
            "apellido": user.apellido,
            "celular": user.celular
        },
        "history": history
    }






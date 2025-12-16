
from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Literal, Optional, List

from models.users import User, Group, UserGroup 
from models.proyecto import Proyecto
from models.eventos_y_configs import SecSettings

from models.notif_y_observaciones import NotificacionesRUA, Mensajeria


from database.config import get_db  # Importá get_db desde config.py
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func

from models.eventos_y_configs import RuaEvento
from datetime import date, datetime, timedelta
from security.security import get_current_user, require_roles, verify_api_key

from helpers.utils import enviar_mail, get_setting_value
from helpers.whatsapp_helper import enviar_whatsapp, enviar_whatsapp_texto, _enviar_template_whatsapp
from helpers.mensajeria_utils import registrar_mensaje



from helpers.notificaciones_utils import crear_notificacion_individual, crear_notificacion_masiva_por_rol, \
    marcar_notificaciones_como_vistas, obtener_notificaciones_para_usuario






notificaciones_router = APIRouter()





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





@notificaciones_router.post("/mensajeria/whatsapp", response_model=dict,
    dependencies=[Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def enviar_whatsapp(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    print("\n" + "=" * 80)
    print("🚀 [ENVÍO WHATSAPP]")
    print("📨 Payload:", data)

    destinatarios = data.get("destinatarios", [])
    plantilla = data.get("plantilla")
    parametros = data.get("parametros", [])

    if not destinatarios:
        raise HTTPException(400, "Debe indicar destinatarios")

    if not plantilla:
        raise HTTPException(400, "Debe indicar 'plantilla'")

    login_emisor = current_user["user"]["login"]
    resultados = []

    for login_destinatario in destinatarios:

        try:
            user = db.query(User).filter_by(login=login_destinatario).first()

            if not user or not user.celular:
                resultados.append({
                    "login": login_destinatario,
                    "success": False,
                    "mensaje": "Usuario sin celular o inexistente"
                })
                continue

            numero = user.celular.replace("+", "").replace(" ", "").replace("-", "")
            if not numero.startswith("54"):
                numero = "54" + numero

            print(f"📲 Enviando a {numero} - Plantilla: {plantilla}")

            # ---- Enviar WhatsApp ----
            respuesta_envio = _enviar_template_whatsapp(
                destinatario=numero,
                template_name=plantilla,
                parametros=parametros
            )

            estado = "enviado" if "messages" in respuesta_envio else "error"
            mensaje_externo_id = None

            if "messages" in respuesta_envio:
                mensaje_externo_id = respuesta_envio["messages"][0].get("id")

            # ---- Registrar mensaje con tu función ----
            registrar_mensaje(
                db,
                tipo="whatsapp",
                login_emisor=login_emisor,
                login_destinatario=login_destinatario,
                destinatario_texto=f"{user.nombre} {user.apellido}",
                contenido=f"Plantilla: {plantilla} / Parámetros: {parametros}",
                estado=estado,
                mensaje_externo_id=mensaje_externo_id,
                data_json=respuesta_envio
            )

            resultados.append({
                "login": login_destinatario,
                "success": estado == "enviado",
                "mensaje": f"WhatsApp {estado}"
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
    tipo: Optional[str] = None,
    estado: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,

    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):


    query = db.query(Mensajeria)

    # FILTRO POR TIPO
    if tipo in ["whatsapp", "email"]:
        query = query.filter(Mensajeria.tipo == tipo)

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






# ==========================================================
#  🔁 Actualizar estado del mensaje
# ==========================================================
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



@notificaciones_router.get("/webhook/whatsapp")
def verificar_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
    ):
    VERIFY_TOKEN = "rua_whatsapp_webhook_2025"

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return int(hub_challenge)
    else:
        raise HTTPException(status_code=403, detail="Webhook no autorizado")




@notificaciones_router.post("/webhook/whatsapp")
def recibir_estado_whatsapp(data: dict, db: Session = Depends(get_db)):

    try:
        entry = data.get("entry", [])
        changes = entry[0].get("changes", [])
        value = changes[0].get("value", {})

        statuses = value.get("statuses", [])

        for status in statuses:
            mensaje_id_externo = status.get("id")
            estado_wp = status.get("status")

            # Mapear estados WhatsApp -> sistema interno
            mapeo = {
                "sent": "enviado",
                "delivered": "entregado",
                "read": "leido",
                "failed": "error"
            }

            nuevo_estado = mapeo.get(estado_wp, "error")

            mensaje = db.query(Mensajeria).filter(
                Mensajeria.mensaje_externo_id == mensaje_id_externo
            ).first()

            if mensaje:
                mensaje.estado = nuevo_estado
                db.commit()

        return {"success": True}

    except Exception as e:
        print("❌ Error webhook:", str(e))
        return {"success": False}








@notificaciones_router.get("/config/mensajeria", response_model=dict,
    dependencies=[ Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_config_mensajeria(db: Session = Depends(get_db)):

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

    return {"config": config, "stats": stats}





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

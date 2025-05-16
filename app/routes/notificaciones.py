
from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Literal

from models.users import User, Group, UserGroup 
from models.proyecto import Proyecto

from models.notif_y_observaciones import NotificacionesRUA

from database.config import get_db  # Importá get_db desde config.py
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func

from models.eventos_y_configs import RuaEvento
from datetime import date, datetime
from security.security import get_current_user, require_roles, verify_api_key

from helpers.utils import enviar_mail, get_setting_value

from helpers.notificaciones_utils import crear_notificacion_individual, crear_notificacion_masiva_por_rol, \
    marcar_notificaciones_como_vistas, obtener_notificaciones_para_usuario



notificaciones_router = APIRouter()



@notificaciones_router.post("/notificaciones", response_model = dict, 
                   dependencies = [Depends(verify_api_key),
                                   Depends(require_roles(["supervisora", "profesional", "adoptante"]))])
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
                                   Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
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
                                 Depends(require_roles(["supervisora", "profesional", "adoptante"]))])
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
                                  Depends(require_roles(["supervisora", "profesional", "adoptante"]))])
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
    dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
    dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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


    if not login_1 or not login_2:
        raise HTTPException(400, "El proyecto no tiene ambos usuarios definidos.")

    # Buscar notificaciones comunes con campos idénticos
    subquery_1 = db.query(NotificacionesRUA).filter(NotificacionesRUA.login_destinatario == login_1)
    subquery_2 = db.query(NotificacionesRUA).filter(NotificacionesRUA.login_destinatario == login_2)

    # Comparar por mensaje, link, tipo_mensaje y data_json
    notis_1 = subquery_1.subquery()
    notis_2 = subquery_2.subquery()

    query = db.query(NotificacionesRUA).join(
        notis_2,
        (NotificacionesRUA.mensaje == notis_2.c.mensaje) &
        (NotificacionesRUA.link == notis_2.c.link) &
        (NotificacionesRUA.tipo_mensaje == notis_2.c.tipo_mensaje) &
        (NotificacionesRUA.data_json == notis_2.c.data_json)
    ).filter(
        NotificacionesRUA.login_destinatario == login_1
    )

    # Aplicar filtro por vistas
    if filtro == "vistas":
        query = query.filter(NotificacionesRUA.vista == True, notis_2.c.vista == True)
    elif filtro == "no_vistas":
        query = query.filter(NotificacionesRUA.vista == False, notis_2.c.vista == False)

    total = query.count()
    resultados = query.order_by(NotificacionesRUA.fecha_creacion.desc()).offset((page - 1) * limit).limit(limit).all()

    from models.users import User  # asegurarse que está importado

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

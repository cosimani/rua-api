from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
from typing import List, Dict, Any
from models.notif_y_observaciones import NotificacionesRUA
from models.users import User, Group, UserGroup
from datetime import datetime

from helpers.whatsapp_helper import enviar_whatsapp


def crear_notificacion_individual(
    db: Session,
    login_destinatario: str,
    mensaje: str,
    link: str,
    data_json: str = None,
    tipo_mensaje: str = None,
    enviar_por_whatsapp: bool = False,
    login_que_notifico: str = None  # 👈 nuevo parámetro
) -> Dict[str, Any]:
    """
    Crea una única notificación para un usuario.
    Si enviar_por_whatsapp=True y el usuario tiene número, también envía WhatsApp.
    No realiza commit.
    """
    try:
        db.add(NotificacionesRUA(
            login_destinatario=login_destinatario,
            mensaje=mensaje,
            link=link,
            data_json=data_json,
            tipo_mensaje=tipo_mensaje,
            login_que_notifico=login_que_notifico  # 👈 se guarda si existe
        ))

        if enviar_por_whatsapp:
            user = db.query(User).filter_by(login=login_destinatario).first()
            if user and user.celular:
                numero_internacional = user.celular
                enviar_whatsapp(numero_internacional, mensaje)

        return {"success": True, "mensaje": "Notificación creada"}
    except SQLAlchemyError as e:
        return {"success": False, "mensaje": f"Error al crear notificación: {str(e)}"}


def crear_notificacion_masiva_por_rol(
    db: Session,
    rol: str,
    mensaje: str,
    link: str,
    data_json: str = None,
    tipo_mensaje: str = None,
    login_que_notifico: str = None  # 👈 nuevo parámetro
) -> Dict[str, Any]:
    """
    Crea notificaciones para todos los usuarios de un rol.
    Devuelve la cantidad. Realiza commit.
    """
    try:
        logins = (
            db.query(User.login)
            .join(UserGroup, User.login == UserGroup.login)
            .join(Group, UserGroup.group_id == Group.group_id)
            .filter(Group.description == rol)
            .all()
        )
        logins = [r.login for r in logins]

        for login_destinatario in logins:
            db.add(NotificacionesRUA(
                login_destinatario=login_destinatario,
                mensaje=mensaje,
                link=link,
                data_json=data_json,
                tipo_mensaje=tipo_mensaje,
                login_que_notifico=login_que_notifico  # 👈 lo agregamos acá también
            ))

        db.commit()

        return {"success": True, "cantidad": len(logins), "mensaje": f"{len(logins)} notificaciones creadas"}
    except SQLAlchemyError as e:
        return {"success": False, "mensaje": f"Error al crear notificaciones masivas: {str(e)}"}



def marcar_notificaciones_como_vistas(
    db: Session,
    login: str,
    notificacion_id: int,
    roles: List[str]
) -> Dict[str, Any]:
    """
    Marca como vista una notificación o grupo de notificaciones.
    No realiza commit.
    """
    try:
        notificacion = db.query(NotificacionesRUA).filter(
            NotificacionesRUA.notificacion_id == notificacion_id,
            NotificacionesRUA.login_destinatario == login
        ).first()

        if not notificacion:
            return {"success": False, "mensaje": "Notificación no encontrada."}

        if "adoptante" in roles:
            notificacion.vista = True
        else:
            db.query(NotificacionesRUA).filter(
                NotificacionesRUA.mensaje == notificacion.mensaje,
                NotificacionesRUA.link == notificacion.link,
                NotificacionesRUA.tipo_mensaje == notificacion.tipo_mensaje,
                NotificacionesRUA.fecha_creacion == notificacion.fecha_creacion,
                NotificacionesRUA.vista == False
            ).update({NotificacionesRUA.vista: True}, synchronize_session=False)

        return {"success": True, "mensaje": "Notificación(es) marcadas como vistas."}
    except SQLAlchemyError as e:
        return {"success": False, "mensaje": f"Error al marcar notificación: {str(e)}"}





def obtener_notificaciones_para_usuario(
    db: Session,
    login: str,
    filtro: str,
    page: int,
    limit: int
) -> Dict[str, Any]:
    """
    Devuelve listado paginado de notificaciones para un usuario.
    Incluye cantidad total y no vistas.
    """

    try:
        # JOIN con la tabla User para obtener info de quien notificó
        query_base = db.query(NotificacionesRUA).options(
            joinedload(NotificacionesRUA.login_que_notifico_rel)  # Ver nota más abajo
        ).filter(
            NotificacionesRUA.login_destinatario == login
        )

        if filtro == "vistas":
            query_base = query_base.filter(NotificacionesRUA.vista == True)
        elif filtro == "no_vistas":
            query_base = query_base.filter(NotificacionesRUA.vista == False)

        total = query_base.count()

        no_vistas = db.query(func.count(NotificacionesRUA.notificacion_id)).filter(
            NotificacionesRUA.login_destinatario == login,
            NotificacionesRUA.vista == False
        ).scalar()

        if filtro == "todas":
            query_base = query_base.order_by(NotificacionesRUA.vista.asc(), NotificacionesRUA.fecha_creacion.desc())
        else:
            query_base = query_base.order_by(NotificacionesRUA.fecha_creacion.desc())

        notificaciones = query_base.offset((page - 1) * limit).limit(limit).all()

        resultado = []
        for n in notificaciones:
            usuario = n.login_que_notifico_rel  # relación hacia User
            resultado.append({
                "notificacion_id": n.notificacion_id,
                "fecha": n.fecha_creacion.strftime("%Y-%m-%d %H:%M"),
                "mensaje": n.mensaje,
                "link": n.link,
                "data_json": n.data_json,
                "tipo_mensaje": n.tipo_mensaje,
                "vista": n.vista,
                "login_que_notifico": n.login_que_notifico,
                "nombre_completo_que_notifico": f"{usuario.nombre} {usuario.apellido}" if usuario else "Sistema"
            })

        return {
            "page": page,
            "limit": limit,
            "total": total,
            "no_vistas": no_vistas,
            "notificaciones": resultado
        }

    except SQLAlchemyError as e:
        return {"success": False, "mensaje": f"Error al obtener notificaciones: {str(e)}"}




# def obtener_notificaciones_para_usuario(
#     db: Session,
#     login: str,
#     filtro: str,
#     page: int,
#     limit: int
# ) -> Dict[str, Any]:
#     """
#     Devuelve listado paginado de notificaciones para un usuario.
#     Incluye cantidad total y no vistas.
#     """
#     try:
#         query_base = db.query(NotificacionesRUA).filter(
#             NotificacionesRUA.login_destinatario == login
#         )

#         if filtro == "vistas":
#             query_base = query_base.filter(NotificacionesRUA.vista == True)
#         elif filtro == "no_vistas":
#             query_base = query_base.filter(NotificacionesRUA.vista == False)

#         total = query_base.count()

#         no_vistas = db.query(func.count(NotificacionesRUA.notificacion_id)).filter(
#             NotificacionesRUA.login_destinatario == login,
#             NotificacionesRUA.vista == False
#         ).scalar()

#         if filtro == "todas":
#             query_base = query_base.order_by(NotificacionesRUA.vista.asc(), NotificacionesRUA.fecha_creacion.desc())
#         else:
#             query_base = query_base.order_by(NotificacionesRUA.fecha_creacion.desc())

#         notificaciones = query_base.offset((page - 1) * limit).limit(limit).all()

#         resultado = [
#             {
#                 "notificacion_id": n.notificacion_id,
#                 "fecha": n.fecha_creacion.strftime("%Y-%m-%d %H:%M"),
#                 "mensaje": n.mensaje,
#                 "link": n.link,
#                 "data_json": n.data_json,
#                 "tipo_mensaje": n.tipo_mensaje,
#                 "vista": n.vista
#             }
#             for n in notificaciones
#         ]

#         return {
#             "page": page,
#             "limit": limit,
#             "total": total,
#             "no_vistas": no_vistas,
#             "notificaciones": resultado
#         }

#     except SQLAlchemyError as e:
#         return {"success": False, "mensaje": f"Error al obtener notificaciones: {str(e)}"}

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text, and_
from database.config import get_db, SessionLocal
from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, is_curso_aprobado, get_setting_value
import time
from models.users import User
from security.security import get_current_user, require_roles, verify_api_key
from helpers.moodle import eliminar_usuario_en_moodle, get_idusuario_by_mail

from datetime import datetime, timedelta
from models.proyecto import Proyecto, ProyectoHistorialEstado, FechaRevision
from models.eventos_y_configs import RuaEvento


check_router = APIRouter()



@check_router.post("/verificaciones_de_cron", response_model=dict, dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def verificaciones_de_cron(db: Session = Depends(get_db)):
    """
    Revisa proyectos con estado 'no_viable' y si tienen más de 2 años en ese estado,
    los pasa a estado 'baja_caducidad' y registra el cambio.
    """

    dos_anios_atras = datetime.now() - timedelta(days=730)
    proyectos_afectados = []

    proyectos_no_viables = db.query(Proyecto).filter(Proyecto.estado_general == 'no_viable').all()

    for proyecto in proyectos_no_viables:
        fecha_no_viable = None

        # Buscar la última fecha donde el estado fue cambiado a no_viable en el historial
        historial_no_viable = (
            db.query(ProyectoHistorialEstado)
            .filter(
                ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
                ProyectoHistorialEstado.estado_nuevo == 'no_viable'
            )
            .order_by(ProyectoHistorialEstado.fecha_hora.desc())
            .first()
        )

        print(f"[CADUCIDAD de NO VIABLE - Que no cumplieron 2 años] Proyecto {proyecto.proyecto_id}")

        if historial_no_viable:
            fecha_no_viable = historial_no_viable.fecha_hora
        elif proyecto.ultimo_cambio_de_estado:
            fecha_no_viable = datetime.combine(proyecto.ultimo_cambio_de_estado, datetime.min.time())

        # Si la fecha es mayor a 2 años, se debe actualizar el estado
        if fecha_no_viable and fecha_no_viable < dos_anios_atras:
            estado_anterior = proyecto.estado_general
            proyecto.estado_general = 'baja_caducidad'
            proyecto.ultimo_cambio_de_estado = datetime.now().date()

            db.add(ProyectoHistorialEstado(
                proyecto_id=proyecto.proyecto_id,
                estado_anterior=estado_anterior,
                estado_nuevo='baja_caducidad',
                comentarios='Cambio automático por cron: más de 2 años en estado no_viable.',
                fecha_hora=datetime.now()
            ))

            ahora = datetime.now()  # fuera del bucle si querés usar el mismo timestamp

            if proyecto.login_1:
                db.add(RuaEvento(
                    evento_detalle = f"Cambio automático de estado del proyecto {proyecto.proyecto_id}: de 'no_viable' a 'baja_caducidad' por antigüedad mayor a 2 años.",
                    evento_fecha = ahora,
                    login = proyecto.login_1
                ))

            if proyecto.login_2:
                db.add(RuaEvento(
                    evento_detalle = f"Cambio automático de estado del proyecto {proyecto.proyecto_id}: de 'no_viable' a 'baja_caducidad' por antigüedad mayor a 2 años.",
                    evento_fecha = ahora,
                    login = proyecto.login_2
                ))


            proyectos_afectados.append({
                "proyecto_id": proyecto.proyecto_id,
                "login_1": proyecto.login_1,
                "login_2": proyecto.login_2,
                "fecha_no_viable": fecha_no_viable.strftime("%Y-%m-%d")
            })

            print(f"[CADUCIDAD de NO VIABLE] Proyecto {proyecto.proyecto_id} pasó de 'no_viable' a 'baja_caducidad' "
                  f"(login_1: {proyecto.login_1}, login_2: {proyecto.login_2}, desde: {fecha_no_viable.date()})")

    db.commit()

    return {
        "cantidad_proyectos_actualizados": len(proyectos_afectados),
        "proyectos_actualizados": proyectos_afectados
    }



@check_router.get("/api_moodle_check", response_model=dict, dependencies=[Depends(verify_api_key)])
def api_moodle_check(
    dni: str = Query(..., description="DNI a verificar en Moodle"),
    mail: str = Query(..., description="Correo electrónico a verificar en Moodle"),
    db: Session = Depends(get_db)
):
    """
    Verifica si el DNI y el mail proporcionados existen en Moodle y mide el tiempo de respuesta.
    """
    
    # Medir tiempo para la consulta del DNI en Moodle
    start_time_dni = time.perf_counter()
    existe_dni = existe_dni_en_moodle(dni, db)
    end_time_dni = time.perf_counter()
    tiempo_respuesta_dni = end_time_dni - start_time_dni  # Tiempo en segundos
    
    # Medir tiempo para la consulta del mail en Moodle
    start_time_mail = time.perf_counter()
    existe_mail = existe_mail_en_moodle(mail, db)
    end_time_mail = time.perf_counter()
    tiempo_respuesta_mail = end_time_mail - start_time_mail  # Tiempo en segundos

    return {
        "dni": dni,
        "dni_existe_en_moodle": existe_dni,
        "tiempo_respuesta_dni": f"{tiempo_respuesta_dni:.4f} segundos",
        "mail": mail,        
        "mail_existe_en_moodle": existe_mail,        
        "tiempo_respuesta_mail": f"{tiempo_respuesta_mail:.4f} segundos",
        "message": f"El DNI {dni} {'existe' if existe_dni else 'no existe'} en Moodle. "
                   f"El correo {mail} {'existe' if existe_mail else 'no existe'} en Moodle."
    }


@check_router.get("/api_moodle_curso_aprobado", response_model=dict, dependencies=[Depends(verify_api_key)])
def api_moodle_curso_aprobado(
    mail: str = Query(..., description="Correo electrónico del usuario en Moodle"),
    db: Session = Depends(get_db)
):
    """
    Verifica si un usuario ha completado un curso en Moodle y mide el tiempo de respuesta.
    Si el curso está aprobado, actualiza el campo doc_adoptante_curso_aprobado en la base de datos.
    """

    shortname_curso = get_setting_value(db, "shortname_curso")

    start_time = time.perf_counter()
    curso_aprobado = is_curso_aprobado(mail, db)
    end_time = time.perf_counter()
    tiempo_respuesta = end_time - start_time  # Tiempo en segundos

    # Si el curso está aprobado, actualizar el campo doc_adoptante_curso_aprobado en la base de datos
    if curso_aprobado:
        user = db.query(User).filter(User.mail == mail).first()
        if user:
            user.doc_adoptante_curso_aprobado = "Y"
            db.commit()


    return {
        "mail": mail,
        "shortname_curso": shortname_curso,
        "curso_aprobado": curso_aprobado,
        "tiempo_respuesta": f"{tiempo_respuesta:.4f} segundos",
        "message": f"El usuario con mail {mail} {'ha completado' if curso_aprobado else 'no ha completado'} el curso {shortname_curso}."
    }



@check_router.delete("/api_moodle_eliminar_usuario", response_model = dict, 
                     dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador"]))])
def api_moodle_eliminar_usuario(
    mail: str = Query(..., description = "Correo electrónico del usuario a eliminar de Moodle"),
    db: Session = Depends(get_db)
):
    """
    Elimina un usuario de Moodle por su email. Ejecuta la función de eliminación
    y luego verifica si el usuario sigue existiendo en Moodle.
    """

    # Buscar el ID del usuario por email
    user_id = get_idusuario_by_mail(mail, db)

    if user_id == -1:
        raise HTTPException(status_code = 404, detail = f"No se encontró un usuario con el mail {mail} en Moodle")

    # Ejecutar eliminación
    try:
        eliminar_usuario_en_moodle(user_id, db)
    except HTTPException as e:
        return {
            "mail": mail,
            "user_id": user_id,
            "success": False,
            "message": "Error durante la solicitud de eliminación.",
            "error": str(e.detail)
        }

    # Verificar si el usuario sigue existiendo
    sigue_existiendo = existe_mail_en_moodle(mail, db)

    if sigue_existiendo:
        return {
            "mail": mail,
            "user_id": user_id,
            "success": False,
            "message": "Se intentó eliminar el usuario, pero sigue existiendo en Moodle."
        }
    else:
        return {
            "mail": mail,
            "user_id": user_id,
            "success": True,
            "message": f"El usuario con mail {mail} fue eliminado correctamente de Moodle."
        }












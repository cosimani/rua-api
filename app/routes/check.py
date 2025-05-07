from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from database.config import get_db, SessionLocal
from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, is_curso_aprobado, get_setting_value
import time
from models.users import User
from security.security import get_current_user, require_roles, verify_api_key
from helpers.moodle import eliminar_usuario_en_moodle, get_idusuario_by_mail


check_router = APIRouter()


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












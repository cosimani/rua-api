import requests
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException
from database.config import get_db

from helpers.utils import get_setting_value




def get_idcurso(db: Session) -> int:
    """
    Obtiene el ID de un curso en Moodle por su shortname.
    Devuelve el ID del curso si existe, o -1 si no se encuentra.
    """

    shortname = get_setting_value(db, "shortname_curso")

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Default a 10 segundos si no está definido

    # Parámetros de la solicitud a Moodle
    parametros_post = {
        "wstoken": wstoken,
        "moodlewsrestformat": "json",
        "wsfunction": "core_course_get_courses_by_field",
        "field": "shortname",
        "value": shortname
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()

        # Decodificar la respuesta JSON
        data = response.json()
        courses = data.get("courses", [])

        for course in courses:
            if course.get("shortname") == shortname:
                return course.get("id", -1)

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Moodle: {str(e)}")

    return -1  # Retorna -1 si no se encuentra el curso


def get_idusuario_by_mail(mail: str, db: Session) -> int:
    """
    Obtiene el ID de un usuario en Moodle por su correo electrónico.
    Devuelve el ID del usuario si existe, o -1 si no se encuentra.
    """

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Default a 10 segundos si no está definido

    # Parámetros de la solicitud a Moodle
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_get_users",
        "moodlewsrestformat": "json",
        "criteria[0][key]": "email",
        "criteria[0][value]": mail
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()

        # Decodificar la respuesta JSON
        data = response.json()
        users = data.get("users", [])

        if users:
            return users[0].get("id", -1)

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Moodle: {str(e)}")

    return -1  # Retorna -1 si no se encuentra el usuario


def is_curso_aprobado(mail: str, db: Session) -> bool:
    """
    Verifica si un usuario con el correo proporcionado ha completado un curso en Moodle.
    """

    # Obtener ID de curso y usuario en Moodle
    shortname = get_setting_value(db, "shortname_curso")

    id_curso = get_idcurso(db)
    id_user = get_idusuario_by_mail(mail, db)

    if not id_curso or not id_user:
        raise HTTPException(status_code=404, detail="Curso o usuario no encontrado en Moodle.")

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Default a 10 segundos si no está definido

    # Parámetros de la solicitud a Moodle
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_completion_get_course_completion_status",
        "moodlewsrestformat": "json",
        "courseid": id_curso,
        "userid": id_user
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()

        # Decodificar la respuesta JSON
        data = response.json()
        completed = data.get("completionstatus", {}).get("completed", False)

        return completed

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Moodle: {str(e)}")



def existe_mail_en_moodle(email: str, db: Session = Depends(get_db)) -> bool:
    """
    Verifica si un usuario con el correo proporcionado existe en Moodle.
    """

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Default a 10 segundos si no está definido

    # Parámetros de la solicitud a Moodle
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_get_users",
        "moodlewsrestformat": "json",
        "criteria[0][key]": "email",
        "criteria[0][value]": email
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()
        
        # Decodificar la respuesta JSON
        data = response.json()
        users = data.get("users", [])

        return len(users) > 0

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Moodle: {str(e)}")


def existe_dni_en_moodle(dni: str, db: Session = Depends(get_db)) -> bool:
    """
    Verifica si un usuario con el DNI proporcionado (username en Moodle) existe en Moodle.
    """
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Default a 10 segundos si no está definido

    # Parámetros de la solicitud a Moodle
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_get_users",
        "moodlewsrestformat": "json",
        "criteria[0][key]": "username",
        "criteria[0][value]": dni
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()
        
        # Decodificar la respuesta JSON
        data = response.json()
        users = data.get("users", [])

        return len(users) > 0

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Moodle: {str(e)}")


def crear_usuario_en_moodle(login: str, pswd: str, name: str, apellido: str, email: str, db: Session = Depends(get_db)) -> dict:
    """
    Crea un nuevo usuario en Moodle utilizando la API REST.
    Retorna la respuesta completa del servidor Moodle.
    """

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Default a 10 segundos si no está definido

    # Parámetros para la creación del usuario
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_create_users",
        "moodlewsrestformat": "json",
        "users[0][username]": login,
        "users[0][password]": pswd,
        "users[0][firstname]": name,
        "users[0][lastname]": apellido,
        "users[0][email]": email,
        "users[0][createpassword]": "0",   # Para indicar que no debe crear contraseña automática
        "users[0][maildisplay]": "1",      # Mostrar email a todos
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()

        # Retornar la respuesta como dict
        return response.json()

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al crear usuario en Moodle: {str(e)}")



def enrolar_usuario(idcurso: int, idusuario: int, db: Session = Depends(get_db)) -> dict:
    """
    Inscribe un usuario en un curso de Moodle con el rol de estudiante (roleid = 5).
    """

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10  # Valor por defecto: 10 segundos

    # Construir parámetros de inscripción
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "enrol_manual_enrol_users",
        "moodlewsrestformat": "json",
        "enrolments[0][roleid]": "5",        # 5 es el ID del rol "student" en Moodle
        "enrolments[0][userid]": str(idusuario),
        "enrolments[0][courseid]": str(idcurso)
    }

    try:
        # Hacer la solicitud HTTP a Moodle
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()

        # Retornar respuesta completa de Moodle
        return response.json()

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al inscribir usuario en Moodle: {str(e)}")


def eliminar_usuario_en_moodle(user_id: int, db: Session = Depends(get_db)) -> dict:
    """
    Elimina un usuario de Moodle por su ID (no por username o email).
    Utiliza la función core_user_delete_users.
    """

    # Obtener valores de configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = get_setting_value(db, "timeout_api_moodle_segs")

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    timeout = int(timeout) if timeout else 10

    # Parámetros para eliminar el usuario
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_delete_users",
        "moodlewsrestformat": "json",
        "userids[0]": str(user_id)
    }

    try:
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()

        data = response.json()

        # Moodle devuelve [] si fue exitoso
        if data == []:
            return { "success": True, "message": f"Usuario {user_id} eliminado correctamente" }
        else:
            return { "success": False, "detalle": data }

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Moodle: {str(e)}")


def actualizar_usuario_en_moodle(mail_old: str, dni: str, mail: str, nombre: str, apellido: str, db: Session) -> dict:
    """
    Actualiza email, username, nombre y apellido de un usuario en Moodle.
    Requiere el email anterior para buscar el ID del usuario.
    """

    # Obtener ID de usuario actual por email viejo
    iduser = get_idusuario_by_mail(mail_old, db)
    if iduser == -1:
        raise HTTPException(status_code=404, detail="Usuario no encontrado en Moodle")

    # Obtener configuración desde la base de datos
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = int(get_setting_value(db, "timeout_api_moodle_segs") or 10)

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    # Parámetros para actualizar el usuario
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_update_users",
        "moodlewsrestformat": "json",
        "users[0][id]": str(iduser),
        "users[0][username]": dni,
        "users[0][email]": mail,
        "users[0][firstname]": nombre,
        "users[0][lastname]": apellido
    }

    try:
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al actualizar usuario en Moodle: {str(e)}")



def actualizar_clave_en_moodle(mail: str, nueva_password: str, db: Session) -> dict:
    """
    🔒 Actualiza la contraseña de un usuario en Moodle usando su email como identificador.

    📥 Parámetros:
    - mail: correo electrónico del usuario (para buscar el ID en Moodle)
    - nueva_password: nueva contraseña a establecer
    - db: sesión activa de la base de datos

    📤 Retorna:
    Un dict con el resultado del intento de actualización
    """
    # Obtener ID del usuario en Moodle
    iduser = get_idusuario_by_mail(mail, db)
    if iduser == -1:
        raise HTTPException(status_code=404, detail="Usuario no encontrado en Moodle para cambiar la clave.")

    # Obtener configuración
    wstoken = get_setting_value(db, "wstoken")
    url_endpoint = get_setting_value(db, "endpoint_api_moodle")
    timeout = int(get_setting_value(db, "timeout_api_moodle_segs") or 10)

    if not wstoken or not url_endpoint:
        raise HTTPException(status_code=500, detail="Error en configuración de Moodle API")

    # Parámetros de la solicitud
    parametros_post = {
        "wstoken": wstoken,
        "wsfunction": "core_user_update_users",
        "moodlewsrestformat": "json",
        "users[0][id]": str(iduser),
        "users[0][password]": nueva_password
    }

    try:
        response = requests.post(url_endpoint, data=parametros_post, timeout=timeout, verify=False)
        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error al actualizar contraseña en Moodle: {str(e)}")

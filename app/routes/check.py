from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text, and_
from database.config import get_db, SessionLocal
from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, is_curso_aprobado, get_setting_value
from models.users import User
from security.security import get_current_user, require_roles, verify_api_key
from helpers.moodle import eliminar_usuario_en_moodle, get_idusuario_by_mail

from datetime import datetime, timedelta
from models.proyecto import Proyecto, ProyectoHistorialEstado, FechaRevision
from models.eventos_y_configs import RuaEvento

import os, json, hashlib, time


from pathlib import Path
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from fastapi.responses import FileResponse


load_dotenv()


check_router = APIRouter()


# --- Directorio donde se guarda el estado del último backup ---
EXPORT_DIR = os.getenv("EXPORT_DIR") or "/docs-rua/exports"
os.makedirs(EXPORT_DIR, exist_ok=True)
BACKUP_STATE_FILE = os.path.join(EXPORT_DIR, "last_backup_state.json")

# --- Directorios que se recorren ---
DIRS_TO_BACKUP = [
    os.getenv("UPLOAD_DIR_DOC_PRETENSOS"),
    os.getenv("UPLOAD_DIR_DOC_PROYECTOS"),
    os.getenv("UPLOAD_DIR_DOC_INFORMES"),
    os.getenv("UPLOAD_DIR_DOC_NNAS"),
    os.getenv("DIR_PDF_GENERADOS"),
    EXPORT_DIR,  # se incluye el propio EXPORT_DIR
]

def _load_state():
    if os.path.exists(BACKUP_STATE_FILE):
        try:
            with open(BACKUP_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            print("⚠️ Archivo de estado corrupto; se reinicia el estado de backup.")
            return {}
    return {}


def _save_state(state: dict):
    """Guarda el estado del último backup incremental."""
    with open(BACKUP_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _file_md5(path: str) -> str:
    """Calcula un hash MD5 del archivo (opcional, para verificar cambios reales)."""
    try:
        with open(path, "rb") as f:
            h = hashlib.md5()
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""




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





# ============================================================
# 🔁 BACKUP INCREMENTAL - Verificación de cambios
# ============================================================

@check_router.get( "/backup/verificar",
    response_model=dict, dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))],)
def verificar_archivos_para_backup(db: Session = Depends(get_db)):
    """
    Analiza los directorios definidos en .env y devuelve los archivos nuevos o modificados
    desde el último backup.
    No copia ni comprime nada: solo lista diferencias para descarga incremental.
    """

    prev_state = _load_state()
    new_state = {}
    changed_files = []
    total_archivos = 0

    for base_dir in DIRS_TO_BACKUP:
        if not base_dir or not os.path.exists(base_dir):
            continue

        for root, _, files in os.walk(base_dir):
            for f in files:
                full_path = os.path.join(root, f)
                try:
                    stat = os.stat(full_path)
                    new_state[full_path] = {"mtime": stat.st_mtime, "size": stat.st_size}
                    total_archivos += 1

                    prev = prev_state.get(full_path)
                    if not prev or prev["size"] != stat.st_size or prev["mtime"] != stat.st_mtime:
                        changed_files.append({
                            "path": full_path,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "md5": _file_md5(full_path)
                        })
                except Exception:
                    continue

    _save_state(new_state)

    return {
        "total_archivos_escaneados": total_archivos,
        "archivos_cambiados": len(changed_files),
        "detalles": changed_files
    }



@check_router.get("/files/descargar", response_class=FileResponse,
                  dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def descargar_archivo_directo(path: str = Query(..., description="Ruta absoluta del archivo a descargar")):
    """
    Permite descargar directamente un archivo del servidor (usado por el cliente de backup).
    """
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=os.path.basename(path), media_type="application/octet-stream")




@check_router.get("/backup/estado",
    response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def obtener_estado_backup():
    """
    Devuelve un resumen del último backup incremental registrado en el servidor.
    Solo accesible por administradores.
    """
    if not os.path.exists(BACKUP_STATE_FILE):
        raise HTTPException(status_code=404, detail="No existe un estado previo de backup.")

    try:
        with open(BACKUP_STATE_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo el archivo de estado: {str(e)}")

    return {
        "total_archivos_indexados": len(data),
        "ultimo_backup": datetime.fromtimestamp(os.path.getmtime(BACKUP_STATE_FILE)).strftime("%Y-%m-%d %H:%M:%S"),
        "ubicacion": BACKUP_STATE_FILE
    }


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
BACKUP_LOCK_FILE = os.path.join(EXPORT_DIR, "backup.lock")


# --- Directorios que se recorren ---
DIRS_TO_BACKUP = [
    os.getenv("UPLOAD_DIR_DOC_PRETENSOS"),
    os.getenv("UPLOAD_DIR_DOC_PROYECTOS"),
    os.getenv("UPLOAD_DIR_DOC_INFORMES"),
    os.getenv("UPLOAD_DIR_DOC_NNAS"),
    os.getenv("DIR_PDF_GENERADOS"),
    EXPORT_DIR,  # se incluye el propio EXPORT_DIR
]

# Normalizamos rutas permitidas para el backup/descarga
ALLOWED_BACKUP_ROOTS = [
    os.path.realpath(p) for p in DIRS_TO_BACKUP if p
]

# Límite de tamaño de archivo para backup (ejemplo: 500 MB)
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024



def _load_state() -> dict:
    if os.path.exists(BACKUP_STATE_FILE):
        try:
            with open(BACKUP_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            print("⚠️ Archivo de estado de backup corrupto; se reinicia el estado.")
            return {}
    return {}


def _save_state(state: dict) -> None:
    """Guarda el estado del último backup incremental."""
    with open(BACKUP_STATE_FILE, "w", encoding="utf-8") as f:
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





# ======================================================================
#  BACKUP INCREMENTAL - VERIFICACIÓN DE CAMBIOS
# ======================================================================

@check_router.get("/backup/verificar", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def verificar_archivos_para_backup(
    db: Session = Depends(get_db),
    limit: int = Query(0, description="Máximo número de archivos a escanear (0 = sin límite)"),
    modo_rapido: bool = Query(True, description="Si True, ignora hashes MD5 y solo compara tamaño/fecha")
    ):

    """
    Analiza los directorios definidos en .env y devuelve los archivos nuevos o modificados
    desde el último backup. Soporta limitación de cantidad y modo rápido sin hashes.
    Incluye:
    - Lock para evitar ejecuciones concurrentes
    - Límite de tamaño de archivo
    - Registro de evento en RuaEvento
    """

    # 🔒 Lock simple para evitar ejecuciones concurrentes
    if os.path.exists(BACKUP_LOCK_FILE):
        raise HTTPException(status_code=423, detail="Ya hay un proceso de backup en ejecución.")

    open(BACKUP_LOCK_FILE, "w").close()

    try:
        prev_state = _load_state()
        new_state = {}
        changed_files = []
        total_archivos = 0
        procesados = 0
        archivos_omitidos_por_tamano = 0

        for base_dir in DIRS_TO_BACKUP:
            if not base_dir:
                continue
            base_dir_real = os.path.realpath(base_dir)
            if not os.path.exists(base_dir_real):
                continue

            for root, _, files in os.walk(base_dir_real):
                for f in files:
                    full_path = os.path.join(root, f)
                    full_path_real = os.path.realpath(full_path)

                    # Evitar incluir el archivo de estado del backup y el lock en el propio backup
                    if full_path_real == os.path.realpath(BACKUP_STATE_FILE):
                        continue
                    if full_path_real == os.path.realpath(BACKUP_LOCK_FILE):
                        continue

                    try:
                        stat = os.stat(full_path_real)

                        # Límite de tamaño
                        if stat.st_size > MAX_FILE_SIZE_BYTES:
                            archivos_omitidos_por_tamano += 1
                            continue

                        new_state[full_path_real] = {
                            "mtime": stat.st_mtime,
                            "size": stat.st_size
                        }
                        total_archivos += 1

                        prev = prev_state.get(full_path_real)
                        if (not prev or
                            prev.get("size") != stat.st_size or
                            prev.get("mtime") != stat.st_mtime):
                            changed = {
                                "path": full_path_real,
                                "size": stat.st_size,
                                "mtime": stat.st_mtime,
                            }
                            if not modo_rapido:
                                changed["md5"] = _file_md5(full_path_real)
                            changed_files.append(changed)

                        procesados += 1
                        if limit and procesados >= limit:
                            _save_state(new_state)

                            # Registrar evento en RuaEvento
                            db.add(RuaEvento(
                                evento_detalle=(
                                    f"Backup incremental verificado parcialmente (limit={limit}). "
                                    f"Archivos escaneados: {total_archivos}, "
                                    f"archivos cambiados: {len(changed_files)}, "
                                    f"omitidos por tamaño: {archivos_omitidos_por_tamano}."
                                ),
                                evento_fecha=datetime.now()
                            ))
                            db.commit()

                            return {
                                "total_archivos_escaneados": total_archivos,
                                "archivos_cambiados": len(changed_files),
                                "limit_reached": True,
                                "archivos_omitidos_por_tamano": archivos_omitidos_por_tamano,
                                "detalles": changed_files
                            }
                    except Exception:
                        # Podés loguear algo si querés
                        continue

        _save_state(new_state)

        # Registrar evento en RuaEvento
        db.add(RuaEvento(
            evento_detalle=(
                "Backup incremental verificado completamente. "
                f"Archivos escaneados: {total_archivos}, "
                f"archivos cambiados: {len(changed_files)}, "
                f"omitidos por tamaño: {archivos_omitidos_por_tamano}."
            ),
            evento_fecha=datetime.now()
        ))
        db.commit()

        return {
            "total_archivos_escaneados": total_archivos,
            "archivos_cambiados": len(changed_files),
            "limit_reached": False,
            "archivos_omitidos_por_tamano": archivos_omitidos_por_tamano,
            "detalles": changed_files
        }

    finally:
        # Quitar lock siempre, aunque falle algo
        try:
            if os.path.exists(BACKUP_LOCK_FILE):
                os.remove(BACKUP_LOCK_FILE)
        except Exception:
            # Si no se puede borrar, lo registrás en logs del servidor
            pass


@check_router.get("/files/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def descargar_archivo_directo(path: str = Query(..., description="Ruta absoluta del archivo a descargar")):
    """
    Permite descargar directamente un archivo del servidor (usado por el cliente de backup).
    🔒 Solo se permite descargar archivos ubicados dentro de los directorios configurados
    en DIRS_TO_BACKUP / ALLOWED_BACKUP_ROOTS.
    """

    real_path = os.path.realpath(path)

    if not os.path.exists(real_path) or not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    # Bloqueamos symlinks (opcional pero recomendable)
    if os.path.islink(real_path):
        raise HTTPException(status_code=403, detail="No se permite descargar enlaces simbólicos")

    # Validar que el archivo esté dentro de los directorios permitidos
    if not any(real_path.startswith(root) for root in ALLOWED_BACKUP_ROOTS):
        raise HTTPException(status_code=403, detail="Ruta de archivo no permitida para descarga")

    return FileResponse(real_path, filename=os.path.basename(real_path), media_type="application/octet-stream")


@check_router.get("/backup/estado", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def obtener_estado_backup():
    """
    Devuelve un resumen del último backup incremental registrado en el servidor.
    Solo accesible por administradores.
    """
    if not os.path.exists(BACKUP_STATE_FILE):
        raise HTTPException(status_code=404, detail="No existe un estado previo de backup.")

    try:
        with open(BACKUP_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo el archivo de estado: {str(e)}")

    return {
        "total_archivos_indexados": len(data),
        "ultimo_backup": datetime.fromtimestamp(
            os.path.getmtime(BACKUP_STATE_FILE)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "ubicacion": BACKUP_STATE_FILE
    }


@check_router.delete("/backup/reset", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def reiniciar_backup_incremental():
    """
    Reinicia el estado del backup incremental eliminando el archivo last_backup_state.json.
    Permite comenzar de nuevo el proceso completo de backup.
    """
    if not os.path.exists(BACKUP_STATE_FILE):
        return {"success": True, "message": "No existía estado previo de backup. Nada que eliminar."}

    try:
        os.remove(BACKUP_STATE_FILE)
        return {
            "success": True,
            "message": (
                f"El archivo de estado '{BACKUP_STATE_FILE}' fue eliminado. "
                "El próximo backup comenzará desde cero."
            )
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo eliminar el archivo de estado: {str(e)}")
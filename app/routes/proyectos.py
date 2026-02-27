from fastapi import APIRouter, HTTPException, Depends, Query, Request, status, Body, UploadFile, File, Form
from typing import List, Optional, Literal, Tuple
from sqlalchemy.orm import Session, aliased, joinedload, noload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, case, and_, or_, Integer, literal_column, desc, cast, exists, select
from urllib.parse import unquote


from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, crear_usuario_en_moodle, get_idcurso, \
    enrolar_usuario, get_idusuario_by_mail, eliminar_usuario_en_moodle, actualizar_usuario_en_moodle, \
    actualizar_clave_en_moodle, is_curso_aprobado


from datetime import datetime, timedelta, date, time

from models.proyecto import Proyecto, ProyectoHistorialEstado, DetalleEquipoEnProyecto, AgendaEntrevistas, FechaRevision
from models.carpeta import Carpeta, DetalleProyectosEnCarpeta, DetalleNNAEnCarpeta
from models.notif_y_observaciones import ObservacionesProyectos, ObservacionesPretensos, NotificacionesRUA
from models.convocatorias import DetalleProyectoPostulacion
from models.ddjj import DDJJ
from models.nna import Nna, NnaHistorialEstado

from bs4 import BeautifulSoup


# from models.carpeta import DetalleProyectosEnCarpeta
from models.users import User, Group, UserGroup 
from database.config import get_db
from helpers.utils import get_user_name_by_login, construir_subregistro_string, parse_date, generar_codigo_para_link, \
    enviar_mail, enviar_mail_multiples, get_setting_value, edad_como_texto, check_consecutive_numbers, \
    get_notificacion_settings
from helpers.config_whatsapp import get_whatsapp_settings
from helpers.mensajeria_utils import registrar_mensaje

from models.eventos_y_configs import RuaEvento, UsuarioNotificadoRatificacion
from services.proyecto_unificacion import unify_on_enter_vinculacion, get_unificacion_info

from security.security import get_current_user, verify_api_key, require_roles
from dotenv import load_dotenv
from fastapi.responses import FileResponse

import fitz  # PyMuPDF
from PIL import Image
import subprocess

import os, json, shutil
import tempfile
import zipfile


from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol, crear_notificacion_individual

import re




# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener y validar la variable
UPLOAD_DIR_DOC_PROYECTOS = os.getenv("UPLOAD_DIR_DOC_PROYECTOS")

if not UPLOAD_DIR_DOC_PROYECTOS:
    raise RuntimeError("La variable de entorno UPLOAD_DIR_DOC_PROYECTOS no está definida. Verificá tu archivo .env")

# Crear la carpeta si no existe
os.makedirs(UPLOAD_DIR_DOC_PROYECTOS, exist_ok=True)


# Obtener y validar la variable
DIR_PDF_GENERADOS = os.getenv("DIR_PDF_GENERADOS")

if not DIR_PDF_GENERADOS:
    raise RuntimeError("La variable de entorno DIR_PDF_GENERADOS no está definida. Verificá tu archivo .env")

# Crear la carpeta si no existe
os.makedirs(DIR_PDF_GENERADOS, exist_ok=True)


# Destinatarios por defecto para avisos internos del RUA
DESTINATARIOS_RUA: List[str] = [
    "registroadopcion@justiciacordoba.gob.ar",
    "equipotecnicoadopcion@justiciacordoba.gob.ar",
    "cesarosimani@gmail.com"
]


proyectos_router = APIRouter()



MAX_FILE_MB = 25
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
LEGACY_DEFAULT_DATE = "2025-06-23 00:00:00"  # ← “23 de junio de 2025” normalizado

# Campos reales de la tabla Proyecto (columnas)
REAL_FIELDS = {
    "doc_proyecto_convivencia_o_estado_civil",
    "informe_profesionales",
    "doc_informe_vinculacion",
    "doc_informe_seguimiento_guarda",
    "doc_sentencia_guarda",
    "doc_informe_conclusivo",
    "doc_sentencia_adopcion",
    "doc_interrupcion",
    "doc_baja_convocatoria",
}

# Alias aceptados desde el front → columna real
ALIASES = {
    "sentencia_adopcion": "doc_sentencia_adopcion",
    "sentencia_guarda": "doc_sentencia_guarda",
    "doc_interrupcion": "doc_interrupcion",
    "doc_baja_convocatoria": "doc_baja_convocatoria",
    # sumá más si necesitás
}


def _resolve_field(campo: str) -> str:
    real = ALIASES.get(campo, campo)
    if real not in REAL_FIELDS:
        raise HTTPException(status_code=400, detail=f"Campo no permitido: {campo}")
    return real


def _sanitize(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"[^a-z0-9_]", "_", s) or "archivo"


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def _load_archivos(valor: Optional[str]):
    """
    Devuelve lista [{'ruta':..., 'fecha':...}] a partir del valor en DB.
    Soporta formato legacy (string plano con la ruta).
    """
    if not valor:
        return []
    try:
        if isinstance(valor, str) and valor.strip().startswith("["):
            return json.loads(valor)
        # legacy: una sola ruta en string
        return [{"ruta": valor, "fecha": LEGACY_DEFAULT_DATE}]
    except Exception:
        # si está corrupto, devolvemos vacío
        return []


def _dump_archivos(items):
    return json.dumps(items, ensure_ascii=False)



def _parse_fecha(fecha_str: str) -> Optional[datetime]:
    """Intenta parsear varias variantes 'YYYY-MM-DD HH:MM:SS' o ISO."""
    if not fecha_str:
        return None
    s = fecha_str.strip()
    # normalizar espacio/T
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _dentro_24h(dt: Optional[datetime]) -> bool:
    if not dt:
        return False
    return (datetime.now() - dt) <= timedelta(hours=24)


# helpers internos (pueden ir arriba o en utils.py)
def _save_historial_upload(
    proyecto, 
    campo: str, 
    file: UploadFile, 
    UPLOAD_DIR_DOC_PROYECTOS: str,
    db: Session,                # <— añadimos la sesión aquí
    ):

    """Valida, guarda en disco y anexa al JSON histórico."""
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}:
        return {"success": False, "tipo_mensaje": "rojo", "mensaje": f"Extensión no permitida: {ext}", "tiempo_mensaje": 6, "next_page": "actual"}
    file.file.seek(0, os.SEEK_END)
    if file.file.tell() > 5 * 1024 * 1024:
        return {"success": False, "tipo_mensaje": "rojo", "mensaje": "Máximo 5MB", "tiempo_mensaje": 6, "next_page": "actual"}
    file.file.seek(0)
    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto.proyecto_id))
    os.makedirs(proyecto_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = f"{campo}_{ts}{ext}"
    path = os.path.join(proyecto_dir, fn)
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # construir histórico
    raw = getattr(proyecto, campo) or ""
    try:
        arr = json.loads(raw) if raw.strip().startswith("[") else ([{"ruta": raw, "fecha": "desconocida"}] if raw else [])
    except:
        arr = []
    arr.append({"ruta": path, "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    setattr(proyecto, campo, json.dumps(arr, ensure_ascii=False))

    db.commit()   # <- usamos la sesión aquí
    return {"success": True, "tipo_mensaje": "verde", "mensaje": f"Subido '{fn}'", "tiempo_mensaje": 4, "next_page": "actual"}


def _download_all(raw:str, zipname:str, proyecto_id:int):
    """Si solo hay uno lo devuelve; si hay varios, arma ZIP."""
    try:
        arr = json.loads(raw) if raw.strip().startswith("[") else ([{"ruta":raw}] if raw else [])
    except:
        raise HTTPException(500,"JSON inválido")
    if not arr:
        raise HTTPException(404,"No hay documentos")
    if len(arr)==1:
        r=arr[0]["ruta"]
        if not os.path.exists(r): raise HTTPException(404,"No existe")
        return FileResponse(r, filename=os.path.basename(r))
    tmp = tempfile.NamedTemporaryFile(delete=False,suffix=".zip")
    with zipfile.ZipFile(tmp.name,"w",zipfile.ZIP_DEFLATED) as z:
        for e in arr:
            ruta=e.get("ruta")
            if ruta and os.path.exists(ruta):
                z.write(ruta, arcname=os.path.basename(ruta))
    return FileResponse(tmp.name, filename=f"{zipname}_{proyecto_id}.zip", media_type="application/zip")


def _get_proyecto_baja_caducidad_para_login(db: Session, login: Optional[str]):
    """Devuelve el proyecto más reciente en baja por caducidad para el login indicado."""
    if not login:
        return None

    return (
        db.query(Proyecto)
        .filter(
            Proyecto.ingreso_por == "rua",
            Proyecto.estado_general == "baja_caducidad",
            ((Proyecto.login_1 == login) | (Proyecto.login_2 == login))
        )
        .order_by(Proyecto.proyecto_id.desc())
        .first()
    )


def _set_estado_nna_por_proyecto(db: Session, proyecto_id: int, nuevo_estado: str) -> int:
    """
    Actualiza el estado (nna_estado) de todos los NNA asociados a las carpetas
    donde participa el proyecto dado. Devuelve la cantidad de NNA actualizados.
    Además registra el historial de cambios de estado.
    """

    subq_carpetas = (
        db.query(DetalleProyectosEnCarpeta.carpeta_id)
          .filter(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id)
          .subquery()
    )

    nnas_q = (
        db.query(Nna)
          .join(DetalleNNAEnCarpeta, DetalleNNAEnCarpeta.nna_id == Nna.nna_id)
          .filter(DetalleNNAEnCarpeta.carpeta_id.in_(subq_carpetas))
    )

    count = 0

    for nna in nnas_q.all():
        if nna.nna_estado != nuevo_estado:

            estado_anterior = nna.nna_estado
            nna.nna_estado = nuevo_estado

            db.add(NnaHistorialEstado(
                nna_id = nna.nna_id,
                estado_anterior = estado_anterior,
                estado_nuevo = nuevo_estado,
                fecha_hora = datetime.now()
            ))

            count += 1

    return count





FECHA_CORTE_ULTIMO_CAMBIO = date(2025, 6, 1)
ESTADOS_PREVIOS_A_VIABLE = [
    "en_revision", "actualizando", "aprobado",
    "calendarizando", "entrevistando", "para_valorar",
    "en_suspenso", "viable", "no_viable", "vinculacion",
    "guarda_provisoria", "guarda_confirmada",
    "adopcion_definitiva", "baja_anulacion", "baja_caducidad",
    "baja_por_convocatoria", "baja_rechazo_invitacion",
    "baja_interrupcion", "baja_desistimiento"
]

ESTADOS_CAMINO_A_CARPETA = (
    "en_carpeta",
    "enviada_a_juzgado",
    "vinculacion",
    "guarda_provisoria",
    "guarda_confirmada",
    "adopcion_definitiva",
    "en_suspenso",
)

FINAL_PROJECT_STATES = {
    "adopcion_definitiva",
    "baja_anulacion",
    "baja_caducidad",
    "baja_desistimiento",
    "baja_interrupcion",
    "baja_por_convocatoria",
    "baja_rechazo_invitacion",
}

ESTADOS_PROYECTO_ACTIVOS = (
    "invitacion_pendiente",
    "confeccionando",
    "en_revision",
    "actualizando",
    "enviada_a_juzgado",
    "aprobado",
    "calendarizando",
    "entrevistando",
    "para_valorar",
    "viable",
    "viable_no_disponible",
    "en_suspenso",
    "no_viable",
    "en_carpeta",
    "vinculacion",
    "guarda_provisoria",
    "guarda_confirmada",
)


def _preparar_pretensos_para_nuevo_proceso(db: Session, proyecto: Proyecto) -> None:
    """Resetea indicadores de DDJJ y documentación para habilitar un nuevo ciclo adoptivo."""

    if not proyecto or proyecto.ingreso_por != "rua":
        return

    for login in filter(None, [proyecto.login_1, proyecto.login_2]):
        user = db.query(User).filter(User.login == login).first()
        if not user:
            continue

        cambios = False
        if user.doc_adoptante_ddjj_firmada != "N":
            user.doc_adoptante_ddjj_firmada = "N"
            cambios = True

        if user.doc_adoptante_estado != "inicial_cargando":
            user.doc_adoptante_estado = "inicial_cargando"
            cambios = True

        if cambios:
            db.add(user)


def _preparar_pretensos_para_nuevo_proceso_por_logins(db: Session, logins: List[Optional[str]]) -> None:
    """Resetea indicadores de DDJJ y documentación para los logins indicados."""

    for login in filter(None, logins):
        user = db.query(User).filter(User.login == login).first()
        if not user:
            continue

        cambios = False
        if user.doc_adoptante_ddjj_firmada != "N":
            user.doc_adoptante_ddjj_firmada = "N"
            cambios = True

        if user.doc_adoptante_estado != "inicial_cargando":
            user.doc_adoptante_estado = "inicial_cargando"
            cambios = True

        if cambios:
            db.add(user)


def _clip_evento_detalle(texto: str, limit: int = 255) -> str:
    if not texto:
        return ""
    return texto[:limit]


def _is_monoparental_local(proyecto: Proyecto) -> bool:
    if (proyecto.proyecto_tipo or "").strip() == "Monoparental":
        return True
    return not (proyecto.login_2 and str(proyecto.login_2).strip())


def _query_grupo_proyectos(db: Session, proyecto: Proyecto):
    if _is_monoparental_local(proyecto):
        return db.query(Proyecto).filter(
            Proyecto.login_1 == proyecto.login_1,
            or_(Proyecto.login_2.is_(None), Proyecto.login_2 == "")
        )

    login_1 = (proyecto.login_1 or "").strip()
    login_2 = (proyecto.login_2 or "").strip()
    return db.query(Proyecto).filter(
        or_(
            and_(Proyecto.login_1 == login_1, Proyecto.login_2 == login_2),
            and_(Proyecto.login_1 == login_2, Proyecto.login_2 == login_1)
        )
    )


def _calcular_info_ratificacion_proyecto(proyecto: Proyecto, db: Session, logger=None):
    """Centraliza el cálculo de fechas de ratificación para un proyecto."""

    def log(msg: str) -> None:
        if logger:
            logger(msg)

    fecha_viable_a_viable = db.query(func.max(ProyectoHistorialEstado.fecha_hora)).filter(
        ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
        ProyectoHistorialEstado.estado_anterior == "viable",
        ProyectoHistorialEstado.estado_nuevo == "viable",
        ProyectoHistorialEstado.estado_anterior != "en_carpeta",
        ProyectoHistorialEstado.estado_nuevo != "en_carpeta"
    ).scalar()

    fecha_desde_estados_previos_a_viable = db.query(func.max(ProyectoHistorialEstado.fecha_hora)).filter(
        ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
        ProyectoHistorialEstado.estado_nuevo == "viable",
        ProyectoHistorialEstado.estado_anterior.in_(ESTADOS_PREVIOS_A_VIABLE),
        ProyectoHistorialEstado.estado_anterior.notin_(ESTADOS_CAMINO_A_CARPETA),
        ProyectoHistorialEstado.estado_anterior != "en_carpeta",
        ProyectoHistorialEstado.estado_nuevo != "en_carpeta"
    ).scalar()

    fecha_null_a_viable = db.query(func.max(ProyectoHistorialEstado.fecha_hora)).filter(
        ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
        ProyectoHistorialEstado.estado_nuevo == "viable",
        ProyectoHistorialEstado.estado_anterior.is_(None),
        ProyectoHistorialEstado.estado_nuevo != "en_carpeta"
    ).scalar()

    fecha_vinculacion = db.query(func.max(ProyectoHistorialEstado.fecha_hora)).filter(
        ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
        ProyectoHistorialEstado.estado_nuevo.in_(["vinculacion", "guarda_provisoria", "guarda_confirmada"])
    ).scalar()

    fecha_ultima_ratificacion = (
        db.query(func.max(UsuarioNotificadoRatificacion.ratificado))
        .filter(UsuarioNotificadoRatificacion.proyecto_id == proyecto.proyecto_id)
        .scalar()
    )

    log(f"   • fecha_viable_a_viable: {fecha_viable_a_viable}")
    log(f"   • fecha_desde_estados_previos_a_viable: {fecha_desde_estados_previos_a_viable}")
    log(f"   • fecha_null_a_viable: {fecha_null_a_viable}")
    log(f"   • fecha_vinculacion/guarda: {fecha_vinculacion}")
    log(f"   • fecha_ultima_ratificacion: {fecha_ultima_ratificacion}")
    log(f"   • ultimo_cambio_de_estado: {proyecto.ultimo_cambio_de_estado}")

    fechas_posibles = []

    if proyecto.ultimo_cambio_de_estado and proyecto.ultimo_cambio_de_estado <= FECHA_CORTE_ULTIMO_CAMBIO:
        fechas_posibles.append(datetime.combine(proyecto.ultimo_cambio_de_estado, time.min))
        log(f"   ✔️ Se considera ultimo_cambio_de_estado ({proyecto.ultimo_cambio_de_estado})")

    for fecha in (
        fecha_viable_a_viable,
        fecha_desde_estados_previos_a_viable,
        fecha_null_a_viable,
        fecha_vinculacion,
        fecha_ultima_ratificacion,
    ):
        if fecha:
            fechas_posibles.append(fecha)

    fecha_cambio_final = max(fechas_posibles) if fechas_posibles else None
    fecha_ratificacion_exacta = (fecha_cambio_final + timedelta(days=365)) if fecha_cambio_final else None
    fecha_ratificacion = (fecha_cambio_final + timedelta(days=356)) if fecha_cambio_final else None

    log(f"   ➤ fecha_cambio_final elegida: {fecha_cambio_final}")
    log(f"   ➤ fecha_ratificacion (aviso): {fecha_ratificacion}")
    log(f"   ➤ fecha_ratificacion_exacta (1 año): {fecha_ratificacion_exacta}")

    return {
        "fecha_cambio_final": fecha_cambio_final,
        "fecha_ratificacion": fecha_ratificacion,
        "fecha_ratificacion_exacta": fecha_ratificacion_exacta,
        "fecha_ultima_ratificacion": fecha_ultima_ratificacion,
    }






@proyectos_router.delete("/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def eliminar_proyecto(
    proyecto_id: int,
    login: str = Query(..., description="DNI de uno de los pretensos (login_1 o login_2)"),
    db: Session = Depends(get_db)
    ):

    """
    🔥 Elimina un proyecto y sus registros relacionados si el `login` proporcionado corresponde
    al `login_1` o `login_2` del proyecto.

    Borra:
    - DetalleEquipoEnProyecto
    - DetalleProyectosEnCarpeta
    - DetalleProyectoPostulacion
    - FechaRevision
    - ProyectoHistorialEstado
    - ObservacionesProyectos
    - AgendaEntrevistas
    - Proyecto

    Solo para rol 'administrador'.
    """
    try:
        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()

        if not proyecto:
            raise HTTPException(status_code = 404, detail = f"Proyecto con ID {proyecto_id} no encontrado.")

        if login not in [proyecto.login_1, proyecto.login_2]:
            raise HTTPException(status_code = 403, detail = f"El DNI '{login}' no forma parte del proyecto indicado.")

        # 🔸 Eliminar registros relacionados
        db.query(DetalleEquipoEnProyecto).filter(DetalleEquipoEnProyecto.proyecto_id == proyecto_id).delete()
        db.query(DetalleProyectosEnCarpeta).filter(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id).delete()
        db.query(DetalleProyectoPostulacion).filter(DetalleProyectoPostulacion.proyecto_id == proyecto_id).delete()

        db.query(FechaRevision).filter(FechaRevision.proyecto_id == proyecto_id).delete()
                
        db.query(ProyectoHistorialEstado).filter(ProyectoHistorialEstado.proyecto_id == proyecto_id).delete()
        db.query(ObservacionesProyectos).filter(ObservacionesProyectos.observacion_a_cual_proyecto == proyecto_id).delete()
        
        db.query(AgendaEntrevistas).filter(AgendaEntrevistas.proyecto_id == proyecto_id).delete()        

        db.delete(proyecto)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Proyecto #{proyecto_id} eliminado correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "menu_administrador/proyectos"
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al eliminar el proyecto: {str(e)}")




@proyectos_router.get("/", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def get_proyectos(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    # search: Optional[str] = Query(None, min_length=3, description="Búsqueda por al menos 3 dígitos alfanuméricos"),
    search: Optional[str] = Query(None, description="..."),
    proyecto_tipo: Optional[Literal["Monoparental", "Matrimonio", "Unión convivencial"]] = Query(
        None, description="Filtrar por tipo de proyecto (Monoparental, Matrimonio, Unión convivencial)"
    ),
    nro_orden_rua: Optional[int] = Query(None, description="Filtrar por número de orden"),
    fecha_nro_orden_inicio: Optional[str] = Query(None, 
                    description="Filtrar por fecha de asignación de nro. de orden, inicio (AAAA-MM-DD)"),
    fecha_nro_orden_fin: Optional[str] = Query(None,
                    description="Filtrar por fecha de asignación de nro. de orden, fin (AAAA-MM-DD)"),
    fecha_cambio_estado_inicio: Optional[str] = Query(None, 
                    description="Filtrar por fecha de último cambio de estado de proyecto, inicio (AAAA-MM-DD)"),
    fecha_cambio_estado_fin: Optional[str] = Query(None, 
                    description="Filtrar por fecha de último cambio de estado de proyecto, fin (AAAA-MM-DD)"),

    proyecto_estado_general: Optional[List[str]] = Query(None, description="Filtrar por uno o más estados generales del proyecto"),

    login_profesional: Optional[str] = Query(None, description="Filtrar proyectos asignados al profesional con este login"),

    ingreso_por: Optional[Literal["rua", "oficio", "convocatoria"]] = Query(
        None, description="Filtrar por rua, oficio o convocatoria"
    ),

    subregistros: Optional[List[str]] = Query(None, alias="subregistro_portada")  ):

    """
    📋 Devuelve un listado paginado de proyectos adoptivos, permitiendo aplicar múltiples filtros combinados.

    """

    try:

        User1 = aliased(User)
        User2 = aliased(User)

        query = (
            db.query(Proyecto)
            .options(
                joinedload(Proyecto.detalle_equipo_proyecto)
                .joinedload(DetalleEquipoEnProyecto.user)
            )
            .outerjoin(User1, Proyecto.login_1 == User1.login)
            .outerjoin(User2, Proyecto.login_2 == User2.login)
        )

        if fecha_nro_orden_inicio or fecha_nro_orden_fin:
            fecha_nro_orden_inicio = datetime.strptime(fecha_nro_orden_inicio, "%Y-%m-%d") if fecha_nro_orden_inicio else datetime(1970, 1, 1)
            fecha_nro_orden_fin = datetime.strptime(fecha_nro_orden_fin, "%Y-%m-%d") if fecha_nro_orden_fin else datetime.now()

            query = query.filter(
                Proyecto.fecha_asignacion_nro_orden != None,
                Proyecto.fecha_asignacion_nro_orden.between(fecha_nro_orden_inicio, fecha_nro_orden_fin)
            )

        if fecha_cambio_estado_inicio or fecha_cambio_estado_fin:
            fecha_cambio_estado_inicio = datetime.strptime(fecha_cambio_estado_inicio, "%Y-%m-%d") if fecha_cambio_estado_inicio else datetime(1970, 1, 1)
            fecha_cambio_estado_fin = datetime.strptime(fecha_cambio_estado_fin, "%Y-%m-%d") if fecha_cambio_estado_fin else datetime.now()

            query = query.filter(
                Proyecto.ultimo_cambio_de_estado != None,
                Proyecto.ultimo_cambio_de_estado.between(fecha_cambio_estado_inicio, fecha_cambio_estado_fin)
            )

        # Filtro por tipo de proyecto
        if proyecto_tipo:
            query = query.filter(Proyecto.proyecto_tipo == proyecto_tipo)

        # Filtro por rua, oficio o convocatoria
        if ingreso_por:
            query = query.filter(Proyecto.ingreso_por == ingreso_por)            

        if proyecto_estado_general:
            query = query.filter(Proyecto.estado_general.in_(proyecto_estado_general))

        # Filtro por nro de orden
        if nro_orden_rua and len(str(nro_orden_rua)) >= 2:
            search_pattern = f"%{nro_orden_rua}%"  # Busca cualquier nro_orden_rua que contenga estos números
            query = query.filter(Proyecto.nro_orden_rua.ilike(search_pattern))


        if login_profesional:
            subq_proyectos = db.query(DetalleEquipoEnProyecto.proyecto_id).filter(
                DetalleEquipoEnProyecto.login == login_profesional
            ).subquery()

            query = query.filter(Proyecto.proyecto_id.in_(subq_proyectos))

        subregistro_field_map = {
            "1": Proyecto.subreg_1,
            "2": Proyecto.subreg_2,
            "3": Proyecto.subreg_3,
            "4": Proyecto.subreg_4,
            "FE1": Proyecto.subreg_FE1,
            "FE2": Proyecto.subreg_FE2,
            "FE3": Proyecto.subreg_FE3,
            "FE4": Proyecto.subreg_FE4,
            "FET": Proyecto.subreg_FET,
            "5A1E1": Proyecto.subreg_5A1E1,
            "5A1E2": Proyecto.subreg_5A1E2,
            "5A1E3": Proyecto.subreg_5A1E3,
            "5A1E4": Proyecto.subreg_5A1E4,
            "5A1ET": Proyecto.subreg_5A1ET,
            "5A2E1": Proyecto.subreg_5A2E1,
            "5A2E2": Proyecto.subreg_5A2E2,
            "5A2E3": Proyecto.subreg_5A2E3,
            "5A2E4": Proyecto.subreg_5A2E4,
            "5A2ET": Proyecto.subreg_5A2ET,
            "5B1E1": Proyecto.subreg_5B1E1,
            "5B1E2": Proyecto.subreg_5B1E2,
            "5B1E3": Proyecto.subreg_5B1E3,
            "5B1E4": Proyecto.subreg_5B1E4,
            "5B1ET": Proyecto.subreg_5B1ET,
            "5B2E1": Proyecto.subreg_5B2E1,
            "5B2E2": Proyecto.subreg_5B2E2,
            "5B2E3": Proyecto.subreg_5B2E3,
            "5B2E4": Proyecto.subreg_5B2E4,
            "5B2ET": Proyecto.subreg_5B2ET,
            "5B3E1": Proyecto.subreg_5B3E1,
            "5B3E2": Proyecto.subreg_5B3E2,
            "5B3E3": Proyecto.subreg_5B3E3,
            "5B3E4": Proyecto.subreg_5B3E4,
            "5B3ET": Proyecto.subreg_5B3ET,
            "F5S": Proyecto.subreg_F5S, 
            "F5E1": Proyecto.subreg_F5E1,
            "F5E2": Proyecto.subreg_F5E2,
            "F5E3": Proyecto.subreg_F5E3,
            "F5E4": Proyecto.subreg_F5E4,
            "F5ET": Proyecto.subreg_F5ET,
            "61E1": Proyecto.subreg_61E1,
            "61E2": Proyecto.subreg_61E2,
            "61E3": Proyecto.subreg_61E3,
            "61ET": Proyecto.subreg_61ET,
            "62E1": Proyecto.subreg_62E1,
            "62E2": Proyecto.subreg_62E2,
            "62E3": Proyecto.subreg_62E3,
            "62ET": Proyecto.subreg_62ET,
            "63E1": Proyecto.subreg_63E1,
            "63E2": Proyecto.subreg_63E2,
            "63E3": Proyecto.subreg_63E3,
            "63ET": Proyecto.subreg_63ET,
            "FQ1": Proyecto.subreg_FQ1,
            "FQ2": Proyecto.subreg_FQ2,
            "FQ3": Proyecto.subreg_FQ3,
            "F6E1": Proyecto.subreg_F6E1,
            "F6E2": Proyecto.subreg_F6E2,
            "F6E3": Proyecto.subreg_F6E3,
            "F6ET": Proyecto.subreg_F6ET,
        }



        # Suponiendo que ya tenés `query` definido como tu query base
        if subregistros:
            subregistros = list(set(subregistros))  # evitar duplicados

            tags_padres = {
                "FE": ["FE1", "FE2", "FE3", "FE4", "FET"],
                "5A": ["5A1E1", "5A1E2", "5A1E3", "5A1E4", "5A1ET", "5A2E1", "5A2E2", "5A2E3", "5A2E4", "5A2ET"],
                "5B": ["5B1E1", "5B1E2", "5B1E3", "5B1E4", "5B1ET", "5B2E1", "5B2E2", "5B2E3", "5B2E4", "5B2ET", "5B3E1", "5B3E2", "5B3E3", "5B3E4", "5B3ET"],
                "F5": ["F5S", "F5E1", "F5E2", "F5E3", "F5E4", "F5ET"],
                "6": ["61E1", "61E2", "61E3", "61ET", "62E1", "62E2", "62E3", "62ET", "63E1", "63E2", "63E3", "63ET"],
                "F6": ["F6E1", "F6E2", "F6E3", "F6ET"],
            }

            # Excluir padres si ya se mandó algún hijo
            tags_excluidos = set()
            for padre, hijos in tags_padres.items():
                if padre in subregistros and any(hijo in subregistros for hijo in hijos):
                    tags_excluidos.add(padre)

            subregistros_filtrados = [sr for sr in subregistros if sr not in tags_excluidos]

            condiciones = []
            for sr in subregistros_filtrados:
                if sr in tags_padres:
                    grupo_or = [
                        subregistro_field_map[subtag] == "Y"
                        for subtag in tags_padres[sr]
                        if subtag in subregistro_field_map
                    ]
                    if grupo_or:
                        condiciones.append(or_(*grupo_or))
                else:
                    field = subregistro_field_map.get(sr)
                    if field:
                        condiciones.append(field == "Y")

            if condiciones:
                query = query.filter(and_(*condiciones))


        if search and len(search) >= 3:
            palabras = search.lower().split()
            condiciones_por_palabra = []

            for palabra in palabras:
                condiciones_por_palabra.append(
                    or_(
                        func.lower(func.concat(User1.nombre, " ", User1.apellido)).ilike(f"%{palabra}%"),
                        func.lower(func.concat(User2.nombre, " ", User2.apellido)).ilike(f"%{palabra}%"),
                        Proyecto.login_1.ilike(f"%{palabra}%"),
                        Proyecto.login_2.ilike(f"%{palabra}%"),
                        Proyecto.nro_orden_rua.ilike(f"%{palabra}%"),
                        Proyecto.proyecto_calle_y_nro.ilike(f"%{palabra}%"),
                        Proyecto.proyecto_barrio.ilike(f"%{palabra}%"),
                        Proyecto.proyecto_localidad.ilike(f"%{palabra}%"),
                        Proyecto.proyecto_provincia.ilike(f"%{palabra}%")
                    )
                )
            # Todas las palabras deben coincidir en algún campo (AND entre ORs)
            query = query.filter(and_(*condiciones_por_palabra))


        # Determina si nro_orden_rua es válido (4 o 5 dígitos numéricos)
        orden_valido = func.length(Proyecto.nro_orden_rua).in_([4, 5]) & Proyecto.nro_orden_rua.op('REGEXP')('^[0-9]+$')

        # Campo cast a entero si válido, NULL si no
        nro_orden_valido = case(
            (orden_valido, func.cast(Proyecto.nro_orden_rua, Integer)),
            else_=None
        )

        # Campo para forzar que los válidos aparezcan primero
        orden_es_valido = case(
            (orden_valido, 0),  # primero los válidos
            else_=1             # luego los inválidos o vacíos
        )

        query = query.order_by(
            orden_es_valido.asc(),          # 1. Primero los que tienen nro válido
            nro_orden_valido.asc(),        # 2. nro_orden válido (de mayor a menor)
            Proyecto.fecha_asignacion_nro_orden.desc()  # 3. fecha más antigua primero
        )

        # Paginación
        total_records = query.count()
        total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)
        if page > total_pages:
            return {"page": page, "limit": limit, "total_pages": total_pages, "total_records": total_records, "proyectos": []}

        skip = (page - 1) * limit
        proyectos = query.offset(skip).limit(limit).all()


        # Crear la lista de proyectos
        proyectos_list = []
        
        for proyecto in proyectos:

            # Profesionales asignadas → “Nombre Apellido; …”
            profesionales_asignadas = "; ".join(
                sorted(
                    [
                        f"{(d.user.nombre or '').split()[0]} {(d.user.apellido or '').split()[0]}"
                        for d in proyecto.detalle_equipo_proyecto
                        if d.user and d.user.nombre and d.user.apellido
                    ]
                )
            )

            comentarios_sobre_estado = None

            # 1. Casos entrevistando o calendarizando
            if proyecto.estado_general in ["calendarizando", "entrevistando"]:
                evaluaciones = db.query(AgendaEntrevistas.evaluacion_comentarios).filter(
                    AgendaEntrevistas.proyecto_id == proyecto.proyecto_id,
                    AgendaEntrevistas.evaluacion_comentarios != None,
                    AgendaEntrevistas.evaluacion_comentarios != ""
                ).all()

                if evaluaciones:
                    comentarios_sobre_estado = "Entrevistas realizadas:\n" + "\n".join(
                        f"- {e.evaluacion_comentarios}" for e in evaluaciones
                    )
                else:
                    comentarios_sobre_estado = "Aún no se registraron evaluaciones en las entrevistas."

            # 2. Casos vinculacion o guarda
            # Casos vinculacion o guarda → obtener NNA de la carpeta asociada
            elif proyecto.estado_general in ["vinculacion", "guarda_provisoria", "guarda_confirmada", "adopcion_definitiva"]:

                subquery_carpeta = db.query(DetalleProyectosEnCarpeta.carpeta_id).filter(
                    DetalleProyectosEnCarpeta.proyecto_id == proyecto.proyecto_id
                ).subquery()

                nna_relacionados = (
                    db.query(Nna.nna_nombre, Nna.nna_apellido)
                    .join(DetalleNNAEnCarpeta, DetalleNNAEnCarpeta.nna_id == Nna.nna_id)
                    .filter(DetalleNNAEnCarpeta.carpeta_id.in_(subquery_carpeta))
                    .all()
                )

                if nna_relacionados:
                    nombres_nna = list({f"{n.nna_nombre} {n.nna_apellido}" for n in nna_relacionados})
                    comentarios_sobre_estado = "NNA relacionado/s:\n" + "\n".join(nombres_nna)

            # 3. Caso en_carpeta
            elif proyecto.estado_general == "en_carpeta":
                carpeta = db.query(Carpeta).join(DetalleProyectosEnCarpeta).filter(
                    DetalleProyectosEnCarpeta.proyecto_id == proyecto.proyecto_id
                ).order_by(Carpeta.fecha_creacion.desc()).first()

                if carpeta:
                    estado_carpeta_map = {
                        "vacia": "Vacía",
                        "preparando_carpeta": "Preparando",
                        "enviada_a_juzgado": "Enviada a juzgado",
                        "proyecto_seleccionado": "Proyecto seleccionado"
                    }
                    estado_legible = estado_carpeta_map.get(carpeta.estado_carpeta, carpeta.estado_carpeta)
                    comentarios_sobre_estado = f"Estado de carpeta: '{estado_legible}'"



            proyecto_dict = {
                "proyecto_id": proyecto.proyecto_id,
                "proyecto_tipo": proyecto.proyecto_tipo,
                "nro_orden_rua": proyecto.nro_orden_rua,

                "subregistro_string": construir_subregistro_string(proyecto),  

                "proyecto_calle_y_nro": proyecto.proyecto_calle_y_nro,
                "proyecto_depto_etc": proyecto.proyecto_depto_etc,
                "proyecto_barrio": proyecto.proyecto_barrio,
                "proyecto_localidad": proyecto.proyecto_localidad,
                "proyecto_provincia": proyecto.proyecto_provincia,

                "login_1_name": get_user_name_by_login(db, proyecto.login_1),
                "login_1_dni": proyecto.login_1,
                "login_2_name": get_user_name_by_login(db, proyecto.login_2),
                "login_2_dni": proyecto.login_2,

                "fecha_asignacion_nro_orden": parse_date(proyecto.fecha_asignacion_nro_orden),
                "ultimo_cambio_de_estado": parse_date(proyecto.ultimo_cambio_de_estado),

                "doc_proyecto_convivencia_o_estado_civil": proyecto.doc_proyecto_convivencia_o_estado_civil,

                "estado_general": proyecto.estado_general,
                "comentarios_sobre_estado": comentarios_sobre_estado or "",

                "ingreso_por": proyecto.ingreso_por,

                "profesionales_asignadas": profesionales_asignadas,

                # "carpeta_ids": carpeta_ids,  # Lista de carpetas asociadas al proyecto

            }

            # 🔽 Agregar los campos subreg_... explícitamente al dict
            subregistros_definitivos = [
                "subreg_1", "subreg_2", "subreg_3", "subreg_4",
                "subreg_FE1", "subreg_FE2", "subreg_FE3", "subreg_FE4", "subreg_FET",
                "subreg_5A1E1", "subreg_5A1E2", "subreg_5A1E3", "subreg_5A1E4", "subreg_5A1ET",
                "subreg_5A2E1", "subreg_5A2E2", "subreg_5A2E3", "subreg_5A2E4", "subreg_5A2ET",
                "subreg_5B1E1", "subreg_5B1E2", "subreg_5B1E3", "subreg_5B1E4", "subreg_5B1ET",
                "subreg_5B2E1", "subreg_5B2E2", "subreg_5B2E3", "subreg_5B2E4", "subreg_5B2ET",
                "subreg_5B3E1", "subreg_5B3E2", "subreg_5B3E3", "subreg_5B3E4", "subreg_5B3ET",
                "subreg_F5S", "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
                "subreg_61E1", "subreg_61E2", "subreg_61E3", "subreg_61ET",
                "subreg_62E1", "subreg_62E2", "subreg_62E3", "subreg_62ET",
                "subreg_63E1", "subreg_63E2", "subreg_63E3", "subreg_63ET",
                "subreg_FQ1", "subreg_FQ2", "subreg_FQ3",
                "subreg_F6E1", "subreg_F6E2", "subreg_F6E3", "subreg_F6ET",
            ]

            for campo in subregistros_definitivos:
                proyecto_dict[campo] = getattr(proyecto, campo, None)

            
            if login_profesional:
                # Obtener profesionales del proyecto
                profesionales_proyecto = db.query(User).join(DetalleEquipoEnProyecto).filter(
                    DetalleEquipoEnProyecto.proyecto_id == proyecto.proyecto_id
                ).all()

                # Filtrar al profesional actual y determinar el resto
                otros = [p for p in profesionales_proyecto if p.login != login_profesional]

                if not otros:
                    junto_a = "Ninguna"
                elif len(otros) == 1:
                    junto_a = f"{otros[0].nombre} {otros[0].apellido}"
                else:
                    junto_a = " y ".join([p.nombre for p in otros[:2]])


                cant_entrevistas = db.query(func.count()).select_from(AgendaEntrevistas).filter(
                    AgendaEntrevistas.proyecto_id == proyecto.proyecto_id
                ).scalar()

                if proyecto.estado_general == "para_valorar":
                    etapa = "Para valorar"
                elif cant_entrevistas == 0:
                    etapa = "Calendarizando"
                else:
                    sufijos = ["era", "da", "era", "ta", "ta"]
                    sufijo = sufijos[cant_entrevistas - 1] if cant_entrevistas <= len(sufijos) else "ta"
                    etapa = f"{cant_entrevistas}{sufijo}. entrevista"


                proyecto_dict["junto_a"] = junto_a
                proyecto_dict["etapa"] = etapa



            proyectos_list.append(proyecto_dict)

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "proyectos": proyectos_list
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar los proyectos: {str(e)}")





@proyectos_router.get("/{proyecto_id}", response_model=dict, 
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador","supervision","supervisora","profesional","adoptante","coordinadora"]))])
def get_proyecto_por_id(
    request: Request,
    proyecto_id: int,
    db: Session = Depends(get_db)  ):

    """
    Obtiene los detalles de un proyecto específico según su `proyecto_id`
    y agrega listas extra (no intrusivas) de proyectos relacionados:
      - proyectos_rua_activos / proyectos_rua_cerrados / proyectos_convocatoria / proyectos_oficio
        calculados para la misma pareja (login_1, login_2) sin importar el orden.
      - otros_proyectos_login_1 / otros_proyectos_login_2: proyectos donde aparece cada pretenso
        (solo o con otra pareja), excluyendo los de esta misma pareja.
    """
    # ==============================
    # Constantes de estados/agrupación
    # ==============================
    ESTADOS_RUA_ACTIVOS = {
        "invitacion_pendiente", "confeccionando", "en_revision", "actualizando", "aprobado",
        "calendarizando", "entrevistando", "para_valorar", "viable", "viable_no_disponible",
        "en_suspenso", "no_viable", "en_carpeta", "vinculacion", "guarda_provisoria", "guarda_confirmada"
    }
    ESTADOS_RUA_CERRADOS = {
        "adopcion_definitiva", "baja_anulacion", "baja_caducidad",
        "baja_por_convocatoria", "baja_rechazo_invitacion", "baja_interrupcion"
    }

    def serialize_proyecto_lean(p):
        return {
            "proyecto_id": p.proyecto_id,
            "estado_general": p.estado_general,
            "proyecto_tipo": p.proyecto_tipo,
            "ingreso_por": p.ingreso_por,
            "nro_orden_rua": p.nro_orden_rua,
            "login_1": p.login_1,
            "login_2": p.login_2,
            "fecha_asignacion_nro_orden": parse_date(p.fecha_asignacion_nro_orden),
            "ultimo_cambio_de_estado": parse_date(p.ultimo_cambio_de_estado),
        }

    try:
        # ==============================
        # Consulta principal del proyecto
        # ==============================
        proyecto = (
            db.query(
                Proyecto.proyecto_id.label("proyecto_id"),
                Proyecto.proyecto_tipo.label("proyecto_tipo"),
                Proyecto.nro_orden_rua.label("nro_orden_rua"),
                Proyecto.operativo.label("proyecto_operativo"),
                Proyecto.login_1.label("login_1"),
                Proyecto.login_2.label("login_2"),
                Proyecto.aceptado.label("aceptado"),
                Proyecto.aceptado_code.label("aceptado_code"),
                Proyecto.doc_proyecto_convivencia_o_estado_civil.label("doc_proyecto_convivencia_o_estado_civil"),
                Proyecto.informe_profesionales.label("informe_profesionales"),
                Proyecto.doc_dictamen.label("doc_dictamen"),
                Proyecto.doc_informe_vinculacion.label("doc_informe_vinculacion"),
                Proyecto.doc_informe_seguimiento_guarda.label("doc_informe_seguimiento_guarda"),
                Proyecto.doc_sentencia_guarda.label("doc_sentencia_guarda"),
                Proyecto.doc_informe_conclusivo.label("doc_informe_conclusivo"),
                Proyecto.doc_sentencia_adopcion.label("doc_sentencia_adopcion"),
                Proyecto.doc_interrupcion.label("doc_interrupcion"),
                Proyecto.doc_baja_convocatoria.label("doc_baja_convocatoria"),
                Proyecto.subregistro_1.label("subregistro_1"),
                Proyecto.subregistro_2.label("subregistro_2"),
                Proyecto.subregistro_3.label("subregistro_3"),
                Proyecto.subregistro_4.label("subregistro_4"),
                Proyecto.subregistro_5_a.label("subregistro_5_a"),
                Proyecto.subregistro_5_b.label("subregistro_5_b"),
                Proyecto.subregistro_5_c.label("subregistro_5_c"),
                Proyecto.subregistro_6_a.label("subregistro_6_a"),
                Proyecto.subregistro_6_b.label("subregistro_6_b"),
                Proyecto.subregistro_6_c.label("subregistro_6_c"),
                Proyecto.subregistro_6_d.label("subregistro_6_d"),
                Proyecto.subregistro_6_2.label("subregistro_6_2"),
                Proyecto.subregistro_6_3.label("subregistro_6_3"),
                Proyecto.subregistro_6_mas_de_3.label("subregistro_6_mas_de_3"),
                Proyecto.subregistro_flexible.label("subregistro_flexible"),
                Proyecto.subregistro_otra_provincia.label("subregistro_otra_provincia"),
                Proyecto.proyecto_calle_y_nro.label("proyecto_calle_y_nro"),
                Proyecto.proyecto_depto_etc.label("proyecto_depto_etc"),
                Proyecto.proyecto_barrio.label("proyecto_barrio"),
                Proyecto.proyecto_localidad.label("proyecto_localidad"),
                Proyecto.proyecto_provincia.label("proyecto_provincia"),
                Proyecto.fecha_asignacion_nro_orden.label("fecha_asignacion_nro_orden"),
                Proyecto.ultimo_cambio_de_estado.label("ultimo_cambio_de_estado"),
                Proyecto.estado_general.label("estado_general"),
                Proyecto.ingreso_por.label("ingreso_por"),

                # Flexibilidad edad
                Proyecto.flex_edad_1.label("flex_edad_1"),
                Proyecto.flex_edad_2.label("flex_edad_2"),
                Proyecto.flex_edad_3.label("flex_edad_3"),
                Proyecto.flex_edad_4.label("flex_edad_4"),
                Proyecto.flex_edad_todos.label("flex_edad_todos"),

                # Discapacidad
                Proyecto.discapacidad_1.label("discapacidad_1"),
                Proyecto.discapacidad_2.label("discapacidad_2"),
                Proyecto.edad_discapacidad_0.label("edad_discapacidad_0"),
                Proyecto.edad_discapacidad_1.label("edad_discapacidad_1"),
                Proyecto.edad_discapacidad_2.label("edad_discapacidad_2"),
                Proyecto.edad_discapacidad_3.label("edad_discapacidad_3"),
                Proyecto.edad_discapacidad_4.label("edad_discapacidad_4"),

                # Enfermedades
                Proyecto.enfermedad_1.label("enfermedad_1"),
                Proyecto.enfermedad_2.label("enfermedad_2"),
                Proyecto.enfermedad_3.label("enfermedad_3"),
                Proyecto.edad_enfermedad_0.label("edad_enfermedad_0"),
                Proyecto.edad_enfermedad_1.label("edad_enfermedad_1"),
                Proyecto.edad_enfermedad_2.label("edad_enfermedad_2"),
                Proyecto.edad_enfermedad_3.label("edad_enfermedad_3"),
                Proyecto.edad_enfermedad_4.label("edad_enfermedad_4"),

                # Flexibilidad salud
                Proyecto.flex_condiciones_salud.label("flex_condiciones_salud"),
                Proyecto.flex_salud_edad_0.label("flex_salud_edad_0"),
                Proyecto.flex_salud_edad_1.label("flex_salud_edad_1"),
                Proyecto.flex_salud_edad_2.label("flex_salud_edad_2"),
                Proyecto.flex_salud_edad_3.label("flex_salud_edad_3"),
                Proyecto.flex_salud_edad_4.label("flex_salud_edad_4"),

                # Grupo de hermanos
                Proyecto.hermanos_comp_1.label("hermanos_comp_1"),
                Proyecto.hermanos_comp_2.label("hermanos_comp_2"),
                Proyecto.hermanos_comp_3.label("hermanos_comp_3"),
                Proyecto.hermanos_edad_0.label("hermanos_edad_0"),
                Proyecto.hermanos_edad_1.label("hermanos_edad_1"),
                Proyecto.hermanos_edad_2.label("hermanos_edad_2"),
                Proyecto.hermanos_edad_3.label("hermanos_edad_3"),
                Proyecto.flex_hermanos_comp_1.label("flex_hermanos_comp_1"),
                Proyecto.flex_hermanos_comp_2.label("flex_hermanos_comp_2"),
                Proyecto.flex_hermanos_comp_3.label("flex_hermanos_comp_3"),
                Proyecto.flex_hermanos_edad_0.label("flex_hermanos_edad_0"),
                Proyecto.flex_hermanos_edad_1.label("flex_hermanos_edad_1"),
                Proyecto.flex_hermanos_edad_2.label("flex_hermanos_edad_2"),
                Proyecto.flex_hermanos_edad_3.label("flex_hermanos_edad_3"),

                # Subregistros definitivos
                Proyecto.subreg_1, Proyecto.subreg_2, Proyecto.subreg_3, Proyecto.subreg_4,
                Proyecto.subreg_FE1, Proyecto.subreg_FE2, Proyecto.subreg_FE3, Proyecto.subreg_FE4, Proyecto.subreg_FET,
                Proyecto.subreg_5A1E1, Proyecto.subreg_5A1E2, Proyecto.subreg_5A1E3, Proyecto.subreg_5A1E4, Proyecto.subreg_5A1ET,
                Proyecto.subreg_5A2E1, Proyecto.subreg_5A2E2, Proyecto.subreg_5A2E3, Proyecto.subreg_5A2E4, Proyecto.subreg_5A2ET,
                Proyecto.subreg_5B1E1, Proyecto.subreg_5B1E2, Proyecto.subreg_5B1E3, Proyecto.subreg_5B1E4, Proyecto.subreg_5B1ET,
                Proyecto.subreg_5B2E1, Proyecto.subreg_5B2E2, Proyecto.subreg_5B2E3, Proyecto.subreg_5B2E4, Proyecto.subreg_5B2ET,
                Proyecto.subreg_5B3E1, Proyecto.subreg_5B3E2, Proyecto.subreg_5B3E3, Proyecto.subreg_5B3E4, Proyecto.subreg_5B3ET,
                Proyecto.subreg_F5S, Proyecto.subreg_F5E1, Proyecto.subreg_F5E2, Proyecto.subreg_F5E3, Proyecto.subreg_F5E4, Proyecto.subreg_F5ET,
                Proyecto.subreg_61E1, Proyecto.subreg_61E2, Proyecto.subreg_61E3, Proyecto.subreg_61ET,
                Proyecto.subreg_62E1, Proyecto.subreg_62E2, Proyecto.subreg_62E3, Proyecto.subreg_62ET,
                Proyecto.subreg_63E1, Proyecto.subreg_63E2, Proyecto.subreg_63E3, Proyecto.subreg_63ET,
                Proyecto.subreg_FQ1, Proyecto.subreg_FQ2, Proyecto.subreg_FQ3,
                Proyecto.subreg_F6E1, Proyecto.subreg_F6E2, Proyecto.subreg_F6E3, Proyecto.subreg_F6ET,
            )
            .filter(Proyecto.proyecto_id == proyecto_id)
            .first()
        )

        if not proyecto:
            raise HTTPException(status_code=404, detail=f"Proyecto con ID {proyecto_id} no encontrado.")


        info_ratificacion = _calcular_info_ratificacion_proyecto(proyecto, db)
        fecha_cambio_final = info_ratificacion["fecha_cambio_final"]
        fecha_ratificacion_exacta = info_ratificacion["fecha_ratificacion_exacta"]
        fecha_ratificacion_aviso = info_ratificacion["fecha_ratificacion"]
        fecha_ultima_ratificacion = info_ratificacion["fecha_ultima_ratificacion"]



        # --------- Datos de contacto de login_1 / login_2 ---------
        login_1_user = db.query(User).filter(User.login == proyecto.login_1).first()
        login_2_user = db.query(User).filter(User.login == proyecto.login_2).first()

        login_1_telefono = login_1_user.celular if login_1_user else None
        login_2_telefono = login_2_user.celular if login_2_user else None
        login_1_mail = login_1_user.mail if login_1_user else None
        login_2_mail = login_2_user.mail if login_2_user else None
        login_1_nombre_completo = f"{login_1_user.nombre} {login_1_user.apellido}" if login_1_user else None
        login_2_nombre_completo = f"{login_2_user.nombre} {login_2_user.apellido}" if login_2_user else None

        texto_boton_estado_proyecto = {
            "invitacion_pendiente": "CARGANDO P.",
            "confeccionando": "CARGANDO P.",
            "en_revision": "EN REVISIÓN",
            "actualizando": "ACTUALIZANDO P.",
            "aprobado": "P. APROBADO",
            "calendarizando": "CALENDARIZANDO",
            "entrevistando": "ENTREVISTAS",
            "para_valorar": "PARA VALORAR",
            "viable": "VIABLE",
            "viable_no_disponible": "VIABLE NO DISP.",
            "en_suspenso": "EN SUSPENSO",
            "no_viable": "NO VIABLE",
            "en_carpeta": "EN CARPETA",
            "vinculacion": "VINCULACIÓN",
            "guarda_provisoria": "GUARDA PROVISORIA",
            "guarda_confirmada": "GUARDA CONFIRMADA",
            "adopcion_definitiva": "ADOPCIÓN DEF.",
            "baja_anulacion": "P. BAJA ANUL.",
            "baja_caducidad": "P. BAJA CADUC.",
            "baja_por_convocatoria": "P. BAJA CONV.",
            "baja_rechazo_invitacion": "P. BAJA RECHAZO",
            "baja_interrupcion": "P. BAJA INTERR.",
            "baja_desistimiento": "P. BAJA DESIST.",
        }.get(proyecto.estado_general, "ESTADO DESCONOCIDO")

        texto_ingreso_por = {
            "rua": "RUA",
            "oficio": "OFICIO",
            "convocatoria": "CONV.",
        }.get(proyecto.ingreso_por, "RUA")

        


        # ===== Helpers para vacíos / igualdad de pareja =====
        def is_blank(v):
            return v is None or (isinstance(v, str) and v.strip() == "")

        def norm(v):
            return None if is_blank(v) else v

        # Logins normalizados de este proyecto
        l1 = norm(proyecto.login_1)
        l2 = norm(proyecto.login_2)

        def es_misma_pareja(p: Proyecto) -> bool:
            a1, a2 = norm(p.login_1), norm(p.login_2)
            # misma pareja = mismo conjunto {l1,l2} sin importar orden
            return (a1 == l1 and a2 == l2) or (a1 == l2 and a2 == l1)


        # ==============================
        # Armar respuesta base (igual a la tuya)
        # ==============================
        proyecto_dict = {
            "proyecto_id": proyecto.proyecto_id,
            "proyecto_tipo": proyecto.proyecto_tipo,
            "nro_orden_rua": proyecto.nro_orden_rua,
            "subregistro_string": construir_subregistro_string(proyecto),

            "proyecto_calle_y_nro": proyecto.proyecto_calle_y_nro,
            "proyecto_depto_etc": proyecto.proyecto_depto_etc,
            "proyecto_barrio": proyecto.proyecto_barrio,
            "proyecto_localidad": proyecto.proyecto_localidad,
            "proyecto_provincia": proyecto.proyecto_provincia,

            "login_1_dni": proyecto.login_1,
            "login_1_name": login_1_nombre_completo,
            "login_1_telefono": login_1_telefono,
            "login_1_mail": login_1_mail,

            "login_2_dni": proyecto.login_2,
            "login_2_name": login_2_nombre_completo,
            "login_2_telefono": login_2_telefono,
            "login_2_mail": login_2_mail,

            "aceptado": proyecto.aceptado,
            "aceptado_code": proyecto.aceptado_code,

            "fecha_asignacion_nro_orden": parse_date(proyecto.fecha_asignacion_nro_orden),
            "ultimo_cambio_de_estado": parse_date(proyecto.ultimo_cambio_de_estado),

            "doc_proyecto_convivencia_o_estado_civil": proyecto.doc_proyecto_convivencia_o_estado_civil,
            "informe_profesionales": proyecto.informe_profesionales,
            "doc_dictamen": proyecto.doc_dictamen,

            "doc_informe_vinculacion": proyecto.doc_informe_vinculacion,
            "doc_informe_seguimiento_guarda": proyecto.doc_informe_seguimiento_guarda,
            "doc_sentencia_guarda": proyecto.doc_sentencia_guarda,
            "doc_informe_conclusivo": proyecto.doc_informe_conclusivo,
            "doc_sentencia_adopcion": proyecto.doc_sentencia_adopcion,
            "doc_interrupcion": proyecto.doc_interrupcion,
            "doc_baja_convocatoria": proyecto.doc_baja_convocatoria,

            # "boton_solicitar_actualizacion_proyecto": proyecto.estado_general == "en_revision" and \
            #     proyecto.proyecto_tipo in ("Matrimonio", "Unión convivencial"),
            "boton_solicitar_actualizacion_proyecto": proyecto.estado_general == "en_revision",

            "boton_valorar_proyecto": proyecto.estado_general == "en_revision",
            "boton_para_valoracion_final_proyecto": proyecto.estado_general == "para_valorar",
            "boton_para_sentencia_guarda": proyecto.estado_general == "vinculacion",
            "boton_para_sentencia_adopcion": proyecto.estado_general == "guarda_confirmada",
            "boton_agregar_a_carpeta": proyecto.estado_general == "viable",

            "texto_boton_estado_proyecto": texto_boton_estado_proyecto,
            "estado_general": proyecto.estado_general,
            "ingreso_por": proyecto.ingreso_por,          # <-- crudo: "rua" | "oficio" | "convocatoria"
            "texto_ingreso_por": texto_ingreso_por,       # <-- nuevo: "RUA" | "OFICIO" | "CONV."
            
            "subregistro_1": proyecto.subregistro_1,
            "subregistro_2": proyecto.subregistro_2,
            "subregistro_3": proyecto.subregistro_3,
            "subregistro_4": proyecto.subregistro_4,

            "flex_edad_1": proyecto.flex_edad_1,
            "flex_edad_2": proyecto.flex_edad_2,
            "flex_edad_3": proyecto.flex_edad_3,
            "flex_edad_4": proyecto.flex_edad_4,
            "flex_edad_todos": proyecto.flex_edad_todos,

            "discapacidad_1": proyecto.discapacidad_1,
            "discapacidad_2": proyecto.discapacidad_2,
            "edad_discapacidad_0": proyecto.edad_discapacidad_0,
            "edad_discapacidad_1": proyecto.edad_discapacidad_1,
            "edad_discapacidad_2": proyecto.edad_discapacidad_2,
            "edad_discapacidad_3": proyecto.edad_discapacidad_3,
            "edad_discapacidad_4": proyecto.edad_discapacidad_4,

            "enfermedad_1": proyecto.enfermedad_1,
            "enfermedad_2": proyecto.enfermedad_2,
            "enfermedad_3": proyecto.enfermedad_3,
            "edad_enfermedad_0": proyecto.edad_enfermedad_0,
            "edad_enfermedad_1": proyecto.edad_enfermedad_1,
            "edad_enfermedad_2": proyecto.edad_enfermedad_2,
            "edad_enfermedad_3": proyecto.edad_enfermedad_3,
            "edad_enfermedad_4": proyecto.edad_enfermedad_4,

            "flex_condiciones_salud": proyecto.flex_condiciones_salud,
            "flex_salud_edad_0": proyecto.flex_salud_edad_0,
            "flex_salud_edad_1": proyecto.flex_salud_edad_1,
            "flex_salud_edad_2": proyecto.flex_salud_edad_2,
            "flex_salud_edad_3": proyecto.flex_salud_edad_3,
            "flex_salud_edad_4": proyecto.flex_salud_edad_4,

            "hermanos_comp_1": proyecto.hermanos_comp_1,
            "hermanos_comp_2": proyecto.hermanos_comp_2,
            "hermanos_comp_3": proyecto.hermanos_comp_3,
            "hermanos_edad_0": proyecto.hermanos_edad_0,
            "hermanos_edad_1": proyecto.hermanos_edad_1,
            "hermanos_edad_2": proyecto.hermanos_edad_2,
            "hermanos_edad_3": proyecto.hermanos_edad_3,
            "flex_hermanos_comp_1": proyecto.flex_hermanos_comp_1,
            "flex_hermanos_comp_2": proyecto.flex_hermanos_comp_2,
            "flex_hermanos_comp_3": proyecto.flex_hermanos_comp_3,
            "flex_hermanos_edad_0": proyecto.flex_hermanos_edad_0,
            "flex_hermanos_edad_1": proyecto.flex_hermanos_edad_1,
            "flex_hermanos_edad_2": proyecto.flex_hermanos_edad_2,
            "flex_hermanos_edad_3": proyecto.flex_hermanos_edad_3,
        }

        # Agrego todos los subreg_* definitivos
        subregistros_def = [
            "subreg_1", "subreg_2", "subreg_3", "subreg_4",
            "subreg_FE1", "subreg_FE2", "subreg_FE3", "subreg_FE4", "subreg_FET",
            "subreg_5A1E1", "subreg_5A1E2", "subreg_5A1E3", "subreg_5A1E4", "subreg_5A1ET",
            "subreg_5A2E1", "subreg_5A2E2", "subreg_5A2E3", "subreg_5A2E4", "subreg_5A2ET",
            "subreg_5B1E1", "subreg_5B1E2", "subreg_5B1E3", "subreg_5B1E4", "subreg_5B1ET",
            "subreg_5B2E1", "subreg_5B2E2", "subreg_5B2E3", "subreg_5B2E4", "subreg_5B2ET",
            "subreg_5B3E1", "subreg_5B3E2", "subreg_5B3E3", "subreg_5B3E4", "subreg_5B3ET",
            "subreg_F5S", "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
            "subreg_61E1", "subreg_61E2", "subreg_61E3", "subreg_61ET",
            "subreg_62E1", "subreg_62E2", "subreg_62E3", "subreg_62ET",
            "subreg_63E1", "subreg_63E2", "subreg_63E3", "subreg_63ET",
            "subreg_FQ1", "subreg_FQ2", "subreg_FQ3",
            "subreg_F6E1", "subreg_F6E2", "subreg_F6E3", "subreg_F6ET",
        ]
        for campo in subregistros_def:
            proyecto_dict[campo] = getattr(proyecto, campo, None)

        # ==============================
        # NNA asociados y convocatoria (igual a tu lógica)
        # ==============================
        nna_asociados, nna_ids_agregados = [], set()
        detalle_proyecto_carpeta = db.query(DetalleProyectosEnCarpeta).filter(
            DetalleProyectosEnCarpeta.proyecto_id == proyecto.proyecto_id
        ).first()

        if detalle_proyecto_carpeta:
            carpeta = detalle_proyecto_carpeta.carpeta
            if carpeta:
                for detalle_nna in carpeta.detalle_nna:
                    nna = detalle_nna.nna
                    if nna and nna.nna_id not in nna_ids_agregados:
                        edad = date.today().year - nna.nna_fecha_nacimiento.year - (
                            (date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
                        ) if nna.nna_fecha_nacimiento else None
                        nna_asociados.append({"nna_id": nna.nna_id, "nna_nombre": nna.nna_nombre,
                                              "nna_apellido": nna.nna_apellido, "nna_edad": edad})
                        nna_ids_agregados.add(nna.nna_id)

        convocatoria_info = None
        if proyecto.ingreso_por == "convocatoria":
            detalle_postulacion = db.query(DetalleProyectoPostulacion).filter(
                DetalleProyectoPostulacion.proyecto_id == proyecto.proyecto_id
            ).first()
            if detalle_postulacion:
                postulacion = detalle_postulacion.postulacion
                convocatoria = postulacion.convocatoria if postulacion else None
                if convocatoria:
                    convocatoria_info = {
                        "convocatoria_id": convocatoria.convocatoria_id,
                        "convocatoria_referencia": convocatoria.convocatoria_referencia,
                        "convocatoria_llamado": convocatoria.convocatoria_llamado,
                        "convocatoria_juzgado_interviniente": convocatoria.convocatoria_juzgado_interviniente,
                        "convocatoria_fecha_publicacion": str(convocatoria.convocatoria_fecha_publicacion),
                    }
                    for detalle_nna in convocatoria.detalle_nnas:
                        nna = detalle_nna.nna
                        if nna and nna.nna_id not in nna_ids_agregados:
                            edad = date.today().year - nna.nna_fecha_nacimiento.year - (
                                (date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
                            ) if nna.nna_fecha_nacimiento else None
                            nna_asociados.append({"nna_id": nna.nna_id, "nna_nombre": nna.nna_nombre,
                                                  "nna_apellido": nna.nna_apellido, "nna_edad": edad})
                            nna_ids_agregados.add(nna.nna_id)

        proyecto_dict["nna_asociados"] = nna_asociados
        proyecto_dict["convocatoria"] = convocatoria_info


        # Si es biparental: (l1,l2) u (l2,l1). Si es monoparental: (l1,None) u (None,l1)
        if l2 is None:
            pareja_projects_q = (
                db.query(Proyecto)
                .filter(
                    or_(
                        and_(Proyecto.login_1 == l1, or_(Proyecto.login_2 == None, Proyecto.login_2 == "")),
                        and_(Proyecto.login_2 == l1, or_(Proyecto.login_1 == None, Proyecto.login_1 == "")),
                    )
                )
                .order_by(desc(func.coalesce(Proyecto.ultimo_cambio_de_estado, Proyecto.fecha_asignacion_nro_orden)))
            )
        else:
            pareja_projects_q = (
                db.query(Proyecto)
                .filter(
                    or_(
                        and_(Proyecto.login_1 == l1, Proyecto.login_2 == l2),
                        and_(Proyecto.login_1 == l2, Proyecto.login_2 == l1),
                    )
                )
                .order_by(desc(func.coalesce(Proyecto.ultimo_cambio_de_estado, Proyecto.fecha_asignacion_nro_orden)))
            )

        pareja_projects = pareja_projects_q.all()


        proyectos_rua_activos = [serialize_proyecto_lean(p) for p in pareja_projects
                                 if (p.ingreso_por == "rua" and p.estado_general in ESTADOS_RUA_ACTIVOS)]
        proyectos_rua_cerrados = [serialize_proyecto_lean(p) for p in pareja_projects
                                  if (p.ingreso_por == "rua" and p.estado_general in ESTADOS_RUA_CERRADOS)]
        proyectos_convocatoria = [serialize_proyecto_lean(p) for p in pareja_projects if p.ingreso_por == "convocatoria"]
        proyectos_oficio = [serialize_proyecto_lean(p) for p in pareja_projects if p.ingreso_por == "oficio"]


        # Todos los proyectos donde aparece login_1
        proyectos_login_1 = (
            db.query(Proyecto)
            .filter(or_(Proyecto.login_1 == l1, Proyecto.login_2 == l1))
            .order_by(desc(func.coalesce(Proyecto.ultimo_cambio_de_estado, Proyecto.fecha_asignacion_nro_orden)))
            .all()
        )

        # Si el proyecto es monoparental, NO consultes por login_2 (evita traer proyectos con NULL/'' en masa)
        if l2 is None:
            proyectos_login_2 = []
        else:
            proyectos_login_2 = (
                db.query(Proyecto)
                .filter(or_(Proyecto.login_1 == l2, Proyecto.login_2 == l2))
                .order_by(desc(func.coalesce(Proyecto.ultimo_cambio_de_estado, Proyecto.fecha_asignacion_nro_orden)))
                .all()
        )
            
        otros_proyectos_login_1 = []
        for p in proyectos_login_1:
            if p.proyecto_id == proyecto.proyecto_id or es_misma_pareja(p):
                continue
            otros_proyectos_login_1.append({
                **serialize_proyecto_lean(p),
                "pareja_login": p.login_2 if p.login_1 == l1 else p.login_1 or None,
            })

        otros_proyectos_login_2 = []
        for p in proyectos_login_2:
            if p.proyecto_id == proyecto.proyecto_id or es_misma_pareja(p):
                continue
            otros_proyectos_login_2.append({
                **serialize_proyecto_lean(p),
                "pareja_login": p.login_2 if p.login_1 == l2 else p.login_1 or None,
            })

        # ============================================================
        # Anexar NUEVAS CLAVES sin tocar lo anterior
        # ============================================================
        proyecto_dict.update({
            # 4 listas pedidas (misma pareja)
            "proyectos_rua_activos": proyectos_rua_activos,
            "proyectos_rua_cerrados": proyectos_rua_cerrados,
            "proyectos_convocatoria": proyectos_convocatoria,
            "proyectos_oficio": proyectos_oficio,

            # Campos extra: otros proyectos de cada pretenso
            "otros_proyectos_login_1": otros_proyectos_login_1,
            "otros_proyectos_login_2": otros_proyectos_login_2,
        })


        proyecto_dict.update({
            "fecha_cambio_final": fecha_cambio_final.strftime("%Y-%m-%d") if fecha_cambio_final else None,
            "fecha_ratificacion": fecha_ratificacion_aviso.strftime("%Y-%m-%d") if fecha_ratificacion_aviso else None,
            "fecha_ratificacion_exacta": fecha_ratificacion_exacta.strftime("%Y-%m-%d") if fecha_ratificacion_exacta else None,
            "fecha_ultima_ratificacion": fecha_ultima_ratificacion.strftime("%Y-%m-%d") if fecha_ultima_ratificacion else None,
        })
        


        return proyecto_dict

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar el proyecto: {str(e)}")




@proyectos_router.post("/validar-pretenso", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "adoptante"]))])
def validar_pretenso(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    🔐 Valida que un login + mail correspondan a un usuario con rol 'adoptante',
    que no sea el usuario actualmente autenticado, y que existan en el sistema.
    """

    
    login_actual = current_user["user"]["login"]

    login = data.get("login", "").strip()
    mail = data.get("mail", "").strip()

    if not login.strip() or not mail.strip():
        return {
            "success": False,
            "tipo_mensaje": "amarillo",
            "mensaje": "Debe completar el DNI y el mail para validar que correspondan a una persona registrada en el sistema RUA.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if login == login_actual:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "No puede invitarse a usted.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    user = db.query(User).filter(User.login == login).first()
    if not user:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"La persona con DNI '{login}' no fue encontrada en el Sistema RUA.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if (user.mail or "").strip().lower() != mail.strip().lower():
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El mail proporcionado no coincide con el registrado para esta persona.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Verificar que el usuario tenga el rol 'adoptante'
    user_roles = db.query(UserGroup).filter(UserGroup.login == login).all()
    es_adoptante = any(
        db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first()
        for r in user_roles
    )

    if not es_adoptante:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"El usuario '{login}' no tiene rol 'adoptante'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": f"La persona con DNI '{login}' y mail '{mail}' será invitada a este proyecto cuando complete todo este formulario.",
        "tiempo_mensaje": 8,
        "next_page": "actual"
    }



@proyectos_router.post("/preliminar", response_model = dict,
                       dependencies = [Depends(verify_api_key), Depends(require_roles(["adoptante"]))])
def crear_proyecto_preliminar(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
    ):

    """
    Este endpoint permite crear un nuevo proyecto con los datos mínimos requeridos con login_1 como el usuario autenticado.

    🔹 Requiere:
    - `proyecto_tipo`: 'Monoparental', 'Matrimonio' o 'Unión convivencial'.

    🔸 Opcional:
    - `login_2`: Solo si el tipo de proyecto es en pareja. Debe existir, tener rol 'adoptante' y ser distinto del usuario autenticado.

    🔄 Completado automático:
    - Se extraen de la DDJJ del `login_1` información de su DDJJ para completar el proyecto.

    """
    try:
        usuario_actual_login = current_user["user"]["login"]
        nombre_actual = current_user["user"]["nombre"]
        apellido_actual = current_user["user"]["apellido"]

        proyecto_tipo = data.get("proyecto_tipo")
        login_2 = data.get("login_2")

        if not usuario_actual_login or not proyecto_tipo:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "El campo 'proyecto_tipo' es obligatorio.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        if proyecto_tipo not in ["Monoparental", "Matrimonio", "Unión convivencial"]:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Tipo de proyecto inválido.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        roles1 = db.query(UserGroup).filter(UserGroup.login == usuario_actual_login).all()
        if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in roles1):
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"El usuario '{usuario_actual_login}' no tiene el rol 'adoptante'.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        proyecto_baja_caducidad_login1 = _get_proyecto_baja_caducidad_para_login(db, usuario_actual_login)
        if proyecto_baja_caducidad_login1:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Tu último proyecto fue dado de baja por caducidad. Comunicate con el equipo del RUA.",
                "tiempo_mensaje": 8,
                "next_page": "actual"
            }

        doc_adoptante_curso_aprobado = True
        aceptado_code = None

        if proyecto_tipo in ["Matrimonio", "Unión convivencial"]:
            if not login_2:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Para el tipo de proyecto '{proyecto_tipo}' debe incluirse el DNI del segundo pretenso.",
                    "tiempo_mensaje": 4,
                    "next_page": "actual"
                }

            if login_2 == usuario_actual_login:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"El DNI del segundo pretenso debe ser distinto al DNI del usuario autenticado.",
                    "tiempo_mensaje": 4,
                    "next_page": "actual"
                }

            user2 = db.query(User).filter(User.login == login_2).first()
            if not user2:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"El usuario con DNI '{login_2}' no existe.",
                    "tiempo_mensaje": 4,
                    "next_page": "actual"
                }

            roles2 = db.query(UserGroup).filter(UserGroup.login == login_2).all()
            if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in roles2):
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"El usuario '{login_2}' no tiene el rol 'adoptante'.",
                    "tiempo_mensaje": 4,
                    "next_page": "actual"
                }

            proyecto_baja_caducidad_login2 = _get_proyecto_baja_caducidad_para_login(db, login_2)
            if proyecto_baja_caducidad_login2:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        f"La persona con DNI '{login_2}' tiene un proyecto dado de baja por caducidad. "
                        "Comuníquense con el equipo del RUA."
                    ),
                    "tiempo_mensaje": 8,
                    "next_page": "actual"
                }

            proyecto_activo = db.query(Proyecto).filter(
                Proyecto.login_2 == login_2,
                Proyecto.estado_general.in_(["creado", "confeccionando", "en_revision", "actualizando", "aprobado", 
                                             "calendarizando", "entrevistando", "para_valorar",
                                             "viable", "en_suspenso", "en_carpeta", "vinculacion", 
                                             "guarda_provisoria", "guarda_confirmada"])
            ).first()

            if proyecto_activo:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"La persona con DNI '{login_2}' ya forma parte de un proyecto en curso.",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }

            # Verificar curso
            doc_adoptante_curso_aprobado = (getattr(user2, "doc_adoptante_curso_aprobado", "N") == "Y")

            aceptado_code = generar_codigo_para_link(16)

        ddjj = db.query(DDJJ).filter(DDJJ.login == usuario_actual_login).first()

        def subreg(key):
            val = getattr(ddjj, f"ddjj_{key}", None)
            return val if val in ["Y", "N"] else "N"

        estado_general = (
            "confeccionando" if proyecto_tipo == "Monoparental" else "invitacion_pendiente"
        )


        nuevo_proyecto = Proyecto(
            login_1 = usuario_actual_login,
            login_2 = login_2,
            proyecto_tipo = proyecto_tipo,
            proyecto_calle_y_nro = ddjj.ddjj_calle if ddjj else None,
            proyecto_depto_etc = ddjj.ddjj_depto if ddjj else None,
            proyecto_barrio = ddjj.ddjj_barrio if ddjj else None,
            proyecto_localidad = ddjj.ddjj_localidad if ddjj else None,
            proyecto_provincia = ddjj.ddjj_provincia if ddjj else None,

            subregistro_1 = subreg("subregistro_1"),
            subregistro_2 = subreg("subregistro_2"),
            subregistro_3 = subreg("subregistro_3"),
            subregistro_4 = subreg("subregistro_4"),
            subregistro_5_a = subreg("subregistro_5_a"),
            subregistro_5_b = subreg("subregistro_5_b"),
            subregistro_5_c = subreg("subregistro_5_c"),
            subregistro_6_a = subreg("subregistro_6_a"),
            subregistro_6_b = subreg("subregistro_6_b"),
            subregistro_6_c = subreg("subregistro_6_c"),
            subregistro_6_d = subreg("subregistro_6_d"),
            subregistro_6_2 = subreg("subregistro_6_2"),
            subregistro_6_3 = subreg("subregistro_6_3"),
            subregistro_6_mas_de_3 = subreg("subregistro_6_mas_de_3"),
            subregistro_flexible = subreg("subregistro_flexible"),
            subregistro_otra_provincia = subreg("subregistro_otra_provincia"),

            operativo = 'Y',
            estado_general = estado_general,

            aceptado = "N" if aceptado_code else None,
            aceptado_code = aceptado_code
        )

        db.add(nuevo_proyecto)
        db.commit()
        db.refresh(nuevo_proyecto)

        if aceptado_code:
            try:
                
                # Configuración del sistema
                protocolo = get_setting_value(db, "protocolo")
                host = get_setting_value(db, "donde_esta_alojado")
                puerto = get_setting_value(db, "puerto_tcp")
                endpoint = get_setting_value(db, "endpoint_aceptar_invitacion")

                # Asegurar formato correcto del endpoint
                if endpoint and not endpoint.startswith("/"):
                    endpoint = "/" + endpoint


                # Determinar si incluir el puerto en la URL
                puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
                host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

                link_aceptar = f"{protocolo}://{host_con_puerto}{endpoint}?invitacion={aceptado_code}&respuesta=Y"
                link_rechazar = f"{protocolo}://{host_con_puerto}{endpoint}?invitacion={aceptado_code}&respuesta=N"


                asunto = "Invitación a proyecto adoptivo - Sistema RUA"

                aviso_curso = ""
                if not doc_adoptante_curso_aprobado:
                    aviso_curso = "<p style='color: red;'><strong>⚠️ Para aceptar la invitación, debés tener aprobado el Curso Obligatorio.</strong></p>"

                cuerpo = f"""
                    <html>
                    <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                        <tr>
                            <td align="center">
                            <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                <td style="font-size: 24px; color: #007bff; padding-bottom: 20px;">
                                    <strong>Invitación a Proyecto Adoptivo</strong>
                                </td>
                                </tr>
                                <tr>
                                <td style="font-size: 17px; padding-bottom: 10px;">
                                    Hola,
                                </td>
                                </tr>
                                <tr>
                                <td style="font-size: 17px; padding-bottom: 10px;">
                                    Has sido invitado/a a conformar un proyecto adoptivo junto a
                                    <strong>{nombre_actual} {apellido_actual}</strong> (DNI: {usuario_actual_login}).
                                </td>
                                </tr>
                                <tr>
                                <td style="font-size: 17px; padding-bottom: 10px;">
                                    {aviso_curso}
                                </td>
                                </tr>
                                <tr>
                                <td style="font-size: 17px; padding-bottom: 10px;">
                                    Por favor, confirmá tu participación haciendo clic en uno de los siguientes botones:
                                </td>
                                </tr>
                                <tr>
                                <td align="center" style="padding: 20px 0 30px 0;">
                                    <!-- Botones en tabla para mayor compatibilidad -->
                                    <table cellpadding="0" cellspacing="0" style="text-align: center;">
                                    <tr>
                                        <td style="padding-bottom: 10px;">
                                        <a href="{link_aceptar}"
                                            style="display: inline-block; padding: 12px 20px; background-color: #28a745; color: #ffffff; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px; margin-right: 10px;">
                                            ✅ Acepto la invitación
                                        </a>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td>
                                        <a href="{link_rechazar}"
                                            style="display: inline-block; padding: 12px 20px; background-color: #dc3545; color: #ffffff; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px;">
                                            ❌ Rechazo la invitación
                                        </a>
                                        </td>
                                    </tr>
                                    </table>
                                </td>
                                </tr>
                                <tr>
                                <td style="font-size: 17px;">
                                    Muchas gracias por tu tiempo.
                                </td>
                                </tr>
                                <tr>
                                <td>
                                    <hr style="border: none; border-top: 1px solid #dee2e6; margin: 40px 0;">
                                    <p style="font-size: 15px; color: #6c757d;">
                                    Equipo Técnico<br>
                                    <strong>Sistema RUA</strong>
                                    </p>
                                </td>
                                </tr>
                            </table>
                            </td>
                        </tr>
                        </table>
                    </body>
                    </html>
                    """


                enviar_mail(destinatario = user2.mail, asunto = asunto, cuerpo = cuerpo)

                evento_mail = RuaEvento(
                    login = usuario_actual_login,
                    evento_detalle = f"Se envió invitación a {login_2} para sumarse al proyecto adoptivo.",
                    evento_fecha = datetime.now()
                )
                db.add(evento_mail)
                db.commit()
            except Exception as e:
                print("⚠️ No se pudo enviar el mail de invitación:", e)

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Proyecto preliminar creado exitosamente.",
            "tiempo_mensaje": 4,
            "next_page": "menu_adoptantes/proyecto"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al crear el proyecto preliminar: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@proyectos_router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante"]))])
def crear_proyecto(
    data: dict = Body(...),
    db: Session = Depends(get_db)
    ):

    """
    Crea un nuevo proyecto en la base de datos.

    Este endpoint permite registrar un nuevo proyecto indicando el tipo y el usuario principal (`login_1`).
    Se valida que los usuarios existan y que tengan el rol 'adoptante'.
    Si el proyecto es 'Monoparental', solo puede incluir `login_1`.
    Si el proyecto es 'Matrimonio' o 'Unión convivencial', debe incluirse `login_2` y `mail_2` que debe coincidir.

    ### Campos requeridos:
    - **proyecto_tipo**: `"Monoparental"`, `"Matrimonio"` o `"Unión convivencial"`
    - **login_1**: DNI del primer usuario (debe existir en la base de datos)

    ### Campos opcionales:
    - **login_2**
    - **proyecto_barrio**, **proyecto_localidad**, **proyecto_provincia**
    - **subregistro_1**, ..., **subregistro_6_d** (Y/N)
    - **aceptado**, **nro_orden_rua**, **doc_proyecto_convivencia_o_estado_civil**, etc.

    ### Ejemplo de JSON:
    ```json
    {
      "proyecto_tipo": "Matrimonio",
      "login_1": "12345678",
      "login_2": "23456789",
      "mail_2": "persona2@email.com",
      "proyecto_barrio": "Centro",
      "proyecto_localidad": "Córdoba",
      "proyecto_provincia": "Córdoba",
      "subregistro_1": "Y",
      "subregistro_5_a": "N",
      "subregistro_6_d": "Y"
    }
    ```

    ### Respuesta:
    ```json
    {
      "message": "Proyecto creado exitosamente",
      "proyecto_id": 25
    }
    ```
    """

    try:

        # Validar campos obligatorios
        if "proyecto_tipo" not in data or data["proyecto_tipo"] not in ["Monoparental", "Matrimonio", "Unión convivencial"]:
            raise HTTPException(status_code=400, detail="Campo 'proyecto_tipo' inválido o ausente."
                                " Debe ser: [ Monoparental, Matrimonio, Unión convivencial ]")
        
        if "login_1" not in data:
            raise HTTPException(status_code=400, detail="Campo 'login_1' es obligatorio.")

        proyecto_tipo = data["proyecto_tipo"]
        login_1 = data["login_1"]
        login_2 = data.get("login_2")
        mail_2 = data.get("mail_2")


        # 🔒 Validaciones según tipo de proyecto
        if proyecto_tipo == "Monoparental":
            if login_2 is not None or mail_2 is not None:
                raise HTTPException(
                    status_code=400,
                    detail="Los campos 'login_2' y 'mail_2' no deben enviarse en un proyecto de tipo 'Monoparental'."
                )
        elif proyecto_tipo in ["Matrimonio", "Unión convivencial"]:
            if not login_2:
                raise HTTPException(
                    status_code=400,
                    detail="Debe incluirse 'login_2' en proyectos tipo 'Matrimonio' o 'Unión convivencial'."
                )
            if not mail_2:
                raise HTTPException(
                    status_code=400,
                    detail="Debe incluirse 'mail_2' en proyectos tipo 'Matrimonio' o 'Unión convivencial'."
                )
        
        # Verificar existencia y rol del login_1
        login_1_user = db.query(User).filter(User.login == data["login_1"]).first()
        if not login_1_user:
            raise HTTPException(status_code=404, detail=f"El usuario login_1 '{data['login_1']}' no existe.")

        login_1_roles = db.query(UserGroup).filter(UserGroup.login == login_1).all()
        login_1_es_adoptante = any(
            db.query(Group).filter(Group.group_id == g.group_id, Group.description == "adoptante").first()
            for g in login_1_roles
        )
        if not login_1_es_adoptante:
            raise HTTPException(status_code=403, detail=f"El usuario '{data['login_1']}' no tiene el rol 'adoptante'.")

        proyecto_baja_caducidad_login1 = _get_proyecto_baja_caducidad_para_login(db, login_1)
        if proyecto_baja_caducidad_login1:
            raise HTTPException(
                status_code=403,
                detail=(
                    "El usuario '{login}' tiene un proyecto dado de baja por caducidad. "
                    "Comuníquense con el equipo del RUA."
                ).format(login=login_1)
            )

        # Si se incluye login_2, validar también su existencia, rol y mail
        if login_2:

            if proyecto_tipo == "Monoparental":
                raise HTTPException(
                    status_code=400,
                    detail="No se puede crear un proyecto Monoparental si se especifica 'login_2'."
                )
            
            login_2_user = db.query(User).filter(User.login == login_2).first()
            if not login_2_user:
                raise HTTPException(status_code=404, detail=f"El usuario login_2 '{data['login_2']}' no existe.")

            login_2_roles = db.query(UserGroup).filter(UserGroup.login == data["login_2"]).all()
            login_2_es_adoptante = any(
                db.query(Group).filter(Group.group_id == g.group_id, Group.description == "adoptante").first()
                for g in login_2_roles
            )
            if not login_2_es_adoptante:
                raise HTTPException(status_code=403, detail=f"El usuario '{data['login_2']}' no tiene el rol 'adoptante'.")

            if mail_2:
                raise HTTPException(status_code=400, detail="Debe incluirse el campo 'mail_2' si se especifica 'login_2'.")

            if mail_2.strip().lower() != (login_2_user.mail or "").strip().lower():
                raise HTTPException(status_code=400, detail="El mail proporcionado en 'mail_2' no coincide con el del usuario 'login_2'.")

            proyecto_baja_caducidad_login2 = _get_proyecto_baja_caducidad_para_login(db, login_2)
            if proyecto_baja_caducidad_login2:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "El usuario '{login}' tiene un proyecto dado de baja por caducidad. "
                        "Comuníquense con el equipo del RUA."
                    ).format(login=login_2)
                )


        # Verificar que login_1 no tenga un proyecto activo según estado_general
        proyecto_login_1_existente = (
            db.query(Proyecto)
            .filter(
                Proyecto.login_1 == login_1,
                Proyecto.estado_general.in_(["creado", "confeccionando", "en_revision", "actualizando", "aprobado", 
                                             "en_valoracion", "viable", "en_suspenso", "en_carpeta", 
                                             "vinculacion", "guarda_provisoria", "guarda_confirmada"])
            )
            .first()
        )

        if proyecto_login_1_existente:
            raise HTTPException(
                status_code=400,
                detail=f"El usuario '{login_1}' ya tiene un proyecto activo con estado '{proyecto_login_1_existente.estado_general}'."
            )

        # Verificar que login_2 no tenga un proyecto activo (si se proporciona)
        if login_2:
            proyecto_login_2_existente = (
                db.query(Proyecto)
                .filter(
                    Proyecto.login_2 == login_2,
                    Proyecto.estado_general.in_(["creado", "confeccionando", "en_revision", "actualizando", "aprobado", 
                                                "en_valoracion", "viable", "en_suspenso", "en_carpeta", 
                                                "vinculacion", "guarda_provisoria", "guarda_confirmada"])
                )
                .first()
            )

            if proyecto_login_2_existente:
                raise HTTPException(
                    status_code=400,
                    detail=f"El usuario '{login_2}' ya tiene un proyecto activo con estado '{proyecto_login_1_existente.estado_general}'."
                )



        # Campos con default = 'N' si no vienen
        def subreg_val(key): return data.get(key, "N") if data.get(key) in ["Y", "N"] else "N"

        nuevo_proyecto = Proyecto(
            proyecto_tipo = data["proyecto_tipo"],
            login_1 = data["login_1"],
            login_2 = data.get("login_2"),

            proyecto_calle_y_nro = data.get("proyecto_calle_y_nro"),
            proyecto_depto_etc = data.get("proyecto_depto_etc"),
            proyecto_barrio = data.get("proyecto_barrio"),
            proyecto_localidad = data.get("proyecto_localidad"),
            proyecto_provincia = data.get("proyecto_provincia"),

            doc_proyecto_convivencia_o_estado_civil = data.get("doc_proyecto_convivencia_o_estado_civil"),

            aceptado = data.get("aceptado"),
            aceptado_code = data.get("aceptado_code"),

            ultimo_cambio_de_estado = data.get("ultimo_cambio_de_estado"),

            nro_orden_rua = data.get("nro_orden_rua"),
            fecha_asignacion_nro_orden = data.get("fecha_asignacion_nro_orden"),
            ratificacion_code = data.get("ratificacion_code"),

            subregistro_1 = subreg_val("subregistro_1"),
            subregistro_2 = subreg_val("subregistro_2"),
            subregistro_3 = subreg_val("subregistro_3"),
            subregistro_4 = subreg_val("subregistro_4"),
            subregistro_5_a = subreg_val("subregistro_5_a"),
            subregistro_5_b = subreg_val("subregistro_5_b"),
            subregistro_5_c = subreg_val("subregistro_5_c"),
            subregistro_6_a = subreg_val("subregistro_6_a"),
            subregistro_6_b = subreg_val("subregistro_6_b"),
            subregistro_6_c = subreg_val("subregistro_6_c"),
            subregistro_6_d = subreg_val("subregistro_6_d"),
            subregistro_6_2 = subreg_val("subregistro_6_2"),
            subregistro_6_3 = subreg_val("subregistro_6_3"),
            subregistro_6_mas_de_3 = subreg_val("subregistro_6_mas_de_3"),
            subregistro_flexible = subreg_val("subregistro_flexible"),
            subregistro_otra_provincia = subreg_val("subregistro_otra_provincia"),

            operativo = 'Y',
            estado_general = "confeccionando",
        )

        db.add(nuevo_proyecto)
        db.commit()
        db.refresh(nuevo_proyecto)

        return {
            "message": "Proyecto creado exitosamente",
            "proyecto_id": nuevo_proyecto.proyecto_id
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al crear el proyecto: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error en los datos de entrada: {str(e)}")




@proyectos_router.post("/notificacion/{proyecto_id}", response_model = dict,
                       dependencies = [Depends(verify_api_key), 
                                       Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def crear_notificacion_proyecto(
    proyecto_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    Registra una observación sobre un proyecto adoptivo con notificacion a pretensos por correo.

    Ejemplo JSON:
    {
        "observacion": "El certificado debe actualizarseno en el próximo mes."
    }
    """
    observacion = data.get("observacion")
    login_que_observo = current_user["user"]["login"]

    if not observacion:
        raise HTTPException(status_code = 400, detail = "Debe proporcionar el campo 'observacion'.")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado.")

    try:
        # ✅ Guardar la observación
        nueva_obs = ObservacionesProyectos(
            observacion_fecha = datetime.now(),
            observacion = observacion,
            login_que_observo = login_que_observo,
            observacion_a_cual_proyecto = proyecto_id
        )
        db.add(nueva_obs)

        resumen = (observacion[:100] + "...") if len(observacion) > 100 else observacion
        evento = RuaEvento(
            login = proyecto.login_1,
            evento_detalle = (
                f"Observación registrada y notificacion por correo a pretensos sobre proyecto #{proyecto_id}"
                f" por {login_que_observo}: {resumen}"
            ),
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()


        # ✅ Enviar correo a login_1 y login_2 si corresponde
        logins_a_notificar = [proyecto.login_1]
        if proyecto.proyecto_tipo in ["Matrimonio", "Unión convivencial"] and proyecto.login_2:
            logins_a_notificar.append(proyecto.login_2)

        for login in logins_a_notificar:
            usuario = db.query(User).filter(User.login == login).first()
            if usuario and usuario.mail:
                try:
                    cuerpo_html = f"""
                        <html>
                        <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                            <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                            <tr>
                                <td align="center">
                                <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: Arial, sans-serif; color: #333333; box-shadow: 0 0 10px rgba(0,0,0,0.05);">
                                    <tr>
                                    <td style="font-size: 18px; padding-bottom: 20px;">
                                        Hola <strong>{usuario.nombre}</strong>,
                                    </td>
                                    </tr>
                                    <tr>
                                    <td style="font-size: 17px; padding-bottom: 10px;">
                                        Se ha registrado una observación sobre tu proyecto adoptivo:
                                    </td>
                                    </tr>
                                    <tr>
                                    <td>
                                        <table cellpadding="0" cellspacing="0" width="100%">
                                        <tr>
                                            <td style="border-left: 4px solid #ccc; padding-left: 12px; font-size: 17px; color: #555555; background-color: #f9f9f9; padding: 12px; border-radius: 4px;">
                                            {observacion}
                                            </td>
                                        </tr>
                                        </table>
                                    </td>
                                    </tr>
                                    <tr>
                                    <td style="font-size: 17px; color: #d48806; padding-top: 20px;">
                                        📄 Se ha solicitado que <strong>actualices</strong> los datos.
                                    </td>
                                    </tr>
                                    <tr>
                                    <td style="padding-top: 30px; font-size: 16px;">
                                        Saludos cordiales,<br><strong>Equipo RUA</strong>
                                    </td>
                                    </tr>
                                </table>
                                </td>
                            </tr>
                            </table>
                        </body>
                        </html>
                        """


                    enviar_mail(
                        destinatario = usuario.mail,
                        asunto = "Observación sobre tu proyecto adoptivo",
                        cuerpo = cuerpo_html
                    )

                except Exception as e:
                    print(f"⚠️ Error al enviar correo a {login}: {str(e)}")

        return {"message": "Observación registrada correctamente."}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar observación: {str(e)}")






@proyectos_router.post("/revision/solicitar", response_model = dict,
                      dependencies = [Depends(verify_api_key), Depends(require_roles(["adoptante"]))])
def solicitar_revision_proyecto(
    datos: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
    ):

    """
    📌 Endpoint para solicitar la revisión del proyecto adoptivo.

    🔄 Este endpoint permite a un usuario con rol 'adoptante' cambiar el estado de su proyecto a `pedido_valoracion`,
    indicando que está listo para ser revisado por el equipo de supervisión.

    🧾 Además de cambiar el estado, el endpoint actualiza todos los campos relevantes del modelo `Proyecto` con la información
    recibida en el cuerpo de la solicitud (método POST, formato JSON). Esto permite consolidar y enviar toda la información
    del proyecto en una única operación.

    ⚠️ Requisitos:
    - El usuario debe tener un proyecto en estado `inicial_cargando` o `actualizando`.
    - El cuerpo del `POST` debe contener los campos a actualizar (pueden enviarse parcialmente).

    🛠️ En caso de éxito:
    - Se actualizan los campos del proyecto.
    - Se cambia el estado del proyecto a `pedido_valoracion`.
    - Se registra un evento de auditoría en la tabla `RuaEvento`.
    - Se devuelve un mensaje de éxito para mostrar en el frontend.

    🚫 En caso de error:
    - Si no se encuentra el proyecto o el estado no permite revisión, se notifica al usuario.
    - Si ocurre un error de base de datos, se hace rollback y se informa el problema.

    📨 Ejemplo de JSON a enviar en el `POST`:

    ```json
    {
    "proyecto_calle_y_nro": "Av. Siempre Viva 742",
    "proyecto_depto_etc": "Dpto A",
    "proyecto_barrio": "Centro",
    "proyecto_localidad": "Córdoba",
    "proyecto_provincia": "Córdoba",
    "subregistro_1": "Y",
    "subregistro_2": "Y",
    "subregistro_3": "N",
    "subregistro_4": "N",
    "subregistro_5_a": "N",
    "subregistro_5_b": "Y",
    "subregistro_5_c": "N",
    "subregistro_6_a": "Y",
    "subregistro_6_b": "N",
    "subregistro_6_c": "N",
    "subregistro_6_d": "N",
    "subregistro_6_2": "Y",
    "subregistro_6_3": "N",
    "subregistro_6_mas_de_3": "N",
    "subregistro_flexible": "Y",
    "subregistro_otra_provincia": "N"
    }
    ```
    """


    login_actual = current_user["user"]["login"]

    # Buscar el proyecto asociado al usuario autenticado
    proyecto = db.query(Proyecto).filter(
        or_(Proyecto.login_1 == login_actual, Proyecto.login_2 == login_actual)
    ).first()

    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>No se encontró un proyecto asociado a tu usuario.</p>"
                "<p>Verificá que hayas iniciado correctamente tu proyecto adoptivo.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Solo permitir si el estado es inicial o actualizando
    if proyecto.estado_general not in ["confeccionando", "actualizando"]:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Solo se puede solicitar la revisión del proyecto si está en estado "
                "<strong>inicial</strong> o <strong>actualizando</strong>.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Actualizar los campos del proyecto con lo que viene en el JSON
    campos_actualizables = [
        "proyecto_calle_y_nro", "proyecto_depto_etc",
        "proyecto_barrio", "proyecto_localidad", "proyecto_provincia",
        "subregistro_1", "subregistro_2", "subregistro_3", "subregistro_4",
        "subregistro_5_a", "subregistro_5_b", "subregistro_5_c",
        "subregistro_6_a", "subregistro_6_b", "subregistro_6_c",
        "subregistro_6_d", "subregistro_6_2", "subregistro_6_3",
        "subregistro_6_mas_de_3", "subregistro_flexible", "subregistro_otra_provincia"
    ]

    for campo in campos_actualizables:
        if campo in datos:
            setattr(proyecto, campo, datos[campo])


    # Validación: debe haber al menos un subregistro en "Y"
    subregistros = [
        proyecto.subregistro_1, proyecto.subregistro_2, proyecto.subregistro_3, proyecto.subregistro_4,
        proyecto.subregistro_5_a, proyecto.subregistro_5_b, proyecto.subregistro_5_c,
        proyecto.subregistro_6_a, proyecto.subregistro_6_b, proyecto.subregistro_6_c, proyecto.subregistro_6_d,
        proyecto.subregistro_6_2, proyecto.subregistro_6_3, proyecto.subregistro_6_mas_de_3,
        proyecto.subregistro_flexible, proyecto.subregistro_otra_provincia
    ]
    if not any(s == "Y" for s in subregistros):
        return {
            "success": False,
            "tipo_mensaje": "amarillo",
            "mensaje": "Debe seleccionar al menos un subregistro para solicitar la revisión.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Validación: si hay login_2, debe haber aceptado la invitación
    if proyecto.login_2:

        # Validación: debe haberse subido el documento obligatorio
        if not proyecto.doc_proyecto_convivencia_o_estado_civil:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "Debe subir el documento de convivencia o estado civil antes de solicitar revisión.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        user2 = db.query(User).filter(User.login == proyecto.login_2).first()
        if proyecto.aceptado == "N" and proyecto.aceptado_code is None:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": (
                    f"<p>El usuario <strong>{user2.nombre} {user2.apellido}</strong> (DNI: {user2.login}) <strong>rechazó</strong> la invitación.</p>"
                    "<p>No se puede continuar con la solicitud de revisión.</p>"
                ),
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }
        elif proyecto.aceptado == "N" and proyecto.aceptado_code is not None:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": (
                    f"<p>El usuario <strong>{user2.nombre} {user2.apellido}</strong> (DNI: {user2.login}) aún <strong>no ha respondido</strong> a la invitación.</p>"
                    "<p>Para poder continuar debe aceptar la invitación que le llegó por mail.</p>"
                ),
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

    # Cambiar el estado a "pedido_valoracion"
    proyecto.estado_general = "en_revision"

    try:
        # Registrar evento
        evento = RuaEvento(
            login = login_actual,
            evento_detalle = "Solicitud de revisión de proyecto enviada.",
            evento_fecha = datetime.now()
        )
        db.add(evento)

        # Confirmar en base de datos
        db.commit()

        # Enviar notificación a todas las supervisoras
        crear_notificacion_masiva_por_rol(
            db = db,
            rol = "supervisora",
            mensaje = f"El usuario {login_actual} solicitó revisión del proyecto.",
            link = "/menu_supervisoras/detalleProyecto",
            data_json= { "proyecto_id": proyecto.proyecto_id },
            tipo_mensaje = "azul"
        )



        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>La solicitud de revisión del proyecto fue enviada correctamente.</p>"
            ),
            "tiempo_mensaje": 8,
            "next_page": "menu_adoptantes/proyecto"
        }

    except SQLAlchemyError:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al registrar la solicitud.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }



@proyectos_router.put("/documentos/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante"]))])
def subir_documento_proyecto(
    proyecto_id: int,
    campo: Literal[
        "doc_proyecto_convivencia_o_estado_civil"
    ] = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
    ):
    
    """
    Sube un documento al proyecto identificado por `proyecto_id`.
    Guarda el archivo en una carpeta específica del proyecto y actualiza el campo correspondiente.
    """

    # ——— Validación de MIME type ———
    allowed_mime_types = {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    }
    if file.content_type not in allowed_mime_types:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no permitido: {file.content_type}"
        )

    # Validar extensión
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Extensión de archivo no permitida: {ext}")

    # ——— Validación de tamaño (5 MB) ———
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    if size > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="El archivo excede el tamaño máximo permitido (5 MB)."
        )
    file.file.seek(0)
    
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # Mapeo para nombre del archivo base
    nombre_archivo_map = {
        "doc_proyecto_convivencia_o_estado_civil": "convivencia_o_estado_civil"
    }

    nombre_archivo = nombre_archivo_map[campo]

    # Crear carpeta del proyecto
    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok=True)

    # Generar nombre único con fecha y hora
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"{nombre_archivo}_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Actualizar ruta del archivo en la base
        setattr(proyecto, campo, filepath)
        db.commit()

        return {"message": f"Documento '{campo}' subido como '{final_filename}'"}
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al guardar documento: {str(e)}")



@proyectos_router.get("/documentos/{proyecto_id}/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante"]))])
def descargar_documento_proyecto(
    proyecto_id: int,
    campo: Literal["doc_proyecto_convivencia_o_estado_civil"] = Query(...),
    db: Session = Depends(get_db)
    ):

    """
    Descarga un documento del proyecto identificado por `proyecto_id`.
    El campo debe ser uno de los documentos almacenados.
    """

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # Obtener ruta del archivo desde el campo especificado
    filepath = getattr(proyecto, campo)

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )



@proyectos_router.post("/solicitar-valoracion", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["supervision", "supervisora"]))])
def solicitar_valoracion(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📌 Solicita la valoración de un proyecto asignando profesionales y número de orden.

    ### JSON esperado:
    {
    "proyecto_id": 123,
    "profesionales": ["11222333", "22333444"],
    "observacion_interna": "Observación interna opcional sobre el proyecto"
    }
    """
    try:
        proyecto_id = data.get("proyecto_id")
        profesionales = data.get("profesionales", [])
        observacion_interna = data.get("observacion_interna")  # Puede ser None o string


        if not isinstance(profesionales, list):
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "'profesionales' debe ser una lista de logins",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if not proyecto_id:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe especificarse el 'proyecto_id'",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if not (1 <= len(profesionales) <= 3):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Se deben asignar 1, 2 o 3 profesionales",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Proyecto no encontrado",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Validación de profesionales
        for login in profesionales:
            user = db.query(User).filter(User.login == login).first()
            if not user:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"El usuario con login '{login}' no existe",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }

            roles = db.query(UserGroup).filter(UserGroup.login == login).all()
            es_profesional = any(
                db.query(Group).filter(Group.group_id == r.group_id, Group.description == "profesional").first()
                for r in roles
            )
            if not es_profesional:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El usuario '{login}' no tiene el rol 'profesional'",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }


        # Solo asignar nro de orden para proyectos que ingresaron por RUA
        def _necesita_nro_orden(p: Proyecto) -> bool:
            # vacío / None / "0" se consideran "sin número"
            val = (p.nro_orden_rua or "").strip()
            return val == "" or val == "0"

        if proyecto.ingreso_por == "rua" and _necesita_nro_orden(proyecto):
            # Tomar el MAX(nro_orden_rua) solo entre proyectos de RUA y numéricos de hasta 4 dígitos
            # Usamos REGEXP para asegurar numeritos, y CAST a INT para la agregación
            max_actual = (
                db.query(func.coalesce(func.max(cast(Proyecto.nro_orden_rua, Integer)), 0))
                .filter(
                    Proyecto.ingreso_por == "rua",
                    # Solo valores numéricos de 1 a 4 dígitos (ajustá si querés permitir más)
                    Proyecto.nro_orden_rua.op("REGEXP")("^[0-9]{1,4}$")
                )
                .scalar()
            ) or 0

            nuevo_nro_orden = str(max_actual + 1)
            proyecto.nro_orden_rua = nuevo_nro_orden
            proyecto.fecha_asignacion_nro_orden = date.today()
        else:
            # Para convocatoria/oficio (o si ya tenía nro válido) NO tocar el nro de orden
            nuevo_nro_orden = (proyecto.nro_orden_rua or "").strip() or None


        # Cambiar estado
        estado_anterior = proyecto.estado_general
        proyecto.estado_general = "calendarizando"

        # Ya no lo uso porque ya se actualiza en ProyectoHistorialEstado y dificulta la ratificación
        # proyecto.ultimo_cambio_de_estado = date.today()

        historial = ProyectoHistorialEstado(
            proyecto_id = proyecto.proyecto_id,
            estado_anterior = estado_anterior,
            estado_nuevo = "calendarizando",
            fecha_hora = datetime.now()
        )
        db.add(historial)


        # Agregar detalle del equipo en proyecto
        for login in profesionales:
            detalle = DetalleEquipoEnProyecto(
                proyecto_id = proyecto.proyecto_id,
                login = login,
                fecha_asignacion = datetime.now().date()
            )
            db.merge(detalle)


        # Obtener datos de la supervisora
        login_supervisora = current_user["user"]["login"]
        supervisora = db.query(User).filter(User.login == login_supervisora).first()
        nombre_supervisora = f"{supervisora.nombre} {supervisora.apellido}"

        # Registrar evento RuaEvento
        detalle_evento = (
            f"El proyecto fue asignado para valoración a las profesionales: "
            f"{', '.join(profesionales)} por {nombre_supervisora}."
        )
        evento = RuaEvento(
            login = login_supervisora,
            evento_detalle = detalle_evento,
            evento_fecha = datetime.now()
        )
        db.add(evento)

        # Enviar notificación a cada profesional asignado
        for login_profesional in profesionales:
            notif_result = crear_notificacion_individual(
                db = db,
                login_destinatario = login_profesional,
                mensaje = (
                    f"Nuevo proyecto para valoración. asignado por {nombre_supervisora}."
                ),
                link = "/menu_profesionales/detalleEntrevista",
                data_json = { "proyecto_id": proyecto.proyecto_id },
                tipo_mensaje = "naranja"
            )
            if not notif_result["success"]:
                db.rollback()
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": (
                        f"<p>Error al notificar a la profesional {login_profesional}.</p>"
                    ),
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }


        # Registrar observación si fue provista
        if observacion_interna:
            db.add(ObservacionesProyectos(
                observacion_a_cual_proyecto=proyecto.proyecto_id,
                observacion=observacion_interna,
                login_que_observo=login_supervisora,
                observacion_fecha=datetime.now()
            ))


        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Solicitud de valoración a profesionales registrada correctamente",
            "tiempo_mensaje": 3,
            "next_page": "actual",
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al solicitar valoración: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@proyectos_router.get("/profesionales-asignadas/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def obtener_profesionales_asignadas(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):


    """
    📄 Devuelve el listado de profesionales asignadas a un proyecto.
    """
    try:
        asignaciones = db.query(DetalleEquipoEnProyecto).filter(
            DetalleEquipoEnProyecto.proyecto_id == proyecto_id
        ).all()

        if not asignaciones:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "No hay profesionales asignadas a este proyecto.",
                "tiempo_mensaje": 5,
                "profesionales": []
            }

        profesionales = []
        for asignacion in asignaciones:
            user = db.query(User).filter(User.login == asignacion.login).first()
            if user:
                profesionales.append({
                    "login": user.login,
                    "nombre": user.nombre,
                    "apellido": user.apellido
                })

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "profesionales asignadas obtenidas correctamente.",
            "tiempo_mensaje": 3,
            "profesionales": profesionales
        }

    except SQLAlchemyError as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al obtener profesionales: {str(e)}",
            "tiempo_mensaje": 6,
            "profesionales": []
        }




@proyectos_router.get("/{proyecto_id}/historial", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_historial_estado_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):


    """
    📚 Devuelve el historial de cambios de estado de un proyecto.

    Cada entrada incluye el estado anterior, el nuevo estado y la fecha y hora del cambio.
    """
    try:
        historial = (
            db.query(ProyectoHistorialEstado)
            .filter(ProyectoHistorialEstado.proyecto_id == proyecto_id)
            .order_by(ProyectoHistorialEstado.fecha_hora.desc())
            .all()
        )

        historial_list = [
            {
                "estado_anterior": h.estado_anterior,
                "estado_nuevo": h.estado_nuevo,
                "fecha_hora": h.fecha_hora.strftime("%Y-%m-%d %H:%M:%S")
            }
            for h in historial
        ]

        return {
            "success": True,
            "historial": historial_list,
            "proyecto_id": proyecto_id
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code = 500, detail = f"Error al obtener el historial: {str(e)}")        





@proyectos_router.post("/entrevista/agendar", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def agendar_entrevista(
    data: dict = Body(..., example={
        "proyecto_id": 123,
        "fecha_hora": "2025-04-22T15:00:00",
        "comentarios": "Se realizará en la sede regional con ambos pretensos presentes.",
        "evaluaciones": ["Deseo y motivación", "Técnicas psicológicas"]
    }),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    
    EVALUACIONES_VALIDAS = [
        "Deseo y motivación",
        "Historia vital",
        "Técnicas psicológicas",
        "Entrevista domiciliaria",
        "Entrevista de devolución"
    ]


    try:
        login_actual = current_user["user"]["login"]

        proyecto_id = data.get("proyecto_id")
        fecha_hora = data.get("fecha_hora")
        comentarios = data.get("comentarios")
        evaluaciones = data.get("evaluaciones", [])  # ✅ puede venir vacío

        if not proyecto_id :
            return {
                "success": False, 
                "tipo_mensaje": "naranja", 
                "mensaje": "No se pudo identificar el proyecto.", 
                "tiempo_mensaje": 5, 
                "next_page": "actual"
            }

        if not fecha_hora:
            return {
                "success": False, 
                "tipo_mensaje": "naranja", 
                "mensaje": "Faltan indicar la fecha y hora.", 
                "tiempo_mensaje": 5, 
                "next_page": "actual"
            }

        proyecto = db.query(Proyecto).filter_by(proyecto_id=proyecto_id).first()
        if not proyecto:
            return {
                "success": False, 
                "tipo_mensaje": "rojo", "mensaje": 
                "Proyecto no encontrado.", 
                "tiempo_mensaje": 5, 
                "next_page": "actual"
            }

        roles_actuales = db.query(Group.description).join(UserGroup, Group.group_id == UserGroup.group_id)\
            .filter(UserGroup.login == login_actual).all()
        rol_actual = [r[0] for r in roles_actuales]

        if "administrador" not in rol_actual:
            asignado = db.query(DetalleEquipoEnProyecto).filter_by(proyecto_id=proyecto_id, login=login_actual).first()
            if not asignado:
                return {
                    "success": False, 
                    "tipo_mensaje": "rojo", 
                    "mensaje": "No estás asignado a este proyecto.", 
                    "tiempo_mensaje": 5, 
                    "next_page": "actual"}

        # Validación de fecha
        fecha_obj = datetime.fromisoformat(fecha_hora)
        if fecha_obj.date() < date.today():
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "No se puede agendar entrevistas en fechas pasadas. "
                           "Las entrevistas deben agendarse en orden y a partir del día actual.",
                "tiempo_mensaje": 7,
                "next_page": "actual"
            }

        # Validar orden de fechas: no debe haber días intermedios sin entrevistas
        entrevistas_previas = db.query(AgendaEntrevistas).filter_by(proyecto_id=proyecto_id).order_by(AgendaEntrevistas.fecha_hora).all()
        if entrevistas_previas:
            ultima_fecha = entrevistas_previas[-1].fecha_hora.date()
            if fecha_obj.date() <= ultima_fecha:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"Ya existe una entrevista posterior a esa fecha. Las entrevistas "
                                "deben agendarse en orden cronológico.",
                    "tiempo_mensaje": 7,
                    "next_page": "actual"
                }

        # Validar que no se salten evaluaciones
        if not all(e in EVALUACIONES_VALIDAS for e in evaluaciones):
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Una o más evaluaciones no son válidas.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Registrar entrevista
        nueva_agenda = AgendaEntrevistas(
            proyecto_id=proyecto_id,
            login_que_agenda=login_actual,
            fecha_hora=fecha_obj,
            comentarios=comentarios,
            evaluaciones=json.dumps(evaluaciones) if evaluaciones else None
        )
        db.add(nueva_agenda)

        db.add(RuaEvento(
            login=login_actual,
            evento_detalle=f"Se agendó una entrevista para el proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        ))

        if proyecto.estado_general == "calendarizando":
            proyecto.estado_general = "entrevistando"


        # # --- cambio de estado y, si corresponde, envío de mails de activación ---
        # enviar_mails_activacion = False

        # if proyecto.estado_general == "calendarizando":
        #     proyecto.estado_general = "entrevistando"
        #     # si es CONVOCATORIA, marcaremos para enviar mails tras commit
        #     if (proyecto.ingreso_por or "").lower() == "convocatoria":
        #         enviar_mails_activacion = True


        db.commit()


        # # si hay que enviar mails (solo si hubo transición y es convocatoria)
        # if enviar_mails_activacion:
        #     # monoparental si login_2 es None o vacío
        #     logins_destino = [proyecto.login_1] if not (proyecto.login_2 and str(proyecto.login_2).strip()) else [proyecto.login_1, proyecto.login_2]
        #     for lg in logins_destino:
        #         try:
        #             ok, detalle, omitido = _enviar_mail_activacion(lg)

        #             # Evento por cada intento (OK / omitido / fallo)
        #             if omitido == "omitido":
        #                 evento_txt = (f"Invitación de activación NO enviada (usuario ya tenía contraseña) "
        #                               f"por cambio a 'entrevistando' (convocatoria) en proyecto #{proyecto_id}.")
        #             else:
        #                 evento_txt = (f"Invitación de activación enviada por cambio a 'entrevistando' (convocatoria) "
        #                               f"en proyecto #{proyecto_id}. Resultado: {'OK' if ok else 'FALLÓ'}")


        #             db.add(RuaEvento(login=lg, evento_detalle=evento_txt, evento_fecha=datetime.now()))

        #             db.commit()
        #         except Exception as _e:
        #             db.rollback()
        #             # registramos el fallo como evento, pero no rompemos el flujo principal
        #             try:
        #                 db.add(RuaEvento(
        #                     login=lg,
        #                     evento_detalle=f"Fallo al enviar/registrar invitación de activación en proyecto #{proyecto_id}: {str(_e)}",
        #                     evento_fecha=datetime.now()
        #                 ))
        #                 db.commit()
        #             except Exception:
        #                 db.rollback()        


        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "📅 Entrevista agendada correctamente.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error al registrar la entrevista: {str(e)}",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }



@proyectos_router.get("/entrevista/listado/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def obtener_entrevistas_de_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📋 Obtener entrevistas agendadas para un proyecto adoptivo.
    Incluye eventos clave como inicio de valoración, entrevistas agendadas y entrega de informe.

    Retorna una lista cronológica de eventos, incluyendo evaluaciones asignadas y comentarios adicionales si existen.
    """

    try:
        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado.")

        entrevistas = db.query(AgendaEntrevistas).filter(
            AgendaEntrevistas.proyecto_id == proyecto_id
        ).order_by(AgendaEntrevistas.fecha_hora.asc()).all()

        resultados = []

        # 🔹 Solicitud de valoración
        historial_valoracion = db.query(ProyectoHistorialEstado).filter(
            ProyectoHistorialEstado.proyecto_id == proyecto_id,
            ProyectoHistorialEstado.estado_nuevo == "calendarizando"
        ).order_by(ProyectoHistorialEstado.fecha_hora.desc()).first()

        if historial_valoracion:
            resultados.append({
                "titulo": "Solicitud de valoración por supervisión",
                "fecha_hora": historial_valoracion.fecha_hora,
                "comentarios": None,
                "login_que_agenda": None,
                "creada_en": None,
                "evaluaciones": [],
                "evaluacion_comentarios": None,
            })

        # 🔹 Entrevistas agendadas
        sufijos = ["era", "da", "era", "ta", "ta"]

        for idx, e in enumerate(entrevistas):
            sufijo = sufijos[idx] if idx < len(sufijos) else "ta"
            titulo = f"{idx+1}{sufijo}. entrevista"
            resultados.append({
                "id": e.id,  # ✅ AÑADIR ID
                "titulo": titulo,
                "fecha_hora": e.fecha_hora,
                "comentarios": e.comentarios,
                "login_que_agenda": e.login_que_agenda,
                "creada_en": e.creada_en,
                "evaluaciones": e.evaluaciones,
                "evaluacion_comentarios": e.evaluacion_comentarios
            })


        # 🔹 Entrega de informe
        historial_entrega = db.query(ProyectoHistorialEstado).filter(
            ProyectoHistorialEstado.proyecto_id == proyecto_id,
            ProyectoHistorialEstado.estado_anterior == "entrevistando",
            ProyectoHistorialEstado.estado_nuevo == "para_valorar"
        ).order_by(ProyectoHistorialEstado.fecha_hora.desc()).first()

        if historial_entrega:
            resultados.append({
                "titulo": "Entrega de informe",
                "fecha_hora": historial_entrega.fecha_hora,
                "comentarios": None,
                "login_que_agenda": None,
                "creada_en": None,
                "evaluaciones": [],
                "evaluacion_comentarios": None,
            })

        # 🔹 Solo calendarizando (sin entrevistas)
        if not resultados and proyecto.estado_general == "calendarizando":
            evento_valoracion = db.query(ProyectoHistorialEstado).filter(
                ProyectoHistorialEstado.proyecto_id == proyecto_id,
                ProyectoHistorialEstado.estado_nuevo == "calendarizando"
            ).order_by(ProyectoHistorialEstado.fecha_hora.desc()).first()

            return {
                "success": True,
                "entrevistas": [{
                    "titulo": "Calendarizando",
                    "fecha_hora": evento_valoracion.fecha_hora if evento_valoracion else None,
                    "comentarios": None,
                    "login_que_agenda": None,
                    "creada_en": None,
                    "evaluaciones": [],
                    "evaluacion_comentarios": None,
                }]
            }

        return { "success": True, "entrevistas": resultados }

    except SQLAlchemyError as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al obtener entrevistas: {str(e)}",
            "tiempo_mensaje": 6,
            "entrevistas": []
        }




@proyectos_router.post("/reasignar-profesionales/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["supervision", "supervisora"]))])
async def reasignar_profesionales(
    proyecto_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    🔁 Reasigna los profesionales de un proyecto ya calendarizado.

    JSON esperado:
    {
        "logins": ["login1", "login2"],
        "observacion": "Texto opcional"
    }
    """
    try:
        logins = payload.get("logins", [])
        observacion = payload.get("observacion")

        if not isinstance(logins, list):
            return {"success": False, "mensaje": "'logins' debe ser una lista"}

        if not (1 <= len(logins) <= 3):
            return {"success": False, "mensaje": "Debe haber entre 1 y 3 profesionales asignadas"}

        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            return {"success": False, "mensaje": "Proyecto no encontrado"}

        # Validar profesionales
        for login in logins:
            user = db.query(User).filter(User.login == login).first()
            if not user:
                return {"success": False, "mensaje": f"El usuario '{login}' no existe"}

            roles = db.query(UserGroup).filter(UserGroup.login == login).all()
            es_profesional = any(
                db.query(Group).filter(Group.group_id == r.group_id, Group.description == "profesional").first()
                for r in roles
            )
            if not es_profesional:
                return {"success": False, "mensaje": f"El usuario '{login}' no es profesional"}

        # 🧹 Borrar asignaciones anteriores
        db.query(DetalleEquipoEnProyecto).filter(
            DetalleEquipoEnProyecto.proyecto_id == proyecto_id
        ).delete()

        # ➕ Agregar nuevas asignaciones
        for login in logins:
            nueva = DetalleEquipoEnProyecto(
                proyecto_id=proyecto_id,
                login=login,
                fecha_asignacion=datetime.now().date()
            )
            db.add(nueva)

        # 🧾 Registrar observación si hay
        if observacion:
            db.add(ObservacionesProyectos(
                observacion_a_cual_proyecto=proyecto_id,
                observacion=observacion,
                login_que_observo=current_user["user"]["login"],
                observacion_fecha=datetime.now()
            ))

        # 📅 Evento
        nombre_supervisora = f"{current_user['user']['nombre']} {current_user['user']['apellido']}"
        evento = RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se reasignaron las profesionales {', '.join(logins)} al proyecto.",
            evento_fecha=datetime.now()
        )
        db.add(evento)

        # 🔔 Notificaciones
        for login in logins:
            crear_notificacion_individual(
                db=db,
                login_destinatario=login,
                mensaje=f"Fuiste reasignada a un proyecto por {nombre_supervisora}.",
                link="/menu_profesionales/detalleEntrevista",
                data_json={"proyecto_id": proyecto_id},
                tipo_mensaje="naranja"
            )

        db.commit()

        return {
            "success": True,
            "mensaje": "Profesionales reasignadas correctamente",
            "tipo_mensaje": "verde",
            "tiempo_mensaje": 3,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "mensaje": f"Error al reasignar profesionales: {str(e)}",
            "tipo_mensaje": "rojo",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@proyectos_router.put("/entrevista/informe/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def subir_informe_profesionales(
    proyecto_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📄 Sube un archivo de informe profesional para un proyecto.

    Guarda el archivo en la carpeta del proyecto y actualiza el campo `informe_profesionales`.

    ✔️ Formatos permitidos:
    - `.pdf`, `.doc`, `.docx`, `.jpg`, `.jpeg`, `.png`
    """

    # 1️⃣ Validar extensión
    allowed_ext = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_ext:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Extensión no permitida: {ext}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    
    # proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # 2️⃣ Crear carpeta si no existe
    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok=True)

    # 3️⃣ Validar tamaño (máx. 5 MB)
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    if size > 5 * 1024 * 1024:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "El archivo excede el tamaño máximo de 5 MB.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # 4️⃣ Preparar nombre y ruta
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_name = f"informe_profesionales_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_name)


    try:
        # 5️⃣ Guardar en disco
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # 6️⃣ Construir nuevo objeto de historial
        nuevo_archivo = {
            "ruta": filepath,
            "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # 7️⃣ Leer y parsear el JSON actual
        raw = proyecto.informe_profesionales or ""
        try:
            if raw.strip().startswith("["):
                arr = json.loads(raw)
            elif raw.strip():
                arr = [{"ruta": raw, "fecha": "desconocida"}]
            else:
                arr = []
        except json.JSONDecodeError:
            arr = []

        # 8️⃣ Añadir el nuevo y guardar
        arr.append(nuevo_archivo)
        proyecto.informe_profesionales = json.dumps(arr, ensure_ascii=False)
        db.commit()

        # 9️⃣ Registrar evento RuaEvento
        login = current_user["user"]["login"]
        evento = RuaEvento(
            login=login,
            evento_detalle=f"Subió informe profesional al proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Informe subido como '{final_name}'.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al guardar el archivo: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    




@proyectos_router.put("/documento/{proyecto_id}/{tipo_documento}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def subir_documento_proyecto(
    proyecto_id: int,
    tipo_documento: Literal["informe_entrevistas", "sentencia_guarda", "sentencia_adopcion","doc_interrupcion"],
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📄 Sube un documento a un proyecto según el tipo indicado.

    ✔️ Formatos permitidos:
    - `.pdf`, `.doc`, `.docx`, `.jpg`, `.jpeg`, `.png`
    """
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code = 400, detail = f"Extensión de archivo no permitida: {ext}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    # Crear carpeta del proyecto si no existe
    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok = True)

    # Guardar archivo con nombre único
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"{tipo_documento}_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Asignar al campo correspondiente
        setattr(proyecto, tipo_documento, filepath)

        # Registrar evento
        login_actual = current_user["user"]["login"]
        evento = RuaEvento(
            login = login_actual,
            evento_detalle = f"Subió el documento '{tipo_documento}' al proyecto #{proyecto_id}",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "success": True,
            "message": f"Documento '{tipo_documento}' subido correctamente como '{final_filename}'.",
            "path": filepath
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar el archivo: {str(e)}")




@proyectos_router.post("/entrevista/solicitar-valoracion-final", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def solicitar_valoracion_final(
    data: dict = Body(..., example = { "proyecto_id": 123 }),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📌 Solicita a Supervisión la valoración final de un proyecto adoptivo.

    ✔️ Requisitos:
    - El proyecto debe tener un informe profesional cargado.
    
    🔁 Acciones:
    - Cambia `estado_general` a `para_valorar`.
    - Registra historial de cambio de estado (`ProyectoHistorialEstado`).
    - Crea un evento (`RuaEvento`) para seguimiento.
    - Notifica a todas las supervisoras con acceso.
    """
    try:
        proyecto_id = data.get("proyecto_id")
        if not proyecto_id:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe especificarse el 'proyecto_id'",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Proyecto no encontrado",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Validar que el informe profesional esté presente
        if not proyecto.informe_profesionales:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Debe cargarse el informe profesional antes de solicitar valoración final.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }


        # Validar que todas las evaluaciones requeridas estén presentes en las entrevistas del proyecto
        EVALUACIONES_REQUERIDAS = {
            "Deseo y motivación",
            "Historia vital",
            "Técnicas psicológicas",
            "Entrevista domiciliaria",
            "Entrevista de devolución"
        }

        entrevistas = db.query(AgendaEntrevistas).filter(AgendaEntrevistas.proyecto_id == proyecto_id).all()

        evaluaciones_realizadas = set()
        for ent in entrevistas:
            if ent.evaluaciones:
                try:
                    evaluaciones_json = json.loads(ent.evaluaciones)
                    evaluaciones_realizadas.update(evaluaciones_json)
                except Exception as e:
                    print(f"⚠️ Error al leer evaluaciones de entrevista ID {ent.id}: {e}")

        faltantes = EVALUACIONES_REQUERIDAS - evaluaciones_realizadas

        # if faltantes:
        #     return {
        #         "success": False,
        #         "tipo_mensaje": "naranja",
        #         "mensaje": f"Debe completar todas las instancias evaluativas antes de solicitar la valoración final. Faltan: {', '.join(faltantes)}.",
        #         "tiempo_mensaje": 8,
        #         "next_page": "actual"
        #     }


        # Cambiar estado
        estado_anterior = proyecto.estado_general
        proyecto.estado_general = "para_valorar"
        db.add(proyecto)

        # Registrar historial de cambio de estado
        historial_estado = ProyectoHistorialEstado(
            proyecto_id = proyecto.proyecto_id,
            estado_anterior = estado_anterior,
            estado_nuevo = "para_valorar",
            fecha_hora = datetime.now()
        )
        db.add(historial_estado)


        # Obtener nombres completos de pretensos
        nombres_pretensos = []

        if proyecto.usuario_1:
            nombre_1 = f"{proyecto.usuario_1.nombre} {proyecto.usuario_1.apellido or ''}".strip()
            nombres_pretensos.append(nombre_1)

        if proyecto.usuario_2:
            nombre_2 = f"{proyecto.usuario_2.nombre} {proyecto.usuario_2.apellido or ''}".strip()
            nombres_pretensos.append(nombre_2)

        # Unir los nombres en un solo string, separados por ' y ' si hay dos
        nombres_completos = " y ".join(nombres_pretensos)


        # Registrar evento
        login_autor = current_user["user"]["login"]
        evento = RuaEvento(
            login = login_autor,
            # evento_detalle = f"Solicitud de valoración final para el proyecto #{proyecto_id}",
            evento_detalle = f"El proyecto de {nombres_completos} fue enviado a valoración final. Proyecto #{proyecto_id}",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        

        # Notificar a supervision
        personal_supervision = db.query(User).join(UserGroup, User.login == UserGroup.login)\
            .join(Group, Group.group_id == UserGroup.group_id)\
            .filter(Group.description == "supervision").all()

        for persona_de_supervision in personal_supervision :
            resultado = crear_notificacion_individual(
                db = db,
                login_destinatario = persona_de_supervision.login,
                # mensaje = "📄 Un proyecto fue enviado a supervisión para valoración final.",
                mensaje = f"📄 El proyecto de {nombres_completos} fue enviado a valoración final.",
                link = "/menu_supervisoras/detalleProyecto",
                data_json = { "proyecto_id": proyecto_id },
                tipo_mensaje = "naranja"
            )
            if not resultado["success"]:
                db.rollback()
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Error al notificar a persona de supervisión {persona_de_supervision.login}",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "📨 Solicitud de valoración final enviada correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "menu_profesionales/portada"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }





@proyectos_router.post("/entrevista/entregar-informe-vinculacion", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def entregar_informe_vinculacion(
    data: dict = Body(..., example = { "proyecto_id": 123 }),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    proyecto_id = data.get("proyecto_id")
    if not proyecto_id:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Debe especificarse el 'proyecto_id'",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Validar que el informe de vinculacion esté presente, al menos uno
    if not proyecto.doc_informe_vinculacion:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Debe cargarse el informe de vinculación antes de presentar.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    if proyecto.estado_general != "vinculacion":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Este proyecto no se encuentra en vinculación. No se puede presentar este informe.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    try:

        # Registrar evento
        login_autor = current_user["user"]["login"]
        evento = RuaEvento(
            login = login_autor,
            evento_detalle = f"Se entregó el informe de vinculación para el proyecto #{proyecto_id}",
            evento_fecha = datetime.now()
        )
        db.add(evento)


        # 🔔 Notificar a supervisoras y supervision
        supervisoras_y_supervision = db.query(User)\
            .join(UserGroup, User.login == UserGroup.login)\
            .join(Group, Group.group_id == UserGroup.group_id)\
            .filter(Group.description.in_(["supervisora", "supervision"]))\
            .all()

        for persona in supervisoras_y_supervision:
            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=persona.login,
                mensaje="📄 Un informe de vinculación fue enviado a supervisión.",
                link="/menu_supervisoras/detalleProyecto",
                data_json={"proyecto_id": proyecto_id},
                tipo_mensaje="naranja"
            )
            if not resultado["success"]:
                db.rollback()
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Error al notificar a {persona.login}",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }


        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "📨 Informe de vinculación enviado correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "menu_profesionales/portada"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@proyectos_router.post("/entrevista/entregar-informe-seguimiento-guarda", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def entregar_informe_vinculacion(
    data: dict = Body(..., example = { "proyecto_id": 123 }),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    proyecto_id = data.get("proyecto_id")
    if not proyecto_id:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Debe especificarse el 'proyecto_id'",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Validar que el informe de vinculacion esté presente, al menos uno
    if not proyecto.doc_informe_seguimiento_guarda:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Debe cargarse el informe de seguimiento de guarda antes de presentar.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    if proyecto.estado_general != "guarda_confirmada":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Este proyecto no se encuentra en guarda. No se puede presentar este informe.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    try:

        # Registrar evento
        login_autor = current_user["user"]["login"]
        evento = RuaEvento(
            login = login_autor,
            evento_detalle = f"Se entregó el informe de seguimiento de guarda para el proyecto #{proyecto_id}",
            evento_fecha = datetime.now()
        )
        db.add(evento)

        
        # 🔔 Notificar a supervisoras y supervision
        supervisoras_y_supervision = db.query(User)\
            .join(UserGroup, User.login == UserGroup.login)\
            .join(Group, Group.group_id == UserGroup.group_id)\
            .filter(Group.description.in_(["supervisora", "supervision"]))\
            .all()

        for persona in supervisoras_y_supervision:
            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=persona.login,
                mensaje="📄 Un informe de seguimiento de guarda fue enviado a supervisión.",
                link="/menu_supervisoras/detalleProyecto",
                data_json={"proyecto_id": proyecto_id},
                tipo_mensaje="naranja"
            )
            if not resultado["success"]:
                db.rollback()
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Error al notificar a {persona.login}",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "📨 Informe de seguimiento de guarda enviado correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "menu_profesionales/portada"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }





@proyectos_router.get("/proyecto/{proyecto_id}/fecha-para-valorar", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def obtener_fecha_para_valorar(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📅 Devuelve la fecha y hora en la que un proyecto pasó al estado `para_valorar`.

    🔐 Solo accesible para administrador, supervisora y profesional.
    """
    try:
        historial = db.query(ProyectoHistorialEstado)\
            .filter(
                ProyectoHistorialEstado.proyecto_id == proyecto_id,
                ProyectoHistorialEstado.estado_nuevo == "para_valorar"
            )\
            .order_by(ProyectoHistorialEstado.fecha_hora.asc())\
            .first()

        if not historial:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "No se encontró un cambio al estado 'para_valorar' para este proyecto.",
                "tiempo_mensaje": 6
            }

        return {
            "success": True,
            "titulo": "Cambio a estado 'para_valorar'",
            "fecha_hora": historial.fecha_hora,  # datetime sin formatear
            "comentarios": None,
            "login_que_agenda": None,
            "creada_en": None,
            "evaluaciones": [],
            "evaluacion_comentarios": None
        }

    except SQLAlchemyError as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al consultar el historial: {str(e)}",
            "tiempo_mensaje": 6
        }



        
@proyectos_router.post("/valoracion/final", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def valorar_proyecto_final(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📌 Endpoint para que una supervisora registre la valoración final del proyecto.

    - Si es "viable", se deben definir los subregistros activos con códigos simples.
    - Si es "en_suspenso", debe indicarse una fecha de revisión.
    - Si es "no_viable" o "baja_anulacion", no requiere datos adicionales.
    - La observación debe ser enviada desde frontend como string.

    📥 JSON esperado:
    ```json
    {
      "proyecto_id": 123,
      "estado_final": "viable",
      "subregistros": ["1", "5a", "6b", "63+"],
      "fecha_revision": "2025-05-10",
      "observacion": "Se valora como disponible por cumplimiento de criterios técnicos y entrevistas satisfactorias."
    }
    ```

    """
    
    try:
        proyecto_id = data.get("proyecto_id")
        estado_final = data.get("estado_final")
        subregistros_raw = data.get("subregistros", [])
        fecha_revision = data.get("fecha_revision")
        texto_observacion = data.get("observacion")
        login_supervisora = current_user["user"]["login"]
        enviar_notificacion = data.get("enviar_notificacion", False)


        if estado_final not in ["viable", "en_suspenso", "no_viable"]:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Se debe indicar un estado final válido.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if not texto_observacion or not texto_observacion.strip():
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Debe indicar una observación.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if estado_final == "en_suspenso" and not fecha_revision:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Debe indicar una fecha de revisión para el estado En suspenso.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }


        proyecto = db.query(Proyecto).filter_by(proyecto_id=proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Proyecto no encontrado.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Observación y/o notificación según lógica
        observacion = None

        # 🔹 Siempre registrar observación si no se envía notificación
        if not enviar_notificacion:
            observacion = ObservacionesProyectos(
                observacion_fecha=datetime.now(),
                observacion=texto_observacion + " Valoración final: " + estado_final,
                login_que_observo=login_supervisora,
                observacion_a_cual_proyecto=proyecto_id
            )
            db.add(observacion)
            db.flush()

        # 🔹 Si se envía notificación y no es en_suspenso: solo notificación
        elif enviar_notificacion and estado_final in ["viable", "no_viable"]:
            if proyecto.login_1:
                crear_notificacion_individual(
                    db=db,
                    login_destinatario=proyecto.login_1,
                    mensaje=texto_observacion,
                    link="/menu_adoptantes/portada",
                    data_json={},
                    tipo_mensaje="naranja",
                    enviar_por_whatsapp=False,
                    login_que_notifico=login_supervisora
                )
            if proyecto.login_2:
                crear_notificacion_individual(
                    db=db,
                    login_destinatario=proyecto.login_2,
                    mensaje=texto_observacion,
                    link="/menu_adoptantes/portada",
                    data_json={},
                    tipo_mensaje="naranja",
                    enviar_por_whatsapp=False,
                    login_que_notifico=login_supervisora
                )
        
        # 🔹 Si es en_suspenso y enviar_notificacion, registrar observación + notificar
        elif enviar_notificacion and estado_final == "en_suspenso":
            observacion = ObservacionesProyectos(
                observacion_fecha=datetime.now(),
                observacion=texto_observacion + " Valoración final: " + estado_final,
                login_que_observo=login_supervisora,
                observacion_a_cual_proyecto=proyecto_id
            )
            db.add(observacion)
            db.flush()

            for login_destinatario in [proyecto.login_1, proyecto.login_2]:
                if login_destinatario:
                    crear_notificacion_individual(
                        db=db,
                        login_destinatario=login_destinatario,
                        mensaje=texto_observacion,
                        link="/menu_adoptantes/portada",
                        data_json={},
                        tipo_mensaje="naranja",
                        enviar_por_whatsapp=False,
                        login_que_notifico=login_supervisora
                    )

        # 🔹 Si es en_suspenso, registrar fecha_revision (requiere observación_id)
        if estado_final == "en_suspenso":
            fecha_revision_registro = FechaRevision(
                fecha_atencion=fecha_revision,
                observacion_id=observacion.observacion_id if observacion else None,
                login_que_registro=login_supervisora,
                proyecto_id=proyecto_id,
                cantidad_notificaciones=0
            )
            db.add(fecha_revision_registro)


        estado_anterior = proyecto.estado_general


        subregistros_definitivos_map = {
            "1": "subreg_1",
            "2": "subreg_2",
            "3": "subreg_3",
            "4": "subreg_4",
            "FE1": "subreg_FE1",
            "FE2": "subreg_FE2",
            "FE3": "subreg_FE3",
            "FE4": "subreg_FE4",
            "FET": "subreg_FET",
            "5A1E1": "subreg_5A1E1",
            "5A1E2": "subreg_5A1E2",
            "5A1E3": "subreg_5A1E3",
            "5A1E4": "subreg_5A1E4",
            "5A1ET": "subreg_5A1ET",
            "5A2E1": "subreg_5A2E1",
            "5A2E2": "subreg_5A2E2",
            "5A2E3": "subreg_5A2E3",
            "5A2E4": "subreg_5A2E4",
            "5A2ET": "subreg_5A2ET",
            "5B1E1": "subreg_5B1E1",
            "5B1E2": "subreg_5B1E2",
            "5B1E3": "subreg_5B1E3",
            "5B1E4": "subreg_5B1E4",
            "5B1ET": "subreg_5B1ET",
            "5B2E1": "subreg_5B2E1",
            "5B2E2": "subreg_5B2E2",
            "5B2E3": "subreg_5B2E3",
            "5B2E4": "subreg_5B2E4",
            "5B2ET": "subreg_5B2ET",
            "5B3E1": "subreg_5B3E1",
            "5B3E2": "subreg_5B3E2",
            "5B3E3": "subreg_5B3E3",
            "5B3E4": "subreg_5B3E4",
            "5B3ET": "subreg_5B3ET",
            "F5S": "subreg_F5S",
            "F5E1": "subreg_F5E1",
            "F5E2": "subreg_F5E2",
            "F5E3": "subreg_F5E3",
            "F5E4": "subreg_F5E4",
            "F5ET": "subreg_F5ET",
            "61E1": "subreg_61E1",
            "61E2": "subreg_61E2",
            "61E3": "subreg_61E3",
            "61ET": "subreg_61ET",
            "62E1": "subreg_62E1",
            "62E2": "subreg_62E2",
            "62E3": "subreg_62E3",
            "62ET": "subreg_62ET",
            "63E1": "subreg_63E1",
            "63E2": "subreg_63E2",
            "63E3": "subreg_63E3",
            "63ET": "subreg_63ET",
            "FQ1": "subreg_FQ1",
            "FQ2": "subreg_FQ2",
            "FQ3": "subreg_FQ3",
            "F6E1": "subreg_F6E1",
            "F6E2": "subreg_F6E2",
            "F6E3": "subreg_F6E3",
            "F6ET": "subreg_F6ET"
        }


        
        # --- helper: genera una contraseña temporal para Moodle ---
        def _generar_password_temporal_moodle() -> str:
            """
            Requisitos:
              - >= 6 dígitos
              - Dígitos no consecutivos (ni ascendentes ni descendentes de longitud >=3)
              - >= 1 mayúscula
              - >= 1 minúscula
              - largo total >= 10 (cumple también la política típica de Moodle >=8)
            """
            import random, string

            def _digitos_no_consecutivos(n=6):
                res = []
                while len(res) < n:
                    d = random.choice("0123456789")
                    if not res:
                        res.append(d)
                        continue
                    # evitar vecino inmediato asc/desc con el último
                    if abs(int(d) - int(res[-1])) != 1:
                        res.append(d)
                return res

            while True:
                # 6 dígitos “espaciados”
                digs = _digitos_no_consecutivos(6)
                # garantías de letras
                must = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase)]
                # relleno extra (letras aleatorias) para llegar a >=10
                extra = [random.choice(string.ascii_letters) for _ in range(4)]
                chars = digs + must + extra
                random.shuffle(chars)
                pwd = "".join(chars)

                # doble verificación por las dudas
                if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) \
                  and sum(c.isdigit() for c in pwd) >= 6 \
                  and not check_consecutive_numbers(pwd):
                    return pwd


        # --- helper: asegura cuenta en Moodle y enrolamiento al curso correspondiente ---
        def _asegurar_moodle_y_enrolamiento(user: User):
            """
            Intenta asegurar que el usuario exista en Moodle (username = DNI/login, mail = user.mail)
            y que esté enrolado en el curso correspondiente.

            - Si existe con mismo DNI y mismo mail: puede opcionalmente actualizar clave temporal (no necesario).
            - Si existe sólo DNI o sólo mail (conflicto): registra evento y salta (no interrumpe).
            - Si no existe: lo crea con clave temporal y lo enrola.
            """
            try:
                dni = str(user.login)
                mail = (user.mail or "").lower()
                nombre = user.nombre or ""
                apellido = user.apellido or ""

                if not dni or not mail:
                    # nada que hacer si faltan datos
                    db.add(RuaEvento(
                        login=user.login,
                        evento_detalle="Moodle: omitido por datos insuficientes (dni/mail).",
                        evento_fecha=datetime.now()
                    ))
                    db.commit()
                    return

                dni_en_moodle = existe_dni_en_moodle(dni, db)
                mail_en_moodle = existe_mail_en_moodle(mail, db)

                if dni_en_moodle and mail_en_moodle:
                    # ✅ Ya existe con mismo DNI y mail: sólo asegurar enrolamiento
                    id_curso = get_idcurso(db)
                    id_usuario = get_idusuario_by_mail(mail, db)
                    enrolar_usuario(id_curso, id_usuario, db)
                    db.add(RuaEvento(
                        login=user.login,
                        evento_detalle="Moodle: usuario ya existía, enrolamiento asegurado.",
                        evento_fecha=datetime.now()
                    ))
                    db.commit()
                    return

                if dni_en_moodle and not mail_en_moodle:
                    # ⚠ conflicto: DNI en Moodle pero con otro mail
                    db.add(RuaEvento(
                        login=user.login,
                        evento_detalle="Moodle: conflicto (existe DNI con otro mail). Enrolamiento omitido.",
                        evento_fecha=datetime.now()
                    ))
                    db.commit()
                    return

                if not dni_en_moodle and mail_en_moodle:
                    # ⚠ conflicto: mail en Moodle ligado a otro username/DNI
                    db.add(RuaEvento(
                        login=user.login,
                        evento_detalle="Moodle: conflicto (existe mail con otro DNI). Enrolamiento omitido.",
                        evento_fecha=datetime.now()
                    ))
                    db.commit()
                    return

                # ✅ No existe: crearlo con contraseña temporal
                clave_tmp = _generar_password_temporal_moodle()
                crear_usuario_en_moodle(dni, clave_tmp, nombre, apellido, mail, db)

                # Enrolar
                id_curso = get_idcurso(db)
                id_usuario = get_idusuario_by_mail(mail, db)
                enrolar_usuario(id_curso, id_usuario, db)

                db.add(RuaEvento(
                    login=user.login,
                    evento_detalle="Moodle: usuario creado y enrolado con clave temporal.",
                    evento_fecha=datetime.now()
                ))
                db.commit()

            except HTTPException as e:
                db.rollback()
                # trazamos, pero no hacemos fallar el endpoint principal
                db.add(RuaEvento(
                    login=user.login,
                    evento_detalle=f"Moodle: error HTTP al crear/enrolar ({e.detail}).",
                    evento_fecha=datetime.now()
                ))
                db.commit()
            except Exception as e:
                db.rollback()
                db.add(RuaEvento(
                    login=user.login,
                    evento_detalle=f"Moodle: error inesperado al crear/enrolar ({str(e)}).",
                    evento_fecha=datetime.now()
                ))
                db.commit()

        # --- helper local: envia mail "crear contraseña" sólo si NO tiene clave + asegura Moodle ---
        # Devuelve: (True/False, "ok"/motivo, "omitido" si ya tenía clave)
        def _enviar_mail_activacion(login_destino: str):
            user: User = db.query(User).filter(User.login == login_destino).first()
            if not user or not user.mail:
                return False, "Usuario sin mail o inexistente.", None

            # 👈 NO enviar si ya tiene contraseña seteada
            tiene_clave = bool((user.clave or "").strip())
            if tiene_clave:
                return True, "omitido_por_tener_clave", "omitido"

            # 1) activar si estaba inactivo
            recien_activado = False
            if user.active != "Y":
                user.active = "Y"
                recien_activado = True

            # 2) asegurar cuenta/enrolamiento en Moodle (sólo en este escenario de SIN CLAVE)
            _asegurar_moodle_y_enrolamiento(user)

            # 3) reusar o generar código
            rec_code = (user.recuperacion_code or "").strip()
            if not rec_code:
                rec_code = generar_codigo_para_link(16)
                user.recuperacion_code = rec_code

            db.commit()
            db.refresh(user)

            # 4) link desde settings
            protocolo = get_setting_value(db, "protocolo")
            host = get_setting_value(db, "donde_esta_alojado")
            puerto = get_setting_value(db, "puerto_tcp")
            endpoint = get_setting_value(db, "endpoint_recuperar_clave")

            if endpoint and not endpoint.startswith("/"):
                endpoint = "/" + endpoint

            puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
            host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host
            link = f"{protocolo}://{host_con_puerto}{endpoint}?activacion={rec_code}"

            # 5) email
            asunto = "Establecimiento de contraseña"
            cuerpo = f"""
            <html>
              <body style="margin:0;padding:0;background-color:#f8f9fa;">
                <table cellpadding="0" cellspacing="0" width="100%" style="background-color:#f8f9fa;padding:20px;">
                  <tr><td align="center">
                    <table cellpadding="0" cellspacing="0" width="600" style="background:#ffffff;border-radius:10px;padding:30px;
                          font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;color:#343a40;
                          box-shadow:0 0 10px rgba(0,0,0,0.1);">
                      
                      <tr>
                        <td style="font-size:24px;color:#007bff;">
                          <strong>¡Hola {user.nombre or ""}!</strong>
                        </td>
                      </tr>

                      <tr>
                        <td style="padding-top:20px;font-size:17px;">
                          <p>
                            Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong> para invitarte a crear tu 
                            contraseña para ingresar a la plataforma RUA, hacer el curso informativo y presentar tu documentación 
                            personal para avanzar con tu postulación en la convocatoria pública.
                          </p>
                        </td>
                      </tr>

                      <tr>
                        <td style="padding-top:18px;font-size:17px;line-height:1.5;">
                          <p>Hacé clic en el siguiente botón para crear tu contraseña. </p>
                          
                        </td>
                      </tr>

                      <tr>
                        <td align="center" style="padding:26px 0;">
                          <a href="{link}" target="_blank"
                            style="display:inline-block;padding:12px 24px;font-size:16px;color:#ffffff;
                                  background:#0d6efd;text-decoration:none;border-radius:8px;font-weight:600;">
                            🔐 Crear mi contraseña
                          </a>
                        </td>
                      </tr>

                      <tr>
                        <td align="center" style="padding-top:10px;">
                          <p style="font-size:15px;color:#555;">
                            Luego, ingresas con tu DNI y la contraseña elegida al 
                            <a href="https://rua.justiciacordoba.gob.ar" target="_blank"
                              style="color:#007bff;text-decoration:none;font-weight:500;">
                              Sistema RUA
                            </a>.
                          </p>
                        </td>
                      </tr>

                      <tr>
                        <td style="padding-top:20px;font-size:13px;color:#888;text-align:center;">
                          Este mensaje fue generado automáticamente. Por favor, no responda este correo.<br>
                          <br>
                          Registro Único de Adopciones de Córdoba
                        </td>
                      </tr>

                    </table>
                  </td></tr>
                </table>
              </body>
            </html>
            """

            enviar_mail(destinatario=user.mail, asunto=asunto, cuerpo=cuerpo)

            # 6) evento best-effort
            try:
                detalle = "Se envió enlace para elegir nueva contraseña."
                if recien_activado:
                    detalle += " Estaba inactivo y fue activado."
                db.add(RuaEvento(login=user.login, evento_detalle=detalle, evento_fecha=datetime.now()))
                db.commit()
            except Exception:
                db.rollback()
            return True, "ok", None

        
        if estado_final == "viable" and not subregistros_raw:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Debe seleccionar al menos un subregistro para valorar como viable.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
        

        if estado_final == "viable":
            # ✅ Limpiar todos los campos subreg_* a "N"
            for campo in subregistros_definitivos_map.values():
                campo_normalizado = str(campo).strip()
                if hasattr(proyecto, campo_normalizado):
                    setattr(proyecto, campo_normalizado, "N")

            # ✅ Activar solo los seleccionados
            for codigo in subregistros_raw:
                codigo_normalizado = str(codigo).strip().upper()  # limpiar espacios y forzar mayúsculas
                campo = subregistros_definitivos_map.get(codigo_normalizado)
                if campo:
                    campo_normalizado = str(campo).strip()
                    if hasattr(proyecto, campo_normalizado):
                        setattr(proyecto, campo_normalizado, "Y")



        elif estado_final == "en_suspenso":
            if not fecha_revision:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "Debe indicar una fecha de revisión para el estado En suspenso",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

        proyecto.estado_general = estado_final
        db.add(proyecto)


        historial = ProyectoHistorialEstado(
            proyecto_id = proyecto_id,
            estado_anterior = estado_anterior,
            estado_nuevo = estado_final,
            fecha_hora = datetime.now()
        )
        db.add(historial)

        evento = RuaEvento(
            login = login_supervisora,
            evento_detalle = f"Proyecto #{proyecto_id} valorado como {estado_final}.",
            evento_fecha = datetime.now()
        )
        db.add(evento)


        # ✅ Enviar mail si se pidió notificación
        if enviar_notificacion:
            logins_destinatarios = [proyecto.login_1]
            if proyecto.login_2:
                logins_destinatarios.append(proyecto.login_2)

            for login_destinatario in logins_destinatarios:
                user = db.query(User).filter(User.login == login_destinatario).first()
                if user and user.mail:
                    try:
                        cuerpo_mensaje_html = f"""
                            <p>Recibiste una notificación del <strong>RUA</strong>:</p>
                            <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                                <em>{texto_observacion}</em>
                            </div>
                        """
                        cuerpo = f"""
                        <html>
                        <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                            <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                            <tr>
                                <td align="center">
                                <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                    <tr>
                                    <td style="font-size: 24px; color: #007bff;">
                                        <strong>Hola {user.nombre},</strong>
                                    </td>
                                    </tr>
                                    <tr>
                                    <td style="padding-top: 20px; font-size: 17px;">
                                        {cuerpo_mensaje_html}
                                    </td>
                                    </tr>
                                    <tr>
                                    <td align="center" style="font-size: 17px; padding-top: 30px;">
                                        <p><strong>Muchas gracias.</strong></p>
                                    </td>
                                    </tr>
                                    <tr>
                                    <td style="padding-top: 30px;">
                                        <hr style="border: none; border-top: 1px solid #dee2e6;">
                                        <p style="font-size: 15px; color: #6c757d; margin-top: 20px;">
                                        <strong>Registro Único de Adopciones de Córdoba</strong>
                                        </p>
                                    </td>
                                    </tr>
                                </table>
                                </td>
                            </tr>
                            </table>
                        </body>
                        </html>
                        """
                        enviar_mail(
                            destinatario=user.mail,
                            asunto="Notificación del Sistema RUA",
                            cuerpo=cuerpo
                        )
                    except Exception as e:
                        db.rollback()
                        return {
                            "success": False,
                            "tipo_mensaje": "naranja",
                            "mensaje": f"⚠️ Error al enviar correo a {user.nombre}: {str(e)}",
                            "tiempo_mensaje": 5,
                            "next_page": "actual"
                        }



        db.commit()



        
        # 🔹 Enviar mails de activación si es proyecto por convocatoria y pasa a viable
        if estado_final == "viable" and (proyecto.ingreso_por or "").lower() == "convocatoria":
            logins_destino = [proyecto.login_1]
            if proyecto.login_2 and str(proyecto.login_2).strip():
                logins_destino.append(proyecto.login_2)

            for lg in logins_destino:
                try:
                    ok, detalle, omitido = _enviar_mail_activacion(lg)

                    evento_txt = (
                        f"Activación {'NO enviada (ya tenía clave)' if omitido == 'omitido' else 'enviada'} "
                        f"por valoración final como 'viable' en proyecto #{proyecto_id}. Resultado: {'OK' if ok else 'FALLÓ'}"
                    )

                    db.add(RuaEvento(login=lg, evento_detalle=evento_txt, evento_fecha=datetime.now()))
                    db.commit()
                except Exception as e:
                    db.rollback()
                    try:
                        db.add(RuaEvento(
                            login=lg,
                            evento_detalle=f"Fallo al enviar activación en valoración final de proyecto #{proyecto_id}: {str(e)}",
                            evento_fecha=datetime.now()
                        ))
                        db.commit()
                    except:
                        db.rollback()




        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Valoración final registrada como {estado_final.replace('_', ' ').upper()}.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Error inesperado al valorar el proyecto:",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }




@proyectos_router.get( "/proyectos/entrevista/informe/{proyecto_id}/descargar", response_class=FileResponse,
    dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"])) ] )
def descargar_informe_valoracion(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📄 Descarga el Informe de Valoración (informe_profesionales) asociado al proyecto.
    """
    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    filepath = proyecto.informe_profesionales
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Informe de valoración no encontrado")

    return FileResponse(
        path=filepath,
        filename=os.path.basename(filepath),
        media_type="application/octet-stream"
    )




# 1) Informe de valoración
@proyectos_router.put( "/entrevista/informe/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional"]))])
def subir_informe_valoracion(
    proyecto_id:int,
    file:UploadFile=File(...),
    db:Session=Depends(get_db)
    ):

    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _save_historial_upload(proyecto,"informe_profesionales",file,UPLOAD_DIR_DOC_PROYECTOS, db)



@proyectos_router.get("/entrevista/informe/{proyecto_id}/descargar-todos", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), 
    Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))])
def descargar_todos_valoracion(
    proyecto_id:int, db:Session=Depends(get_db)
    ):

    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _download_all(proyecto.informe_profesionales or "","informes_valoracion", proyecto_id)



# 2) Informe de vinculación
@proyectos_router.put( "/informe-vinculacion/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), 
    Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))])
def subir_informe_vinculacion(
    proyecto_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
    ):

    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        raise HTTPException(404, "Proyecto no encontrado")
    return _save_historial_upload( proyecto, "doc_informe_vinculacion", file, UPLOAD_DIR_DOC_PROYECTOS, db )



@proyectos_router.get("/informe-vinculacion/{proyecto_id}/descargar-todos",
    response_class=FileResponse, dependencies=[Depends(verify_api_key), 
    Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))])
def descargar_todos_vinculacion(
    proyecto_id:int, db:Session=Depends(get_db)
    ):

    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _download_all(proyecto.doc_informe_vinculacion or "","informes_vinculacion",proyecto_id)



# 3) Informe seguimiento de guarda
@proyectos_router.put( "/informe-seguimiento-guarda/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), 
    Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))])
def subir_informe_guarda(
    proyecto_id:int,
    file:UploadFile=File(...),
    db:Session=Depends(get_db)
    ):

    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _save_historial_upload(proyecto,"doc_informe_seguimiento_guarda",file,UPLOAD_DIR_DOC_PROYECTOS, db)



@proyectos_router.get("/informe-seguimiento-guarda/{proyecto_id}/descargar-todos", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))])
def descargar_todos_guarda(
    proyecto_id:int, db:Session=Depends(get_db)
    ):

    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _download_all(proyecto.doc_informe_seguimiento_guarda or "","informes_guarda",proyecto_id)

    


@proyectos_router.get("/proyectos/entrevista/informe/{proyecto_id}/descargar-todos", response_class=FileResponse,
    dependencies=[ Depends(verify_api_key),
        Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"])) ] )
def descargar_todos_informes_valoracion(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    Descarga todos los informes de valoración asociados a un proyecto:
    - Si hay uno solo, lo devuelve directamente.
    - Si hay varios, los empaqueta en un .zip.
    """
    # 1️⃣ Obtener proyecto
    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # 2️⃣ Leer JSON de informes
    raw = proyecto.informe_profesionales or ""
    try:
        if raw.strip().startswith("["):
            archivos = json.loads(raw)
        elif raw.strip():
            archivos = [{"ruta": raw}]
        else:
            archivos = []
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="JSON inválido en informe_profesionales")

    if not archivos:
        raise HTTPException(status_code=404, detail="No hay informes registrados")

    # 3️⃣ Si solo hay uno, descargar directamente
    if len(archivos) == 1:
        ruta = archivos[0].get("ruta")
        if not ruta or not os.path.exists(ruta):
            raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")
        return FileResponse(
            path=ruta,
            filename=os.path.basename(ruta),
            media_type="application/octet-stream"
        )

    # 4️⃣ Si hay más, crear ZIP
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zipf:
            for entry in archivos:
                ruta = entry.get("ruta")
                if ruta and os.path.exists(ruta):
                    zipf.write(ruta, arcname=os.path.basename(ruta))
        return FileResponse(
            path=tmp.name,
            filename=f"informes_valoracion_{proyecto_id}.zip",
            media_type="application/zip"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al generar ZIP: {e}")    



@proyectos_router.get("/documento/{proyecto_id}/{tipo_documento}/descargar", response_class = FileResponse,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def descargar_documento_proyecto(
    proyecto_id: int,
    tipo_documento: Literal["informe_entrevistas", "sentencia_guarda", "sentencia_adopcion", "doc_interrupcion"],
    db: Session = Depends(get_db)
    ):

    """
    📄 Descarga un documento del proyecto identificado por `proyecto_id`.

    ⚠️ El documento debe haber sido subido previamente mediante el endpoint correspondiente.
    """

    # Mapeo del tipo_documento a los campos del modelo
    campo_por_tipo = {
        "informe_entrevistas": "informe_profesionales",
        "sentencia_guarda": "doc_sentencia_guarda",
        "sentencia_adopcion": "doc_sentencia_adopcion",
        "doc_interrupcion": "doc_interrupcion"
    }

    if tipo_documento not in campo_por_tipo:
        raise HTTPException(status_code = 400, detail = f"Tipo de documento inválido: {tipo_documento}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    campo_modelo = campo_por_tipo[tipo_documento]
    filepath = getattr(proyecto, campo_modelo)

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code = 404, detail = f"Documento '{tipo_documento}' no encontrado")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )



@proyectos_router.put("/dictamen/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def subir_dictamen(
    proyecto_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📄 Sube un archivo que es el dictamen del juzgado cuando elige a este proyecto.

    Guarda el archivo en la carpeta del proyecto y actualiza el campo `doc_dictamen`.
    También actualiza el estado a 'vinculacion' y lo registra en el historial.
    """

    # Validar extensión del archivo
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code = 400, detail = f"Extensión de archivo no permitida: {ext}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    # Crear carpeta si no existe
    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok = True)

    # Guardar con nombre único
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"dictamen_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        login_actual = current_user["user"]["login"]
        estado_anterior = proyecto.estado_general  # 🟡 Guardamos antes de cambiarlo

        proyecto.doc_dictamen = filepath
        proyecto.estado_general = 'vinculacion'  # 🟢 Nuevo estado

        unify_on_enter_vinculacion(
            db = db,
            proyecto_convocatoria_id = proyecto_id,
            login_usuario = login_actual,
        )

        # ✅ Registrar en el historial
        historial = ProyectoHistorialEstado(
            proyecto_id = proyecto_id,
            estado_anterior = estado_anterior,
            estado_nuevo = 'vinculacion',
            fecha_hora = datetime.now(),
        )
        db.add(historial)

        # Registrar evento RuaEvento
        evento = RuaEvento(
            login = login_actual,
            evento_detalle = f"Subió el dictamen al proyecto #{proyecto_id} y pasó a vinculación",
            evento_fecha = datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "message": f"Dictamen subido correctamente como '{final_filename}'.",
            "path": filepath
        }

    except HTTPException as e:
        db.rollback()
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar el archivo: {str(e)}")




@proyectos_router.get("/dictamen/{proyecto_id}/descargar", response_class = FileResponse,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def descargar_dictamen(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📄 Descarga el dictamen del proyecto identificado por `proyecto_id`.

    ⚠️ El dictamen debe haber sido cargado previamente mediante el endpoint de subida.
    """

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    # Usar directamente el campo 'doc_dictamen'
    filepath = proyecto.doc_dictamen

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code = 404, detail = "Dictamen no encontrado")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )



@proyectos_router.post("/por-oficio", response_model = dict, status_code = status.HTTP_200_OK,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def crear_proyecto_por_oficio(data: dict = Body(...), db: Session = Depends(get_db)):
    """
    📄 Crea un nuevo proyecto ingresado por oficio.

    Este endpoint permite registrar un nuevo proyecto cuando el caso proviene de un oficio judicial o similar,
    validando o dando de alta automáticamente a los usuarios adoptantes (login_1 y opcionalmente login_2).

    🔒 Solo accesible para perfiles con rol `administrador` o `supervisora`.

    ---

    ✅ Reglas y Validaciones:
    - El campo `proyecto_tipo` debe ser uno de: `"Monoparental"`, `"Matrimonio"`, `"Unión convivencial"`.
    - El campo `login_1` (DNI del primer adoptante) es obligatorio.
    - Si `proyecto_tipo` es `"Matrimonio"` o `"Unión convivencial"`, también se requiere `login_2`.
    - Si el usuario ya existe en `sec_users`, se valida que el mail ingresado coincida exactamente con el registrado.  
    En caso contrario, se aborta la creación del proyecto y se informa del conflicto.
    - Si el usuario no existe, se da de alta automáticamente con rol "adoptante".

    📥 Campos esperados (JSON):

    ```json
    {
    "proyecto_tipo": "Matrimonio",
    "login_1": "12345678",
    "mail_1": "persona1@example.com",
    "nombre_1": "Lucía",
    "apellido_1": "Gómez",

    "login_2": "23456789",
    "mail_2": "persona2@example.com",
    "nombre_2": "Javier",
    "apellido_2": "Pérez",

    "proyecto_calle_y_nro": "Av. Siempre Viva 742",
    "proyecto_depto_etc": "A",
    "proyecto_barrio": "Centro",
    "proyecto_localidad": "Córdoba",
    "proyecto_provincia": "Córdoba",

    "subregistro_1": "Y",
    "subregistro_2": "N",
    "subregistro_3": "N",
    "subregistro_4": "Y",
    "subregistro_5_a": "N",
    "subregistro_5_b": "N",
    "subregistro_5_c": "N",
    "subregistro_6_a": "N",
    "subregistro_6_b": "N",
    "subregistro_6_c": "N",
    "subregistro_6_d": "N",
    "subregistro_6_2": "N",
    "subregistro_6_3": "N",
    "subregistro_6_mas_de_3": "N",
    "subregistro_flexible": "Y",
    "subregistro_otra_provincia": "N"
    }
    ```
    """


    try:
        tipo = data.get("proyecto_tipo")
        login_1 = data.get("login_1")
        login_2 = data.get("login_2")
        mail_1 = (data.get("mail_1") or "").strip().lower()
        mail_2 = (data.get("mail_2") or "").strip().lower()

        if tipo not in ["Monoparental", "Matrimonio", "Unión convivencial"]:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "<p>Tipo de proyecto inválido.</p>",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if not login_1:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "<p>Falta el campo obligatorio 'login_1'.</p>",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        grupo_adoptante = db.query(Group).filter(Group.description == "adoptante").first()

        # 🔍 Validar login_1
        user1 = db.query(User).filter(User.login == login_1).first()
        if user1:
            user1_mail = (user1.mail or "").strip().lower()
            if user1_mail != mail_1:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"<p>El usuario con DNI {login_1} ya existe, pero su mail registrado es <b>{user1.mail or 'sin mail'}</b>.</p>",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }
        else:
            user1 = User(
                login = login_1,
                nombre = data.get("nombre_1", ""),
                apellido = data.get("apellido_1", ""),
                mail = mail_1,
                active = "Y",
                operativo = "Y",
                doc_adoptante_curso_aprobado = 'Y',
                doc_adoptante_ddjj_firmada = 'Y',
                fecha_alta = date.today()
            )
            db.add(user1)
            if grupo_adoptante:
                db.add(UserGroup(login = login_1, group_id = grupo_adoptante.group_id))

        # 🔍 Validar login_2 si aplica
        if tipo in ["Matrimonio", "Unión convivencial"]:
            if not login_2:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": "<p>Debe incluirse 'login_2' para este tipo de proyecto.</p>",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }

            user2 = db.query(User).filter(User.login == login_2).first()
            if user2:
                user2_mail = (user2.mail or "").strip().lower()
                if user2_mail != mail_2:
                    return {
                        "success": False,
                        "tipo_mensaje": "rojo",
                        "mensaje": f"<p>El usuario con DNI {login_2} ya existe, pero su mail registrado es <b>{user2.mail or 'sin mail'}</b>.</p>",
                        "tiempo_mensaje": 6,
                        "next_page": "actual"
                    }
            else:
                user2 = User(
                    login = login_2,
                    nombre = data.get("nombre_2", ""),
                    apellido = data.get("apellido_2", ""),
                    mail = mail_2,
                    active = "Y",
                    operativo = "Y",
                    doc_adoptante_curso_aprobado = 'Y',
                    doc_adoptante_ddjj_firmada = 'Y',
                    fecha_alta = date.today()
                )
                db.add(user2)
                if grupo_adoptante:
                    db.add(UserGroup(login = login_2, group_id = grupo_adoptante.group_id))

        # Subregistros por defecto
        def sr(key): return data.get(key, "N") if data.get(key) in ["Y", "N"] else "N"

        nuevo_proyecto = Proyecto(
            proyecto_tipo = tipo,
            login_1 = login_1,
            login_2 = login_2 if tipo != "Monoparental" else None,
            proyecto_calle_y_nro = data.get("proyecto_calle_y_nro"),
            proyecto_depto_etc = data.get("proyecto_depto_etc"),
            proyecto_barrio = data.get("proyecto_barrio"),
            proyecto_localidad = data.get("proyecto_localidad"),
            proyecto_provincia = data.get("proyecto_provincia"),

            ingreso_por = "oficio",
            operativo = "Y",

            subregistro_1 = sr("subregistro_1"),
            subregistro_2 = sr("subregistro_2"),
            subregistro_3 = sr("subregistro_3"),
            subregistro_4 = sr("subregistro_4"),
            subregistro_5_a = sr("subregistro_5_a"),
            subregistro_5_b = sr("subregistro_5_b"),
            subregistro_5_c = sr("subregistro_5_c"),
            subregistro_6_a = sr("subregistro_6_a"),
            subregistro_6_b = sr("subregistro_6_b"),
            subregistro_6_c = sr("subregistro_6_c"),
            subregistro_6_d = sr("subregistro_6_d"),
            subregistro_6_2 = sr("subregistro_6_2"),
            subregistro_6_3 = sr("subregistro_6_3"),
            subregistro_6_mas_de_3 = sr("subregistro_6_mas_de_3"),
            subregistro_flexible = sr("subregistro_flexible"),
            subregistro_otra_provincia = sr("subregistro_otra_provincia"),

            aceptado = "Y",
            aceptado_code = None,
            estado_general = "aprobado",
        )

        db.add(nuevo_proyecto)
        db.commit()
        db.refresh(nuevo_proyecto)

        # 📋 Registrar evento en rua_evento
        db.add(RuaEvento(
            login = login_1,
            evento_detalle = f"Proyecto creado por ingreso 'oficio'. ID: {nuevo_proyecto.proyecto_id}",
            evento_fecha = datetime.now()
        ))

        # 📋 RuaEvento para login_2 si corresponde
        if login_2:
            db.add(RuaEvento(
                login = login_2,
                evento_detalle = f"Proyecto creado por ingreso 'oficio'. ID: {nuevo_proyecto.proyecto_id} (como cónyuge)",
                evento_fecha = datetime.now()
            ))

        # 🕓 Registrar en historial de estados
        db.add(ProyectoHistorialEstado(
            proyecto_id = nuevo_proyecto.proyecto_id,
            estado_nuevo = "aprobado",
            fecha_hora = datetime.now()
        ))

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"<p>Proyecto creado exitosamente por oficio.</p>",
            "tiempo_mensaje": 6,
            "next_page": "menu_supervisoras/proyectos",
            "proyecto_id": nuevo_proyecto.proyecto_id
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"<p>Error de base de datos: {str(e)}</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    except Exception as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"<p>Error inesperado: {str(e)}</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@proyectos_router.put("/guarda/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def subir_sentencia_guarda(
    proyecto_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📄 Sube la sentencia de guarda para un proyecto.

    No cambia el estado ni guarda observación.
    """
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code = 400, detail = f"Extensión de archivo no permitida: {ext}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok = True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"sentencia_guarda_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        proyecto.doc_sentencia_guarda = filepath
        db.commit()

        return {
            "success": True,
            "message": f"Sentencia de guarda subido como '{final_filename}'.",
            "path": filepath
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar el archivo: {str(e)}")



@proyectos_router.get("/guarda/{proyecto_id}/descargar", response_class = FileResponse,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def descargar_sentencia_guarda(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📄 Descarga la sentencia de guarda del proyecto identificado por `proyecto_id`.

    ⚠️ La sentencia debe haber sido cargada previamente.
    """
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    filepath = proyecto.doc_sentencia_guarda

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code = 404, detail = "Sentencia de guarda no encontrada")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )



@proyectos_router.put("/confirmar-guarda-provisoria/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def confirmar_guarda_provisoria(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    observacion = body.get("observacion", "").strip()

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False, 
            "tipo_mensaje": "rojo", 
            "mensaje": "Proyecto no encontrado.", 
            "tiempo_mensaje": 5, 
            "next_page": "actual"
        }

    try:
        # (opcional) Validación de estado actual
        # if proyecto.estado_general != "vinculacion":
        #     return {"success": False, "tipo_mensaje": "amarillo", "mensaje": "El proyecto no está en 'vinculacion'.", "tiempo_mensaje": 5, "next_page": "actual"}

        # Evento + observación
        db.add(RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se confirmó la guarda provisoria para el proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        ))

        if observacion:
            db.add(ObservacionesProyectos(
                observacion_a_cual_proyecto=proyecto_id,
                observacion=observacion,
                login_que_observo=current_user["user"]["login"],
                observacion_fecha=datetime.now()
            ))

        db.add(ProyectoHistorialEstado(
            proyecto_id=proyecto_id,
            estado_anterior=proyecto.estado_general,  # ← mejor que hardcodear "vinculacion"
            estado_nuevo="guarda_provisoria",
            fecha_hora=datetime.now()
        ))

        # Actualizar PROYECTO
        proyecto.estado_general = "guarda_provisoria"

        # 🔁 Actualizar NNA asociados
        nnas_actualizados = _set_estado_nna_por_proyecto(db, proyecto_id, "guarda_provisoria")

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"La guarda provisoria fue confirmada correctamente. NNA actualizados: {nnas_actualizados}.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    except Exception:
        db.rollback()
        return {
            "success": False, 
            "tipo_mensaje": "rojo", 
            "mensaje": "Error al confirmar la guarda provisoria.", 
            "tiempo_mensaje": 6, 
            "next_page": "actual"
        }




@proyectos_router.put("/confirmar-sentencia-guarda/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def confirmar_sentencia_guarda(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    observacion = body.get("observacion", "").strip()

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False, 
            "tipo_mensaje": "rojo", 
            "mensaje": "Proyecto no encontrado.", 
            "tiempo_mensaje": 5, 
            "next_page": "actual"
        }

    try:
        # (opcional) Validación de estado actual
        # if proyecto.estado_general != "guarda_provisoria":
        #     return {"success": False, "tipo_mensaje": "amarillo", "mensaje": "El proyecto no está en 'guarda_provisoria'.", "tiempo_mensaje": 5, "next_page": "actual"}

        db.add(RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se confirmó la sentencia de guarda para el proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        ))

        if observacion:
            db.add(ObservacionesProyectos(
                observacion_a_cual_proyecto=proyecto_id,
                observacion=observacion,
                login_que_observo=current_user["user"]["login"],
                observacion_fecha=datetime.now()
            ))

        db.add(ProyectoHistorialEstado(
            proyecto_id=proyecto_id,
            estado_anterior=proyecto.estado_general,  # ← no hardcodear
            estado_nuevo="guarda_confirmada",
            fecha_hora=datetime.now()
        ))

        proyecto.estado_general = "guarda_confirmada"

        # 🔁 NNA asociados
        nnas_actualizados = _set_estado_nna_por_proyecto(db, proyecto_id, "guarda_confirmada")

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"La sentencia de guarda fue confirmada correctamente. NNA actualizados: {nnas_actualizados}.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    except Exception:
        db.rollback()
        return {
            "success": False, 
            "tipo_mensaje": "rojo", 
            "mensaje": "Error al confirmar la sentencia de guarda.", 
            "tiempo_mensaje": 6, 
            "next_page": "actual"
        }



@proyectos_router.put("/confirmar-sentencia-adopcion/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def confirmar_sentencia_adopcion(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    observacion = body.get("observacion", "").strip()

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False, 
            "tipo_mensaje": "rojo", 
            "mensaje": "Proyecto no encontrado.", 
            "tiempo_mensaje": 5, 
            "next_page": "actual"
        }

    try:
        # (opcional) Validación de estado actual
        # if proyecto.estado_general != "guarda_confirmada":
        #     return {"success": False, "tipo_mensaje": "amarillo", "mensaje": "El proyecto no está en 'guarda_confirmada'.", "tiempo_mensaje": 5, "next_page": "actual"}

        db.add(RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se confirmó la sentencia de adopción para el proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        ))

        if observacion:
            db.add(ObservacionesProyectos(
                observacion_a_cual_proyecto=proyecto_id,
                observacion=observacion,
                login_que_observo=current_user["user"]["login"],
                observacion_fecha=datetime.now()
            ))

        db.add(ProyectoHistorialEstado(
            proyecto_id=proyecto_id,
            estado_anterior=proyecto.estado_general,  # ← no hardcodear
            estado_nuevo="adopcion_definitiva",
            fecha_hora=datetime.now()
        ))

        proyecto.estado_general = "adopcion_definitiva"

        # 🔁 NNA asociados
        nnas_actualizados = _set_estado_nna_por_proyecto(db, proyecto_id, "adopcion_definitiva")

        if proyecto.ingreso_por == "rua":
            _preparar_pretensos_para_nuevo_proceso(db, proyecto)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"La sentencia de adopción fue confirmada correctamente. NNA actualizados: {nnas_actualizados}.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    except Exception:
        db.rollback()
        return {
            "success": False, 
            "tipo_mensaje": "rojo", 
            "mensaje": "Error al confirmar la sentencia de adopción.", 
            "tiempo_mensaje": 6, 
            "next_page": "actual"
        }



@proyectos_router.put("/interrumpir-vinculacion-o-guarda/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))],)
def interrumpir_vinculacion_o_guarda(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    ):

    """
    Interrumpe el proceso del proyecto (vinculación/guarda), libera NNA y elimina carpeta(s) asociadas.
    - Proyecto -> 'baja_interrupcion'
    - NNA en carpetas del proyecto -> 'disponible'
    - Se eliminan DetalleProyectosEnCarpeta y DetalleNNAEnCarpeta, y luego la Carpeta.
    - No se tocan archivos: se asume que están en doc_dictamen/doc_interrupcion del proyecto.
    """
    observacion = (body.get("observacion") or "").strip()

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual",
        }

    try:
        estado_anterior = proyecto.estado_general

        # Evento principal
        db.add(
            RuaEvento(
                login=current_user["user"]["login"],
                evento_detalle=f"Se interrumpió la vinculación/guarda del proyecto #{proyecto_id}",
                evento_fecha=datetime.now(),
            )
        )

        # Observación (opcional)
        if observacion:
            db.add(
                ObservacionesProyectos(
                    observacion_a_cual_proyecto=proyecto_id,
                    observacion=observacion,
                    login_que_observo=current_user["user"]["login"],
                    observacion_fecha=datetime.now(),
                )
            )

        # Historial de estado del proyecto
        db.add(
            ProyectoHistorialEstado(
                proyecto_id=proyecto_id,
                estado_anterior=estado_anterior,
                estado_nuevo="baja_interrupcion",
                fecha_hora=datetime.now(),
            )
        )

        # Estado del proyecto
        proyecto.estado_general = "baja_interrupcion"

        # Buscar TODAS las carpetas donde está el proyecto
        carpetas = (
            db.query(Carpeta)
            .options(
                noload(Carpeta.detalle_nna),        # 👈 evita cargar relaciones
                noload(Carpeta.detalle_proyectos),  # 👈 evita cargar relaciones
            )
            .join(
                DetalleProyectosEnCarpeta,
                Carpeta.carpeta_id == DetalleProyectosEnCarpeta.carpeta_id
            )
            .filter(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id)
            .all()
        )

        carpetas_eliminadas = []
        nna_liberados_set = set()  # 👈 para no duplicar

        for carpeta in carpetas:
            carpeta_id = carpeta.carpeta_id

            # Obtener NNA de la carpeta vía query (no desde la relación cargada)
            nna_ids = [
                x[0]
                for x in db.query(DetalleNNAEnCarpeta.nna_id)
                          .filter(DetalleNNAEnCarpeta.carpeta_id == carpeta_id)
                          .all()
            ]

            # 1) NNA -> disponible
            # if nna_ids:
            #     db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).update(
            #         {Nna.nna_estado: "disponible"},
            #         synchronize_session=False
            #     )
            #     nna_liberados_set.update(nna_ids)  # 👈 acumular únicos

            if nna_ids:
                nnas = db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).all()

                for nna in nnas:
                    if nna.nna_estado != "disponible":

                        estado_anterior = nna.nna_estado
                        nna.nna_estado = "disponible"

                        db.add(NnaHistorialEstado(
                            nna_id=nna.nna_id,
                            estado_anterior=estado_anterior,
                            estado_nuevo="disponible",
                            fecha_hora=datetime.now()
                        ))

                        nna_liberados_set.add(nna.nna_id)


            # 2) Borrar vínculos por BULK
            db.query(DetalleProyectosEnCarpeta).filter(
                DetalleProyectosEnCarpeta.carpeta_id == carpeta_id
            ).delete(synchronize_session=False)

            db.query(DetalleNNAEnCarpeta).filter(
                DetalleNNAEnCarpeta.carpeta_id == carpeta_id
            ).delete(synchronize_session=False)

            # 3) Borrar la carpeta por BULK
            db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).delete(synchronize_session=False)

            # 4) Auditoría
            db.add(RuaEvento(
                login=current_user["user"]["login"],
                evento_detalle=_clip_evento_detalle(
                    f"Interrupción: eliminada carpeta #{carpeta_id}. "
                    f"NNA liberados: {nna_ids if nna_ids else '[]'}. "
                    f"Proyecto afectado: #{proyecto_id}."
                ),
                evento_fecha=datetime.now()
            ))
            db.add(ObservacionesProyectos(
                observacion_a_cual_proyecto=proyecto_id,
                observacion=(
                    f"[Interrupción] Carpeta #{carpeta_id} eliminada. "
                    f"NNA puestos en 'disponible': {nna_ids if nna_ids else '[]'}."
                ),
                login_que_observo=current_user["user"]["login"],
                observacion_fecha=datetime.now()
            ))

            carpetas_eliminadas.append(carpeta_id)

        total_nna_liberados = len(nna_liberados_set)

        if proyecto.ingreso_por == "convocatoria":
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            estados_rua_a_baja = {
                "aprobado",
                "calendarizando",
                "entrevistando",
                "para_valorar",
                "viable",
            }

            proyectos_rua = (
                _query_grupo_proyectos(db, proyecto)
                .filter(
                    Proyecto.ingreso_por == "rua",
                    Proyecto.estado_general.in_(estados_rua_a_baja),
                )
                .all()
            )

            for proyecto_rua in proyectos_rua:
                estado_rua_prev = proyecto_rua.estado_general
                proyecto_rua.estado_general = "baja_por_convocatoria"

                db.add(RuaEvento(
                    login = current_user["user"]["login"],
                    evento_detalle = (
                        "Baja por interrupcion (convocatoria): "
                        f"RUA {proyecto_rua.proyecto_id} {estado_rua_prev} -> baja_por_convocatoria; "
                        f"convocatoria {proyecto_id}; {timestamp}"
                    ),
                    evento_fecha = datetime.now(),
                ))

                db.add(ObservacionesProyectos(
                    observacion_a_cual_proyecto = proyecto_rua.proyecto_id,
                    observacion = (
                        "Baja por interrupcion (convocatoria): "
                        f"RUA {proyecto_rua.proyecto_id} -> baja_por_convocatoria; "
                        f"convocatoria {proyecto_id}; {timestamp}"
                    ),
                    login_que_observo = current_user["user"]["login"],
                    observacion_fecha = datetime.now(),
                ))

                db.add(ProyectoHistorialEstado(
                    proyecto_id = proyecto_rua.proyecto_id,
                    estado_anterior = estado_rua_prev,
                    estado_nuevo = "baja_por_convocatoria",
                    fecha_hora = datetime.now(),
                ))

                _preparar_pretensos_para_nuevo_proceso(db, proyecto_rua)

            proyectos_convocatoria = (
                _query_grupo_proyectos(db, proyecto)
                .filter(
                    Proyecto.ingreso_por == "convocatoria",
                    Proyecto.proyecto_id != proyecto_id,
                    Proyecto.estado_general.notin_(FINAL_PROJECT_STATES),
                )
                .all()
            )

            for proyecto_conv in proyectos_convocatoria:
                estado_conv_prev = proyecto_conv.estado_general
                proyecto_conv.estado_general = "baja_por_convocatoria"

                db.add(RuaEvento(
                    login = current_user["user"]["login"],
                    evento_detalle = (
                        "Baja por interrupcion (convocatoria): "
                        f"conv {proyecto_conv.proyecto_id} {estado_conv_prev} -> baja_por_convocatoria; "
                        f"convocatoria {proyecto_id}; {timestamp}"
                    ),
                    evento_fecha = datetime.now(),
                ))

                db.add(ObservacionesProyectos(
                    observacion_a_cual_proyecto = proyecto_conv.proyecto_id,
                    observacion = (
                        "Baja por interrupcion (convocatoria): "
                        f"conv {proyecto_conv.proyecto_id} -> baja_por_convocatoria; "
                        f"convocatoria {proyecto_id}; {timestamp}"
                    ),
                    login_que_observo = current_user["user"]["login"],
                    observacion_fecha = datetime.now(),
                ))

                db.add(ProyectoHistorialEstado(
                    proyecto_id = proyecto_conv.proyecto_id,
                    estado_anterior = estado_conv_prev,
                    estado_nuevo = "baja_por_convocatoria",
                    fecha_hora = datetime.now(),
                ))


        _preparar_pretensos_para_nuevo_proceso(db, proyecto)
        if proyecto.ingreso_por == "convocatoria":
            _preparar_pretensos_para_nuevo_proceso_por_logins(
                db,
                [proyecto.login_1, proyecto.login_2],
            )

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                "Interrupción realizada. Proyecto pasado a 'baja_interrupcion'. "
                f"Carpetas eliminadas: {carpetas_eliminadas if carpetas_eliminadas else 'ninguna'}. "
                f"NNA liberados: {total_nna_liberados}."
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al interrumpir y eliminar carpeta(s): {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }



@proyectos_router.get("/interrupcion-info/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def obtener_info_interrupcion(
    proyecto_id: int,
    db: Session = Depends(get_db),
    ):
    """
    Devuelve un resumen de los efectos de la interrupción antes de confirmar.
    """
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
        }

    carpeta_ids = [
        x[0]
        for x in db.query(DetalleProyectosEnCarpeta.carpeta_id)
                  .filter(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id)
                  .all()
    ]

    nna_ids = []
    carpetas_detalle = []
    nna_detalle = []
    if carpeta_ids:
        nna_ids = [
            x[0]
            for x in db.query(DetalleNNAEnCarpeta.nna_id)
                      .filter(DetalleNNAEnCarpeta.carpeta_id.in_(carpeta_ids))
                      .distinct()
                      .all()
        ]

        proyectos_rows = (
            db.query(
                DetalleProyectosEnCarpeta.carpeta_id,
                Proyecto.proyecto_id,
                Proyecto.estado_general,
            )
            .join(Proyecto, DetalleProyectosEnCarpeta.proyecto_id == Proyecto.proyecto_id)
            .filter(DetalleProyectosEnCarpeta.carpeta_id.in_(carpeta_ids))
            .all()
        )

        nnas_rows = (
            db.query(
                DetalleNNAEnCarpeta.carpeta_id,
                Nna.nna_id,
                Nna.nna_nombre,
                Nna.nna_apellido,
                Nna.nna_estado,
            )
            .join(Nna, DetalleNNAEnCarpeta.nna_id == Nna.nna_id)
            .filter(DetalleNNAEnCarpeta.carpeta_id.in_(carpeta_ids))
            .all()
        )

        carpetas_map = {cid: {"carpeta_id": cid, "proyectos": [], "nnas": []} for cid in carpeta_ids}

        for carpeta_id, proyecto_id_row, estado_general in proyectos_rows:
            if carpeta_id not in carpetas_map:
                carpetas_map[carpeta_id] = {"carpeta_id": carpeta_id, "proyectos": [], "nnas": []}
            carpetas_map[carpeta_id]["proyectos"].append(
                {
                    "proyecto_id": proyecto_id_row,
                    "estado_general": estado_general,
                }
            )

        nna_map = {}
        for carpeta_id, nna_id, nna_nombre, nna_apellido, nna_estado in nnas_rows:
            if carpeta_id not in carpetas_map:
                carpetas_map[carpeta_id] = {"carpeta_id": carpeta_id, "proyectos": [], "nnas": []}
            nombre_completo = " ".join([p for p in [nna_nombre, nna_apellido] if p]).strip()
            if not nombre_completo:
                nombre_completo = f"NNA #{nna_id}"

            nna_item = {
                "nna_id": nna_id,
                "nombre_completo": nombre_completo,
                "estado_actual": nna_estado,
                "estado_nuevo": "disponible",
            }
            carpetas_map[carpeta_id]["nnas"].append(nna_item)
            if nna_id not in nna_map:
                nna_map[nna_id] = nna_item

        carpetas_detalle = list(carpetas_map.values())
        nna_detalle = list(nna_map.values())

    proyectos_rua_a_baja = []
    proyectos_convocatoria_a_baja = []

    if proyecto.ingreso_por == "convocatoria":
        estados_rua_a_baja = {
            "aprobado",
            "calendarizando",
            "entrevistando",
            "para_valorar",
            "viable",
        }

        proyectos_rua = (
            _query_grupo_proyectos(db, proyecto)
            .filter(
                Proyecto.ingreso_por == "rua",
                Proyecto.estado_general.in_(estados_rua_a_baja),
            )
            .all()
        )

        proyectos_rua_a_baja = [
            {
                "proyecto_id": p.proyecto_id,
                "estado_general": p.estado_general,
            }
            for p in proyectos_rua
        ]

        proyectos_convocatoria = (
            _query_grupo_proyectos(db, proyecto)
            .filter(
                Proyecto.ingreso_por == "convocatoria",
                Proyecto.proyecto_id != proyecto_id,
                Proyecto.estado_general.notin_(FINAL_PROJECT_STATES),
            )
            .all()
        )

        proyectos_convocatoria_a_baja = [
            {
                "proyecto_id": p.proyecto_id,
                "estado_general": p.estado_general,
            }
            for p in proyectos_convocatoria
        ]

    return {
        "success": True,
        "proyecto_id": proyecto.proyecto_id,
        "ingreso_por": proyecto.ingreso_por,
        "carpetas_ids": carpeta_ids,
        "nna_ids": nna_ids,
        "carpetas": carpetas_detalle,
        "nna_detalle": nna_detalle,
        "nna_estado_nuevo": "disponible",
        "proyectos_rua_a_baja": proyectos_rua_a_baja,
        "proyectos_convocatoria_a_baja": proyectos_convocatoria_a_baja,
    }


@proyectos_router.put("/baja-por-convocatoria/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def baja_por_convocatoria(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    ):

    """
    Marca el proyecto como 'baja_por_convocatoria'.
    Requisitos:
    - Proyecto existente.
    - proyecto.ingreso_por == 'convocatoria'

    Efectos:
    - Cambia estado_general -> 'baja_por_convocatoria'
    - Agrega evento RUA y observación (opcional)
    - Agrega historial de cambio de estado
    - No toca carpetas ni NNAs
    """
    observacion = (body.get("observacion") or "").strip()

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual",
        }

    # Validación: sólo proyectos ingresados por convocatoria
    if proyecto.ingreso_por != "convocatoria":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Este proyecto no ingresó por convocatoria. No corresponde la baja por convocatoria.",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }

    try:
        estado_anterior = proyecto.estado_general

        # Evento RUA
        db.add(
            RuaEvento(
                login=current_user["user"]["login"],
                evento_detalle=f"Se confirmó baja por convocatoria del proyecto #{proyecto_id}",
                evento_fecha=datetime.now(),
            )
        )

        # Observación (opcional)
        if observacion:
            db.add(
                ObservacionesProyectos(
                    observacion_a_cual_proyecto=proyecto_id,
                    observacion=observacion,
                    login_que_observo=current_user["user"]["login"],
                    observacion_fecha=datetime.now(),
                )
            )

        # Historial de estado
        db.add(
            ProyectoHistorialEstado(
                proyecto_id=proyecto_id,
                estado_anterior=estado_anterior,
                estado_nuevo="baja_por_convocatoria",
                fecha_hora=datetime.now(),
            )
        )

        # Estado del proyecto
        proyecto.estado_general = "baja_por_convocatoria"

        _preparar_pretensos_para_nuevo_proceso(db, proyecto)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Baja por convocatoria confirmada. Estado del proyecto actualizado.",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al confirmar la baja por convocatoria: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }




@proyectos_router.get("/unificacion-info/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def obtener_info_unificacion(
    proyecto_id: int,
    permitir_pre: bool = Query(False),
    db: Session = Depends(get_db),
    ):
    """
    Devuelve un resumen de unificación para proyectos convocatoria en vinculacion.
    """
    try:
        info = get_unificacion_info(db, proyecto_id, permitir_pre=permitir_pre)
        return {
            "success": True,
            **info,
        }
    except HTTPException as e:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": str(e.detail),
            "can_unify": False,
        }
    except Exception as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al obtener la información de unificación: {str(e)}",
            "can_unify": False,
        }


@proyectos_router.post("/unificar/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def unificar_proyecto_convocatoria(
    proyecto_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    ):
    """
    Ejecuta la unificación manual para un proyecto convocatoria ya en vinculacion.
    """
    try:
        unify_on_enter_vinculacion(
            db = db,
            proyecto_convocatoria_id = proyecto_id,
            login_usuario = current_user["user"]["login"],
        )

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Unificación realizada correctamente.",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }
    except HTTPException as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": str(e.detail),
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }
    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al unificar proyectos: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }




@proyectos_router.put("/adopcion/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def subir_sentencia_adopcion(
    proyecto_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📄 Sube la sentencia de adopción para un proyecto.

    No cambia el estado ni guarda observación.
    """
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code = 400, detail = f"Extensión de archivo no permitida: {ext}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok = True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"sentencia_adopcion_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        proyecto.doc_sentencia_adopcion = filepath
        db.commit()

        return {
            "success": True,
            "message": f"Sentencia de adopción subido como '{final_filename}'.",
            "path": filepath
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar el archivo: {str(e)}")



@proyectos_router.get("/adopcion/{proyecto_id}/descargar", response_class = FileResponse,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def descargar_sentencia_adopcion(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📄 Descarga la sentencia de adopción del proyecto identificado por `proyecto_id`.

    ⚠️ La sentencia debe haber sido cargada previamente.
    """
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    filepath = proyecto.doc_sentencia_adopcion

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code = 404, detail = "Sentencia de adopción no encontrada")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )





@proyectos_router.post("/crear-proyecto-completo", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["adoptante"]))])
def crear_proyecto_completo( 
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user) ):

    """
    📋 Crea o actualiza un proyecto adoptivo (monoparental o biparental).
    Si el proyecto del usuario está en estado 'confeccionando' (ya no se usa más) o 'actualizando',
    lo actualiza con los nuevos datos (tipo, pareja, domicilio y subregistros).
    """
    
    try:

        login_1 = current_user["user"]["login"]
        nombre_1 = current_user["user"]["nombre"]
        apellido_1 = current_user["user"]["apellido"]

        tipo = data.get("proyecto_tipo")
        login_2 = data.get("login_2")
        proyecto_barrio = data.get("proyecto_barrio")
        proyecto_calle_y_nro = data.get("proyecto_calle_y_nro")
        proyecto_depto_etc = data.get("proyecto_depto_etc")
        proyecto_localidad = data.get("proyecto_localidad")
        provincia = data.get("proyecto_provincia")

        if tipo not in ["Monoparental", "Matrimonio", "Unión convivencial"]:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Tipo de proyecto inválido.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        print( '1', login_1, login_2 )

        user_1_obj = db.query(User).filter(User.login == login_1).first()
        if not user_1_obj:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "No se encontró el usuario adoptante asociado a la sesión.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        user1_roles = db.query(UserGroup).filter(UserGroup.login == login_1).all()
        if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in user1_roles):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"El usuario no tiene el rol 'adoptante'.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        proyecto_baja_caducidad_login1 = _get_proyecto_baja_caducidad_para_login(db, login_1)
        if proyecto_baja_caducidad_login1:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Tu último proyecto fue dado de baja por caducidad. Comunicate con el equipo del RUA.",
                "tiempo_mensaje": 8,
                "next_page": "actual"
            }

        login_2_user = None
        if tipo != "Monoparental":
            if not login_2:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "Debe especificar el DNI de la pareja para proyectos en pareja.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            if login_2 == login_1:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El DNI de la pareja es igual al tuyo.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            
            login_2_user = db.query(User).filter(User.login == login_2).first()
            if not login_2_user:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El DNI de su pareja {login_2} no corresponde a un usuario en el Sistema RUA. "
                        "Es necesario que su pareja se registre primero en el sistema antes de poder continuar.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            
            login_2_roles = db.query(UserGroup).filter(UserGroup.login == login_2).all()
            if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in login_2_roles):
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El usuario de su pareja no tiene el rol 'adoptante'.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

            if login_2_user.doc_adoptante_estado != "aprobado":
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        f"El usuario con DNI {login_2} no puede unirse al proyecto porque su documentación personal aún "
                        "no fue aprobada. Primero debe completar y aprobar la documentación en el sistema. "
                        "Una vez que esté en condiciones, realizar esta solicitud nuevamente."
                    ),               
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

            proyecto_baja_caducidad_login2 = _get_proyecto_baja_caducidad_para_login(db, login_2)
            if proyecto_baja_caducidad_login2:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        f"La persona con DNI {login_2} tiene un proyecto dado de baja por caducidad. "
                        "Comuníquense con el equipo del RUA."
                    ),
                    "tiempo_mensaje": 8,
                    "next_page": "actual"
                }

        # ───────────────────────────────────────────────────────────────
        # SUBREGISTROS
        # ───────────────────────────────────────────────────────────────
        subregistros_definitivos = [
            "subreg_1", "subreg_2", "subreg_3", "subreg_4",
            "subreg_FE1", "subreg_FE2", "subreg_FE3", "subreg_FE4", "subreg_FET",
            "subreg_5A1E1", "subreg_5A1E2", "subreg_5A1E3", "subreg_5A1E4", "subreg_5A1ET",
            "subreg_5A2E1", "subreg_5A2E2", "subreg_5A2E3", "subreg_5A2E4", "subreg_5A2ET",
            "subreg_5B1E1", "subreg_5B1E2", "subreg_5B1E3", "subreg_5B1E4", "subreg_5B1ET",
            "subreg_5B2E1", "subreg_5B2E2", "subreg_5B2E3", "subreg_5B2E4", "subreg_5B2ET",
            "subreg_5B3E1", "subreg_5B3E2", "subreg_5B3E3", "subreg_5B3E4", "subreg_5B3ET",
            "subreg_F5S", "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
            "subreg_61E1", "subreg_61E2", "subreg_61E3", "subreg_61ET",
            "subreg_62E1", "subreg_62E2", "subreg_62E3", "subreg_62ET",
            "subreg_63E1", "subreg_63E2", "subreg_63E3", "subreg_63ET",
            "subreg_FQ1", "subreg_FQ2", "subreg_FQ3",
            "subreg_F6E1", "subreg_F6E2", "subreg_F6E3", "subreg_F6ET",
        ]

        subreg_data = {campo: ("Y" if data.get(campo) == "Y" else "N") for campo in subregistros_definitivos}
        if not any(valor == "Y" for valor in subreg_data.values()):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Debe seleccionar al menos un subregistro.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }               


        # ───────────────────────────────────────────────────────────────
        # BUSCAR PROYECTO EXISTENTE (CONFECCIONANDO o ACTUALIZANDO)
        # ───────────────────────────────────────────────────────────────
        proyecto_existente = (
            db.query(Proyecto)
            .filter(
                Proyecto.ingreso_por == "rua",
                Proyecto.estado_general.in_(["confeccionando", "actualizando"]),
                (Proyecto.login_1 == login_1) | (Proyecto.login_2 == login_1)
            )
            .first()
        )

        # Si el proyecto existe y quien actualiza era login_2 → invertir roles
        if proyecto_existente and proyecto_existente.login_2 == login_1:
            print("🔁 Intercambiando roles: quien actualiza era login_2, pasa a ser login_1")
            temp = proyecto_existente.login_1
            proyecto_existente.login_1 = login_1
            proyecto_existente.login_2 = temp
            db.flush()

        # Guardar valores anteriores para comparación
        login_2_anterior = proyecto_existente.login_2 if proyecto_existente else None
        tipo_anterior = proyecto_existente.proyecto_tipo if proyecto_existente else None

        if not proyecto_existente:
            if user_1_obj.doc_adoptante_ddjj_firmada != "Y":
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "Debes completar y firmar la DDJJ antes de presentar un proyecto.",
                    "tiempo_mensaje": 5,
                    "next_page": "/menu_adoptantes/alta_ddjj",
                }

            if user_1_obj.doc_adoptante_estado != "aprobado":
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "Tu documentación personal debe estar aprobada antes de crear un proyecto.",
                    "tiempo_mensaje": 5,
                    "next_page": "/menu_adoptantes/personales",
                }


        proyecto_activo_usuario = (
            db.query(Proyecto)
            .filter(
                Proyecto.ingreso_por == "rua",
                Proyecto.estado_general.in_(ESTADOS_PROYECTO_ACTIVOS),
                ((Proyecto.login_1 == login_1) | (Proyecto.login_2 == login_1)),
            )
        )

        if proyecto_existente:
            proyecto_activo_usuario = proyecto_activo_usuario.filter(Proyecto.proyecto_id != proyecto_existente.proyecto_id)

        proyecto_activo_usuario = proyecto_activo_usuario.first()

        if proyecto_activo_usuario:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": (
                    "Ya contás con un proyecto en curso (estado: "
                    f"{proyecto_activo_usuario.estado_general}). Debes finalizarlo antes de iniciar uno nuevo."
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual",
            }



        if login_2:
            proyecto_pareja_activo = (
                db.query(Proyecto)
                .filter(
                    ((Proyecto.login_1 == login_2) | (Proyecto.login_2 == login_2)),
                    Proyecto.estado_general.in_(ESTADOS_PROYECTO_ACTIVOS),
                    Proyecto.ingreso_por == "rua"
                )
                .first()
            )


            if proyecto_pareja_activo:
                if not (proyecto_existente and proyecto_pareja_activo.proyecto_id == proyecto_existente.proyecto_id):
                    return {
                        "success": False,
                        "tipo_mensaje": "naranja",
                        "mensaje": f"El usuario con DNI {login_2} ya forma parte de otro proyecto activo y no puede sumarse a este.",
                        "tiempo_mensaje": 5,
                        "next_page": "actual"
                    }


        # ───────────────────────────────────────────────────────────────
        # SI EXISTE → ACTUALIZA
        # ───────────────────────────────────────────────────────────────
        if proyecto_existente:

            # Guardar valores previos para comparación
            login_2_anterior = proyecto_existente.login_2
            tipo_anterior = proyecto_existente.proyecto_tipo

            estado_anterior = proyecto_existente.estado_general
            proyecto_existente.proyecto_tipo = tipo
            proyecto_existente.login_2 = login_2 if tipo != "Monoparental" else None
            proyecto_existente.proyecto_calle_y_nro = proyecto_calle_y_nro
            proyecto_existente.proyecto_depto_etc = proyecto_depto_etc
            proyecto_existente.proyecto_barrio = proyecto_barrio
            proyecto_existente.proyecto_localidad = proyecto_localidad
            proyecto_existente.proyecto_provincia = provincia

            for campo, valor in subreg_data.items():
                setattr(proyecto_existente, campo, valor)

            # ───────────────────────────────────────────────────────────────
            # Caso especial: proyecto biparental y manejo de invitación
            # ───────────────────────────────────────────────────────────────
            if tipo != "Monoparental" and login_2:

                # Caso 1: proyecto pasa de monoparental a biparental
                paso_a_biparental = tipo_anterior == "Monoparental" and tipo != "Monoparental"

                # Caso 2: cambiaron los DNIs respecto al proyecto anterior
                cambio_de_pareja = login_2_anterior != login_2

                # Caso 3: ya aceptada la pareja anterior
                pareja_ya_aceptada = (
                    proyecto_existente.aceptado == "Y" and
                    proyecto_existente.aceptado_code is not None
                )


                if (paso_a_biparental or cambio_de_pareja) and not pareja_ya_aceptada:
                    # 🔸 Enviar nueva invitación
                    aceptado_code = generar_codigo_para_link(16)
                    proyecto_existente.aceptado_code = aceptado_code
                    proyecto_existente.aceptado = "N"
                    proyecto_existente.estado_general = "invitacion_pendiente"


                    protocolo = get_setting_value(db, "protocolo")
                    host = get_setting_value(db, "donde_esta_alojado")
                    puerto = get_setting_value(db, "puerto_tcp")
                    endpoint = get_setting_value(db, "endpoint_aceptar_invitacion")
                    if endpoint and not endpoint.startswith("/"):
                        endpoint = "/" + endpoint

                    puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
                    host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

                    link_aceptar = f"{protocolo}://{host_con_puerto}{endpoint}?invitacion={aceptado_code}&respuesta=Y"
                    link_rechazar = f"{protocolo}://{host_con_puerto}{endpoint}?invitacion={aceptado_code}&respuesta=N"

                    cuerpo = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                          <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                  <td style="font-size: 24px; color: #007bff;">
                                      <strong>¡Hola {login_2_user.nombre}!</strong>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 17px;">
                                    <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                                    <p><strong>{nombre_1} {apellido_1}</strong> (DNI: {login_1}) te invitó a conformar un 
                                      proyecto adoptivo en conjunto.</p>
                                    <p>Te pedimos que confirmes tu participación para poder avanzar:</p>
                                  </td>
                                </tr>
                                <tr>
                                  <td align="center" style="padding: 30px 0;">
                                    <table cellpadding="0" cellspacing="0" style="text-align: center;">
                                      <tr>
                                        <td style="padding-bottom: 10px;">
                                          <a href="{link_aceptar}"
                                              style="display: inline-block; padding: 12px 20px; background-color: #28a745; color: #ffffff; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px;">
                                              ✅ Acepto la invitación
                                          </a>
                                        </td>
                                      </tr>
                                      <tr>
                                        <td>
                                          <a href="{link_rechazar}"
                                              style="display: inline-block; padding: 12px 20px; background-color: #dc3545; color: #ffffff; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px;">
                                              ❌ Rechazo la invitación
                                          </a>
                                        </td>
                                      </tr>
                                    </table>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 30px; font-size: 17px;">
                                    <p>¡Muchas gracias por querer formar parte del Registro Único de Adopciones de Córdoba!</p>
                                  </td>
                                </tr>
                              </table>
                            </td>
                          </tr>
                          </table>
                      </body>
                    </html>
                    """

                    try:
                        enviar_mail(
                            destinatario = login_2_user.mail,
                            asunto = "Invitación a conformar proyecto adoptivo en pareja",
                            cuerpo = cuerpo
                        )
                        print("✅ Correo de invitación enviado correctamente")
                    except Exception as e:
                        print(f"❌ Error al enviar correo: {e}")


                    db.add(ProyectoHistorialEstado(
                        proyecto_id = proyecto_existente.proyecto_id,
                        estado_anterior = estado_anterior,
                        estado_nuevo = "invitacion_pendiente",
                        fecha_hora = datetime.now()
                    ))

                    db.add(RuaEvento(
                        login = login_1,
                        evento_detalle = f"Se envió invitación a {login_2} para sumarse al proyecto.",
                        evento_fecha = datetime.now()
                    ))


                    db.commit()

                    return {
                        "success": True,
                        "tipo_mensaje": "verde",
                        "mensaje": "Invitación enviada correctamente.",
                        "tiempo_mensaje": 4,
                        "next_page": "actual",
                        "proyecto_id": proyecto_existente.proyecto_id  # ✅ devuelve el id del proyecto creado
                    }

                    
                else:
                    # Si sigue siendo monoparental → revisión normal
                    proyecto_existente.estado_general = "en_revision"

                    db.add(ProyectoHistorialEstado(
                        proyecto_id = proyecto_existente.proyecto_id,
                        estado_anterior = estado_anterior,
                        estado_nuevo = "en_revision",
                        fecha_hora = datetime.now()
                    ))

                    db.add(RuaEvento(
                        login = login_1,
                        evento_detalle = "Actualizó proyecto y solicitó revisión.",
                        evento_fecha = datetime.now()
                    ))

                    crear_notificacion_masiva_por_rol(
                        db = db,
                        rol = "supervisora",
                        mensaje = f"{nombre_1} {apellido_1} actualizó su proyecto y solicitó revisión.",
                        link = "/menu_supervisoras/detalleProyecto",
                        data_json = {"proyecto_id": proyecto_existente.proyecto_id},
                        tipo_mensaje = "azul"
                    )

                    db.commit()
                    
                    return {
                        "success": True,
                        "tipo_mensaje": "verde",
                        "mensaje": "Proyecto actualizado correctamente.",
                        "tiempo_mensaje": 5,
                        "next_page": "menu_adoptantes/proyecto",
                        "proyecto_id": proyecto_existente.proyecto_id
                    }

            # ─────────────── CASO MONOPARENTAL ───────────────
            else:
                proyecto_existente.estado_general = "en_revision"

                db.add(ProyectoHistorialEstado(
                    proyecto_id = proyecto_existente.proyecto_id,
                    estado_anterior = estado_anterior,
                    estado_nuevo = "en_revision",
                    fecha_hora = datetime.now()
                ))

                db.add(RuaEvento(
                    login = login_1,
                    evento_detalle = "Actualizó proyecto monoparental y solicitó revisión.",
                    evento_fecha = datetime.now()
                ))

                crear_notificacion_masiva_por_rol(
                    db = db,
                    rol = "supervisora",
                    mensaje = f"{nombre_1} {apellido_1} actualizó su proyecto monoparental y solicitó revisión.",
                    link = "/menu_supervisoras/detalleProyecto",
                    data_json = {"proyecto_id": proyecto_existente.proyecto_id},
                    tipo_mensaje = "azul"
                )

                db.commit()

                return {
                    "success": True,
                    "tipo_mensaje": "verde",
                    "mensaje": "Proyecto monoparental actualizado correctamente.",
                    "tiempo_mensaje": 4,
                    "next_page": "menu_adoptantes/proyecto",
                    "proyecto_id": proyecto_existente.proyecto_id
                }


        # ───────────────────────────────────────────────────────────────
        # SI NO EXISTE → CREA NUEVO
        # ───────────────────────────────────────────────────────────────
        aceptado_code = generar_codigo_para_link(16) if tipo != "Monoparental" else None
        aceptado = "N" if tipo != "Monoparental" else "Y"
        estado = "invitacion_pendiente" if tipo != "Monoparental" else "en_revision"

        nuevo = Proyecto(
            login_1=login_1,
            login_2=login_2 if tipo != "Monoparental" else None,
            proyecto_tipo=tipo,
            proyecto_calle_y_nro=proyecto_calle_y_nro,
            proyecto_depto_etc=proyecto_depto_etc,
            proyecto_barrio=proyecto_barrio,
            proyecto_localidad=proyecto_localidad,
            proyecto_provincia=provincia,
            ingreso_por="rua",
            aceptado=aceptado,
            aceptado_code=aceptado_code,
            operativo="Y",
            estado_general=estado,
            **subreg_data
        )

        db.add(nuevo)
        db.flush()

        if tipo == "Monoparental":
            db.add(RuaEvento(
                login=login_1,
                evento_detalle="Creó proyecto monoparental.",
                evento_fecha=datetime.now()
            ))
        else:
            # Envía invitación 
            try:
                protocolo = get_setting_value(db, "protocolo")
                host = get_setting_value(db, "donde_esta_alojado")
                puerto = get_setting_value(db, "puerto_tcp")
                endpoint = get_setting_value(db, "endpoint_aceptar_invitacion")
                if endpoint and not endpoint.startswith("/"):
                    endpoint = "/" + endpoint

                puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
                host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

                link_aceptar = f"{protocolo}://{host_con_puerto}{endpoint}?invitacion={aceptado_code}&respuesta=Y"
                link_rechazar = f"{protocolo}://{host_con_puerto}{endpoint}?invitacion={aceptado_code}&respuesta=N"

                cuerpo = f"""
                <html>
                  <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                      <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                      <tr>
                        <td align="center">
                          <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                            <tr>
                              <td style="font-size: 24px; color: #007bff;">
                                  <strong>¡Hola {login_2_user.nombre}!</strong>
                              </td>
                            </tr>

                            <tr>
                              <td style="padding-top: 20px; font-size: 17px;">
                                <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                                <p><strong>{nombre_1} {apellido_1}</strong> (DNI: {login_1}) te invitó a conformar un 
                                  proyecto adoptivo en conjunto.</p>
                                <p>Te pedimos que confirmes tu participación para poder avanzar:</p>
                              </td>
                            </tr>
                            <tr>
                              <td align="center" style="padding: 30px 0;">
                                <table cellpadding="0" cellspacing="0" style="text-align: center;">
                                  <tr>
                                    <td style="padding-bottom: 10px;">
                                      <a href="{link_aceptar}"
                                          style="display: inline-block; padding: 12px 20px; background-color: #28a745; color: #ffffff; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px;">
                                          ✅ Acepto la invitación
                                      </a>
                                    </td>
                                  </tr>
                                  <tr>
                                    <td>
                                      <a href="{link_rechazar}"
                                          style="display: inline-block; padding: 12px 20px; background-color: #dc3545; color: #ffffff; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px;">
                                          ❌ Rechazo la invitación
                                      </a>
                                    </td>
                                  </tr>
                                </table>
                              </td>
                            </tr>

                            <tr>
                              <td style="padding-top: 30px; font-size: 17px;">
                                <p>¡Muchas gracias por querer formar parte del Registro Único de Adopciones de Córdoba!</p>
                              </td>
                            </tr>
                              
                          </table>
                          </td>
                      </tr>
                      </table>
                  </body>
                </html>
                """

                try:
                    db.flush()
                    enviar_mail(destinatario=login_2_user.mail, 
                                asunto="Invitación a conformar proyecto adoptivo en pareja", 
                                cuerpo=cuerpo)
                    print("✅ Correo enviado correctamente")
                except Exception as e:
                    print(f"❌ Error al enviar correo: {e}")


                print( '10', login_2_user.mail )

                evento = RuaEvento(
                    login=login_1,
                    evento_detalle=f"Se envío invitación a {login_2} para sumarse al proyecto.",
                    evento_fecha=datetime.now()
                )
                db.add(evento)
                db.flush()

                db.commit()

                return {
                    "success": True,
                    "tipo_mensaje": "verde",
                    "mensaje": "Invitación enviada correctamente.",
                    "tiempo_mensaje": 4,
                    "next_page": "actual",
                    "proyecto_id": nuevo.proyecto_id  # ✅ devuelve el id del proyecto creado
                }

            except Exception as e:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"⚠️ Error al enviar correo de invitación: {str(e)}",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

        crear_notificacion_masiva_por_rol(
            db=db,
            rol="supervisora",
            mensaje=f"{nombre_1} {apellido_1} creó un nuevo proyecto y solicitó revisión.",
            link="/menu_supervisoras/detalleProyecto",
            data_json={"proyecto_id": nuevo.proyecto_id},
            tipo_mensaje="azul"
        )

        db.add(ProyectoHistorialEstado(
            proyecto_id=nuevo.proyecto_id,
            estado_anterior=None,
            estado_nuevo=estado,
            fecha_hora=datetime.now()
        ))

        db.commit()
        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Proyecto registrado correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "menu_adoptantes/proyecto",
            "proyecto_id": nuevo.proyecto_id
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {"success": False, "tipo_mensaje": "rojo", "mensaje": str(e)}





@proyectos_router.post("/notificacion/proyecto/mensaje", response_model=dict,
    dependencies=[ Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def notificar_proyecto_mensaje(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📢 Envía una notificación completa a los pretensos vinculados a un proyecto:
    - Notificación interna
    - Registro menajería email/whatsapp
    - Envío real según settings
    - Cambio de estado si corresponde
    """

    proyecto_id = data.get("proyecto_id")
    mensaje = data.get("mensaje")
    link = data.get("link")
    data_json = data.get("data_json") or {}
    tipo_mensaje = data.get("tipo_mensaje", "naranja")

    login_que_observa = current_user["user"]["login"]
    accion = data_json.get("accion")  # puede ser None

    if not all([proyecto_id, mensaje, link]):
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Faltan campos requeridos: proyecto_id, mensaje o link.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    try:
        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "El proyecto no existe.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # ------------- RESPONSABLES (1 o 2 pretensos) -------------
        logins_destinatarios = [proyecto.login_1]
        if proyecto.login_2:
            logins_destinatarios.append(proyecto.login_2)

        # ------------- BASE PARA CONFIG SEGÚN ACCIÓN -------------
        if accion in ["solicitar_actualizacion_doc", "aprobar_documentacion"]:
            base_setting = "doc_proyecto"
        else:
            base_setting = "notif_pretenso_proyecto"

        canales = get_notificacion_settings(db, base_setting)
        enviar_email_flag = canales.get("email", False)
        enviar_whatsapp_flag = canales.get("whatsapp", False)
        whatsapp_settings = get_whatsapp_settings(db) if enviar_whatsapp_flag else None

        # ------------- ESTADO DEL PROYECTO -------------
        nuevo_estado = None
        if accion == "solicitar_actualizacion_doc":
            nuevo_estado = "actualizando"
        elif accion == "aprobar_documentacion":
            nuevo_estado = "aprobado"

        mensaje_texto_plano = BeautifulSoup(mensaje, "lxml").get_text(separator=" ", strip=True)
        if not mensaje_texto_plano:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "El mensaje debe tener contenido con información.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }


        # ===========================================================
        #            📩 ENVIAR A CADA PRETENSO DEL PROYECTO
        # ===========================================================
        for login in logins_destinatarios:
            user = db.query(User).filter(User.login == login).first()
            if not user:
                continue

            # Crear notificación interna RUA
            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=login,
                mensaje=mensaje_texto_plano,
                link=link,
                data_json=data_json,
                tipo_mensaje=tipo_mensaje,
                enviar_por_whatsapp=False,
                login_que_notifico=login_que_observa
            )
            if not resultado["success"]:
                raise Exception(resultado["mensaje"])


            # Registrar evento
            evento_detalle = f"Notificación enviada a {login} por proyecto {proyecto_id}: {mensaje_texto_plano[:150]}"
            if nuevo_estado:
                evento_detalle += f" | Estado documentación: '{nuevo_estado}'"

            db.add(RuaEvento(
                login=login,
                evento_detalle=evento_detalle,
                evento_fecha=datetime.now()
            ))


            # ---------------------------------------------------------
            # 📧 EMAIL
            # ---------------------------------------------------------
            email_enviado = False
            
            if enviar_email_flag and user.mail:
                try:

                    cuerpo = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                  <td style="font-size: 24px; color: #007bff;">
                                      <strong>¡Hola {user.nombre}!</strong>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 17px;">
                                    <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                                    <p>Te informamos que recibiste la siguiente notificación en la plataforma:</p>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 16px;">
                                    <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                                        {mensaje}
                                    </div>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 30px; font-size: 17px;">
                                    <p>¡Saludos!</p>
                                  </td>
                                </tr>
                              </table>
                            </td>
                          </tr>
                        </table>
                      </body>
                    </html>
                    """

                    enviar_mail(
                        destinatario=user.mail,
                        asunto="Notificación del Sistema RUA",
                        cuerpo=cuerpo
                    )
                    email_enviado = True

                except Exception as e:
                    print("⚠ Error enviando email:", str(e))
                    email_enviado = False

                # Registrar en Mensajeria
                try:
                    registrar_mensaje(
                        db=db,
                        tipo="email",
                        login_emisor=login_que_observa,
                        login_destinatario=login,
                        destinatario_texto=f"{user.nombre} {user.apellido}",
                        asunto="Notificación del Sistema RUA",
                        contenido=mensaje_texto_plano,
                        estado="enviado" if email_enviado else "no_enviado"
                    )
                except Exception as e:
                    print("⚠ Error registrando mensaje email:", str(e))



            # ---------------------------------------------------------
            # 📲 WHATSAPP
            # ---------------------------------------------------------
            whatsapp_enviado = False

            if enviar_whatsapp_flag:

                if not user.celular:
                    registrar_mensaje(
                        db=db,
                        tipo="whatsapp",
                        login_emisor=login_que_observa,
                        login_destinatario=login,
                        destinatario_texto=f"{user.nombre} {user.apellido}",
                        contenido=mensaje_texto_plano,
                        estado="no_enviado",
                        data_json="No hay número de celular"
                    )
                else:
                    try:
                        numero = user.celular.replace("+","").replace(" ","").replace("-","")
                        if not numero.startswith("54"):
                            numero = "54" + numero

                        respuesta = enviar_whatsapp_rua_notificacion(
                            db=db,
                            destinatario=numero,
                            nombre=user.nombre,
                            mensaje=mensaje_texto_plano,
                            whatsapp_settings=whatsapp_settings
                        )

                        whatsapp_enviado = "messages" in respuesta
                        mensaje_externo_id = (
                            respuesta["messages"][0].get("id") if whatsapp_enviado else None
                        )

                        registrar_mensaje(
                            db=db,
                            tipo="whatsapp",
                            login_emisor=login_que_observa,
                            login_destinatario=login,
                            destinatario_texto=f"{user.nombre} {user.apellido}",
                            contenido=mensaje_texto_plano,
                            estado="enviado" if whatsapp_enviado else "error",
                            mensaje_externo_id=mensaje_externo_id,
                            data_json=respuesta
                        )

                    except Exception as e:
                        registrar_mensaje(
                            db=db,
                            tipo="whatsapp",
                            login_emisor=login_que_observa,
                            login_destinatario=login,
                            destinatario_texto=f"{user.nombre} {user.apellido}",
                            contenido=mensaje_texto_plano,
                            estado="error",
                            data_json=str(e)
                        )


        # -------- CAMBIO DE ESTADO DEL PROYECTO -------
        if nuevo_estado:
            proyecto.doc_proyecto_estado = nuevo_estado

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Notificación enviada correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error procesando notificación: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }


    
@proyectos_router.post("/proyectos/{proyecto_id}/observacion", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def registrar_observacion_proyecto(
    proyecto_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    Registra una observación interna para un proyecto, sin enviar mail ni modificar estados.
    """
    observacion = data.get("observacion")
    login_que_observo = current_user["user"]["login"]

    if not observacion or not observacion.strip():
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Debe proporcionar el campo 'observacion'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    proyecto = db.query(Proyecto).filter_by(proyecto_id=proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "El proyecto indicado no fue encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    try:
        # Registrar observación
        nueva_obs = ObservacionesProyectos(
            observacion_fecha=datetime.now(),
            observacion=observacion.strip(),
            login_que_observo=login_que_observo,
            observacion_a_cual_proyecto=proyecto_id
        )
        db.add(nueva_obs)

        # Registrar evento
        resumen = observacion.strip()
        resumen = resumen[:100] + "..." if len(resumen) > 100 else resumen

        nuevo_evento = RuaEvento(
            login=login_que_observo,
            evento_detalle=f"Observación registrada al proyecto #{proyecto_id}: {resumen}",
            evento_fecha=datetime.now()
        )
        db.add(nuevo_evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Observación del proyecto registrada correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error al registrar la observación: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@proyectos_router.get("/observacion/{proyecto_id}/listado", response_model=dict,
                      dependencies=[Depends(verify_api_key),
                                    Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def listar_observaciones_de_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
    ):

    """
    Devuelve un listado paginado de observaciones asociadas a un proyecto identificado por su `proyecto_id`.
    Incluye también los registros del historial dentro del mismo listado `observaciones`, con paginación correcta.
    """
    try:
        # Verificar existencia del proyecto
        existe_proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not existe_proyecto:
            raise HTTPException(status_code=404, detail="El proyecto indicado no existe.")

        # 🔹 1. Obtener observaciones
        observaciones = (
            db.query(ObservacionesProyectos)
            .filter(ObservacionesProyectos.observacion_a_cual_proyecto == proyecto_id)
            .all()
        )

        # 🔹 2. Obtener nombres de observadores
        logins_observadores = [o.login_que_observo for o in observaciones if o.login_que_observo]
        usuarios_observadores = (
            db.query(User.login, User.nombre, User.apellido)
            .filter(User.login.in_(logins_observadores))
            .all()
        )
        mapa_observadores = {u.login: f"{u.nombre} {u.apellido}".strip() for u in usuarios_observadores}

        # 🔹 3. Armar observaciones
        resultado = []
        for o in observaciones:
            resultado.append({
                "tipo": "observacion",
                "observacion": o.observacion,
                "fecha": o.observacion_fecha.strftime("%Y-%m-%d %H:%M") if o.observacion_fecha else None,
                "login_que_observo": o.login_que_observo,
                "nombre_completo_que_observo": mapa_observadores.get(o.login_que_observo, "")
            })

        # 🔹 4. Agregar historial
        historial = (
            db.query(ProyectoHistorialEstado)
            .filter(ProyectoHistorialEstado.proyecto_id == proyecto_id)
            .all()
        )

        for h in historial:
            comentario = (h.comentarios or "").strip()
            mismo_estado = (h.estado_anterior or "") == (h.estado_nuevo or "")
            if mismo_estado and comentario:
                descripcion = comentario
            else:
                descripcion = f"Cambio de estado: {h.estado_anterior or '—'} → {h.estado_nuevo or '—'}"

            resultado.append({
                "tipo": "evento",
                "observacion": descripcion,
                "fecha": h.fecha_hora.strftime("%Y-%m-%d %H:%M") if h.fecha_hora else None,
                "login_que_observo": None,
                "nombre_completo_que_observo": None
            })

        # 🔹 5. Ordenar por fecha descendente
        resultado = sorted(resultado, key=lambda x: x["fecha"] or "0000-00-00 00:00", reverse=True)

        # 🔹 6. Aplicar paginación manual
        total = len(resultado)
        start = (page - 1) * limit
        end = start + limit
        resultado_paginado = resultado[start:end]

        # 🔹 7. Devolver respuesta final (misma estructura)
        return {
            "page": page,
            "limit": limit,
            "total": total,
            "observaciones": resultado_paginado
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener las observaciones del proyecto: {str(e)}")



@proyectos_router.delete("/entrevista/{entrevista_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def eliminar_entrevista_agendada(
    entrevista_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    ❌ Eliminar una entrevista agendada.

    Permite a un profesional o administrador eliminar una entrevista agendada.

    Requiere:
    - Token válido
    - Rol 'administrador' o 'profesional'
    """
    try:
        entrevista = db.query(AgendaEntrevistas).filter(AgendaEntrevistas.id == entrevista_id).first()
        if not entrevista:
            return {
                "success": False, 
                "tipo_mensaje": "naranja", 
                "mensaje": "Entrevista no encontrada.", 
                "tiempo_mensaje": 5, 
                "next_page": "actual"
            }

        login_actual = current_user["user"]["login"]

        # Validar asignación si no es administrador
        roles_actuales = db.query(Group.description).join(UserGroup, Group.group_id == UserGroup.group_id)\
            .filter(UserGroup.login == login_actual).all()
        roles = [r[0] for r in roles_actuales]

        if "administrador" not in roles:
            asignado = db.query(DetalleEquipoEnProyecto).filter(
                DetalleEquipoEnProyecto.proyecto_id == entrevista.proyecto_id,
                DetalleEquipoEnProyecto.login == login_actual
            ).first()
            if not asignado:
                raise HTTPException(status_code=403, detail="No tenés permisos para eliminar esta entrevista.")

        db.delete(entrevista)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "🗑️ Entrevista eliminada correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al eliminar la entrevista: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@proyectos_router.post("/entrevista/comentario-extra/{entrevista_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional"]))])
def agregar_comentario_extra(
    entrevista_id: int,
    data: dict = Body(..., example={"comentario_extra": "Comentario posterior a la entrevista"}),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    ✏️ Agregar un comentario adicional a una entrevista ya registrada.

    Permite a profesionales o administradores registrar observaciones posteriores a la fecha de la entrevista.

    - Solo si el usuario tiene rol permitido.
    - El comentario se guarda en el campo `comentario_extra` de la entrevista.

    📤 Cuerpo esperado:
    ```json
    {
      "comentario_extra": "Observación realizada luego de la entrevista..."
    }
    """
    try:
        entrevista = db.query(AgendaEntrevistas).filter(AgendaEntrevistas.id == entrevista_id).first()

        if not entrevista:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Entrevista no encontrada.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        comentario_extra = data.get("comentario_extra", "").strip()
        if not comentario_extra:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "El comentario adicional es requerido.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }
       

        entrevista.evaluacion_comentarios = comentario_extra
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "📝 Comentario adicional guardado correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error al guardar el comentario: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@proyectos_router.post("/solicitar-actualizacion", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["supervision", "supervisora"]))])
def solicitar_actualizacion_proyecto(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user) ):
    """
    📥 Solicita una actualización de la documentación del proyecto adoptivo.

    ### JSON esperado:
    {
      "proyecto_id": 123,
      "mensaje_html": "<p>Falta completar la documentación del proyecto. Por favor, revise y actualice la información requerida.</p>"
    }
    """
    try:
        proyecto_id = data.get("proyecto_id")
        mensaje_html = data.get("mensaje_html")

        if not proyecto_id or not mensaje_html:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe especificarse el 'proyecto_id' y el 'mensaje_html'",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Proyecto no encontrado",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if proyecto.estado_general == "actualizando":
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Este proyecto ya se encuentra en proceso de actualización.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Guardar estado anterior
        estado_anterior = proyecto.estado_general

        proyecto.estado_general = "actualizando"


        # Ya no lo uso porque ya se actualiza en ProyectoHistorialEstado y dificulta la ratificación
        # proyecto.ultimo_cambio_de_estado = datetime.now().date()

        # Registrar historial
        historial = ProyectoHistorialEstado(
            proyecto_id=proyecto.proyecto_id,
            estado_anterior=estado_anterior,
            estado_nuevo="actualizando",
            fecha_hora=datetime.now()
        )
        db.add(historial)

        # Evento general
        login_supervisora = current_user["user"]["login"]
        supervisora = db.query(User).filter(User.login == login_supervisora).first()
        nombre_supervisora = f"{supervisora.nombre} {supervisora.apellido}"

        # Notificación y correo a cada usuario
        logins_destinatarios = [proyecto.login_1]
        if proyecto.login_2:
            logins_destinatarios.append(proyecto.login_2)

        for login_destinatario in logins_destinatarios:
            user = db.query(User).filter(User.login == login_destinatario).first()
            if not user:
                continue

            # Extraer texto plano del mensaje HTML para guardar en base
            mensaje_texto_plano = BeautifulSoup(mensaje_html, "lxml").get_text(separator=" ", strip=True)

            if not mensaje_texto_plano:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "El mensaje debe tener contenido con información.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=login_destinatario,
                mensaje=mensaje_texto_plano,
                link="/menu_adoptantes/portada",
                tipo_mensaje="naranja",
                enviar_por_whatsapp=False,
                login_que_notifico=login_supervisora
            )
            if not resultado["success"]:
                raise Exception(resultado["mensaje"])

            db.add(RuaEvento(
                login=login_supervisora,
                evento_detalle=(
                    f"Se solicitó actualización del proyecto a "
                    f"{proyecto.login_1}" +
                    (f" y {proyecto.login_2}" if proyecto.login_2 else "") +
                    f": {mensaje_texto_plano[:150]}"
                ),
                evento_fecha=datetime.now()
            ))


            if user.mail:
                try:

                    cuerpo = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                  <td style="font-size: 24px; color: #007bff;">
                                      <strong>¡Hola {user.nombre}!</strong>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 17px;">
                                    <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                                    <p>Te informamos que recibiste la siguiente notificación en la plataforma con
                                    una solicitud para actualizar tu proyecto adoptivo:</p>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 16px;">
                                    <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                                        {mensaje_html}
                                    </div>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 30px; font-size: 17px;">
                                    <p>¡Saludos!</p>
                                  </td>
                                </tr>
                              </table>
                            </td>
                          </tr>
                        </table>
                      </body>
                    </html>
                    """

                    enviar_mail(
                        destinatario=user.mail,
                        asunto="Solicitud de actualización del proyecto adoptivo",
                        cuerpo=cuerpo
                    )
                except Exception as e:
                    db.rollback()
                    return {
                        "success": False,
                        "tipo_mensaje": "naranja",
                        "mensaje": f"⚠️ Estado actualizado, pero error al enviar correo a {user.nombre}: {str(e)}",
                        "tiempo_mensaje": 6,
                        "next_page": "actual"
                    }

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<b>Solicitud de actualización registrada correctamente.</b><br>"
                "Se notificó al/los pretensos para que actualicen la documentación."
            ),
            "tiempo_mensaje": 6,
            "next_page": "menu_supervisoras/detalleProyecto"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al solicitar actualización del proyecto: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }





@proyectos_router.post("/aprobar-proyecto", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["supervision", "supervisora"]))])
def aprobar_proyecto(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):
     
    """
    ✅ Aprueba formalmente un proyecto y asigna número de orden si no lo tiene.
    También envía una notificación al/los pretensos informando la aprobación.

    ### JSON esperado:
    {
      "proyecto_id": 123
    }
    """
    try:
        proyecto_id = data.get("proyecto_id")

        if not proyecto_id:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe especificarse el 'proyecto_id'",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Proyecto no encontrado",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Asignar número de orden si no tiene
        if not proyecto.nro_orden_rua:
            ultimos_nros = db.query(Proyecto.nro_orden_rua)\
                .filter(Proyecto.nro_orden_rua != None)\
                .all()

            numeros_validos = [
                int(p.nro_orden_rua) for p in ultimos_nros
                if p.nro_orden_rua.isdigit() and len(p.nro_orden_rua) < 5
            ]

            nuevo_nro_orden = str(max(numeros_validos) + 1) if numeros_validos else "1"
            proyecto.nro_orden_rua = nuevo_nro_orden
            proyecto.fecha_asignacion_nro_orden = date.today()
        else:
            nuevo_nro_orden = proyecto.nro_orden_rua

        # Guardar estado anterior
        estado_anterior = proyecto.estado_general

        # Cambiar estado a aprobado
        proyecto.estado_general = "aprobado"

        # Ya no lo uso porque ya se actualiza en ProyectoHistorialEstado y dificulta la ratificación
        # proyecto.ultimo_cambio_de_estado = date.today()

        # Registrar en historial
        historial = ProyectoHistorialEstado(
            proyecto_id=proyecto.proyecto_id,
            estado_anterior=estado_anterior,
            estado_nuevo="aprobado",
            fecha_hora=datetime.now()
        )
        db.add(historial)

        # Registrar evento
        login_supervisora = current_user["user"]["login"]
        supervisora = db.query(User).filter(User.login == login_supervisora).first()
        nombre_supervisora = f"{supervisora.nombre} {supervisora.apellido}"

        evento = RuaEvento(
            login=login_supervisora,
            evento_detalle=(
                f"Se aprobó el proyecto adoptivo y se asignó el N° de orden {nuevo_nro_orden} "
                f"por parte de {nombre_supervisora}."
            ),
            evento_fecha=datetime.now()
        )
        db.add(evento)

        # 🟢 Enviar notificación al/los pretensos
        logins_destinatarios = [proyecto.login_1]
        if proyecto.login_2:
            logins_destinatarios.append(proyecto.login_2)

        mensaje_notificacion = (
            "Tu proyecto adoptivo fue revisado y aprobado. La psicóloga y trabajadora social que van a trabajar en tu caso "
            "se van a comunicar para coordinar una entrevista."
        )

        for login_destinatario in logins_destinatarios:
            user = db.query(User).filter(User.login == login_destinatario).first()
            if not user:
                continue

            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=login_destinatario,
                mensaje=mensaje_notificacion,
                link="/menu_adoptantes/portada",
                # data_json={"accion": "aprobar_documentacion"},
                tipo_mensaje="verde",
                enviar_por_whatsapp=False,
                login_que_notifico=login_supervisora,
            )
            if not resultado["success"]:
                raise Exception(resultado["mensaje"])

            # Evento individual
            db.add(RuaEvento(
                login=login_supervisora,
                evento_detalle=(
                    f"Se aprobó el proyecto adoptivo correspondiente a "
                    f"{proyecto.login_1}" +
                    (f" y {proyecto.login_2}" if proyecto.login_2 else "") +
                    f". Se asignó el N° de orden {nuevo_nro_orden} por parte de {nombre_supervisora}."
                ),
                evento_fecha=datetime.now()
            ))


            if user.mail:
                try:
                    cuerpo = f"""
                      <html>
                        <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                          <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                            <tr>
                              <td align="center">
                                <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                  <tr>
                                    <td style="font-size: 24px; color: #007bff;">
                                        <strong>¡Hola {user.nombre}!</strong>
                                    </td>
                                  </tr>
                                  <tr>
                                    <td style="padding-top: 20px; font-size: 17px;">
                                      <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                                      <p>Te informamos que recibiste la siguiente notificación en la plataforma:</p>
                                    </td>
                                  </tr>
                                  <tr>
                                    <td style="padding-top: 20px; font-size: 16px;">
                                      <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                                          
                                        Tu proyecto adoptivo fue revisado y aprobado. La psicóloga y trabajadora social que 
                                        van a trabajar en tu caso se van a comunicar para coordinar una entrevista.

                                      </div>
                                    </td>
                                  </tr>
                                  <tr>
                                    <td style="padding-top: 30px; font-size: 17px;">
                                      <p>¡Saludos!</p>
                                    </td>
                                  </tr>
                                </table>
                              </td>
                            </tr>
                          </table>
                        </body>
                      </html>
                      """
                    
                    enviar_mail(
                        destinatario=user.mail,
                        asunto="Notificación del Sistema RUA",
                        cuerpo=cuerpo
                    )

                except Exception as e:
                    db.rollback()
                    return {
                        "success": False,
                        "tipo_mensaje": "naranja",
                        "mensaje": f"⚠️ Proyecto aprobado, pero hubo un error al enviar mail a {user.nombre}: {str(e)}",
                        "tiempo_mensaje": 6,
                        "next_page": "actual"
                    }


        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                f"<b>Proyecto aprobado exitosamente.</b><br>"
                f"Número de orden asignado: <b>{nuevo_nro_orden}</b>.<br>"
                f"Se notificó al/los pretensos."
            ),
            "tiempo_mensaje": 5,
            "next_page": "menu_supervisoras/detalleProyecto"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al aprobar el proyecto: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@proyectos_router.get("/proyectos/{proyecto_id}/descargar-pdf", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def descargar_pdf_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    output_path = os.path.join(DIR_PDF_GENERADOS, f"proyecto_{proyecto_id}.pdf")
    pdf_paths: List[Tuple[str, str]] = []

    documentos_a_incluir = {
        "Informe del equipo técnico": proyecto.informe_profesionales,
        "Dictamen profesional": proyecto.doc_dictamen,
        "Sentencia de guarda": proyecto.doc_sentencia_guarda,
        "Sentencia de adopción": proyecto.doc_sentencia_adopcion,
        "Convivencia o estado civil": proyecto.doc_proyecto_convivencia_o_estado_civil,
        "Informe de interrución": proyecto.doc_interrupcion
    }

    def convertir_a_pdf_y_agregar(nombre, ruta_original):
        if not ruta_original or not os.path.exists(ruta_original):
            return
        ext = os.path.splitext(ruta_original)[1].lower()
        out_pdf = os.path.join(DIR_PDF_GENERADOS, f"{nombre}_{os.path.basename(ruta_original)}.pdf")

        if ext == ".pdf":
            shutil.copy(ruta_original, out_pdf)
        # elif ext in [".jpg", ".jpeg", ".png"]:
        #     Image.open(ruta_original).convert("RGB").save(out_pdf)
        elif ext in [".jpg", ".jpeg", ".png"]:
          img = Image.open(ruta_original).convert("RGB")

          # Tamaño A4 en puntos (1 punto = 1/72 pulgadas)
          a4_width, a4_height = 595, 842

          # Crear nuevo lienzo blanco A4
          new_img = Image.new("RGB", (a4_width, a4_height), (255, 255, 255))

          # Redimensionar imagen manteniendo proporción para que quepa en A4
          img.thumbnail((a4_width, a4_height))

          # Calcular posición para centrarla
          x = (a4_width - img.width) // 2
          y = (a4_height - img.height) // 2

          new_img.paste(img, (x, y))

          # Guardar como PDF en tamaño A4
          new_img.save(out_pdf, "PDF", resolution=100.0)

        elif ext in [".doc", ".docx"]:
            subprocess.run([
                "libreoffice", "--headless", "--convert-to", "pdf", "--outdir", DIR_PDF_GENERADOS, ruta_original
            ], check=True)
            out_pdf = os.path.join(DIR_PDF_GENERADOS, os.path.splitext(os.path.basename(ruta_original))[0] + ".pdf")
        else:
            return

        if os.path.exists(out_pdf):
            pdf_paths.append((nombre, out_pdf))

    def agregar_documentos_personales(user, destino: List[Tuple[str, str]]):
        campos = [
            "doc_adoptante_salud", "doc_adoptante_dni_frente", "doc_adoptante_dni_dorso", "doc_adoptante_domicilio",
            "doc_adoptante_deudores_alimentarios", "doc_adoptante_antecedentes", "doc_adoptante_migraciones"
        ]
        for campo in campos:
            ruta = getattr(user, campo, None)
            if ruta and os.path.exists(ruta):
                ext = os.path.splitext(ruta)[1].lower()
                out_pdf = os.path.join(DIR_PDF_GENERADOS, f"{campo}_{user.login}.pdf")
                if ext == ".pdf":
                    shutil.copy(ruta, out_pdf)
                elif ext in [".jpg", ".jpeg", ".png"]:
                    Image.open(ruta).convert("RGB").save(out_pdf)
                elif ext in [".doc", ".docx"]:
                    subprocess.run([
                        "libreoffice", "--headless", "--convert-to", "pdf", "--outdir", DIR_PDF_GENERADOS, ruta
                    ], check=True)
                    out_pdf = os.path.join(DIR_PDF_GENERADOS, os.path.splitext(os.path.basename(ruta))[0] + ".pdf")
                else:
                    continue
                if os.path.exists(out_pdf):
                    destino.append((f"{campo.replace('doc_adoptante_', '').replace('_', ' ').capitalize()} de {user.nombre} {user.apellido}", out_pdf))

    for nombre, ruta in documentos_a_incluir.items():
        convertir_a_pdf_y_agregar(nombre, ruta)

    pretenso_1 = proyecto.usuario_1
    pretenso_2 = proyecto.usuario_2 if proyecto.login_2 else None

    if pretenso_1:
        agregar_documentos_personales(pretenso_1, pdf_paths)
    if pretenso_2:
        agregar_documentos_personales(pretenso_2, pdf_paths)

    merged = fitz.open()

    portada = merged.new_page(width=595, height=842)
    portada.insert_textbox(fitz.Rect(0, 50, 595, 100), "SERVICIO DE GUARDA Y ADOPCIÓN", fontname="helv", fontsize=16, align=1, color=(0.1, 0.1, 0.3))
    portada.insert_textbox(fitz.Rect(0, 75, 595, 120), "REGISTRO ÚNICO DE ADOPCIONES Y EQUIPO TÉCNICO", fontname="helv", fontsize=13, align=1, color=(0.2, 0.2, 0.4))
    portada.insert_textbox(fitz.Rect(0, 105, 595, 135), "DOCUMENTACIÓN DEL PROYECTO ADOPTIVO", fontname="helv", fontsize=11, align=1, color=(0.4, 0.4, 0.4))

    domicilio = proyecto.proyecto_calle_y_nro or ""
    if proyecto.proyecto_depto_etc:
        domicilio += f", {proyecto.proyecto_depto_etc}"
    if proyecto.proyecto_barrio:
        domicilio += f", {proyecto.proyecto_barrio}"
    if proyecto.proyecto_localidad:
        domicilio += f", {proyecto.proyecto_localidad}"

    datos = []
    if proyecto.nro_orden_rua:
        datos.append(f"N° de orden RUA: {proyecto.nro_orden_rua}")
    if proyecto.proyecto_tipo:
        datos.append(f"Tipo de proyecto: {proyecto.proyecto_tipo}")
    if pretenso_1:
        datos.append(f"Pretenso 1: {pretenso_1.nombre} {pretenso_1.apellido} - DNI: {pretenso_1.login}")
    if pretenso_2:
        datos.append(f"Pretenso 2: {pretenso_2.nombre} {pretenso_2.apellido} - DNI: {pretenso_2.login}")
    if domicilio.strip():
        datos.append(f"Domicilio: {domicilio}")
    if proyecto.proyecto_provincia:
        datos.append(f"Provincia: {proyecto.proyecto_provincia}")
    if proyecto.estado_general:
        datos.append(f"Estado actual: {proyecto.estado_general}")

    
    fondo = fitz.Rect(50, 160, 545, 380)
    portada.draw_rect(fondo, fill=(0.88, 0.93, 0.98))
    y = 170
    for linea in datos:
        portada.insert_textbox(fitz.Rect(60, y, 530, y + 25), linea, fontsize=13, fontname="helv", align=0, color=(0.1, 0.1, 0.1))
        y += 28

    portada.draw_line(p1=(60, y + 10), p2=(portada.rect.width - 60, y + 10), color=(0.5, 0.5, 0.5), width=0.6)

    for titulo, path in pdf_paths:
        page = merged.new_page(width=595, height=842)
        page.insert_textbox(fitz.Rect(0, 280, 595, 320), titulo, fontsize=20, fontname="helv", align=1)
        icono = "/app/recursos/imagenes/flecha_hacia_abajo.png"
        if os.path.exists(icono):
            page.insert_image(fitz.Rect(250, 340, 345, 440), filename=icono)

        with fitz.open(path) as doc:
            merged.insert_pdf(doc)

    merged.save(output_path)

    nombre_archivo = f"{pretenso_1.nombre}_{pretenso_1.apellido}".replace(" ", "_")
    if pretenso_2:
        nombre_archivo += f"_{pretenso_2.nombre}_{pretenso_2.apellido}".replace(" ", "_")

    return FileResponse(
        path=output_path,
        filename=f"proyecto_{nombre_archivo}.pdf",
        media_type="application/pdf"
    )




@proyectos_router.put("/informe-vinculacion/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def subir_informe_vinculacion(
    proyecto_id: int,
    file: UploadFile = File(...),
    observacion: str = Form(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):
    
    """
    📄 Sube el informe de vinculación del proyecto, con una observación interna.
    """
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: {ext}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"informe_vinculacion_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        proyecto.doc_informe_vinculacion = filepath

        # Registrar observación
        if observacion.strip():
            db.add(ObservacionesProyectos(
                observacion=observacion.strip(),
                observacion_fecha=datetime.now(),
                login_que_observo=current_user["user"]["login"],
                observacion_a_cual_proyecto=proyecto_id
            ))

        # Evento
        db.add(RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se subió informe de vinculación para el proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        ))

        db.commit()

        return {
            "success": True,
            "message": f"Informe de vinculación subido como '{final_filename}'.",
            "tipo_mensaje": "verde",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al guardar: {str(e)}")




@proyectos_router.get("/informe-vinculacion/{proyecto_id}/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def descargar_informe_vinculacion(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📄 Descarga el informe de vinculación asociado al proyecto.
    """
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    filepath = proyecto.doc_informe_vinculacion

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    return FileResponse(
        path=filepath,
        filename=os.path.basename(filepath),
        media_type="application/octet-stream"
    )



@proyectos_router.put("/informe-seguimiento-guarda/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def subir_informe_seguimiento_guarda(
    proyecto_id: int,
    file: UploadFile = File(...),
    observacion: str = Form(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    🛡️ Sube el informe de seguimiento de guarda del proyecto, con una observación interna.
    """
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: {ext}")

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    proyecto_dir = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    os.makedirs(proyecto_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"informe_seguimiento_guarda_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        proyecto.doc_informe_seguimiento_guarda = filepath

        # Registrar observación
        if observacion.strip():
            db.add(ObservacionesProyectos(
                observacion=observacion.strip(),
                observacion_fecha=datetime.now(),
                login_que_observo=current_user["user"]["login"],
                observacion_a_cual_proyecto=proyecto_id
            ))

        # Evento
        db.add(RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se subió informe de seguimiento de guarda para el proyecto #{proyecto_id}",
            evento_fecha=datetime.now()
        ))

        db.commit()

        return {
            "success": True,
            "message": f"Informe de seguimiento de guarda subido como '{final_filename}'.",
            "tipo_mensaje": "verde",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al guardar: {str(e)}")




@proyectos_router.get("/informe-seguimiento-guarda/{proyecto_id}/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def descargar_informe_seguimiento_guarda(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):

    """
    🛡️ Descarga el informe de seguimiento de guarda asociado al proyecto.
    """
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    filepath = proyecto.doc_informe_seguimiento_guarda

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    return FileResponse(
        path=filepath,
        filename=os.path.basename(filepath),
        media_type="application/octet-stream"
    )



@proyectos_router.put("/modificar/{proyecto_id}/actualizar-nro-orden",
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def actualizar_nro_orden(
    proyecto_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    ):
    
    nuevo_nro = data.get("nuevo_nro_orden", "").strip()

    # Validar que no esté vacío
    if not nuevo_nro:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El nuevo nro. de orden está vacío.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Validar que sea solo números (sin espacios, letras, guiones, etc.)
    if not nuevo_nro.isdigit():
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El nuevo nro. de orden debe contener solo números (sin espacios ni letras).",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Verificar que no exista otro proyecto con ese número de orden
    nro_duplicado = (
        db.query(Proyecto)
        .filter(Proyecto.nro_orden_rua == nuevo_nro, Proyecto.proyecto_id != proyecto_id)
        .first()
    )
    if nro_duplicado:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Ya existe otro proyecto con ese número de orden",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    nro_anterior = proyecto.nro_orden_rua or "—"
    proyecto.nro_orden_rua = nuevo_nro

    # Registrar observación del cambio
    observacion_texto = (
        f"🔄 Se modificó el número de orden RUA.\n"
        f"Anterior: {nro_anterior}\n"
        f"Nuevo: {nuevo_nro}"
    )

    observacion = ObservacionesProyectos(
        observacion=observacion_texto,
        observacion_fecha=datetime.now(),
        login_que_observo=current_user["user"]["login"],        
        observacion_a_cual_proyecto=proyecto_id
    )
    db.add(observacion)

    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": f"✅ Número de orden actualizado correctamente a '{nuevo_nro}'.",
        "tiempo_mensaje": 5,
        "next_page": "actual"
    }




@proyectos_router.get("/nnas/por-proyecto/{proyecto_id}", dependencies=[
    Depends(verify_api_key),
    Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def get_nnas_por_proyecto(proyecto_id: int, db: Session = Depends(get_db)):
    """
    🔍 Devuelve los NNAs vinculados a las carpetas en las que participa el proyecto indicado.
    """
    carpetas_ids = db.query(DetalleProyectosEnCarpeta.carpeta_id)\
        .filter(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id)\
        .distinct().all()

    if not carpetas_ids:
        return []

    carpetas_ids = [c[0] for c in carpetas_ids]

    detalle_nna = (
        db.query(DetalleNNAEnCarpeta)
        .join(Nna)
        .filter(DetalleNNAEnCarpeta.carpeta_id.in_(carpetas_ids))
        .all()
    )

    nna_vistos = {}
    for detalle in detalle_nna:
        nna = detalle.nna
        if nna.nna_id in nna_vistos:
            continue

        # Cálculo de edad en años y texto legible
        edad = None
        edad_legible = None
        if nna.nna_fecha_nacimiento:
            edad = (
                date.today().year - nna.nna_fecha_nacimiento.year
                - ((date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day))
            )
            edad_legible = edad_como_texto(nna.nna_fecha_nacimiento)

        nna_vistos[nna.nna_id] = {
            "nna_id": nna.nna_id,
            "nna_nombre": nna.nna_nombre,
            "nna_apellido": nna.nna_apellido,
            "nna_dni": nna.nna_dni,
            "nna_fecha_nacimiento": str(nna.nna_fecha_nacimiento) if nna.nna_fecha_nacimiento else None,
            "nna_edad": edad_legible,
            "nna_edad_num": edad,
            "nna_localidad": nna.nna_localidad,
            "nna_provincia": nna.nna_provincia,
            "nna_subregistro_salud": nna.nna_subregistro_salud,
            "nna_en_convocatoria": nna.nna_en_convocatoria,
            "nna_disponible": getattr(nna, "nna_disponible", None),
            "nna_ficha": nna.nna_ficha,
            "nna_sentencia": nna.nna_sentencia,
            "nna_archivado": nna.nna_archivado,
            "nna_5A": nna.nna_5A,
            "nna_5B": nna.nna_5B
        }

    return list(nna_vistos.values())




@proyectos_router.post("/estado-por-atajo/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def actualizar_estado_proyecto(
    proyecto_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    Cambia `estado_general` de un proyecto y deja registro
    en historial + observaciones.
    """
    nuevo_estado   = data.get("nuevo_estado")
    observacion    = (data.get("observacion") or "").strip()
    subregistros   = data.get("subregistros")      # list | None
    fecha_suspenso = data.get("fecha_suspenso")    # "YYYY-MM-DD" | None

    estados_validos = {"aprobado", "entrevistando", "viable", "en_suspenso",
                       "no_viable", "baja_anulacion", "baja_caducidad", "baja_desistimiento"}
    
    if nuevo_estado not in estados_validos :
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "El nuevo estado no es correcto para este proyecto. "
                "Analice el estado actual del proyecto, si forma parte de una carpeta."
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    estado_anterior = proyecto.estado_general

    # -- helper: transiciones entre estados de "baja"
    BAJA_STATES = {"baja_anulacion", "baja_caducidad", "baja_desistimiento"}
    both_baja = (estado_anterior in BAJA_STATES) and (nuevo_estado in BAJA_STATES) and (estado_anterior != nuevo_estado)


    # ───────── validaciones especiales para BAJA POR DESISTIMIENTO ─────────
    if nuevo_estado == "baja_desistimiento" and not both_baja:
        incumplimientos = []

        # a) Estado actual permitido
        permitidos = {"aprobado", "viable", "calendarizando", "entrevistando"}
        if estado_anterior not in permitidos:
            incumplimientos.append(
                'el estado actual no permite "baja por desistimiento" '
                f'(debe ser uno de: {", ".join(sorted(permitidos))})'
            )

        # b) No debe estar en ninguna carpeta
        en_carpeta = db.query(
            exists().where(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id)
        ).scalar()
        if en_carpeta:
            incumplimientos.append("el proyecto está asociado a una carpeta")

        # c) No debe estar en ninguna postulación
        en_postulacion = db.query(
            exists().where(DetalleProyectoPostulacion.proyecto_id == proyecto_id)
        ).scalar()
        if en_postulacion:
            incumplimientos.append("el proyecto está asociado a una postulación")

        # d) Debe haber ingresado por RUA
        if proyecto.ingreso_por != "rua":
            incumplimientos.append('el proyecto no ingresó por "rua"')

        if incumplimientos:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "No se puede dar de baja por desistimiento porque: " + "; ".join(incumplimientos) + ".",
                "tiempo_mensaje": 7,
                "next_page": "actual",
            }


    # ───────── validaciones especiales para BAJA POR CADUCIDAD ─────────
    if nuevo_estado == "baja_caducidad" and not both_baja: 

        incumplimientos = []

        # a) Debe estar en en_suspenso
        if estado_anterior != "en_suspenso":
            incumplimientos.append('el proyecto no está en estado "en_suspenso"')

        # b) No debe estar en ninguna carpeta
        en_carpeta = db.query(
            exists().where(DetalleProyectosEnCarpeta.proyecto_id == proyecto_id)
        ).scalar()
            
        if en_carpeta:
            incumplimientos.append("el proyecto está asociado a una carpeta")

        # c) No debe estar en ninguna postulación
        en_postulacion = db.query(
            exists().where(DetalleProyectoPostulacion.proyecto_id == proyecto_id)
        ).scalar()
            
        if en_postulacion:
            incumplimientos.append("el proyecto está asociado a una postulación")

        # d) Debe haber ingresado por RUA
        if proyecto.ingreso_por != "rua":
            incumplimientos.append('el proyecto no ingresó por "rua"')

        if incumplimientos:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": (
                    "No se puede dar de baja por caducidad porque: "
                    + "; ".join(incumplimientos) + "."
                ),
                "tiempo_mensaje": 7,
                "next_page": "actual"
            }
        

    # ───────── validación: si está en carpeta, no permitir ─────────
    proyecto_en_carpeta = db.query(DetalleProyectosEnCarpeta).filter_by(proyecto_id=proyecto_id).first()
    if proyecto_en_carpeta:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "Este proyecto se encuentra actualmente asociado a una carpeta. "
                "Los cambios de estado para este proyecto deben realizarse desde la sección Carpetas."
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    

    # ───────── nueva validación ─────────
    if estado_anterior == nuevo_estado:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El estado seleccionado es igual al estado actual. No se realizaron cambios.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # ───────── observación obligatoria para ciertos estados ─────────
    if nuevo_estado in {"no_viable", "en_suspenso", "baja_anulacion", 
                        "baja_caducidad", "baja_desistimiento"} and not observacion:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"Debe ingresar una observación para el estado '{nuevo_estado}'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # ───────── lógica específica por estado ──────────────────────────
    if nuevo_estado == "viable":
        # Esperamos una lista de subregistros válidos en subregistros[]
        if not subregistros or not isinstance(subregistros, list):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Se requiere al menos un subregistro.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
        # Limpio todos a "N" y marco los elegidos a "Y"
        pref = "subreg_"          # ej. subreg_1, subreg_FE1…
        for col in [c.name for c in Proyecto.__table__.columns
                    if c.name.startswith(pref)]:
            setattr(proyecto, col, "Y" if col.replace(pref, "") in subregistros else "N")

    elif nuevo_estado == "en_suspenso":
        if not fecha_suspenso:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Debe indicar la fecha de suspenso.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
        proyecto.fecha_suspenso = fecha_suspenso      # asegúrate de tener la col.

    # (otros estados no necesitan datos extra)

    proyecto.estado_general         = nuevo_estado

    if nuevo_estado in FINAL_PROJECT_STATES:
        _preparar_pretensos_para_nuevo_proceso(db, proyecto)

    # Este cambio de estado ya no lo registraré porque ya lo hace en ProyectoHistorialEstado
    # y dificulta el seguimiento de la ratificación. Más que nada se usa para para los proyectos de RUA v1
    # proyecto.ultimo_cambio_de_estado = datetime.now().date()

    # ───────── historial de estados ─────────
    db.add(ProyectoHistorialEstado(
        proyecto_id     = proyecto_id,
        estado_anterior = estado_anterior,
        estado_nuevo    = nuevo_estado,
        fecha_hora      = datetime.now()
    ))

    # ───────── observación interna ──────────
    if observacion:
        login_obs = current_user["user"]["login"]
        db.add(ObservacionesProyectos(
            observacion_fecha           = datetime.now(),
            observacion                 = observacion,
            login_que_observo           = login_obs,
            observacion_a_cual_proyecto = proyecto_id
        ))

    db.commit()

    return {
        "success"      : True,
        "tipo_mensaje" : "verde",
        "mensaje"      : "Estado actualizado correctamente.",
        "tiempo_mensaje": 4,
        "next_page"    : "actual"
    }
    


@proyectos_router.put("/update_domicilio_de_proyecto/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def update_domicilio_de_proyecto(
    proyecto_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db)
    ):

    """
    ✏️ Actualiza el domicilio del proyecto y sincroniza el domicilio real en la DDJJ
    y en el usuario correspondiente (o en ambos usuarios si es biparental).
    """
    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"Proyecto con ID {proyecto_id} no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Campos de domicilio a actualizar
    campos_domicilio = {
        "proyecto_calle_y_nro": "calle_y_nro",
        "proyecto_depto_etc": "depto_etc",
        "proyecto_barrio": "barrio",
        "proyecto_localidad": "localidad",
        "proyecto_provincia": "provincia",
    }

    # ✅ 1. Actualiza en proyecto
    for campo in campos_domicilio.keys():
        if campo in data:
            setattr(proyecto, campo, data[campo])

    # ✅ 2. Función interna para actualizar DDJJ y User
    def actualizar_domicilio_pretenso(login):
        if not login:
            return

        # Update User
        user = db.query(User).filter(User.login == login).first()
        if user:
            for campo_proy, campo_user in campos_domicilio.items():
                if campo_proy in data:
                    setattr(user, campo_user, data[campo_proy])

        # Update DDJJ
        ddjj = db.query(DDJJ).filter(DDJJ.login == login).first()
        if ddjj:
            if "proyecto_calle_y_nro" in data:
                ddjj.ddjj_calle = data["proyecto_calle_y_nro"]
            if "proyecto_depto_etc" in data:
                ddjj.ddjj_depto = data["proyecto_depto_etc"]
            if "proyecto_barrio" in data:
                ddjj.ddjj_barrio = data["proyecto_barrio"]
            if "proyecto_localidad" in data:
                ddjj.ddjj_localidad = data["proyecto_localidad"]
            if "proyecto_provincia" in data:
                ddjj.ddjj_provincia = data["proyecto_provincia"]

    # ✅ 3. Actualiza login_1 y login_2 si existen
    actualizar_domicilio_pretenso(proyecto.login_1)
    actualizar_domicilio_pretenso(proyecto.login_2)

    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "Domicilio de proyecto y de pretensos actualizado correctamente.",
        "tiempo_mensaje": 4,
        "next_page": "actual"
    }





@proyectos_router.get("/ratificar/proyectos_que_deben_ratificar_al_dia_del_parametro", response_model=list,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def get_proyectos_para_ratificar_al_dia_del_parametro(
    request: Request,
    fecha_parametro: Optional[str] = Query(None, description="Fecha en formato YYYY-MM-DD para calcular ratificación"),
    db: Session = Depends(get_db)
    ):

    """
    Devuelve los proyectos que deben ratificar a la fecha indicada o al día de hoy si no se pasa parámetro.
    Considera proyectos en estado 'viable' o 'en_carpeta'.
    Ignora transiciones hacia/desde 'en_carpeta'.
    Incluye fechas NULL→viable, viables repetidos, vinculación/guardas y ratificaciones de pretensos.
    No considera 'ultimo_cambio_de_estado' si es posterior a 2025-06-01.
    Devuelve fecha de aviso (ratificación) y fecha exacta al año.
    """
    try:
        # 1️⃣ Fecha límite
        fecha_limite = (
            datetime.strptime(fecha_parametro, "%Y-%m-%d").date()
            if fecha_parametro else date.today()
        )
        print(f"\n🗓️ Fecha límite de cálculo: {fecha_limite}")

        # 2️⃣ Proyectos candidatos
        proyectos = db.query(Proyecto).filter(
            Proyecto.estado_general.in_(["viable", "en_carpeta"]),
            Proyecto.ingreso_por == "rua"
        ).all()

        resultado = []

        for proyecto in proyectos:
            print(f"\n🔍 Proyecto ID {proyecto.proyecto_id} | Estado actual: {proyecto.estado_general}")
            print(f"   • Pretensos: {proyecto.login_1 or '-'}" + (f" y {proyecto.login_2}" if proyecto.login_2 else ""))
            info = _calcular_info_ratificacion_proyecto(proyecto, db, logger=print)
            fecha_ratificacion = info["fecha_ratificacion"]
            fecha_ratificacion_exacta = info["fecha_ratificacion_exacta"]

            # 6️⃣ Comparar con la fecha límite
            if fecha_ratificacion and fecha_ratificacion.date() <= fecha_limite:
                print(f"✅ Debe ratificar (límite {fecha_limite}, aviso {fecha_ratificacion.date()})")
                resultado.append({
                    "proyecto_id": proyecto.proyecto_id,
                    "login_1": proyecto.login_1,
                    "login_2": proyecto.login_2,
                    "estado_general": proyecto.estado_general,
                    "fecha_cambio_final": info["fecha_cambio_final"].strftime("%Y-%m-%d") if info["fecha_cambio_final"] else None,
                    "fecha_ratificacion": fecha_ratificacion.strftime("%Y-%m-%d") if fecha_ratificacion else None,
                    "fecha_ratificacion_exacta": fecha_ratificacion_exacta.strftime("%Y-%m-%d") if fecha_ratificacion_exacta else None,
                    "fecha_ultima_ratificacion": info["fecha_ultima_ratificacion"].strftime("%Y-%m-%d") if info["fecha_ultima_ratificacion"] else None
                })
            else:
                print("❎ No debe ratificar aún.")

        print(f"\n📊 Total de proyectos que deben ratificar: {len(resultado)}")
        return resultado

    except SQLAlchemyError as e:
        print(f"💥 Error al recuperar proyectos para ratificar: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al recuperar proyectos para ratificar: {str(e)}")



@proyectos_router.post("/ratificar/notificar-siguiente-proyecto", 
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def notificar_siguiente_proyecto_para_ratificar(
    request: Request,
    fecha_parametro: Optional[str] = Query(None, description="Fecha en formato YYYY-MM-DD para calcular ratificación"),
    db: Session = Depends(get_db)
    ):

    """
    Selecciona el siguiente proyecto pendiente de ratificación según la misma lógica
    que get_proyectos_para_ratificar_al_dia_del_parametro, evitando enviar mails duplicados
    (últimos 7 días) y aplicando el flujo de notificación y caducidad.
    """
    try:
        hoy = datetime.now()
        hace_7 = hoy - timedelta(days=7)

        # 1️⃣ Fecha límite
        fecha_limite = (
            datetime.strptime(fecha_parametro, "%Y-%m-%d").date()
            if fecha_parametro else date.today()
        )
        # 2️⃣ Subconsulta: proyectos con notificación en los últimos 7 días
        subq_notificados = (
            db.query(UsuarioNotificadoRatificacion.proyecto_id)
            .filter(
                func.greatest(
                    func.coalesce(UsuarioNotificadoRatificacion.mail_enviado_1, datetime.min),
                    func.coalesce(UsuarioNotificadoRatificacion.mail_enviado_2, datetime.min),
                    func.coalesce(UsuarioNotificadoRatificacion.mail_enviado_3, datetime.min),
                    func.coalesce(UsuarioNotificadoRatificacion.mail_enviado_4, datetime.min),
                ) > hace_7
            )
        )

        # 3️⃣ Proyectos candidatos (mismos criterios del GET)
        proyectos = db.query(Proyecto).filter(
            Proyecto.estado_general.in_(["viable", "en_carpeta"]),
            Proyecto.ingreso_por == "rua",
            ~Proyecto.proyecto_id.in_(subq_notificados)
        ).all()

        candidatos = []

        for proyecto in proyectos:
            info = _calcular_info_ratificacion_proyecto(proyecto, db)
            fecha_ratificacion = info["fecha_ratificacion"]

            if fecha_ratificacion and fecha_ratificacion.date() <= fecha_limite:
                candidatos.append((proyecto, info))

        # 4️⃣ Seleccionar el más antiguo
        if not candidatos:
            raise HTTPException(status_code=404, detail="No hay proyectos pendientes para notificar.")

        candidatos.sort(key=lambda x: x[1]["fecha_ratificacion"])
        proyecto_obj, info_seleccionada = candidatos[0]
        fecha_ratif = info_seleccionada["fecha_ratificacion"]

        # 5️⃣ Enviar notificación (idéntico a tu flujo actual)
        logins = [proyecto_obj.login_1, proyecto_obj.login_2] if proyecto_obj.login_2 else [proyecto_obj.login_1]
        enviados = []
        baja_caducidad_triggered = False
        baja_contexto = {"proyecto_id": proyecto_obj.proyecto_id, "logins": [l for l in logins if l], "cuando": None}

        for login in logins:
            if not login:
                continue

            usuario = db.query(User).filter(User.login == login).first()
            if not usuario or not usuario.mail:
                continue

            notificacion = db.query(UsuarioNotificadoRatificacion).filter_by(
                proyecto_id=proyecto_obj.proyecto_id,
                login=login
            ).first()

            hoy = datetime.now()
            nro_envio = 1

            if not notificacion:
                notificacion = UsuarioNotificadoRatificacion(
                    proyecto_id=proyecto_obj.proyecto_id,
                    login=login,
                    mail_enviado_1=hoy
                )
                db.add(notificacion)

            elif notificacion.mail_enviado_2 is None:
                notificacion.mail_enviado_2 = hoy
                nro_envio = 2
            elif notificacion.mail_enviado_3 is None:
                notificacion.mail_enviado_3 = hoy
                nro_envio = 3
            elif notificacion.mail_enviado_4 is None:
                notificacion.mail_enviado_4 = hoy
                nro_envio = 4
            else:
                # Si pasaron más de 7 días del cuarto aviso → baja_caducidad
                if notificacion.mail_enviado_4 and (hoy - notificacion.mail_enviado_4) > timedelta(days=7):
                    proyecto_obj.estado_general = "baja_caducidad"
                    # Marcamos que debemos enviar el mail interno (una vez)
                    if not baja_caducidad_triggered:
                        baja_caducidad_triggered = True
                        baja_contexto["cuando"] = hoy
                    _preparar_pretensos_para_nuevo_proceso(db, proyecto_obj)
                    db.commit()
                    enviados.append({"login": login, "mensaje": "Proyecto pasado a baja_caducidad"})
                continue

            # === envío del correo ===
            primer_nombre = usuario.nombre.split()[0].capitalize() if usuario.nombre else ""
            cuerpo_html = f"""
            <html>
              <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                  <tr>
                    <td align="center">
                      <table cellpadding="0" cellspacing="0" width="600"
                        style="background-color: #ffffff; border-radius: 10px; padding: 30px;
                              font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40;
                              box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                        <tr>
                          <td style="padding-top: 20px; font-size: 17px;">
                            <p>¡Hola, <strong>{primer_nombre}</strong>! Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                            <p>Te informamos que se cumple un año de tu inscripción en el Registro Único de Adopciones 
                              de Córdoba. Por indicaciones del artículo 14 de la ley 25.854 necesitamos que
                              confirmes tu voluntad de continuar inscripta/o ingresando al Sistema RUA y haciendo clic 
                              en el botón de Ratificación que estará disponible durante los próximos 30 días dentro del Sistema RUA.
                            </p>

                            <p><em>
                              Transcurrido ese plazo sin que nos confirmes tu continuidad, el sistema te excluye
                              automáticamente del Registro y, para volver a formar parte, tendrás que iniciar el trámite
                              nuevamente.
                            </em></p>
                          </td>
                        </tr>

                        <tr>
                          <td align="center" style="padding: 30px 0;">
                            <a href="https://rua.justiciacordoba.gob.ar/login/" target="_blank"
                              style="display: inline-block; padding: 12px 24px; background-color: #007bff;
                                      color: #ffffff; border-radius: 8px; text-decoration: none;
                                      font-weight: bold; font-size: 16px;">
                              Ir al sistema RUA
                            </a>
                          </td>
                        </tr>
              
                        <tr>
                          <td style="font-size: 17px; padding-top: 20px;">
                            ¡Muchas gracias por seguir en el Registro Único de Adopciones de Córdoba!
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                </table>
              </body>
            </html>
            """     

            enviar_mail(destinatario=usuario.mail, asunto="Ratificación de inscripción", cuerpo=cuerpo_html)
            evento = RuaEvento(
                login=login,
                evento_fecha=hoy,
                evento_detalle=f"Notificación de ratificación enviada (intento {nro_envio}) al mail {usuario.mail}"
            )
            db.add(evento)
            enviados.append({"login": login, "mail": usuario.mail, "envio": nro_envio})

        db.commit()

        # 6️⃣ Mail interno si hubo baja
        if baja_caducidad_triggered:
            try:
                cuando = baja_contexto["cuando"] or datetime.now()
                lista_logins = ", ".join(baja_contexto["logins"]) or "(s/d)"
                cuerpo_int = f"""
                <html>
                  <body style="margin:0; padding:0; background-color:#f8f9fa;">
                    <table cellpadding="0" cellspacing="0" width="100%" style="background-color:#f8f9fa; padding:20px;">
                      <tr>
                        <td align="center">
                          <table cellpadding="0" cellspacing="0" width="600"
                                 style="background-color:#ffffff; border-radius:10px; padding:30px;
                                        font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                                        color:#343a40; box-shadow:0 0 10px rgba(0,0,0,0.1);">
                            <tr>
                              <td style="font-size:22px; color:#dc2626;">
                                <strong>Proyecto dado de baja por caducidad</strong>
                              </td>
                            </tr>
                            <tr>
                              <td style="padding-top:16px; font-size:16px;">
                                <p>Se alcanzó la cantidad máxima de notificaciones y venció el plazo tras el último aviso.</p>
                              </td>
                            </tr>
                            <tr>
                              <td style="padding-top:12px; font-size:16px;">
                                <div style="background-color:#fef2f2; padding:15px 20px; border-left:4px solid #dc2626; border-radius:6px;">
                                  <p><strong>Proyecto:</strong> #{baja_contexto['proyecto_id']}</p>
                                  <p><strong>Logins involucrados:</strong> {lista_logins}</p>
                                  <p><strong>Fecha/hora:</strong> {cuando.strftime('%d/%m/%Y %H:%M:%S')}</p>
                                  <p><strong>Nuevo estado:</strong> baja_caducidad</p>
                                </div>
                              </td>
                            </tr>
                            <tr>
                              <td style="padding-top:20px; font-size:16px;">
                                <p>Este correo es informativo y no requiere respuesta.</p>
                              </td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>
                  </body>
                </html>
                """

                enviar_mail_multiples(
                    destinatarios=DESTINATARIOS_RUA,
                    asunto="Proyecto dado de baja por caducidad — RUA",
                    cuerpo=cuerpo_int
                )
            except Exception as e:
                print(f"⚠️ Error enviando correo interno por baja_caducidad: {e}")

        return {
            "message": f"Se enviaron {len(enviados)} notificaciones para el proyecto {proyecto_obj.proyecto_id}.",
            "fecha_ratificacion": fecha_ratif.strftime("%Y-%m-%d"),
            "detalles": enviados,
            "baja_caducidad_notificada": bool(baja_caducidad_triggered),
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al enviar notificaciones: {str(e)}")





@proyectos_router.post("/proyectos/ratificar", response_model=dict, dependencies=[Depends(verify_api_key)])
def ratificar_proyecto(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    Ratifica la continuidad del proyecto del usuario. Registra un cambio simbólico viable → viable.
    Y notifica por correo a algunas chicas del RUA
    """

    login = current_user["user"]["login"]

    user  = current_user["user"]  # nombre, apellido, mail, etc.

    # Buscar el proyecto asociado
    proyecto = db.query(Proyecto).filter(
        (Proyecto.login_1 == login) | (Proyecto.login_2 == login),
        Proyecto.estado_general == "viable"
    ).first()
    proyecto = db.query(Proyecto).filter(
        (Proyecto.login_1 == login) | (Proyecto.login_2 == login),
        Proyecto.estado_general.in_(["viable", "en_carpeta"])
    ).first()

    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"No se encontró un proyecto viable para ratificar.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    try:      
        
        ahora = datetime.now()

        # Registrar historial simbólico
        # historial = ProyectoHistorialEstado(
        #     proyecto_id=proyecto.proyecto_id,
        #     fecha_hora=ahora,
        #     estado_anterior="viable",
        #     estado_nuevo="viable",
        #     comentarios="Ratificación de continuidad en el registro"
        # )
        historial = ProyectoHistorialEstado(
            proyecto_id = proyecto.proyecto_id,
            fecha_hora = ahora,
            estado_anterior = proyecto.estado_general,
            estado_nuevo = proyecto.estado_general,
            comentarios = "Ratificación de continuidad en el registro"
        )
        db.add(historial)

        # Buscar notificación existente (si existe)
        notificacion = db.query(UsuarioNotificadoRatificacion).filter_by(
            proyecto_id=proyecto.proyecto_id,
            login=login
        ).first()


        if not notificacion:
            # Crear nuevo registro si no existe
            notificacion = UsuarioNotificadoRatificacion(
                proyecto_id=proyecto.proyecto_id,
                login=login,
                ratificado=ahora
            )
            db.add(notificacion)
        else:
            # Actualizar ratificado y limpiar avisos
            notificacion.ratificado = ahora
            notificacion.mail_enviado_1 = None
            notificacion.mail_enviado_2 = None
            notificacion.mail_enviado_3 = None
            notificacion.mail_enviado_4 = None

        # Registrar evento en RuaEvento
        evento = RuaEvento(
            login=login,
            evento_fecha=ahora,
            evento_detalle=f"Ratificación realizada para el proyecto {proyecto.proyecto_id}"
        )
        db.add(evento)


        db.commit()

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"Error al registrar la ratificación: {str(e)}",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    # ───────────────────── Envío de correo a responsables internos ─────────────────────
    try:
        nombre_completo = f"{user.get('nombre','').strip()} {user.get('apellido','').strip()}".strip() or login
        asunto = "Ratificación de proyecto — RUA"

        mensaje_html = (
            f"<p>El/la pretenso/a <strong>{nombre_completo}</strong> ({login}) "
            f"ratificó la continuidad de su proyecto.</p>"
            f"<p><strong>Fecha y hora:</strong> {ahora.strftime('%d/%m/%Y %H:%M:%S')}</p>"
        )

        cuerpo = f"""
        <html>
          <body style="margin:0; padding:0; background-color:#f8f9fa;">
            <table cellpadding="0" cellspacing="0" width="100%" style="background-color:#f8f9fa; padding:20px;">
              <tr>
                <td align="center">
                  <table cellpadding="0" cellspacing="0" width="600"
                         style="background-color:#ffffff; border-radius:10px; padding:30px;
                                font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                                color:#343a40; box-shadow:0 0 10px rgba(0,0,0,0.1);">
                    <tr>
                      <td style="font-size:22px; color:#0d6efd;">
                        <strong>Ratificación de proyecto</strong>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:18px; font-size:16px;">
                        <p>Se registra la siguiente acción en el sistema RUA:</p>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:12px; font-size:16px;">
                        <div style="background-color:#f1f3f5; padding:15px 20px; border-left:4px solid #0d6efd; border-radius:6px; margin-top:10px;">
                          {mensaje_html}
                        </div>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:24px; font-size:16px;">
                        <p>Este correo es informativo y no requiere respuesta.</p>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
          </body>
        </html>
        """


        enviar_mail_multiples(
            destinatarios=DESTINATARIOS_RUA,
            asunto=asunto,
            cuerpo=cuerpo,
            # si preferís que no vean los correos entre sí, usá BCC:
            # destinatarios=[],
            # bcc=["misomoza@justiciacordoba.gob.ar", "sfurque@justiciacordoba.gob.ar"],
        )

    except Exception as e:
        # No hacemos rollback de la ratificación; solo registramos el error
        print(f"⚠️ Error enviando correo de ratificación a responsables internos: {e}")

    # Respuesta final al adoptante
    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "<p>Se ha registrado correctamente la ratificación de su proyecto.</p>",
        "tiempo_mensaje": 5,
        "next_page": "actual"
    }




# ---------- PUT: subir / migrar a multi ----------
@proyectos_router.put("/proyectos/documentos/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))],)
def subir_documento_proyecto(
    proyecto_id: int,
    campo: str = Form(...),         # acepta alias o nombre real
    file: UploadFile = File(...),
    prefijo: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    ):


    real_field = _resolve_field(campo)

    _, ext = os.path.splitext(file.filename.lower())
    if ext not in ALLOWED_EXT:
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": f"Extensión de archivo no permitida: {ext}",
            "tiempo_mensaje": 6, "next_page": "actual"
        }

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 6, "next_page": "actual"
        }

    # tamaño
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    if size > MAX_FILE_MB * 1024 * 1024:
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": f"El archivo excede el tamaño máximo permitido de {MAX_FILE_MB}MB.",
            "tiempo_mensaje": 6, "next_page": "actual"
        }

    carpeta = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, str(proyecto_id))
    _ensure_dir(carpeta)

    nombre_base = _sanitize(prefijo) if prefijo else _sanitize(real_field)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"{nombre_base}_{timestamp}{ext}"
    destino = os.path.join(carpeta, final_filename)

    try:
        with open(destino, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # ---- migrar si es legacy (string) y luego agregar el nuevo ----
        valor_actual = getattr(proyecto, real_field, None)
        archivos = _load_archivos(valor_actual)  # si era string, ya mete la ruta con fecha LEGACY_DEFAULT_DATE
        archivos.append({"ruta": destino, "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        setattr(proyecto, real_field, _dump_archivos(archivos))

        # Evento (opcional)
        try:
            db.add(RuaEvento(
                login=current_user["user"]["login"],
                evento_detalle=f"Subió documento en '{real_field}' para proyecto #{proyecto_id}: {os.path.basename(destino)}",
                evento_fecha=datetime.now()
            ))
        except Exception:
            pass

        db.commit()
        return {
            "success": True, "tipo_mensaje": "verde",
            "mensaje": "Documento subido correctamente.",
            "tiempo_mensaje": 4, "next_page": "actual"
        }
    except Exception as e:
        db.rollback()
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": f"Error al guardar el documento: {str(e)}",
            "tiempo_mensaje": 6, "next_page": "actual"
        }



# ---------- GET: descargar uno o todos ----------
@proyectos_router.get("/proyectos/documentos/{proyecto_id}/descargar-todos", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))],)
def descargar_todos_documentos_proyecto(
    proyecto_id: int,
    campo: str = Query(...),        # acepta alias o nombre real
    db: Session = Depends(get_db),
    ):

    real_field = _resolve_field(campo)

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    valor = getattr(proyecto, real_field, None)
    archivos = _load_archivos(valor)

    if not archivos:
        raise HTTPException(status_code=404, detail="No hay documentos registrados")

    if len(archivos) == 1:
        ruta = archivos[0].get("ruta")
        if not (ruta and os.path.exists(ruta)):
            raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")
        return FileResponse(path=ruta, filename=os.path.basename(ruta), media_type="application/octet-stream")

    # varios → zip
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zipf:
            for a in archivos:
                ruta = a.get("ruta")
                if ruta and os.path.exists(ruta):
                    zipf.write(ruta, arcname=os.path.basename(ruta))
        return FileResponse(
            path=tmp.name,
            filename=f"{real_field}_{proyecto_id}.zip",
            media_type="application/zip"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al generar el ZIP: {str(e)}")



# ---------- DELETE: eliminar (sólo si ≤ 24h, soporta legacy) ----------
@proyectos_router.delete("/proyectos/documentos/{proyecto_id}/eliminar", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def eliminar_documento_proyecto(
    proyecto_id: int,
    campo: str = Query(...),        # acepta alias o nombre real
    ruta: str = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)   # 👈 necesario para chequear rol
    ):


    real_field = _resolve_field(campo)

    # ¿Debemos aplicar la ventana de 24h? SOLO si es "profesional"
    roles = set((current_user or {}).get("roles", []))
    enforce_24h = any(r in roles for r in ["profesional"])

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 6, "next_page": "actual"
        }


    try:
        valor = getattr(proyecto, real_field, None)

        # === Caso LEGACY: un string plano con la ruta ===
        if valor and isinstance(valor, str) and not valor.strip().startswith("["):
            legacy_path = valor
            if legacy_path != ruta:
                return {
                    "success": False, "tipo_mensaje": "naranja",
                    "mensaje": "Archivo no encontrado.",
                    "tiempo_mensaje": 5, "next_page": "actual"
                }

            # --- [DESHABILITADO] Verificación 24h por mtime ---
            # fecha_subida = None
            # if os.path.exists(ruta):
            #     try:
            #         fecha_subida = datetime.fromtimestamp(os.path.getmtime(ruta))
            #     except Exception:
            #         fecha_subida = None
            # if not fecha_subida or not _dentro_24h(fecha_subida):
            #     return {
            #         "success": False, "tipo_mensaje": "naranja",
            #         "mensaje": "No se puede eliminar el archivo porque han pasado más de 24 horas desde su subida (o no es posible verificar la fecha).",
            #         "tiempo_mensaje": 6, "next_page": "actual"
            #     }

            # # Intentar inferir "fecha de subida" con mtime del archivo
            # fecha_subida = None
            # if os.path.exists(ruta):
            #     try:
            #         fecha_subida = datetime.fromtimestamp(os.path.getmtime(ruta))
            #     except Exception:
            #         fecha_subida = None

            # # Si no hay forma de verificar la fecha, bloquear
            # if not fecha_subida or not _dentro_24h(fecha_subida):
            #     return {
            #         "success": False, "tipo_mensaje": "naranja",
            #         "mensaje": "No se puede eliminar el archivo porque han pasado más de 24 horas desde su subida (o no es posible verificar la fecha).",
            #         "tiempo_mensaje": 6, "next_page": "actual"
            #     }


            # --- [NUEVO] 24h SOLO si es "supervisora" ---
            if enforce_24h:
                fecha_subida = None
                if os.path.exists(ruta):
                    try:
                        fecha_subida = datetime.fromtimestamp(os.path.getmtime(ruta))
                    except Exception:
                        fecha_subida = None
                if not fecha_subida or not _dentro_24h(fecha_subida):
                    return {
                        "success": False, "tipo_mensaje": "naranja",
                        "mensaje": "No se puede eliminar el archivo porque han pasado más de 24 horas desde su subida.",
                        "tiempo_mensaje": 6, "next_page": "actual"
                    }

            # Eliminar
            if os.path.exists(ruta):
                try:
                    os.remove(ruta)
                except Exception as e:
                    return {
                        "success": False, "tipo_mensaje": "naranja",
                        "mensaje": f"No se pudo eliminar el archivo físico: {e}",
                        "tiempo_mensaje": 5, "next_page": "actual"
                    }

            setattr(proyecto, real_field, None)
            db.commit()
            return {
                "success": True, "tipo_mensaje": "verde",
                "mensaje": "Archivo eliminado correctamente.",
                "tiempo_mensaje": 4, "next_page": "actual"
            }

        # === Caso JSON (multi-archivo) ===
        archivos = _load_archivos(valor)
        objetivo = next((a for a in archivos if a.get("ruta") == ruta), None)
        if not objetivo:
            return {
                "success": False, "tipo_mensaje": "naranja",
                "mensaje": "Archivo no encontrado.",
                "tiempo_mensaje": 5, "next_page": "actual"
            }


        # --- [DESHABILITADO] Verificación 24h por fecha JSON o mtime ---
        # fecha_subida = _parse_fecha(objetivo.get("fecha", ""))
        # if not fecha_subida and os.path.exists(ruta):
        #     try:
        #         fecha_subida = datetime.fromtimestamp(os.path.getmtime(ruta))
        #     except Exception:
        #         fecha_subida = None
        # if not fecha_subida or not _dentro_24h(fecha_subida):
        #     return {
        #         "success": False, "tipo_mensaje": "naranja",
        #         "mensaje": "No se puede eliminar el archivo porque han pasado más de 24 horas desde su subida.",
        #         "tiempo_mensaje": 5, "next_page": "actual"
        #     }

        # --- [NUEVO] 24h SOLO si es "supervisora" ---
        if enforce_24h:
            fecha_subida = _parse_fecha(objetivo.get("fecha", ""))
            if not fecha_subida and os.path.exists(ruta):
                try:
                    fecha_subida = datetime.fromtimestamp(os.path.getmtime(ruta))
                except Exception:
                    fecha_subida = None
            if not fecha_subida or not _dentro_24h(fecha_subida):
                return {
                    "success": False, "tipo_mensaje": "naranja",
                    "mensaje": "No se puede eliminar el archivo porque han pasado más de 24 horas desde su subida.",
                    "tiempo_mensaje": 5, "next_page": "actual"
                }

        # Eliminar
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except Exception as e:
                return {
                    "success": False, "tipo_mensaje": "naranja",
                    "mensaje": f"No se pudo eliminar el archivo físico: {e}",
                    "tiempo_mensaje": 5, "next_page": "actual"
                }


        nuevos = [a for a in archivos if a.get("ruta") != ruta]
        setattr(proyecto, real_field, _dump_archivos(nuevos) if nuevos else None)
        db.commit()

        return {
            "success": True, "tipo_mensaje": "verde",
            "mensaje": "Archivo eliminado correctamente.",
            "tiempo_mensaje": 4, "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": f"Error al eliminar archivo: {str(e)}",
            "tiempo_mensaje": 6, "next_page": "actual"
        }




@proyectos_router.get("/proyectos/documentos/{proyecto_id}/descargar-uno", response_class=FileResponse,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def descargar_un_documento_proyecto(
    proyecto_id: int,
    campo: str = Query(...),
    ruta: str = Query(...),
    db: Session = Depends(get_db),
    ):

    real_field = _resolve_field(campo)
    ruta_param = unquote(ruta).replace("\\", "/")  # ← decodificar y normalizar

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    valor = getattr(proyecto, real_field, None)
    archivos = _load_archivos(valor)

    # seguridad: la ruta debe existir dentro de ese campo del proyecto
    objetivo = next((a for a in archivos if (a.get("ruta") or "") == ruta_param), None)
    if not objetivo:
        disponibles = [os.path.basename(a.get("ruta", "")) for a in archivos][:5]
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Archivo no encontrado en el proyecto",
                "buscado": ruta_param,
                "disponibles_ejemplo": disponibles,
            }
        )

    # Usamos la ruta tal cual; si fuese relativa, la resolvemos dentro del directorio base
    fs_path = ruta_param
    if not os.path.isabs(fs_path):
        fs_path = os.path.normpath(os.path.join(UPLOAD_DIR_DOC_PROYECTOS, fs_path.lstrip("/")))

    if not os.path.exists(fs_path):
        # Alternativa tentativa: mismo nombre de archivo dentro del base dir
        alt_path = os.path.join(UPLOAD_DIR_DOC_PROYECTOS, os.path.basename(ruta_param))
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Archivo no encontrado en disco",
                "ruta_proyecto": ruta_param,
                "ruta_fs_intentada": fs_path,
                "alternativa_probada": alt_path,
                "alternativa_existe": os.path.exists(alt_path),
            }
        )

    return FileResponse(
        path=fs_path,
        filename=os.path.basename(fs_path),
        media_type="application/octet-stream"
    )




@proyectos_router.get("/{proyecto_id}/info-notificaciones", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))]
)
def obtener_info_notificaciones_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db)
    ):
    """
    🔎 Devuelve información para describir qué notificaciones se enviarán
    al valorar un proyecto (especialmente en convocatorias).
    """

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "mensaje": "Proyecto no encontrado"
        }

    pretensos = []
    logins = [proyecto.login_1]
    if proyecto.login_2:
        logins.append(proyecto.login_2)

    for login in logins:
        user = db.query(User).filter(User.login == login).first()
        if not user:
            continue

        tiene_password = bool(user.clave) and user.active == "Y"

        pretensos.append({
            "login": user.login,
            "nombre": f"{user.nombre} {user.apellido or ''}".strip(),
            "mail": user.mail,
            "tiene_password": tiene_password
        })

    return {
        "success": True,
        "proyecto_id": proyecto.proyecto_id,
        "ingreso_por": proyecto.ingreso_por,  # rua | oficio | convocatoria
        "es_convocatoria": proyecto.ingreso_por == "convocatoria",
        "pretensos": pretensos
    }

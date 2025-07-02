from fastapi import APIRouter, HTTPException, Depends, Query, Request, status, Body, UploadFile, File, Form
from typing import List, Optional, Literal, Tuple
from sqlalchemy.orm import Session, aliased, joinedload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, case, and_, or_, Integer, literal_column


from datetime import datetime, date
from models.proyecto import Proyecto, ProyectoHistorialEstado, DetalleEquipoEnProyecto, AgendaEntrevistas, FechaRevision
from models.carpeta import Carpeta, DetalleProyectosEnCarpeta, DetalleNNAEnCarpeta
from models.notif_y_observaciones import ObservacionesProyectos, ObservacionesPretensos, NotificacionesRUA
from models.convocatorias import DetalleProyectoPostulacion
from models.ddjj import DDJJ
from models.nna import Nna

from bs4 import BeautifulSoup


# from models.carpeta import DetalleProyectosEnCarpeta
from models.users import User, Group, UserGroup 
from database.config import get_db
from helpers.utils import get_user_name_by_login, construir_subregistro_string, parse_date, generar_codigo_para_link, \
    enviar_mail, get_setting_value, edad_como_texto
from models.eventos_y_configs import RuaEvento

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



proyectos_router = APIRouter()





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

    subregistros: Optional[List[str]] = Query(None, alias="subregistro_portada")

):
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


        # query = (
        #     db.query(Proyecto)
        #     .outerjoin(User1, Proyecto.login_1 == User1.login)
        #     .outerjoin(User2, Proyecto.login_2 == User2.login)
        # )

        if fecha_nro_orden_inicio or fecha_nro_orden_fin:
            fecha_nro_orden_inicio = datetime.strptime(fecha_nro_orden_inicio, "%Y-%m-%d") if fecha_nro_orden_inicio else datetime(1970, 1, 1)
            fecha_nro_orden_fin = datetime.strptime(fecha_nro_orden_fin, "%Y-%m-%d") if fecha_nro_orden_fin else datetime.now()

            # Verificar que Proyecto.fecha_asignacion_nro_orden no sea None antes de aplicar between
            # query = query.filter(
            #     Proyecto.fecha_asignacion_nro_orden != None,
            #     func.str_to_date(Proyecto.fecha_asignacion_nro_orden, "%d/%m/%Y").between(fecha_nro_orden_inicio, fecha_nro_orden_fin)
            # )
            query = query.filter(
                Proyecto.fecha_asignacion_nro_orden != None,
                Proyecto.fecha_asignacion_nro_orden.between(fecha_nro_orden_inicio, fecha_nro_orden_fin)
            )

        if fecha_cambio_estado_inicio or fecha_cambio_estado_fin:
            fecha_cambio_estado_inicio = datetime.strptime(fecha_cambio_estado_inicio, "%Y-%m-%d") if fecha_cambio_estado_inicio else datetime(1970, 1, 1)
            fecha_cambio_estado_fin = datetime.strptime(fecha_cambio_estado_fin, "%Y-%m-%d") if fecha_cambio_estado_fin else datetime.now()

            # Verificar que Proyecto.fecha_asignacion_nro_orden no sea None antes de aplicar between
            # query = query.filter(
            #     Proyecto.ultimo_cambio_de_estado != None,
            #     func.str_to_date(Proyecto.ultimo_cambio_de_estado, "%d/%m/%Y").between(fecha_cambio_estado_inicio, fecha_cambio_estado_fin)
            # )
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


        if subregistros:
            for sr in subregistros:
                field = subregistro_field_map.get(sr)
                if field is not None:
                    query = query.filter(field == "Y")
        


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


        # if search:
        #     palabras = search.lower().split()  # divide en palabras
        #     condiciones_por_palabra = []

        #     for palabra in palabras:
        #         condiciones_por_palabra.append(
        #             or_(
        #                 func.lower(func.concat(User1.nombre, " ", User1.apellido)).ilike(f"%{palabra}%"),
        #                 func.lower(func.concat(User2.nombre, " ", User2.apellido)).ilike(f"%{palabra}%"),
        #                 Proyecto.login_1.ilike(f"%{palabra}%"),
        #                 Proyecto.login_2.ilike(f"%{palabra}%"),
        #                 Proyecto.nro_orden_rua.ilike(f"%{palabra}%"),
        #                 Proyecto.proyecto_calle_y_nro.ilike(f"%{palabra}%"),
        #                 Proyecto.proyecto_barrio.ilike(f"%{palabra}%"),
        #                 Proyecto.proyecto_localidad.ilike(f"%{palabra}%"),
        #                 Proyecto.proyecto_provincia.ilike(f"%{palabra}%")
        #             )
        #         )

        #     # Todas las palabras deben coincidir en algún campo (AND entre ORs)
        #     query = query.filter(and_(*condiciones_por_palabra))


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

            # Obtener todas las carpetas en las que está el proyecto
            # carpeta_ids = [
            #     row.carpeta_id for row in db.query(DetalleProyectosEnCarpeta.carpeta_id)
            #     .filter(DetalleProyectosEnCarpeta.proyecto_id == proyecto.proyecto_id)
            #     .all()
            # ]

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
            elif proyecto.estado_general in ["vinculacion", "guarda", "adopcion_definitiva"]:

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
                "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
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
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante", "coordinadora"]))])
def get_proyecto_por_id(
    request: Request,
    proyecto_id: int,
    db: Session = Depends(get_db),
):
    """
    Obtiene los detalles de un proyecto específico según su `proyecto_id`.
    """
    try:
        # Consulta principal para obtener los detalles del proyecto
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
                Proyecto.doc_sentencia_adopcion.label("doc_sentencia_adopcion"),

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

                Proyecto.subreg_1,
                Proyecto.subreg_2,
                Proyecto.subreg_3,
                Proyecto.subreg_4,
                Proyecto.subreg_FE1,
                Proyecto.subreg_FE2,
                Proyecto.subreg_FE3,
                Proyecto.subreg_FE4,
                Proyecto.subreg_FET,
                Proyecto.subreg_5A1E1,
                Proyecto.subreg_5A1E2,
                Proyecto.subreg_5A1E3,
                Proyecto.subreg_5A1E4,
                Proyecto.subreg_5A1ET,
                Proyecto.subreg_5A2E1,
                Proyecto.subreg_5A2E2,
                Proyecto.subreg_5A2E3,
                Proyecto.subreg_5A2E4,
                Proyecto.subreg_5A2ET,
                Proyecto.subreg_5B1E1,
                Proyecto.subreg_5B1E2,
                Proyecto.subreg_5B1E3,
                Proyecto.subreg_5B1E4,
                Proyecto.subreg_5B1ET,
                Proyecto.subreg_5B2E1,
                Proyecto.subreg_5B2E2,
                Proyecto.subreg_5B2E3,
                Proyecto.subreg_5B2E4,
                Proyecto.subreg_5B2ET,
                Proyecto.subreg_5B3E1,
                Proyecto.subreg_5B3E2,
                Proyecto.subreg_5B3E3,
                Proyecto.subreg_5B3E4,
                Proyecto.subreg_5B3ET,
                Proyecto.subreg_F5E1,
                Proyecto.subreg_F5E2,
                Proyecto.subreg_F5E3,
                Proyecto.subreg_F5E4,
                Proyecto.subreg_F5ET,
                Proyecto.subreg_61E1,
                Proyecto.subreg_61E2,
                Proyecto.subreg_61E3,
                Proyecto.subreg_61ET,
                Proyecto.subreg_62E1,
                Proyecto.subreg_62E2,
                Proyecto.subreg_62E3,
                Proyecto.subreg_62ET,
                Proyecto.subreg_63E1,
                Proyecto.subreg_63E2,
                Proyecto.subreg_63E3,
                Proyecto.subreg_63ET,
                Proyecto.subreg_FQ1,
                Proyecto.subreg_FQ2,
                Proyecto.subreg_FQ3,
                Proyecto.subreg_F6E1,
                Proyecto.subreg_F6E2,
                Proyecto.subreg_F6E3,
                Proyecto.subreg_F6ET,
                
            )
            .filter(Proyecto.proyecto_id == proyecto_id)
            .first()
        )

        if not proyecto:
            raise HTTPException(status_code=404, detail=f"Proyecto con ID {proyecto_id} no encontrado.")

        
        # Obtener celulares directamente desde sec_users
        login_1_user = db.query(User).filter(User.login == proyecto.login_1).first()
        login_2_user = db.query(User).filter(User.login == proyecto.login_2).first()

        login_1_telefono = login_1_user.celular if login_1_user else None
        login_2_telefono = login_2_user.celular if login_2_user else None

        # También podés agregar mail si lo querés
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
            "guarda": "GUARDA",
            "adopcion_definitiva": "ADOPCIÓN DEF.",
            "baja_anulacion": "P. BAJA ANUL.",
            "baja_caducidad": "P. BAJA CADUC.",
            "baja_por_convocatoria": "P. BAJA POR C.",
            "baja_rechazo_invitacion": "P. BAJA RECHAZO",
        }.get(proyecto.estado_general, "ESTADO DESCONOCIDO")


        texto_ingreso_por = {
            "rua": "RUA",
            "oficio": "OFICIO",
            "convocatoria": "CONV.",
        }.get(proyecto.ingreso_por, "RUA")


        # Construir la respuesta con los detalles del proyecto
        proyecto_dict = {
            "proyecto_id": proyecto.proyecto_id,
            "proyecto_tipo": proyecto.proyecto_tipo,
            "nro_orden_rua": proyecto.nro_orden_rua,
            "subregistro_string": construir_subregistro_string(proyecto),  # Concatenación de subregistros

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
            "doc_sentencia_adopcion": proyecto.doc_sentencia_adopcion,

            "boton_solicitar_actualizacion_proyecto": proyecto.estado_general == "en_revision" and \
                proyecto.proyecto_tipo in ("Matrimonio", "Unión convivencial"),

            "boton_valorar_proyecto": proyecto.estado_general == "en_revision",
            "boton_para_valoracion_final_proyecto": proyecto.estado_general == "para_valorar",
            "boton_para_sentencia_guarda": proyecto.estado_general == "vinculacion",
            "boton_para_sentencia_adopcion": proyecto.estado_general == "guarda",
            "boton_agregar_a_carpeta": proyecto.estado_general == "viable",

            # "carpeta_ids": carpeta_ids,  # Lista de carpetas asociadas al proyecto

            "texto_boton_estado_proyecto": texto_boton_estado_proyecto,

            "estado_general": proyecto.estado_general,

            "ingreso_por": texto_ingreso_por,

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

        # 🔁 Agrega todos los campos subreg_... al dict
        subregistros_definitivos = [
            "subreg_1", "subreg_2", "subreg_3", "subreg_4",
            "subreg_FE1", "subreg_FE2", "subreg_FE3", "subreg_FE4", "subreg_FET",
            "subreg_5A1E1", "subreg_5A1E2", "subreg_5A1E3", "subreg_5A1E4", "subreg_5A1ET",
            "subreg_5A2E1", "subreg_5A2E2", "subreg_5A2E3", "subreg_5A2E4", "subreg_5A2ET",
            "subreg_5B1E1", "subreg_5B1E2", "subreg_5B1E3", "subreg_5B1E4", "subreg_5B1ET",
            "subreg_5B2E1", "subreg_5B2E2", "subreg_5B2E3", "subreg_5B2E4", "subreg_5B2ET",
            "subreg_5B3E1", "subreg_5B3E2", "subreg_5B3E3", "subreg_5B3E4", "subreg_5B3ET",
            "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
            "subreg_61E1", "subreg_61E2", "subreg_61E3", "subreg_61ET",
            "subreg_62E1", "subreg_62E2", "subreg_62E3", "subreg_62ET",
            "subreg_63E1", "subreg_63E2", "subreg_63E3", "subreg_63ET",
            "subreg_FQ1", "subreg_FQ2", "subreg_FQ3",
            "subreg_F6E1", "subreg_F6E2", "subreg_F6E3", "subreg_F6ET",
        ]

        for campo in subregistros_definitivos:
            proyecto_dict[campo] = getattr(proyecto, campo, None)


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

            proyecto_activo = db.query(Proyecto).filter(
                Proyecto.login_2 == login_2,
                Proyecto.estado_general.in_(["creado", "confeccionando", "en_revision", "actualizando", "aprobado", 
                                             "calendarizando", "entrevistando", "para_valorar",
                                             "viable", "en_suspenso", "en_carpeta", "vinculacion", "guarda"])
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


        # Verificar que login_1 no tenga un proyecto activo según estado_general
        proyecto_login_1_existente = (
            db.query(Proyecto)
            .filter(
                Proyecto.login_1 == login_1,
                Proyecto.estado_general.in_(["creado", "confeccionando", "en_revision", "actualizando", "aprobado", 
                                             "en_valoracion", "viable", "en_suspenso", "en_carpeta", 
                                             "vinculacion", "guarda"])
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
                                                "vinculacion", "guarda"])
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


        # Asignar nro_orden_rua solo si no está ya asignado
        if not proyecto.nro_orden_rua:
            ultimos_nros = db.query(Proyecto.nro_orden_rua)\
                .filter(Proyecto.nro_orden_rua != None)\
                .all()

            # Filtrar solo los que tienen menos de 5 dígitos y son números válidos
            numeros_validos = [
                int(p.nro_orden_rua) for p in ultimos_nros
                if p.nro_orden_rua.isdigit() and len(p.nro_orden_rua) < 5
            ]

            nuevo_nro_orden = str(max(numeros_validos) + 1) if numeros_validos else "1"

            proyecto.nro_orden_rua = nuevo_nro_orden
            proyecto.fecha_asignacion_nro_orden = date.today()
        else:
            nuevo_nro_orden = proyecto.nro_orden_rua


        # Cambiar estado
        proyecto.estado_general = "calendarizando"
        proyecto.ultimo_cambio_de_estado = date.today()

        # Registrar en historial
        historial = ProyectoHistorialEstado(
            proyecto_id = proyecto.proyecto_id,
            estado_anterior = "aprobado",
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

        print( evaluaciones)

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

        # Validación de evaluaciones
        evaluaciones_previas = []
        for ent in entrevistas_previas:
            if ent.evaluaciones:
                evaluaciones_previas.extend(json.loads(ent.evaluaciones))

        # Convertir a índices
        indices_previos = [EVALUACIONES_VALIDAS.index(e) for e in evaluaciones_previas if e in EVALUACIONES_VALIDAS]
        max_evaluacion_completada = max(indices_previos) if indices_previos else -1

        indices_actuales = [EVALUACIONES_VALIDAS.index(e) for e in evaluaciones if e in EVALUACIONES_VALIDAS]

        # Validar que no se salten evaluaciones
        if not all(e in EVALUACIONES_VALIDAS for e in evaluaciones):
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Una o más evaluaciones no son válidas.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # if not indices_actuales or min(indices_actuales) > max_evaluacion_completada + 1:
        #     return {
        #         "success": False,
        #         "tipo_mensaje": "naranja",
        #         "mensaje": "Las evaluaciones deben realizarse en orden. No se pueden saltear "
        #                    "evaluaciones. Deben completarse en secuencia.",
        #         "tiempo_mensaje": 7,
        #         "next_page": "actual"
        #     }

        # if sorted(indices_actuales) != list(range(min(indices_actuales), max(indices_actuales)+1)):
        #     return {
        #         "success": False,
        #         "tipo_mensaje": "naranja",
        #         "mensaje": "Las evaluaciones seleccionadas deben ser consecutivas y estar en orden.",
        #         "tiempo_mensaje": 7,
        #         "next_page": "actual"
        #     }

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

        # Cambiar el estado del proyecto a "entrevistando" si aún no lo está
        if proyecto.estado_general == "calendarizando":
            proyecto.estado_general = "entrevistando"
            db.add(proyecto)  # No es obligatorio porque ya está en la sesión, pero por claridad está bien

        db.commit()


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
    tipo_documento: Literal["informe_entrevistas", "sentencia_guarda", "sentencia_adopcion"],
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    📄 Sube un documento a un proyecto según el tipo indicado.

    ✔️ Tipos válidos:
    - `informe_entrevistas`
    - `sentencia_guarda`
    - `sentencia_adopcion`

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

    if proyecto.estado_general != "guarda":
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
                "mensaje": "Pryecto no encontrado.",
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

        # subregistros_map = {
        #     "1": "subregistro_1",
        #     "2": "subregistro_2",
        #     "3": "subregistro_3",
        #     "4": "subregistro_4",
        #     "FE1": "flex_edad_1",
        #     "FE2": "flex_edad_2",
        #     "FE3": "flex_edad_3",
        #     "FE4": "flex_edad_4",
        #     "FET": "flex_edad_todos",
        #     "5A1": "discapacidad_1",
        #     "5A2": "discapacidad_2",
        #     "5A1E1": "edad_discapacidad_0",
        #     "5A1E2": "edad_discapacidad_1",
        #     "5A1E3": "edad_discapacidad_2",
        #     "5A1E4": "edad_discapacidad_3",
        #     "5A1ET": "edad_discapacidad_4",
        #     "F5S": "flex_condiciones_salud",
        #     "F5E1": "flex_salud_edad_0",
        #     "F5E2": "flex_salud_edad_1",
        #     "F5E3": "flex_salud_edad_2",
        #     "F5E4": "flex_salud_edad_3",
        #     "F5ET": "flex_salud_edad_4",
        #     "61": "hermanos_comp_1",
        #     "62": "hermanos_comp_2",
        #     "63": "hermanos_comp_3",
        #     "61E1": "hermanos_edad_0",
        #     "61E2": "hermanos_edad_1",
        #     "61E3": "hermanos_edad_2",
        #     "61ET": "hermanos_edad_3",
        #     "FQ1": "flex_hermanos_comp_1",
        #     "FQ2": "flex_hermanos_comp_2",
        #     "FQ3": "flex_hermanos_comp_3",
        #     "F6E1": "flex_hermanos_edad_0",
        #     "F6E2": "flex_hermanos_edad_1",
        #     "F6E3": "flex_hermanos_edad_2",
        #     "F6E4": "flex_hermanos_edad_3",
        #     "F6ET": "flex_hermanos_edad_3"
        # }

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
            "F5E1": "subreg_F5E1",
            "F5E2": "subreg_F5E2",
            "F5E3": "subreg_F5E3",
            "F5E4": "subreg_F5E4",
            "F5ET": "subreg_F5ET",
            "61E1": "subreg_61E1",
            "61E2": "subreg_61E2",
            "61E3": "subreg_61E3",
            "61ET": "subreg_61ET",
            "FQ1": "subreg_FQ1",
            "FQ2": "subreg_FQ2",
            "FQ3": "subreg_FQ3",
            "F6E1": "subreg_F6E1",
            "F6E2": "subreg_F6E2",
            "F6E3": "subreg_F6E3",
            "F6ET": "subreg_F6ET"
        }


        if estado_final == "viable":
            # # Primero limpiamos todo
            # for campo in set(subregistros_map.values()):
            #     setattr(proyecto, campo, "N")

            # # Luego activamos los seleccionados
            # for codigo in subregistros_raw:
            #     campo = subregistros_map.get(codigo)
            #     if campo:
            #         setattr(proyecto, campo, "Y")

            # Limpiar todos los subreg_...
            for campo in set(subregistros_definitivos_map.values()):
                setattr(proyecto, campo, "N")

            # Activar los seleccionados
            for codigo in subregistros_raw:
                campo = subregistros_definitivos_map.get(codigo)
                if campo:
                    setattr(proyecto, campo, "Y")


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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional"]))]
)
def subir_informe_valoracion(
    proyecto_id:int,
    file:UploadFile=File(...),
    db:Session=Depends(get_db)
):
    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _save_historial_upload(proyecto,"informe_profesionales",file,UPLOAD_DIR_DOC_PROYECTOS, db)



@proyectos_router.get(
    "/entrevista/informe/{proyecto_id}/descargar-todos",
    response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))]
)
def descargar_todos_valoracion(
    proyecto_id:int, db:Session=Depends(get_db)
):
    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _download_all(proyecto.informe_profesionales or "","informes_valoracion", proyecto_id)


# 2) Informe de vinculación
@proyectos_router.put( "/informe-vinculacion/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))]
)
def subir_informe_vinculacion(
    proyecto_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        raise HTTPException(404, "Proyecto no encontrado")
    return _save_historial_upload( proyecto, "doc_informe_vinculacion", file, UPLOAD_DIR_DOC_PROYECTOS, db )


@proyectos_router.get(
    "/informe-vinculacion/{proyecto_id}/descargar-todos",
    response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))]
)
def descargar_todos_vinculacion(
    proyecto_id:int, db:Session=Depends(get_db)
):
    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _download_all(proyecto.doc_informe_vinculacion or "","informes_vinculacion",proyecto_id)



# 3) Informe seguimiento de guarda
@proyectos_router.put( "/informe-seguimiento-guarda/{proyecto_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))]
)
def subir_informe_guarda(
    proyecto_id:int,
    file:UploadFile=File(...),
    db:Session=Depends(get_db)
):
    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _save_historial_upload(proyecto,"doc_informe_seguimiento_guarda",file,UPLOAD_DIR_DOC_PROYECTOS, db)


@proyectos_router.get(
    "/informe-seguimiento-guarda/{proyecto_id}/descargar-todos",
    response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador","profesional","supervision", "supervisora"]))]
)
def descargar_todos_guarda(
    proyecto_id:int, db:Session=Depends(get_db)
):
    proyecto=db.query(Proyecto).get(proyecto_id)
    if not proyecto: raise HTTPException(404,"Proyecto no encontrado")
    return _download_all(proyecto.doc_informe_seguimiento_guarda or "","informes_guarda",proyecto_id)

    


@proyectos_router.get(
    "/proyectos/entrevista/informe/{proyecto_id}/descargar-todos",
    response_class=FileResponse,
    dependencies=[
        Depends(verify_api_key),
        Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))
    ]
)
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
    tipo_documento: Literal["informe_entrevistas", "sentencia_guarda", "sentencia_adopcion"],
    db: Session = Depends(get_db)
):
    """
    📄 Descarga un documento del proyecto identificado por `proyecto_id`.

    ✔️ Tipos válidos:
    - `informe_entrevistas` → informe_profesionales
    - `sentencia_guarda` → doc_sentencia_guarda
    - `sentencia_adopcion` → doc_sentencia_adopcion

    ⚠️ El documento debe haber sido subido previamente mediante el endpoint correspondiente.
    """

    # Mapeo del tipo_documento a los campos del modelo
    campo_por_tipo = {
        "informe_entrevistas": "informe_profesionales",
        "sentencia_guarda": "doc_sentencia_guarda",
        "sentencia_adopcion": "doc_sentencia_adopcion"
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

    except SQLAlchemyError as e:
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




@proyectos_router.put("/confirmar-sentencia-guarda/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def confirmar_sentencia_guarda(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    observacion = body.get("observacion", "").strip()

    if not observacion:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "La observación es obligatoria.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if not proyecto.doc_sentencia_guarda:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "No se ha subido la sentencia de guarda.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    # Registrar evento y observación
    evento = RuaEvento(
        login = current_user["user"]["login"],
        evento_detalle = f"Se confirmó la sentencia de guarda para el proyecto #{proyecto_id}",
        evento_fecha = datetime.now()
    )
    db.add(evento)

    observ = ObservacionesProyectos(
        observacion_a_cual_proyecto = proyecto_id,
        observacion = observacion,
        login_que_observo = current_user["user"]["login"],
        observacion_fecha = datetime.now()
    )
    db.add(observ)

    historial = ProyectoHistorialEstado(
        proyecto_id = proyecto_id,
        estado_anterior = "vinculacion",
        estado_nuevo = "guarda",
        fecha_hora = datetime.now()
    )
    db.add(historial)

    proyecto.estado_general = "guarda"
    
    
    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "La sentencia de guarda fue confirmada correctamente.",
        "tiempo_mensaje": 5,
        "next_page": "actual"
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




@proyectos_router.put("/confirmar-sentencia-adopcion/{proyecto_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervision", "supervisora"]))])
def confirmar_sentencia_adopcion(
    proyecto_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    observacion = body.get("observacion", "").strip()

    if not observacion:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "La observación es obligatoria.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Proyecto no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if not proyecto.doc_sentencia_adopcion:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "No se ha subido la sentencia de adopción.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    
    # Registrar evento y observación
    evento = RuaEvento(
        login = current_user["user"]["login"],
        evento_detalle = f"Se confirmó la sentencia de adopción para el proyecto #{proyecto_id}",
        evento_fecha = datetime.now()
    )
    db.add(evento)

    observ = ObservacionesProyectos(
        observacion_a_cual_proyecto = proyecto_id,
        observacion = observacion,
        login_que_observo = current_user["user"]["login"],
        observacion_fecha = datetime.now()
    )
    db.add(observ)

    historial = ProyectoHistorialEstado(
        proyecto_id = proyecto_id,
        estado_anterior = "guarda",
        estado_nuevo = "adopcion_definitiva",
        fecha_hora = datetime.now()
    )
    db.add(historial)

    proyecto.estado_general = "adopcion_definitiva"
    
    
    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "La sentencia de adopción fue confirmada correctamente.",
        "tiempo_mensaje": 5,
        "next_page": "actual"
    }




@proyectos_router.post("/crear-proyecto-completo", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["adoptante"]))])
def crear_proyecto_completo(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    
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

        user1_roles = db.query(UserGroup).filter(UserGroup.login == login_1).all()
        if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in user1_roles):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"El usuario no tiene el rol 'adoptante'.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }


        # Todas estas son validaciones para los proyectos en pareja
        if tipo != "Monoparental":
            if not login_2:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "Debe especificar el DNI de la pareja para proyectos biparentales.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            if login_2 == login_1:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El DNI de la pareja debe ser distinto al suyo.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            
            login_2_user = db.query(User).filter(User.login == login_2).first()
            if not login_2_user:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El DNI de su pareja no corresponde a un usuario en el sistema.",
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

            print( '2', login_2_user, login_2_roles )
            
            # # ❌ No permitir si login_2 ya tiene otro proyecto como titular o pareja
            # proyecto_pareja_activo = db.query(Proyecto).filter(
            #     or_(
            #         Proyecto.login_1 == login_2,
            #         Proyecto.login_2 == login_2
            #     ),
            #     Proyecto.estado_general.in_([
            #         "en_revision", "actualizando", "aprobado", "calendarizando", "entrevistando",
            #         "para_valorar", "viable", "en_suspenso", "en_carpeta", "vinculacion", "guarda"
            #     ])
            # ).first()

            # print( '3', proyecto_pareja_activo )

            # if proyecto_pareja_activo:
            #     return {
            #         "success": False,
            #         "tipo_mensaje": "naranja",
            #         "mensaje": f"Su pareja ya forma parte de otro proyecto activo. No se puede continuar.",
            #         "tiempo_mensaje": 5,
            #         "next_page": "actual"
            #     }



        # 🔍 1) ¿existe un proyecto activo confeccionando/aprobado/actualizando?. 
        proyecto_existente = (
            db.query(Proyecto)
            .filter(
                Proyecto.login_1 == login_1,
                Proyecto.ingreso_por == "rua",
                Proyecto.estado_general.in_(["confeccionando", "actualizando"]),
                Proyecto.login_2 == login_2,
                Proyecto.proyecto_tipo == tipo
            )
            .first()
        )
        

        print( '6', proyecto_existente )


        # 🔁 Automatiza la carga de subregistros
        subregistros_definitivos = [
            "subreg_1", "subreg_2", "subreg_3", "subreg_4",
            "subreg_FE1", "subreg_FE2", "subreg_FE3", "subreg_FE4", "subreg_FET",
            "subreg_5A1E1", "subreg_5A1E2", "subreg_5A1E3", "subreg_5A1E4", "subreg_5A1ET",
            "subreg_5A2E1", "subreg_5A2E2", "subreg_5A2E3", "subreg_5A2E4", "subreg_5A2ET",
            "subreg_5B1E1", "subreg_5B1E2", "subreg_5B1E3", "subreg_5B1E4", "subreg_5B1ET",
            "subreg_5B2E1", "subreg_5B2E2", "subreg_5B2E3", "subreg_5B2E4", "subreg_5B2ET",
            "subreg_5B3E1", "subreg_5B3E2", "subreg_5B3E3", "subreg_5B3E4", "subreg_5B3ET",
            "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
            "subreg_61E1", "subreg_61E2", "subreg_61E3", "subreg_61ET",
            "subreg_62E1", "subreg_62E2", "subreg_62E3", "subreg_62ET",
            "subreg_63E1", "subreg_63E2", "subreg_63E3", "subreg_63ET",
            "subreg_FQ1", "subreg_FQ2", "subreg_FQ3",
            "subreg_F6E1", "subreg_F6E2", "subreg_F6E3", "subreg_F6ET",
        ]

        def subreg(k):
            return "Y" if data.get(k) == "Y" else "N"

        subreg_data = {campo: subreg(campo) for campo in subregistros_definitivos}



        if proyecto_existente:            

            # ── actualizar campos básicos ─────────────────────────────
            proyecto_existente.proyecto_calle_y_nro = proyecto_calle_y_nro
            proyecto_existente.proyecto_depto_etc   = proyecto_depto_etc
            proyecto_existente.proyecto_barrio      = proyecto_barrio
            proyecto_existente.proyecto_localidad   = proyecto_localidad
            proyecto_existente.proyecto_provincia   = provincia

            
            # subregistros
            for campo, valor in subreg_data.items():
                setattr(proyecto_existente, campo, valor)

            estado_nuevo = "en_revision"
            estado_anterior = proyecto_existente.estado_general  # <-- guardar antes de cambiar
            proyecto_existente.estado_general = estado_nuevo           # <-- luego actualizar

            # registrar el cambio
            db.add(
                ProyectoHistorialEstado(
                    proyecto_id     = proyecto_existente.proyecto_id,
                    estado_anterior = estado_anterior,
                    estado_nuevo    = estado_nuevo,
                    fecha_hora      = datetime.now()
                )
            )


            db.commit()
            db.refresh(proyecto_existente)

            return {
                "success": True,
                "tipo_mensaje": "verde",
                "mensaje": "Solicitud de revisión enviada correctamente.",
                "tiempo_mensaje": 4,
                "next_page": "menu_adoptantes/proyecto",
            }
        
        
        # Sigue por este else cuando el proeycto no existe todavía
        else :

            if tipo != "Monoparental":

                aceptado_code = generar_codigo_para_link(16)
                aceptado = "N"
                estado = "invitacion_pendiente"

            else :

                aceptado_code = None
                aceptado = "Y"
                estado = "en_revision"
                login_2 = None


            nuevo = Proyecto(
                login_1=login_1,
                login_2=login_2,
                
                proyecto_tipo=tipo,

                proyecto_calle_y_nro = proyecto_calle_y_nro,
                proyecto_depto_etc = proyecto_depto_etc,
                proyecto_barrio = proyecto_barrio,
                proyecto_localidad = proyecto_localidad,
                proyecto_provincia = provincia,
                ingreso_por="rua",

                aceptado = aceptado,
                aceptado_code = aceptado_code,
                operativo = "Y",
                estado_general = estado,
                **subreg_data  # ✅ Desempaqueta todos los subreg_... con "Y"/"N"
            )

            db.add(nuevo)
            db.commit()
            db.refresh(nuevo)

            print( '8', aceptado_code )

            # Hay aceptado_code cuando es biparental
            if tipo != "Monoparental" :
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

                    print( '9', link_aceptar )

                    cuerpo = f"""
                        <html>
                        <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                            <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                            <tr>
                                <td align="center">
                                <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                    <tr>
                                    <td style="font-size: 24px; color: #007bff;">
                                        <strong>Invitación a Proyecto Adoptivo</strong>
                                    </td>
                                    </tr>
                                    <tr>
                                    <td style="padding-top: 20px; font-size: 17px;">
                                        <p>Has sido invitado/a a conformar un proyecto adoptivo junto a <strong>{nombre_1} {apellido_1}</strong> (DNI: {login_1}).</p>
                                        {"<p style='color: red;'><strong>⚠️ Para aceptar la invitación, debés tener aprobado el Curso Obligatorio.</strong></p>" if not doc_adoptante_curso_aprobado else ""}
                                        <p>Por favor, confirmá tu participación haciendo clic en uno de los siguientes botones:</p>
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
                                    <td align="center" style="font-size: 17px;">
                                        <p><strong>Muchas gracias</strong></p>
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


                    enviar_mail(destinatario=login_2_user.mail, asunto="Invitación a proyecto adoptivo - RUA", cuerpo=cuerpo)

                    print( '10', login_2_user.mail )

                    evento = RuaEvento(
                        login=login_1,
                        evento_detalle=f"Se envío invitación a {login_2} para sumarse al proyecto.",
                        evento_fecha=datetime.now()
                    )
                    db.add(evento)
                    db.commit()

                except Exception as e:
                    return {
                        "success": False,
                        "tipo_mensaje": "naranja",
                        "mensaje": f"⚠️ Error al enviar correo de invitación: {str(e)}",
                        "tiempo_mensaje": 5,
                        "next_page": "actual"
                    }

            print( '11' )

            # Registrar RuaEvento si es monoparental
            if tipo == "Monoparental":
                evento = RuaEvento(
                    login=login_1,
                    evento_detalle="Se creó proyecto adoptivo monoparental.",
                    evento_fecha=datetime.now()
                )
                db.add(evento)


                # 🔔 Notificar a todas las supervisoras
                if tipo == "Monoparental":
                    nombre_completo = f"{nombre_1} {apellido_1}"
                else:
                    nombre_2 = login_2_user.nombre
                    apellido_2 = login_2_user.apellido
                    nombre_completo = f"{nombre_1} {apellido_1} y {nombre_2} {apellido_2}"

                accion = "solicitó" if tipo == "Monoparental" else "solicitaron"

                crear_notificacion_masiva_por_rol(
                    db=db,
                    rol="supervisora",
                    mensaje=f"{nombre_completo} {accion} revisión del proyecto.",
                    link="/menu_supervisoras/detalleProyecto",
                    data_json={"proyecto_id": nuevo.proyecto_id},
                    tipo_mensaje="azul"
                )



            # Registrar historial de estado
            historial = ProyectoHistorialEstado(
                proyecto_id=nuevo.proyecto_id,
                estado_anterior=None,
                estado_nuevo=estado,
                fecha_hora=datetime.now()
            )
            db.add(historial)

            db.commit()

            return {
                "success": True,
                "tipo_mensaje": "verde",
                "mensaje": "Proyecto creado correctamente.",
                "tiempo_mensaje": 4,
                "next_page": "menu_adoptantes/proyecto"
            }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"Error al crear el proyecto avanzado: {str(e)}",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }



@proyectos_router.post("/notificacion/proyecto/mensaje", response_model=dict,
                      dependencies=[Depends(verify_api_key),
                                    Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def notificar_proyecto_mensaje(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    📢 Envía una notificación completa a los pretensos vinculados a un proyecto:
    - Crea notificaciones individuales
    - Registra observaciones internas
    - Cambia estado de proyecto si corresponde
    - Envía correos electrónicos a los pretensos

    ### Ejemplo del JSON esperado:
    ```json
    {
        "proyecto_id": 123,
        "mensaje": "Recordá subir el certificado de salud.",
        "link": "/menu_adoptantes/documentacion",
        "data_json": { "accion": "solicitar_actualizacion_doc" },
        "tipo_mensaje": "naranja"
    }
    """
    proyecto_id = data.get("proyecto_id")
    mensaje = data.get("mensaje")
    link = data.get("link")
    data_json = data.get("data_json") or {}
    tipo_mensaje = data.get("tipo_mensaje", "naranja")
    login_que_observa = current_user["user"]["login"]
    accion = data_json.get("accion")  # puede ser None, "solicitar_actualizacion_doc", "aprobar_documentacion"

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

        logins_destinatarios = [proyecto.login_1]
        if proyecto.login_2:
            logins_destinatarios.append(proyecto.login_2)

        nuevo_estado = None
        if accion == "solicitar_actualizacion_doc":
            nuevo_estado = "actualizando"
        elif accion == "aprobar_documentacion":
            nuevo_estado = "aprobado"

        for login in logins_destinatarios:
            user = db.query(User).filter(User.login == login).first()
            if not user:
                continue

            # Extraer texto plano del mensaje HTML para guardar en base
            mensaje_texto_plano = BeautifulSoup(mensaje, "lxml").get_text(separator=" ", strip=True)

            if not mensaje_texto_plano:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "El mensaje debe tener contenido con información.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }


            # Crear notificación individual
            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=login,
                mensaje=mensaje_texto_plano,
                link=link,
                data_json=data_json,
                tipo_mensaje=tipo_mensaje,
                enviar_por_whatsapp=False,
                login_que_notifico=login_que_observa,
            )
            if not resultado["success"]:
                raise Exception(resultado["mensaje"])

            # Registrar evento
            evento_detalle = f"Notificación a {login} desde proyecto {proyecto_id}: {mensaje_texto_plano[:150]}"
            if nuevo_estado:
                evento_detalle += f" | Estado actualizado: '{nuevo_estado}'"

            db.add(RuaEvento(
                login=login,
                evento_detalle=evento_detalle,
                evento_fecha=datetime.now()
            ))

            # Enviar correo si tiene mail
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
                except Exception as e:
                    db.rollback()
                    return {
                        "success": False,
                        "tipo_mensaje": "naranja",
                        "mensaje": f"⚠️ Error al enviar correo a {user.nombre}: {str(e)}",
                        "tiempo_mensaje": 5,
                        "next_page": "actual"
                    }

        # Aplicar cambio de estado si corresponde
        if nuevo_estado:
            proyecto.doc_proyecto_estado = nuevo_estado

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "✅ Notificación enviada correctamente a los pretensos.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"❌ Error al procesar la notificación: {str(e)}",
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

    - Solo roles 'administrador', 'supervisora' o 'profesional' pueden acceder a esta información.
    """
    try:
        # Verificar existencia del proyecto
        existe_proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
        if not existe_proyecto:
            raise HTTPException(status_code=404, detail="El proyecto indicado no existe.")

        # Contar total de observaciones
        total_observaciones = (
            db.query(func.count(ObservacionesProyectos.observacion_id))
            .filter(ObservacionesProyectos.observacion_a_cual_proyecto == proyecto_id)
            .scalar()
        )

        # Paginación
        offset = (page - 1) * limit
        observaciones = (
            db.query(ObservacionesProyectos)
            .filter(ObservacionesProyectos.observacion_a_cual_proyecto == proyecto_id)
            .order_by(ObservacionesProyectos.observacion_fecha.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        # Obtener todos los logins de quienes observaron
        logins_observadores = [o.login_que_observo for o in observaciones]

        # Obtener nombres y apellidos de esos logins
        usuarios_observadores = (
            db.query(User.login, User.nombre, User.apellido)
            .filter(User.login.in_(logins_observadores))
            .all()
        )
        mapa_observadores = {u.login: {"nombre": u.nombre, "apellido": u.apellido} for u in usuarios_observadores}

        # Armar respuesta
        resultado = []
        for o in observaciones:
            datos_observador = mapa_observadores.get(o.login_que_observo, {"nombre": "", "apellido": ""})
            nombre_completo = f"{datos_observador['nombre']} {datos_observador['apellido']}".strip()
            resultado.append({
                "observacion": o.observacion,
                "fecha": o.observacion_fecha.strftime("%Y-%m-%d %H:%M") if o.observacion_fecha else None,
                "login_que_observo": o.login_que_observo,
                "nombre_completo_que_observo": nombre_completo
            })

        return {
            "page": page,
            "limit": limit,
            "total": total_observaciones,
            "observaciones": resultado
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
    current_user: dict = Depends(get_current_user)
):
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

        # Guardar estado anterior
        estado_anterior = proyecto.estado_general

        # Cambiar estado a "actualizando"
        proyecto.estado_general = "actualizando"
        proyecto.ultimo_cambio_de_estado = datetime.now().date()

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

            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=login_destinatario,
                mensaje=mensaje_html,
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
                    f"Se solicitó actualización del proyecto adoptivo correspondiente a "
                    f"{proyecto.login_1}" +
                    (f" y {proyecto.login_2}" if proyecto.login_2 else "") +
                    f" por parte de {nombre_supervisora}."
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
                                <td style="font-size: 24px; color: #fd7e14;">
                                    <strong>Hola {user.nombre},</strong>
                                </td>
                                </tr>
                                <tr>
                                <td style="padding-top: 20px; font-size: 17px;">
                                    {mensaje_html}
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
        proyecto.ultimo_cambio_de_estado = date.today()

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
    }

    def convertir_a_pdf_y_agregar(nombre, ruta_original):
        if not ruta_original or not os.path.exists(ruta_original):
            return
        ext = os.path.splitext(ruta_original)[1].lower()
        out_pdf = os.path.join(DIR_PDF_GENERADOS, f"{nombre}_{os.path.basename(ruta_original)}.pdf")

        if ext == ".pdf":
            shutil.copy(ruta_original, out_pdf)
        elif ext in [".jpg", ".jpeg", ".png"]:
            Image.open(ruta_original).convert("RGB").save(out_pdf)
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



@proyectos_router.put(
    "/modificar/{proyecto_id}/actualizar-nro-orden",
    dependencies=[
        Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora"]))
    ]
)
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
    Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))
])
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
                       "no_viable", "baja_anulacion"}
    if nuevo_estado not in estados_validos or not observacion:
        raise HTTPException(status_code=400, detail="Datos inválidos")

    proyecto = db.query(Proyecto).get(proyecto_id)
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    estado_anterior = proyecto.estado_general

    # ───────── lógica específica por estado ──────────────────────────
    if nuevo_estado == "viable":
        # Esperamos una lista de subregistros válidos en subregistros[]
        if not subregistros or not isinstance(subregistros, list):
            raise HTTPException(status_code=400,
                                detail="Se requiere lista de subregistros")
        # Limpio todos a "N" y marco los elegidos a "Y"
        pref = "subreg_"          # ej. subreg_1, subreg_FE1…
        for col in [c.name for c in Proyecto.__table__.columns
                    if c.name.startswith(pref)]:
            setattr(proyecto, col, "Y" if col.replace(pref, "") in subregistros else "N")

    elif nuevo_estado == "en_suspenso":
        if not fecha_suspenso:
            raise HTTPException(status_code=400,
                                detail="Debe indicar fecha_suspenso")
        proyecto.fecha_suspenso = fecha_suspenso      # asegúrate de tener la col.

    # (otros estados no necesitan datos extra)

    proyecto.estado_general         = nuevo_estado
    proyecto.ultimo_cambio_de_estado = datetime.now().date()

    # ───────── historial de estados ─────────
    db.add(ProyectoHistorialEstado(
        proyecto_id     = proyecto_id,
        estado_anterior = estado_anterior,
        estado_nuevo    = nuevo_estado,
        fecha_hora      = datetime.now()
    ))

    # ───────── observación interna ──────────
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
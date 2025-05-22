from fastapi import APIRouter, HTTPException, Depends, Query, Request, status, Body, UploadFile, File, Form
from typing import List, Optional, Literal, Tuple
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, case, and_, or_, Integer
import json


from datetime import datetime, date
from models.proyecto import Proyecto, ProyectoHistorialEstado, DetalleEquipoEnProyecto, AgendaEntrevistas, FechaRevision
from models.carpeta import Carpeta, DetalleProyectosEnCarpeta
from models.notif_y_observaciones import ObservacionesProyectos, ObservacionesPretensos, NotificacionesRUA
from models.convocatorias import DetalleProyectoPostulacion
from models.ddjj import DDJJ


# from models.carpeta import DetalleProyectosEnCarpeta
from models.users import User, Group, UserGroup 
from database.config import get_db
from helpers.utils import get_user_name_by_login, construir_subregistro_string, parse_date, generar_codigo_para_link, \
    enviar_mail, get_setting_value
from models.eventos_y_configs import RuaEvento

from security.security import get_current_user, verify_api_key, require_roles
import os, shutil
from dotenv import load_dotenv
from fastapi.responses import FileResponse

import fitz  # PyMuPDF
from PIL import Image
import subprocess


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
                                Depends(require_roles(["administrador", "supervisora", "profesional", "coordinadora"]))])
def get_proyectos(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None, min_length=3, description="Búsqueda por al menos 3 dígitos alfanuméricos"),
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
    # subregistro_1: Optional[bool] = Query(None, description="Filtrar por subregistro_1"),
    # subregistro_2: Optional[bool] = Query(None, description="Filtrar por subregistro_2"),
    # subregistro_3: Optional[bool] = Query(None, description="Filtrar por subregistro_3"),
    # subregistro_4: Optional[bool] = Query(None, description="Filtrar por subregistro_4"),
    # subregistro_5_a: Optional[bool] = Query(None, description="Filtrar por subregistro_5_a"),
    # subregistro_5_b: Optional[bool] = Query(None, description="Filtrar por subregistro_5_b"),
    # subregistro_5_c: Optional[bool] = Query(None, description="Filtrar por subregistro_5_c"),
    # subregistro_6_a: Optional[bool] = Query(None, description="Filtrar por subregistro_6_a"),
    # subregistro_6_b: Optional[bool] = Query(None, description="Filtrar por subregistro_6_b"),
    # subregistro_6_c: Optional[bool] = Query(None, description="Filtrar por subregistro_6_c"),
    # subregistro_6_d: Optional[bool] = Query(None, description="Filtrar por subregistro_6_d"),
    # subregistro_6_2: Optional[bool] = Query(None, description="Filtrar por subregistro_6_2"),
    # subregistro_6_3: Optional[bool] = Query(None, description="Filtrar por subregistro_6_3"),
    # subregistro_6_mas_de_3: Optional[bool] = Query(None, description="Filtrar por subregistro_6_mas_de_3"),
    # subregistro_flexible: Optional[bool] = Query(None, description="Filtrar por subregistro_flexible"),
    # subregistro_otra_provincia: Optional[bool] = Query(None, description="Filtrar por subregistro_otra_provincia"),

    proyecto_estado_general: Optional[str] = Query(None, description="Filtrar por estado general del proyecto"),

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
            .outerjoin(User1, Proyecto.login_1 == User1.login)
            .outerjoin(User2, Proyecto.login_2 == User2.login)
        )

        if fecha_nro_orden_inicio or fecha_nro_orden_fin:
            fecha_nro_orden_inicio = datetime.strptime(fecha_nro_orden_inicio, "%Y-%m-%d") if fecha_nro_orden_inicio else datetime(1970, 1, 1)
            fecha_nro_orden_fin = datetime.strptime(fecha_nro_orden_fin, "%Y-%m-%d") if fecha_nro_orden_fin else datetime.now()

            # Verificar que Proyecto.fecha_asignacion_nro_orden no sea None antes de aplicar between
            query = query.filter(
                Proyecto.fecha_asignacion_nro_orden != None,
                func.str_to_date(Proyecto.fecha_asignacion_nro_orden, "%d/%m/%Y").between(fecha_nro_orden_inicio, fecha_nro_orden_fin)
            )

        if fecha_cambio_estado_inicio or fecha_cambio_estado_fin:
            fecha_cambio_estado_inicio = datetime.strptime(fecha_cambio_estado_inicio, "%Y-%m-%d") if fecha_cambio_estado_inicio else datetime(1970, 1, 1)
            fecha_cambio_estado_fin = datetime.strptime(fecha_cambio_estado_fin, "%Y-%m-%d") if fecha_cambio_estado_fin else datetime.now()

            # Verificar que Proyecto.fecha_asignacion_nro_orden no sea None antes de aplicar between
            query = query.filter(
                Proyecto.ultimo_cambio_de_estado != None,
                func.str_to_date(Proyecto.ultimo_cambio_de_estado, "%d/%m/%Y").between(fecha_cambio_estado_inicio, fecha_cambio_estado_fin)
            )

        # Filtro por tipo de proyecto
        if proyecto_tipo:
            query = query.filter(Proyecto.proyecto_tipo == proyecto_tipo)

        # Filtro por rua, oficio o convocatoria
        if ingreso_por:
            query = query.filter(Proyecto.ingreso_por == ingreso_por)            

        if proyecto_estado_general:
            estados = [estado.strip() for estado in proyecto_estado_general.split(",") if estado.strip()]
            if estados:
                query = query.filter(Proyecto.estado_general.in_(estados))

        # Filtro por nro de orden
        if nro_orden_rua and len(str(nro_orden_rua)) >= 2:
            search_pattern = f"%{nro_orden_rua}%"  # Busca cualquier nro_orden_rua que contenga estos números
            query = query.filter(Proyecto.nro_orden_rua.ilike(search_pattern))

        # # Filtro por subregistros
        # if subregistro_1 is not None:  # Verificamos que no sea None, porque False es un valor válido
        #     query = query.filter(Proyecto.subregistro_1 == ("Y" if subregistro_1 else "N"))
        # if subregistro_2 is not None: 
        #     query = query.filter(Proyecto.subregistro_2 == ("Y" if subregistro_2 else "N"))
        # if subregistro_3 is not None: 
        #     query = query.filter(Proyecto.subregistro_3 == ("Y" if subregistro_3 else "N"))
        # if subregistro_4 is not None: 
        #     query = query.filter(Proyecto.subregistro_4 == ("Y" if subregistro_4 else "N"))
        # if subregistro_5_a is not None: 
        #     query = query.filter(Proyecto.subregistro_5_a == ("Y" if subregistro_5_a else "N"))
        # if subregistro_5_b is not None: 
        #     query = query.filter(Proyecto.subregistro_5_b == ("Y" if subregistro_5_b else "N"))
        # if subregistro_5_c is not None: 
        #     query = query.filter(Proyecto.subregistro_5_c == ("Y" if subregistro_5_c else "N"))
        # if subregistro_6_a is not None: 
        #     query = query.filter(Proyecto.subregistro_6_a == ("Y" if subregistro_6_a else "N"))
        # if subregistro_6_b is not None: 
        #     query = query.filter(Proyecto.subregistro_6_b == ("Y" if subregistro_6_b else "N"))
        # if subregistro_6_c is not None: 
        #     query = query.filter(Proyecto.subregistro_6_c == ("Y" if subregistro_6_c else "N"))
        # if subregistro_6_d is not None: 
        #     query = query.filter(Proyecto.subregistro_6_d == ("Y" if subregistro_6_d else "N"))
        # if subregistro_6_2 is not None: 
        #     query = query.filter(Proyecto.subregistro_6_2 == ("Y" if subregistro_6_2 else "N"))
        # if subregistro_6_3 is not None: 
        #     query = query.filter(Proyecto.subregistro_6_3 == ("Y" if subregistro_6_3 else "N"))
        # if subregistro_6_mas_de_3 is not None: 
        #     query = query.filter(Proyecto.subregistro_6_mas_de_3 == ("Y" if subregistro_6_mas_de_3 else "N"))
        # if subregistro_flexible is not None: 
        #     query = query.filter(Proyecto.subregistro_flexible == ("Y" if subregistro_flexible else "N"))
        # if subregistro_otra_provincia is not None: 
        #     query = query.filter(Proyecto.subregistro_otra_provincia == ("Y" if subregistro_otra_provincia else "N"))


        if login_profesional:
            subq_proyectos = db.query(DetalleEquipoEnProyecto.proyecto_id).filter(
                DetalleEquipoEnProyecto.login == login_profesional
            ).subquery()

            query = query.filter(Proyecto.proyecto_id.in_(subq_proyectos))

        subregistro_field_map = {
            "1": Proyecto.subregistro_1,
            "2": Proyecto.subregistro_2,
            "3": Proyecto.subregistro_3,
            "4": Proyecto.subregistro_4,
            "FE1": Proyecto.flex_edad_1,
            "FE2": Proyecto.flex_edad_2,
            "FE3": Proyecto.flex_edad_3,
            "FE4": Proyecto.flex_edad_4,
            "FET": Proyecto.flex_edad_todos,
            "5A1": Proyecto.discapacidad_1,
            "5A2": Proyecto.discapacidad_2,
            "5A1E1": Proyecto.edad_discapacidad_0,
            "5A1E2": Proyecto.edad_discapacidad_1,
            "5A1E3": Proyecto.edad_discapacidad_2,
            "5A1E4": Proyecto.edad_discapacidad_3,
            "5A1ET": Proyecto.edad_discapacidad_4,
            "F5S": Proyecto.flex_condiciones_salud,
            "F5E1": Proyecto.flex_salud_edad_0,
            "F5E2": Proyecto.flex_salud_edad_1,
            "F5E3": Proyecto.flex_salud_edad_2,
            "F5E4": Proyecto.flex_salud_edad_3,
            "F5ET": Proyecto.flex_salud_edad_4,
            "61": Proyecto.hermanos_comp_1,
            "62": Proyecto.hermanos_comp_2,
            "63": Proyecto.hermanos_comp_3,
            "61E1": Proyecto.hermanos_edad_0,
            "61E2": Proyecto.hermanos_edad_1,
            "61E3": Proyecto.hermanos_edad_2,
            "61ET": Proyecto.hermanos_edad_3,
            "FQ1": Proyecto.flex_hermanos_comp_1,
            "FQ2": Proyecto.flex_hermanos_comp_2,
            "FQ3": Proyecto.flex_hermanos_comp_3,
            "F6E1": Proyecto.flex_hermanos_edad_0,
            "F6E2": Proyecto.flex_hermanos_edad_1,
            "F6E3": Proyecto.flex_hermanos_edad_2,
            "F6E4": Proyecto.flex_hermanos_edad_3,
            "F6ET": Proyecto.flex_hermanos_edad_3,  # O el que corresponda
        }

        if subregistros:
            for sr in subregistros:
                field = subregistro_field_map.get(sr)
                if field is not None:
                    query = query.filter(field == "Y")


        

        if search:
            palabras = search.lower().split()  # divide en palabras
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


            proyecto_dict = {
                "proyecto_id": proyecto.proyecto_id,
                "proyecto_tipo": proyecto.proyecto_tipo,
                "nro_orden_rua": proyecto.nro_orden_rua,

                "subregistro_string": construir_subregistro_string(proyecto),  # Aquí se construye el string concatenado

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

                "ingreso_por": proyecto.ingreso_por,

                # "carpeta_ids": carpeta_ids,  # Lista de carpetas asociadas al proyecto

            }

            
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
                                Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante", "coordinadora"]))])
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

        return proyecto_dict

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar el proyecto: {str(e)}")




@proyectos_router.post("/validar-pretenso", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "adoptante"]))])
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
                  Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
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
                                       Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
                "<p>La solicitud de revisión del proyecto fue enviada correctamente a la supervisión.</p>"
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
                  Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
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

    # Validar extensión
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Extensión de archivo no permitida: {ext}")

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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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

        if not indices_actuales or min(indices_actuales) > max_evaluacion_completada + 1:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Las evaluaciones deben realizarse en orden. No se pueden saltear "
                           "evaluaciones. Deben completarse en secuencia.",
                "tiempo_mensaje": 7,
                "next_page": "actual"
            }

        if sorted(indices_actuales) != list(range(min(indices_actuales), max(indices_actuales)+1)):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Las evaluaciones seleccionadas deben ser consecutivas y estar en orden.",
                "tiempo_mensaje": 7,
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
    final_filename = f"informe_profesionales_{timestamp}{ext}"
    filepath = os.path.join(proyecto_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Actualizar campo en la base
        proyecto.informe_profesionales = filepath

        # 🔎 Obtener login del usuario actual
        login_actual = current_user["user"]["login"]

        # Registrar evento RuaEvento
        evento = RuaEvento(
            login = login_actual,
            evento_detalle = f"Subió el informe profesional al proyecto #{proyecto_id}",
            evento_fecha = datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "message": f"Informe profesional subido correctamente como '{final_filename}'.",
            "path": filepath
        }

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar el archivo: {str(e)}")




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

        if faltantes:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"Debe completar todas las instancias evaluativas antes de solicitar la valoración final. Faltan: {', '.join(faltantes)}.",
                "tiempo_mensaje": 8,
                "next_page": "actual"
            }


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

        # Registrar evento
        login_autor = current_user["user"]["login"]
        evento = RuaEvento(
            login = login_autor,
            evento_detalle = f"Solicitud de valoración final para el proyecto #{proyecto_id}",
            evento_fecha = datetime.now()
        )
        db.add(evento)

        # Notificar a supervisoras
        supervisoras = db.query(User).join(UserGroup, User.login == UserGroup.login)\
            .join(Group, Group.group_id == UserGroup.group_id)\
            .filter(Group.description == "supervisora").all()

        for supervisora in supervisoras:
            resultado = crear_notificacion_individual(
                db = db,
                login_destinatario = supervisora.login,
                mensaje = "📄 Un proyecto fue enviado a supervisión para valoración final.",
                link = "/menu_supervisoras/detalleProyecto",
                data_json = { "proyecto_id": proyecto_id },
                tipo_mensaje = "naranja"
            )
            if not resultado["success"]:
                db.rollback()
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Error al notificar a la supervisora {supervisora.login}",
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


        
@proyectos_router.post("/valoracion/final", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
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

        subregistros_map = {
            "1": "subregistro_1",
            "2": "subregistro_2",
            "3": "subregistro_3",
            "4": "subregistro_4",
            "FE1": "flex_edad_1",
            "FE2": "flex_edad_2",
            "FE3": "flex_edad_3",
            "FE4": "flex_edad_4",
            "FET": "flex_edad_todos",
            "5A1": "discapacidad_1",
            "5A2": "discapacidad_2",
            "5A1E1": "edad_discapacidad_0",
            "5A1E2": "edad_discapacidad_1",
            "5A1E3": "edad_discapacidad_2",
            "5A1E4": "edad_discapacidad_3",
            "5A1ET": "edad_discapacidad_4",
            "F5S": "flex_condiciones_salud",
            "F5E1": "flex_salud_edad_0",
            "F5E2": "flex_salud_edad_1",
            "F5E3": "flex_salud_edad_2",
            "F5E4": "flex_salud_edad_3",
            "F5ET": "flex_salud_edad_4",
            "61": "hermanos_comp_1",
            "62": "hermanos_comp_2",
            "63": "hermanos_comp_3",
            "61E1": "hermanos_edad_0",
            "61E2": "hermanos_edad_1",
            "61E3": "hermanos_edad_2",
            "61ET": "hermanos_edad_3",
            "FQ1": "flex_hermanos_comp_1",
            "FQ2": "flex_hermanos_comp_2",
            "FQ3": "flex_hermanos_comp_3",
            "F6E1": "flex_hermanos_edad_0",
            "F6E2": "flex_hermanos_edad_1",
            "F6E3": "flex_hermanos_edad_2",
            "F6E4": "flex_hermanos_edad_3",
            "F6ET": "flex_hermanos_edad_3"
        }

        if estado_final == "viable":
            # Primero limpiamos todo
            for campo in set(subregistros_map.values()):
                setattr(proyecto, campo, "N")

            # Luego activamos los seleccionados
            for codigo in subregistros_raw:
                campo = subregistros_map.get(codigo)
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






@proyectos_router.get("/entrevista/informe/{proyecto_id}/descargar", response_class = FileResponse,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
def descargar_informe_profesionales(
    proyecto_id: int,
    campo: Literal["informe_profesionales", "doc_informe_vinculacion", "doc_informe_seguimiento_guarda"] = Query(...),
    db: Session = Depends(get_db)
):
    """
    📄 Descarga el informe profesional del proyecto identificado por `proyecto_id`.

    ⚠️ El campo debe haber sido cargado previamente mediante el endpoint de subida.
    """

    proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado")

    # Obtener ruta del informe
    filepath = getattr(proyecto, campo)

    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code = 404, detail = "Informe no encontrado")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )


@proyectos_router.get("/documento/{proyecto_id}/{tipo_documento}/descargar", response_class = FileResponse,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
        provincia = data.get("proyecto_provincia")

        if tipo not in ["Monoparental", "Matrimonio", "Unión convivencial"]:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Tipo de proyecto inválido.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        user1_roles = db.query(UserGroup).filter(UserGroup.login == login_1).all()
        if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in user1_roles):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"El usuario '{login_1}' no tiene el rol 'adoptante'.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }


        aceptado_code = None
        login_2_user = None
        doc_adoptante_curso_aprobado = True
        estado = "en_revision"

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
                    "mensaje": f"El DNI de la peraja debe ser distinto a {login_1}.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            
            login_2_user = db.query(User).filter(User.login == login_2).first()
            if not login_2_user:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El usuario con DNI {login_2} no exite.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            

            login_2_roles = db.query(UserGroup).filter(UserGroup.login == login_2).all()
            if not any(db.query(Group).filter(Group.group_id == r.group_id, Group.description == "adoptante").first() for r in login_2_roles):
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El usuario con DNI {login_2} no tiene el rol 'adoptante'.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            

            aceptado_code = generar_codigo_para_link(16)
            estado = "invitacion_pendiente"

            # Verificar que login_2 no tenga proyecto activo
            if db.query(Proyecto).filter(Proyecto.login_2 == login_2,
                Proyecto.estado_general.in_(["creado", "confeccionando", "en_revision", "actualizando", "aprobado", 
                                             "calendarizando", "entrevistando", "para_valorar", "viable", 
                                             "en_suspenso", "en_carpeta", "vinculacion", "guarda"])).first():
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El usuario con DNI {login_2} ya forma parte de un proyecto activo.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            

            doc_adoptante_curso_aprobado = (getattr(login_2_user, "doc_adoptante_curso_aprobado", "N") == "Y")

        def subreg(k):
            return "Y" if data.get(k) == "Y" else "N"

        nuevo = Proyecto(
            login_1=login_1,
            login_2=login_2,
            
            proyecto_tipo=tipo,
            proyecto_calle_y_nro=data.get("proyecto_calle_y_nro"),
            proyecto_depto_etc=data.get("proyecto_depto_etc"),
            proyecto_barrio=data.get("proyecto_barrio"),
            proyecto_localidad=data.get("proyecto_localidad"),
            proyecto_provincia=provincia,
            ingreso_por="rua",

            subregistro_1=subreg("subregistro_1"),
            subregistro_2=subreg("subregistro_2"),
            subregistro_3=subreg("subregistro_3"),
            subregistro_4=subreg("subregistro_4"),
            
            # Flexibilidad edad
            flex_edad_1=subreg("flex_edad_1"),
            flex_edad_2=subreg("flex_edad_2"),
            flex_edad_3=subreg("flex_edad_3"),
            flex_edad_4=subreg("flex_edad_4"),
            flex_edad_todos=subreg("flex_edad_todos"),

            # Discapacidad
            discapacidad_1=subreg("discapacidad_1"),
            discapacidad_2=subreg("discapacidad_2"),
            edad_discapacidad_0=subreg("edad_discapacidad_0"),
            edad_discapacidad_1=subreg("edad_discapacidad_1"),
            edad_discapacidad_2=subreg("edad_discapacidad_2"),
            edad_discapacidad_3=subreg("edad_discapacidad_3"),
            edad_discapacidad_4=subreg("edad_discapacidad_4"),

            # Enfermedades
            enfermedad_1=subreg("enfermedad_1"),
            enfermedad_2=subreg("enfermedad_2"),
            enfermedad_3=subreg("enfermedad_3"),
            edad_enfermedad_0=subreg("edad_enfermedad_0"),
            edad_enfermedad_1=subreg("edad_enfermedad_1"),
            edad_enfermedad_2=subreg("edad_enfermedad_2"),
            edad_enfermedad_3=subreg("edad_enfermedad_3"),
            edad_enfermedad_4=subreg("edad_enfermedad_4"),

            # Flexibilidad salud
            flex_condiciones_salud=subreg("flex_condiciones_salud"),
            flex_salud_edad_0=subreg("flex_salud_edad_0"),
            flex_salud_edad_1=subreg("flex_salud_edad_1"),
            flex_salud_edad_2=subreg("flex_salud_edad_2"),
            flex_salud_edad_3=subreg("flex_salud_edad_3"),
            flex_salud_edad_4=subreg("flex_salud_edad_4"),

            # Grupo de hermanos
            hermanos_comp_1=subreg("hermanos_comp_1"),
            hermanos_comp_2=subreg("hermanos_comp_2"),
            hermanos_comp_3=subreg("hermanos_comp_3"),
            hermanos_edad_0=subreg("hermanos_edad_0"),
            hermanos_edad_1=subreg("hermanos_edad_1"),
            hermanos_edad_2=subreg("hermanos_edad_2"),
            hermanos_edad_3=subreg("hermanos_edad_3"),
            flex_hermanos_comp_1=subreg("flex_hermanos_comp_1"),
            flex_hermanos_comp_2=subreg("flex_hermanos_comp_2"),
            flex_hermanos_comp_3=subreg("flex_hermanos_comp_3"),
            flex_hermanos_edad_0=subreg("flex_hermanos_edad_0"),
            flex_hermanos_edad_1=subreg("flex_hermanos_edad_1"),
            flex_hermanos_edad_2=subreg("flex_hermanos_edad_2"),
            flex_hermanos_edad_3=subreg("flex_hermanos_edad_3"),


            aceptado="N" if aceptado_code else None,
            aceptado_code=aceptado_code,
            operativo="Y",
            estado_general=estado
        )

        db.add(nuevo)
        db.commit()
        db.refresh(nuevo)

        if aceptado_code:
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
                                    <strong>Registro Único de Adopción (RUA) de Córdoba</strong>
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

        # Registrar RuaEvento si es monoparental
        if tipo == "Monoparental":
            evento = RuaEvento(
                login=login_1,
                evento_detalle="Se creó proyecto adoptivo monoparental.",
                evento_fecha=datetime.now()
            )
            db.add(evento)

            # 🔔 Notificar a todas las supervisoras
            crear_notificacion_masiva_por_rol(
                db=db,
                rol="supervisora",
                mensaje=f"El usuario {login_1} solicitó revisión del proyecto.",
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
                                    Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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

            # Crear notificación individual
            resultado = crear_notificacion_individual(
                db=db,
                login_destinatario=login,
                mensaje=mensaje,
                link=link,
                data_json=data_json,
                tipo_mensaje=tipo_mensaje,
                enviar_por_whatsapp=False,
                login_que_notifico=login_que_observa,
            )
            if not resultado["success"]:
                raise Exception(resultado["mensaje"])

            # Registrar evento
            evento_detalle = f"Notificación a {login} desde proyecto {proyecto_id}: {mensaje[:150]}"
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
                    cuerpo_mensaje_html = f"""
                        <p>Recibiste una notificación del <strong>RUA</strong>:</p>
                        <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                            <em>{mensaje}</em>
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
                                    <strong>Registro Único de Adopción (RUA) de Córdoba</strong>
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
                  Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
                                    Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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




@proyectos_router.post("/aprobar-proyecto", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["supervisora"]))])
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
            "La presentación de tu proyecto adoptivo fue aceptado. Pronto se pondrán en contacto desde el RUA para coordinar las entrevistas."
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
                login=login_destinatario,
                evento_detalle=f"Proyecto aprobado. Notificación enviada a {login_destinatario}.",
                evento_fecha=datetime.now()
            ))


            if user.mail:
                try:
                    cuerpo_mensaje_html = f"""
                        <p>Recibiste una notificación del <strong>RUA</strong>:</p>
                        <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                            <em>La presentación de tu proyecto adoptivo fue aceptado. Pronto se pondrán en contacto con vos para iniciar las entrevistas correspondientes.</em>
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
                                    <strong>Registro Único de Adopción (RUA) de Córdoba</strong>
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def descargar_pdf_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db)
):
    """
    📄 Descarga un PDF unificado con información del proyecto adoptivo y sus documentos adjuntos.
    """

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
    }

    def convertir_a_pdf_y_agregar(nombre, ruta_original):
        if not ruta_original or not os.path.exists(ruta_original):
            return

        ext = os.path.splitext(ruta_original)[1].lower()
        nombre_archivo = f"{nombre}_{os.path.basename(ruta_original)}.pdf"
        out_pdf = os.path.join(DIR_PDF_GENERADOS, nombre_archivo)

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

    for nombre, ruta in documentos_a_incluir.items():
        convertir_a_pdf_y_agregar(nombre, ruta)

    # Crear PDF combinado
    merged = fitz.open()

    portada = merged.new_page(width=595, height=842)
    portada.insert_textbox(
        fitz.Rect(0, 50, portada.rect.width, 100),
        "Proyecto Adoptivo - Detalles Generales",
        fontsize=22,
        fontname="helv",
        align=1
    )

    pretenso_1 = proyecto.usuario_1  # ← accede al User relacionado
    pretenso_2 = proyecto.usuario_2 if proyecto.login_2 else None


    # Domicilio completo
    domicilio = proyecto.proyecto_calle_y_nro or ""
    if proyecto.proyecto_depto_etc:
        domicilio += f", {proyecto.proyecto_depto_etc}"
    if proyecto.proyecto_barrio:
        domicilio += f", {proyecto.proyecto_barrio}"
    if proyecto.proyecto_localidad:
        domicilio += f", {proyecto.proyecto_localidad}"

    # Lista de líneas a insertar
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


    y = 130
    for linea in datos:
        portada.insert_textbox(fitz.Rect(60, y, 530, y + 25), linea, fontsize=14, fontname="helv", align=0)
        y += 30

    portada.draw_line(p1=(60, y), p2=(portada.rect.width - 60, y), color=(0.5, 0.5, 0.5), width=0.8)

    # Agregar documentos adjuntos
    for titulo, path in pdf_paths:
        titulo_page = merged.new_page(width=595, height=842)
        titulo_page.insert_textbox(
            fitz.Rect(0, 280, 595, 320),
            titulo,
            fontsize=20,
            fontname="helv",
            align=1
        )

        if os.path.exists("/app/recursos/imagenes/flecha_hacia_abajo.png"):
            titulo_page.insert_image(fitz.Rect(250, 340, 345, 440), filename="/app/recursos/imagenes/flecha_hacia_abajo.png")

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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "profesional", "supervisora"]))])
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
        Depends(require_roles(["administrador", "supervisora"]))
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

from fastapi import APIRouter, HTTPException, Depends, Query, Request, Body, UploadFile, File, Form

from typing import List, Dict, Optional, Literal, Tuple
from math import ceil
from database.config import SessionLocal
from helpers.utils import check_consecutive_numbers, get_user_name_by_login, \
        build_subregistro_string, parse_date, calculate_age, validar_correo, generar_codigo_para_link, \
        normalizar_y_validar_dni, capitalizar_nombre, normalizar_celular

from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, crear_usuario_en_moodle, get_idcurso, \
    enrolar_usuario, get_idusuario_by_mail, eliminar_usuario_en_moodle, actualizar_usuario_en_moodle, \
    actualizar_clave_en_moodle, is_curso_aprobado

from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol, crear_notificacion_individual



from models.users import User, Group, UserGroup 

from models.proyecto import Proyecto, ProyectoHistorialEstado, AgendaEntrevistas
from models.notif_y_observaciones import ObservacionesPretensos, NotificacionesRUA
from models.ddjj import DDJJ
import hashlib
import time
from datetime import datetime, timedelta, date, time as dt_time

import time


from database.config import get_db  # Importá get_db desde config.py
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import case, func, and_, or_, select, union_all, join, literal_column, desc
from sqlalchemy.sql import literal_column


from models.eventos_y_configs import RuaEvento
from datetime import date, datetime
from security.security import get_current_user, require_roles, verify_api_key, get_password_hash
import os
import re
from dotenv import load_dotenv

import shutil
from fastapi.responses import FileResponse, JSONResponse

from helpers.utils import enviar_mail, get_setting_value, detect_hash_and_verify

import fitz  # PyMuPDF
from PIL import Image
import subprocess
from pathlib import Path


 


# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener y validar la variable
UPLOAD_DIR_DOC_PRETENSOS = os.getenv("UPLOAD_DIR_DOC_PRETENSOS")

if not UPLOAD_DIR_DOC_PRETENSOS:
    raise RuntimeError("La variable de entorno UPLOAD_DIR_DOC_PRETENSOS no está definida. Verificá tu archivo .env")

# Crear la carpeta si no existe
os.makedirs(UPLOAD_DIR_DOC_PRETENSOS, exist_ok=True)


# Obtener y validar la variable
DIR_PDF_GENERADOS = os.getenv("DIR_PDF_GENERADOS")

if not DIR_PDF_GENERADOS:
    raise RuntimeError("La variable de entorno DIR_PDF_GENERADOS no está definida. Verificá tu archivo .env")

# Crear la carpeta si no existe
os.makedirs(DIR_PDF_GENERADOS, exist_ok=True)



users_router = APIRouter()



@users_router.get("/", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervisora", "profesional", "coordinadora"]))])
def get_users(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    group_description: Literal["adoptante", "profesional", "supervisora", "administrador"] = Query(
        None, description="Grupo o rol del usuario"),   

    search: Optional[str] = Query(None, min_length=3, description="Búsqueda por al menos 3 dígitos alfanuméricos"),
    proyecto_tipo: Optional[Literal["Monoparental", "Matrimonio", "Unión convivencial"]] = Query(
        None, description="Filtrar por tipo de proyecto (Monoparental, Matrimonio, Unión convivencial)" ),
    curso_aprobado: Optional[bool] = Query(None, description="Filtrar por curso aprobado"),
    doc_adoptante_estado: Optional[Literal["inicial_cargando", "pedido_revision", 
                                           "actualizando", "aprobado", "rechazado"]] = Query(
                                               None, description="Filtrar por estado de documentación personal"),

    nro_orden_rua: Optional[int] = Query(None, description="Filtrar por número de orden"),

    fecha_alta_inicio: Optional[str] = Query(None, description="Filtrar por fecha de alta de usuario, inicio (AAAA-MM-DD)"),
    fecha_alta_fin: Optional[str] = Query(None, description="Filtrar por fecha de alta de usuario, fin (AAAA-MM-DD)"),
    edad_min: Optional[int] = Query(None, description="Edad mínima edad según fecha de nacimiento en DDJJ"),
    edad_max: Optional[int] = Query(None, description="Edad máxima según fecha de naciminieto en DDJJ"),
    fecha_nro_orden_inicio: Optional[str] = Query(None, 
                    description="Filtrar por fecha de asignación de nro. de orden, inicio (AAAA-MM-DD)"),
    fecha_nro_orden_fin: Optional[str] = Query(None, 
                    description="Filtrar por fecha de asignación de nro. de orden, fin (AAAA-MM-DD)"),
):
    """
    Devuelve los usuarios de sec_users paginados. <br>
    group_description puede ser: adoptante, profesional, supervisora o administrador <br>
    Búsqueda parcial en: name, apellido, login, email, celular, calle_y_nro, barrio, localidad, provincia. <br>
    proyecto_tipo puede ser: Monoparental, Matrimonio, Unión convivencial. <br>
    curso_aprobado puede ser: Y o N <br>
    doc_adoptante_estado puede ser: "inicial_cargando", "pedido_revision", "actualizando", "aprobado", "rechazado" <br>
    nro_orden_rua debe ser un número <br>
    estado_proyecto puede ser: "Inactivo", "Activo", "Entrevistas", "En valoración", "No viable", "En suspenso", "Viable", 
             "En carpeta", "En cancelación", "Cancelado", "Baja definitiva", "Preparando entrevistas", "Adopción definitiva" <br>
    fecha_alta_inicio y fecha_alta_fin es un filtro que considera el campo fecha_alta de la tabla sec_users, solo uno es obligatorio. <br>
    edad_min_en_ddjj y edad_max_en_ddjj es un filtro que considera la edad según el campo ddjj_fecha_nac de la tabla ddjj, 
                solo uno es obligatorio. Si no tiene DDJJ o no está seteada la fecha de nacimiento, no traerá al usuario.<br>


    """
   

    try:

        t0 = time.perf_counter()

        query = (
            db.query(
                User.login.label("login"),
                User.nombre.label("nombre"),
                User.apellido.label("apellido"),
                User.celular.label("celular"),
                User.operativo.label("operativo"),
                User.mail.label("mail"),
                User.calle_y_nro.label("calle_y_nro"),
                User.barrio.label("barrio"),
                User.localidad.label("localidad"),
                User.provincia.label("provincia"),
                User.fecha_nacimiento.label("fecha_nacimiento"),
                User.fecha_alta.label("fecha_alta"),
                User.doc_adoptante_curso_aprobado.label("doc_adoptante_curso_aprobado"),
                User.doc_adoptante_estado.label("doc_adoptante_estado"),
                User.doc_adoptante_ddjj_firmada.label("doc_adoptante_ddjj_firmada"),

                Group.description.label("group"),

                DDJJ.ddjj_fecha_nac.label("ddjj_fecha_nac"),
                DDJJ.ddjj_calle.label("ddjj_calle"),
                DDJJ.ddjj_calle_legal.label("ddjj_calle_legal"),
                DDJJ.ddjj_barrio.label("ddjj_barrio"),
                DDJJ.ddjj_barrio_legal.label("ddjj_barrio_legal"),
                DDJJ.ddjj_localidad.label("ddjj_localidad"),
                DDJJ.ddjj_localidad_legal.label("ddjj_localidad_legal"),
                DDJJ.ddjj_cp.label("ddjj_cp"),
                DDJJ.ddjj_cp_legal.label("ddjj_cp_legal"),
                DDJJ.ddjj_provincia.label("ddjj_provincia"),
                DDJJ.ddjj_provincia_legal.label("ddjj_provincia_legal"),
                
                Proyecto.proyecto_id.label("proyecto_id"),
                Proyecto.proyecto_tipo.label("proyecto_tipo"),
                Proyecto.nro_orden_rua.label("nro_orden_rua"),
                Proyecto.ingreso_por.label("ingreso_por"),
                Proyecto.operativo.label("proyecto_operativo"),
                Proyecto.login_1.label("login_1"),
                Proyecto.login_2.label("login_2"),      

                Proyecto.proyecto_calle_y_nro.label("proyecto_calle_y_nro"),
                Proyecto.proyecto_barrio.label("proyecto_barrio"),
                Proyecto.proyecto_localidad.label("proyecto_localidad"),
                Proyecto.proyecto_provincia.label("proyecto_provincia"),
                Proyecto.fecha_asignacion_nro_orden.label("fecha_asignacion_nro_orden"),
                Proyecto.ultimo_cambio_de_estado.label("ultimo_cambio_de_estado"),

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
                Proyecto.estado_general.label("estado_general"),

            )
            # El .join se usa para traer los usuarios solo si existe en ambas tablas, sino no trae los usuarios
            .join(UserGroup, User.login == UserGroup.login) 
            .join(Group, UserGroup.group_id == Group.group_id)
            # El .outerjoin se usa para traer los usuarios existan o no en la segunda tabla, si no existe, trae los campos en null
            # Esto es porque puede que existan usuarios que aún no tengan DDJJ, o Proyecto, etc.
            .outerjoin(DDJJ, User.login == DDJJ.login)
            .outerjoin(Proyecto, (User.login == Proyecto.login_1) | (User.login == Proyecto.login_2))
        )

        t1 = time.perf_counter()
        print(f"Tiempo para construir la consulta: {t1 - t0:.4f} segundos")

        # Aplicar el filtro de fechas si fecha_alta_inicio y fecha_alta_fin fueron seteadas
        if fecha_alta_inicio or fecha_alta_fin:
            # Validar y ajustar fechas
            fecha_alta_inicio = datetime.strptime(fecha_alta_inicio, "%Y-%m-%d") if fecha_alta_inicio else datetime(1970, 1, 1)
            fecha_alta_fin = datetime.strptime(fecha_alta_fin, "%Y-%m-%d") if fecha_alta_fin else datetime.now()

            # Filtro por rango de fechas en fecha_alta
            query = query.filter(User.fecha_alta.between(fecha_alta_inicio, fecha_alta_fin))


        if edad_min is not None or edad_max is not None:
            hoy = datetime.now().date()
            edad_min = edad_min if edad_min is not None else 0
            edad_max = edad_max if edad_max is not None else 120

            fecha_nac_max = hoy.replace(year=hoy.year - edad_min)
            fecha_nac_min = hoy.replace(year=hoy.year - edad_max - 1) + timedelta(days=1)

            # Usamos COALESCE para tomar la fecha de nacimiento desde ddjj o sec_users
            fecha_nac_coalescida = func.coalesce(DDJJ.ddjj_fecha_nac, User.fecha_nacimiento)

            query = query.filter(
                fecha_nac_coalescida != None,  # Solo usuarios que tengan alguna fecha
                fecha_nac_coalescida.between(fecha_nac_min.strftime("%Y-%m-%d"), fecha_nac_max.strftime("%Y-%m-%d"))
            )


        if fecha_nro_orden_inicio or fecha_nro_orden_fin:
            fecha_nro_orden_inicio = datetime.strptime(fecha_nro_orden_inicio, "%Y-%m-%d") if fecha_nro_orden_inicio else datetime(1970, 1, 1)
            fecha_nro_orden_fin = datetime.strptime(fecha_nro_orden_fin, "%Y-%m-%d") if fecha_nro_orden_fin else datetime.now()

            # Verificar que Proyecto.fecha_asignacion_nro_orden no sea None antes de aplicar between
            query = query.filter(
                Proyecto.fecha_asignacion_nro_orden != None,
                func.str_to_date(Proyecto.fecha_asignacion_nro_orden, "%d/%m/%Y").between(fecha_nro_orden_inicio, fecha_nro_orden_fin)
            )

        # Filtro por descripción de grupo
        if group_description:
            query = query.filter(Group.description == group_description)

        # Filtro por tipo de proyecto
        if proyecto_tipo:
            query = query.filter(Proyecto.proyecto_tipo == proyecto_tipo)


        # Filtro por curso aprobado
        if curso_aprobado is not None:  # Verificamos que no sea None, porque False es un valor válido
            query = query.filter(User.doc_adoptante_curso_aprobado == ("Y" if curso_aprobado else "N"))

        # Filtro por estado de documentación personal
        if doc_adoptante_estado:
            query = query.filter(User.doc_adoptante_estado == doc_adoptante_estado)

        # Filtro por nro de orden
        if nro_orden_rua:
            query = query.filter(Proyecto.nro_orden_rua == nro_orden_rua)    

    
        if search:
            palabras = search.lower().split()  # divide en palabras
            condiciones_por_palabra = []

            for palabra in palabras:
                condiciones_por_palabra.append(
                    or_(
                        func.lower(func.concat(User.nombre, " ", User.apellido)).ilike(f"%{palabra}%"),
                        User.login.ilike(f"%{palabra}%"),
                        User.mail.ilike(f"%{palabra}%"),
                        User.calle_y_nro.ilike(f"%{palabra}%"),
                        User.barrio.ilike(f"%{palabra}%"),
                        User.localidad.ilike(f"%{palabra}%")
                    )
                )

            # Todas las palabras deben coincidir en algún campo (AND entre ORs)
            query = query.filter(and_(*condiciones_por_palabra))


        # Paginación sin count(): se solicita (limit + 1) registros
        skip = (page - 1) * limit
        t_query_start = time.perf_counter()
        users = query.offset(skip).limit(limit + 1).all()
        t_query_end = time.perf_counter()
        print(f"Tiempo para obtener datos paginados: {t_query_end - t_query_start:.4f} segundos")

        # Determinar si existe página siguiente
        has_next = len(users) > limit
        if has_next:
            users = users[:limit]

        # Procesamiento de resultados
        t_process_start = time.perf_counter()

        valid_states = {"inicial_cargando", "pedido_revision", "actualizando", "aprobado", "rechazado"}
        valid_proyecto_tipos = {"Monoparental", "Matrimonio", "Unión convivencial"}
        valid_doc_proyecto_states = {"inicial_cargando", "pedido_valoracion", "actualizando", "aprobado", "en_valoracion", "baja_definitiva"}


        users_list = []

        for user in users:

            # Subconsulta para contar proyectos no definitivos por usuario (login_1 o login_2), según estado_general
            proyectos_no_definitivos_subquery = (
                db.query(
                    func.coalesce(Proyecto.login_1, Proyecto.login_2).label("login"),
                    func.sum(
                        case(
                            (
                                Proyecto.estado_general.notin_(["baja_definitiva", "adopcion_definitiva"]),
                                1
                            ),
                            else_=0
                        )
                    ).label("proyectos_no_definitivos")
                )
                .filter(
                    or_(
                        Proyecto.login_1 == user.login,
                        Proyecto.login_2 == user.login
                    )
                )
                .group_by(func.coalesce(Proyecto.login_1, Proyecto.login_2))
                .subquery("proyectos_no_definitivos_subquery")
            )

            # Ejecutar la subconsulta para obtener el número de proyectos no definitivos
            result_proyectos_no_definitivos = db.execute(
                select(proyectos_no_definitivos_subquery.c.proyectos_no_definitivos)
            ).fetchone()

            # Obtener el resultado o asignar 0 si no hay proyectos
            proyectos_no_definitivos = result_proyectos_no_definitivos[0] if result_proyectos_no_definitivos else 0  


            proyectos_ids_subquery = (
                db.query(Proyecto.proyecto_id)
                .filter(or_(
                    Proyecto.login_1 == user.login,
                    Proyecto.login_2 == user.login
                ))
                .all()
            )

            # Obtener solo los valores de `proyecto_id` en una lista
            proyectos_ids = [proyecto[0] for proyecto in proyectos_ids_subquery] if proyectos_ids_subquery else []

            estado_general_row = db.query(
                Proyecto.estado_general,
                func.str_to_date(Proyecto.fecha_asignacion_nro_orden, "%d/%m/%Y").label("fecha_orden")
            ).filter(
                or_(Proyecto.login_1 == user.login, Proyecto.login_2 == user.login),
                Proyecto.fecha_asignacion_nro_orden != None
            ).order_by(desc("fecha_orden")).first()

            estado_general = estado_general_row.estado_general if estado_general_row else ""



            # Verificar si la lista está vacía y asignar un valor por defecto
            if not proyectos_ids:
                proyectos_ids = []

            # Determinar fecha de nacimiento y edad según prioridad
            fecha_nacimiento = user.ddjj_fecha_nac or user.fecha_nacimiento
            fecha_nacimiento_str = parse_date(fecha_nacimiento) if fecha_nacimiento else ""

            if fecha_nacimiento:
                if isinstance(fecha_nacimiento, (datetime, date)):
                    edad = calculate_age(fecha_nacimiento.strftime("%Y-%m-%d"))
                else:
                    edad = calculate_age(str(fecha_nacimiento))
            else:
                edad = ""

            MAPA_ESTADOS_PROYECTO = {
                "sin_curso": "Curso pendiente",
                "ddjj_pendiente": "DDJJ pendiente",
                "inicial_cargando": "Doc. inicial",
                "pedido_revision": "Doc. en revisión",
                "actualizando": "Actualizando doc.",
                "aprobado": "Doc. aprobada",
                "rechazado": "Doc. rechazada",

                "invitacion_pendiente": "Invit. pendiente",
                "confeccionando": "Confeccionando",
                "en_revision": "Proy. en revisión",
                "calendarizando": "Agendando entrev.",
                "entrevistando": "Entrev. en curso",
                "para_valorar": "En valoración.",
                "viable": "Viable",
                "viable_no_disponible": "Viable no disp.",
                "en_suspenso": "En suspenso",
                "no_viable": "No viable",
                "en_carpeta": "En carpeta",
                "vinculacion": "Vinculación",
                "guarda": "Guarda",
                "adopcion_definitiva": "Adopción def.",
                "baja_anulacion": "Baja anulación",
                "baja_caducidad": "Baja caducidad",
                "baja_por_convocatoria": "Baja conv.",
                "baja_rechazo_invitacion": "Baja por rechazo"
                
            }

            # Determinar el estado en bruto
            estado_raw = (
                user.estado_general if user.estado_general else (
                    "sin_curso" if not user.doc_adoptante_curso_aprobado or user.doc_adoptante_curso_aprobado != "Y"
                    else "ddjj_pendiente" if not user.doc_adoptante_ddjj_firmada or user.doc_adoptante_ddjj_firmada != "Y"
                    else user.doc_adoptante_estado if user.doc_adoptante_estado in valid_states
                    else ""
                )
            )


            user_dict = {
                "login": user.login,
                "nombre": user.nombre if user.nombre else "",
                "apellido": user.apellido if user.apellido else "",
                "celular": user.celular if user.celular else "",
                "operativo": user.operativo if user.operativo else "N",
                "mail": user.mail if user.mail else "",
                # Prioridad: proyecto > ddjj_legal > ddjj > sec_users > ""
                "calle_y_nro": (
                    user.proyecto_calle_y_nro if user.proyecto_calle_y_nro else
                    user.ddjj_calle_legal if user.ddjj_calle_legal else
                    user.ddjj_calle if user.ddjj_calle else
                    user.calle_y_nro if user.calle_y_nro else ""
                ),
                "barrio": (
                    user.proyecto_barrio if user.proyecto_barrio else
                    user.ddjj_barrio_legal if user.ddjj_barrio_legal else
                    user.ddjj_barrio if user.ddjj_barrio else
                    user.barrio if user.barrio else ""
                ),
                "localidad": (
                    user.proyecto_localidad if user.proyecto_localidad else
                    user.ddjj_localidad_legal if user.ddjj_localidad_legal else
                    user.ddjj_localidad if user.ddjj_localidad else
                    user.localidad if user.localidad else ""
                ),
                "provincia": (
                    user.proyecto_provincia if user.proyecto_provincia else
                    user.ddjj_provincia_legal if user.ddjj_provincia_legal else
                    user.ddjj_provincia if user.ddjj_provincia else
                    user.provincia if user.provincia else ""
                ),
                "cp": (
                    user.ddjj_cp_legal if user.ddjj_cp_legal else
                    user.ddjj_cp if user.ddjj_cp else ""
                ),
                "fecha_alta": parse_date(user.fecha_alta),
                "group": user.group if user.group else "Sin rol asignado",
                "fecha_nacimiento": fecha_nacimiento_str,
                "edad": edad,
                "doc_adoptante_curso_aprobado": user.doc_adoptante_curso_aprobado == "Y",
                "doc_adoptante_estado": user.doc_adoptante_estado if user.doc_adoptante_estado in valid_states else "",

                "proyecto_id": user.proyecto_id,
                "proyecto_tipo": user.proyecto_tipo if user.proyecto_tipo in valid_proyecto_tipos else "",
                "nro_orden_rua": user.nro_orden_rua if user.nro_orden_rua else "",
                "ingreso_por": user.ingreso_por if user.ingreso_por else "",
                "proyecto_operativo": user.proyecto_operativo == "Y",
                "login_1_info": get_user_name_by_login(db, user.login_1),
                "login_2_info": get_user_name_by_login(db, user.login_2),
                "fecha_asignacion_nro_orden": parse_date(user.fecha_asignacion_nro_orden),
                "ultimo_cambio_de_estado": parse_date(user.ultimo_cambio_de_estado),

                "subregistro_string": build_subregistro_string(user),  # Aquí se construye el string concatenado

                "proyectos_no_definitivos": proyectos_no_definitivos,

                # "proyecto_estado_general": user.estado_general if user.estado_general else "",
                "proyecto_estado_general": MAPA_ESTADOS_PROYECTO.get(estado_raw, estado_raw),

                "proyectos_ids": proyectos_ids  # Aquí agregamos la lista de proyecto_id

                


            }
            users_list.append(user_dict)

        t_process_end = time.perf_counter()
        print(f"Tiempo para procesar los datos: {t_process_end - t_process_start:.4f} segundos")

        t_total = time.perf_counter()
        print(f"Tiempo total de la consulta: {t_total - t0:.4f} segundos")

        return {
            "page": page,
            "limit": limit,
            "has_next": has_next,
            "users": users_list,
        }


    except SQLAlchemyError as e:
        print(str(e))  # Esto imprime el error en los logs
        raise HTTPException(status_code=500, detail=f"Error al recuperar los usuarios: {str(e)}")



@users_router.get("/{login}", response_model=dict,
    dependencies=[Depends(verify_api_key)])
def get_user_by_login(
    login: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Devuelve un único usuario por su `login`.

    - Usuarios con rol 'administrador', 'supervisora', o 'profesional' pueden ver a todos los usuarios.
    - Usuarios con rol 'adoptante' solo pueden ver su propio usuario.
    """

    usuario_actual_login = current_user["user"]["login"]

    # Obtener los roles del usuario actual desde la base de datos
    roles = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == usuario_actual_login)
        .all()
    )
    roles = [r.description for r in roles]

    # Permitir siempre si es Admin, supervisora o profesional
    if any(r in ["administrador", "supervisora", "profesional", "coordinadora"] for r in roles):
        pass  # acceso completo

    # Si es adoptante, permitir solo si consulta su propio login
    elif "adoptante" in roles:
        if login != usuario_actual_login:
            raise HTTPException(status_code=403, detail="No tiene permiso para ver a otros usuariosss.")
    else:
        raise HTTPException(status_code=403, detail="No tiene permisos para acceder a este recurso.")


    try:
        query = (
            db.query(
                User.login.label("login"),
                User.nombre.label("nombre"),
                User.apellido.label("apellido"),
                User.celular.label("celular"),
                User.operativo.label("operativo"),
                User.mail.label("mail"),
                User.calle_y_nro.label("calle_y_nro"),
                User.depto_etc.label("depto_etc"),                
                User.barrio.label("barrio"),
                User.localidad.label("localidad"),
                User.provincia.label("provincia"),
                User.fecha_alta.label("fecha_alta"),
                User.doc_adoptante_curso_aprobado.label("doc_adoptante_curso_aprobado"),
                User.doc_adoptante_estado.label("doc_adoptante_estado"),
                User.doc_adoptante_ddjj_firmada.label("doc_adoptante_ddjj_firmada"),
                User.doc_adoptante_domicilio.label("doc_adoptante_domicilio"),
                User.doc_adoptante_dni_frente.label("doc_adoptante_dni_frente"),
                User.doc_adoptante_dni_dorso.label("doc_adoptante_dni_dorso"),
                User.doc_adoptante_deudores_alimentarios.label("doc_adoptante_deudores_alimentarios"),
                User.doc_adoptante_antecedentes.label("doc_adoptante_antecedentes"),
                User.doc_adoptante_migraciones.label("doc_adoptante_migraciones"),
                User.doc_adoptante_salud.label("doc_adoptante_salud"),

                Group.description.label("group"),

                DDJJ.login.label("login_ddjj"),
                DDJJ.ddjj_fecha_nac.label("ddjj_fecha_nac"),
                DDJJ.ddjj_calle.label("ddjj_calle"),
                DDJJ.ddjj_calle_legal.label("ddjj_calle_legal"),
                DDJJ.ddjj_barrio.label("ddjj_barrio"),
                DDJJ.ddjj_barrio_legal.label("ddjj_barrio_legal"),
                DDJJ.ddjj_localidad.label("ddjj_localidad"),
                DDJJ.ddjj_localidad_legal.label("ddjj_localidad_legal"),
                DDJJ.ddjj_cp.label("ddjj_cp"),
                DDJJ.ddjj_cp_legal.label("ddjj_cp_legal"),
                DDJJ.ddjj_provincia.label("ddjj_provincia"),
                DDJJ.ddjj_provincia_legal.label("ddjj_provincia_legal"),
                DDJJ.ddjj_acepto_1.label("ddjj_acepto_1"),
                DDJJ.ddjj_acepto_2.label("ddjj_acepto_2"),
                DDJJ.ddjj_acepto_3.label("ddjj_acepto_3"),
                DDJJ.ddjj_acepto_4.label("ddjj_acepto_4"),

                Proyecto.proyecto_id.label("proyecto_id"),
                Proyecto.proyecto_tipo.label("proyecto_tipo"),
                Proyecto.nro_orden_rua.label("nro_orden_rua"),
                Proyecto.ingreso_por.label("ingreso_por"),
                Proyecto.operativo.label("proyecto_operativo"),
                Proyecto.login_1.label("login_1"),
                Proyecto.login_2.label("login_2"),                
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
                Proyecto.proyecto_barrio.label("proyecto_barrio"),
                Proyecto.proyecto_localidad.label("proyecto_localidad"),
                Proyecto.proyecto_provincia.label("proyecto_provincia"),
                Proyecto.fecha_asignacion_nro_orden.label("fecha_asignacion_nro_orden"),
                Proyecto.ultimo_cambio_de_estado.label("ultimo_cambio_de_estado"),

                Proyecto.estado_general.label("estado_general"),
                
            )
            # El .join se usa para traer los usuarios solo si existe en ambas tablas, sino no trae los usuarios
            .join(UserGroup, User.login == UserGroup.login) 
            .join(Group, UserGroup.group_id == Group.group_id)
            # El .outerjoin se usa para traer los usuarios existan o no en la segunda tabla, si no existe, trae los campos en null
            # Esto es porque puede que existan usuarios que aún no tengan DDJJ, o Proyecto, etc.
            .outerjoin(DDJJ, User.login == DDJJ.login)
            .outerjoin(Proyecto, (User.login == Proyecto.login_1) | (User.login == Proyecto.login_2))
            .filter(User.login == login)
        )
           
        user = query.first()

        if not user:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        # Traer instancia completa de la DDJJ si existe
        ddjj = db.query(DDJJ).filter(DDJJ.login == user.login).first()


        proyectos_ids = (
            db.query(Proyecto.proyecto_id)
            .filter(or_(Proyecto.login_1 == user.login, Proyecto.login_2 == user.login))
            .all()
        )
        proyectos_ids = [proyecto[0] for proyecto in proyectos_ids] if proyectos_ids else []

        valid_states = {"inicial_cargando", "pedido_revision", "actualizando", "aprobado", "rechazado"}
        valid_proyecto_tipos = {"Monoparental", "Matrimonio", "Unión convivencial"}
        valid_doc_proyecto_states = {"inicial_cargando", "pedido_valoracion", "actualizando",
                                     "aprobado", "en_valoracion", "baja_definitiva"}

        pendientes = {}

        
        if user.group == 'supervisora' :
            # Obtener pendientes de supervisora
            pendientes_doc_adoptante = db.query(User).filter(User.doc_adoptante_estado == "pedido_revision").count()

            pendientes_doc_proyecto = db.query(Proyecto).filter(
                Proyecto.estado_general == "en_revision"
            ).count()

            pendientes_proyectos_en_entrevistas = db.query(Proyecto).filter(
                Proyecto.estado_general.in_(["calendarizando", "entrevistando"])
            ).count()

            pendientes_proyectos_en_valoracion = db.query(Proyecto).filter(
                Proyecto.estado_general == "en_valoracion"
            ).count()


            pendientes = {
                "doc_adoptante": pendientes_doc_adoptante,
                "doc_proyecto": pendientes_doc_proyecto,
                "proyectos_en_entrevistas": pendientes_proyectos_en_entrevistas,
                "proyectos_en_valoracion": pendientes_proyectos_en_valoracion,
            }

        
        docs_de_pretenso_presentados = all([
            user.doc_adoptante_salud,
            user.doc_adoptante_domicilio,
            user.doc_adoptante_dni_frente,
            user.doc_adoptante_dni_dorso,
            user.doc_adoptante_deudores_alimentarios,
            user.doc_adoptante_antecedentes,
        ])

        docs_todos_vacios = all([
            not user.doc_adoptante_salud,
            not user.doc_adoptante_domicilio,
            not user.doc_adoptante_dni_frente,
            not user.doc_adoptante_dni_dorso,
            not user.doc_adoptante_deudores_alimentarios,
            not user.doc_adoptante_antecedentes,
        ])

        # 🔤 Texto del botón de estado del pretenso
        if user.doc_adoptante_curso_aprobado == "N":
            texto_boton_estado_pretenso = "CURSO PENDIENTE"
        elif user.doc_adoptante_curso_aprobado == "Y" and user.doc_adoptante_ddjj_firmada == "N":
            texto_boton_estado_pretenso = "LLENANDO DDJJ"
        elif user.doc_adoptante_curso_aprobado == "Y" and \
             user.doc_adoptante_ddjj_firmada == "Y" and docs_todos_vacios:
            texto_boton_estado_pretenso = "DDJJ FIRMADA"
        else:
            estado_a_texto = {
                "inicial_cargando": "DOC. PERSONAL", 
                "pedido_revision": "REVISIÓN DE DOC.", 
                "rechazado": "RECHAZADO",
                "invitacion_pendiente": "INVITACIÓN PENDIENTE",
                "confeccionando": "DOC. PROYECTO",
                "en_revision": "EN REVISIÓN",
                "actualizando": "ACTUALIZANDO DOC.",
                "aprobado": "DOC. APROBADA",
                "calendarizando": "CALENDARIZANDO",
                "entrevistando": "ENTREVISTANDO",
                "para_valorar": "PENDIENTE DE VALORACIÓN",
                "viable": "VIABLE",
                "viable_no_disponible": "VIABLE / NO DISPONIBLE",
                "en_suspenso": "EN SUSPENSO",
                "no_viable": "NO VIABLE",
                "en_carpeta": "EN CARPETA",
                "vinculacion": "EN VINCULACIÓN",
                "guarda": "GUARDA",
                "adopcion_definitiva": "ADOPCIÓN DEFINITIVA",
                "baja_anulacion": "BAJA - ANULACIÓN",
                "baja_caducidad": "BAJA - CADUCIDAD",
                "baja_por_convocatoria": "BAJA - CONVOCATORIA",
                "baja_rechazo_invitacion": "BAJA - RECHAZO INVITACIÓN"
            }

            if not user.proyecto_id:
                texto_boton_estado_pretenso = estado_a_texto.get(user.doc_adoptante_estado, "DESCONOCIDO")
            else:
                texto_boton_estado_pretenso = estado_a_texto.get(user.estado_general, "DESCONOCIDO")

            # texto_boton_estado_pretenso = estado_a_texto.get(user.estado_general, "DESCONOCIDO")



        # Evaluar qué secciones de la DDJJ tienen datos cargados
        def tiene_datos(ddjj, campos):
            return any(getattr(ddjj, campo, None) for campo in campos) if ddjj else False

        ddjj_checks = {
            "ddjj_datos_personales": tiene_datos(ddjj, [
                "ddjj_nombre", "ddjj_apellido", "ddjj_estado_civil",
                "ddjj_fecha_nac", "ddjj_nacionalidad", "ddjj_sexo",
                "ddjj_correo_electronico", "ddjj_telefono"
            ]),
            "ddjj_grupo_familiar_hijos": tiene_datos(ddjj, [f"ddjj_hijo{i}_nombre_completo" for i in range(1, 6)]),
            "ddjj_grupo_familiar_otros": tiene_datos(ddjj, [f"ddjj_otro{i}_nombre_completo" for i in range(1, 6)]),
            "ddjj_red_de_apoyo": tiene_datos(ddjj, [f"ddjj_apoyo{i}_nombre_completo" for i in range(1, 3)]),
            "ddjj_informacion_laboral": tiene_datos(ddjj, ["ddjj_ocupacion", "ddjj_horas_semanales", "ddjj_ingreso_mensual"]),
            "ddjj_procesos_judiciales": tiene_datos(ddjj, ["ddjj_causa_penal", "ddjj_juicios_filiacion", "ddjj_denunciado_violencia_familiar"]),
            "ddjj_disponibilidad_adoptiva": tiene_datos(ddjj, ["ddjj_subregistro_1", "ddjj_subregistro_2", "ddjj_subregistro_3"]),
            "ddjj_tramo_final": tiene_datos(ddjj, ["ddjj_guardo_1", "ddjj_guardo_2"]),
        }


        user_dict = {
            "login": user.login,
            "nombre": user.nombre if user.nombre else "",
            "apellido": user.apellido if user.apellido else "",
            "celular": user.celular if user.celular else "",
            "operativo": user.operativo if user.operativo else "N",
            "mail": user.mail if user.mail else "",
            # Prioridad: proyecto > ddjj_legal > ddjj > sec_users > ""
            "calle_y_nro": user.calle_y_nro if user.calle_y_nro else "",
            "depto_etc": user.depto_etc if user.depto_etc else "",
            "barrio": user.barrio if user.barrio else "",
            "localidad": user.localidad if user.localidad else "",
            "provincia": user.provincia if user.provincia else "",
            "cp": (
                user.ddjj_cp_legal if user.ddjj_cp_legal else
                user.ddjj_cp if user.ddjj_cp else ""
            ),
            "fecha_alta": parse_date(user.fecha_alta),
            "group": user.group if user.group else "Sin grupo asignado",
            "ddjj_fecha_nac": parse_date(user.ddjj_fecha_nac),
            "edad_segun_ddjj": calculate_age(user.ddjj_fecha_nac),
            "doc_adoptante_curso_aprobado": user.doc_adoptante_curso_aprobado == "Y",
            "doc_adoptante_ddjj_firmada": user.doc_adoptante_ddjj_firmada == "Y",
            "doc_adoptante_estado": user.doc_adoptante_estado if user.doc_adoptante_estado in valid_states else "desconocido",

            "doc_adoptante_salud": user.doc_adoptante_salud,
            "doc_adoptante_domicilio": user.doc_adoptante_domicilio,
            "doc_adoptante_dni_frente": user.doc_adoptante_dni_frente,
            "doc_adoptante_dni_dorso": user.doc_adoptante_dni_dorso,
            "doc_adoptante_deudores_alimentarios": user.doc_adoptante_deudores_alimentarios,
            "doc_adoptante_antecedentes": user.doc_adoptante_antecedentes,
            "doc_adoptante_migraciones": user.doc_adoptante_migraciones,
            

            "proyecto_id": user.proyecto_id,
            "proyecto_tipo": user.proyecto_tipo if user.proyecto_tipo in valid_proyecto_tipos else "desconocido",
            "nro_orden_rua": user.nro_orden_rua if user.nro_orden_rua else "",
            "ingreso_por": user.ingreso_por if user.ingreso_por else "",
            "proyecto_operativo": user.proyecto_operativo == "Y",
            "subregistro_string": build_subregistro_string(user),  # Aquí se construye el string concatenado
            "login_1_info": get_user_name_by_login(db, user.login_1),
            "login_2_info": get_user_name_by_login(db, user.login_2),
            "fecha_asignacion_nro_orden": parse_date(user.fecha_asignacion_nro_orden),
            "ultimo_cambio_de_estado": parse_date(user.ultimo_cambio_de_estado),

            "proyectos_ids": proyectos_ids,  # Aquí agregamos la lista de proyecto_id

            "pendientes": pendientes,

            "mostrar_datos_de_ddjj": bool(user.login_ddjj),
            "ddjj_datos_personales": ddjj_checks["ddjj_datos_personales"],
            "ddjj_grupo_familiar_hijos": ddjj_checks["ddjj_grupo_familiar_hijos"],
            "ddjj_grupo_familiar_otros": ddjj_checks["ddjj_grupo_familiar_otros"],
            "ddjj_red_de_apoyo": ddjj_checks["ddjj_red_de_apoyo"],
            "ddjj_informacion_laboral": ddjj_checks["ddjj_informacion_laboral"],
            "ddjj_procesos_judiciales": ddjj_checks["ddjj_procesos_judiciales"],
            "ddjj_disponibilidad_adoptiva": ddjj_checks["ddjj_disponibilidad_adoptiva"],
            "ddjj_tramo_final": ddjj_checks["ddjj_tramo_final"],
            "ddjj_firmada": bool(user.ddjj_acepto_1 and user.ddjj_acepto_2 and user.ddjj_acepto_3 and user.ddjj_acepto_4),

            "mostrar_boton_proyecto": bool(user.proyecto_id),

            "boton_aprobar_documentacion": docs_de_pretenso_presentados and user.doc_adoptante_estado == "pedido_revision",
            "boton_solicitar_actualizacion": docs_de_pretenso_presentados and ( 
                user.doc_adoptante_estado == "pedido_revision" or user.doc_adoptante_estado == "aprobado" ),

            "boton_ver_proyecto": bool(user.proyecto_id),

            "texto_boton_estado_pretenso": texto_boton_estado_pretenso,

        }

        return user_dict

    except SQLAlchemyError as e:
        print(str(e))
        raise HTTPException(status_code=500, detail=f"Error al recuperar el usuario: {str(e)}")




@users_router.post("/", response_model=dict, dependencies=[Depends( verify_api_key )])
def create_user(user: dict = Body(...), db: Session = Depends(get_db)):
    """
    📌 **Crea un nuevo usuario y asigna su grupo.**

    ### 📝 Campos requeridos en el JSON:

    - **dni**: `str`  
    (Se usará como login)

    - **clave**: `str`  
    (Contraseña numérica de al menos 6 dígitos)

    - **confirm_clave**: `str`  
    (Confirmación de la contraseña)

    - **nombre**: `str`  
    - **apellido**: `str`  
    - **celular**: `str`  
    - **mail**: `str`  
    (Correo electrónico)

    - **group_description**: `str`  
    (Debe ser uno de: `"adoptante"`, `"profesional"`, `"supervisora"`, `"administrador"`)

    ---

    ### 📦 Ejemplo de JSON de entrada:

    ```json
    {
    "dni": "00123456",
    "clave": "135246",
    "confirm_clave": "135246",
    "nombre": "Juan",
    "apellido": "Pérez",
    "celular": "1234567890",
    "mail": "juan.perez@example.com",
    "group_description": "adoptante"
    }
    """

    # Extraer datos de entrada

    dni = normalizar_y_validar_dni(user.get("dni")) 
    if not dni: 
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>Debe indicar un DNI válido.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    clave = user.get("clave", "")
    confirm_clave = user.get("confirm_clave", "")
    nombre = capitalizar_nombre(user.get("nombre", ""))
    apellido = capitalizar_nombre(user.get("apellido", ""))
    mail = user.get("mail", "").lower()
    group_description = user.get("group_description", "")

    celular_raw = user.get("celular", "")
    resultado_validacion_celular = normalizar_celular(celular_raw)

    if resultado_validacion_celular["valido"]:
        celular = resultado_validacion_celular["celular"]
    else:
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>Ingrese un número de celular válido.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    # Validar campos obligatorios
    if not dni or not clave or not confirm_clave or not nombre or not mail or not group_description:
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios.")
    

    if db.query(User).filter( User.login == dni ).first() :
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Ya existe un usuario con ese DNI en el Sistema RUA.</p>"
                "<p>Por favor, comunicarse con supervisión.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if db.query(User).filter( User.mail == mail ).first() :
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Ya existe un usuario con ese mail en el Sistema RUA.</p>"
                "<p>Por favor, comunicarse con supervisión.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Verificar que las contraseñas coincidan
    if clave != confirm_clave:
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>Las contraseñas no coinciden.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    

    # Validar la contraseña
    if not clave.isdigit() or len(clave) < 6:
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>La contraseña debe tener al menos 6 dígitos y solo números.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    
    if check_consecutive_numbers(clave):
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>La contraseña no puede tener números consecutivos.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    

    # Validar formato de correo
    if not validar_correo(mail):
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>El correo electrónico no tiene un formato válido.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    
    
    # Validar que el grupo sea uno de los permitidos
    allowed_groups = ["adoptante", "profesional", "supervisora", "administrador"]
    if group_description not in allowed_groups :
        raise HTTPException(status_code=400, detail=f"El grupo debe ser uno de: {', '.join(allowed_groups)}")

    # Buscar en la tabla de grupos el grupo correspondiente a la descripción
    group = db.query(Group).filter(Group.description == group_description).first()
    if not group:
        raise HTTPException(status_code=400, detail="El grupo seleccionado no existe en la base de datos.")



    if group_description == "adoptante":
        try:
            if existe_mail_en_moodle(mail, db):
                return {
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        "<p>Ya existe un usuario con ese mail en nuestro sistema de capacitación.</p>"
                        "<p>Por favor, comunicarse con personal del RUA.</p>"
                    ),
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

            if existe_dni_en_moodle(dni, db):
                return {
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        "<p>Ya existe un usuario con ese DNI en nuestro sistema de capacitación.</p>"
                        "<p>Por favor, comunicarse con personal del RUA.</p>"
                    ),
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

            retorno = crear_usuario_en_moodle(dni, clave, nombre, apellido, mail, db)
            print('crear_usuario_en_moodle', retorno)

            id_curso = get_idcurso(db)
            id_usuario = get_idusuario_by_mail(mail, db)

            retorno = enrolar_usuario(id_curso, id_usuario, db)
            print('enrolar_usuario', retorno)

        except HTTPException as e:
            return {
                "tipo_mensaje": "rojo",
                "mensaje": (
                    "<p>No se pudo completar el registro en el sistema de capacitación (Moodle).</p>"
                    f"<p>Detalle técnico: {e.detail}</p>"
                    "<p>Por favor, intente más tarde o comuníquese con personal del RUA.</p>"
                ),
                "tiempo_mensaje": 10,
                "next_page": "actual"
            }


    # Generar código de activación aleatorio
    activation_code = generar_codigo_para_link(16)

     # Aplicar hash a la contraseña
    hashed_password = get_password_hash(clave)

    # Crear el nuevo usuario
    new_user = User(
        login=dni,
        clave=hashed_password,
        nombre=nombre,
        apellido=apellido,
        celular=celular,
        mail=mail,
        active="N",
        activation_code=activation_code,
        operativo="Y",
        fecha_alta=date.today()
    )

    
    # Crear registro en UserGroup usando el group_id obtenido
    new_user_group = UserGroup(
        login=dni,
        group_id=group.group_id
    )

    try:
        db.add(new_user)
        db.add(new_user_group)
        db.commit()
        db.refresh(new_user)

        # **Registrar el evento en rua_evento**
        nuevo_evento = RuaEvento(
            login=dni,
            evento_detalle="Nuevo usuario registrado.",
            evento_fecha=datetime.now()
        )
        db.add(nuevo_evento)
        db.commit()
    
    except SQLAlchemyError:
        db.rollback()
        return {
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al registrar el usuario.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
 

    try:

        # Construir link de activación
        protocolo = get_setting_value(db, "protocolo")
        host = get_setting_value(db, "donde_esta_alojado")
        puerto = get_setting_value(db, "puerto_tcp")
        endpoint = get_setting_value(db, "endpoint_alta_adoptante")

        # Asegurar formato correcto del endpoint
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint


        # Determinar si incluir el puerto en la URL
        puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
        host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

        # Construir el link completo
        link_activacion = f"{protocolo}://{host_con_puerto}{endpoint}?activacion={activation_code}"


        # Asunto del correo
        asunto = "Activación de cuenta - Sistema RUA"

        # Cuerpo en HTML
        cuerpo = f"""
        <html>
            <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                <tr>
                    <td align="center">
                    <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                        <tr>
                        <td style="font-size: 24px; color: #007bff;">
                            <strong>¡Hola!</strong>
                        </td>
                        </tr>
                        <tr>
                        <td style="padding-top: 20px; font-size: 17px;">
                            <p>El Sistema ha creado tu cuenta en el sistema <strong>RUA</strong> para que presentes tu <strong>Proyecto Adoptivo</strong>.</p>
                            <p>También se creó tu cuenta en el <strong>Campus Virtual del Poder Judicial</strong>, donde realizarás el curso de sensibilización.</p>
                            <p>Solo hacé clic en el botón para continuar:</p>
                        </td>
                        </tr>
                        <tr>
                        <td align="center" style="padding: 30px 0;">
                            <!-- BOTÓN RESPONSIVE -->
                            <table cellpadding="0" cellspacing="0" border="0" style="border-radius: 8px;">
                            <tr>
                                <td align="center" bgcolor="#0d6efd" style="border-radius: 8px;">
                                <a href="{link_activacion}"
                                    target="_blank"
                                    style="display: inline-block; padding: 12px 25px; font-size: 16px; color: #ffffff; background-color: #0d6efd; text-decoration: none; border-radius: 8px; font-weight: bold;">
                                    Confirmo mi registro en el sistema RUA
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
        

        # Enviar el correo HTML
        enviar_mail(destinatario = mail, asunto = asunto, cuerpo = cuerpo)

        # Registrar evento de envío de mail
        evento_mail = RuaEvento(
            login = dni,
            evento_detalle = "Se envió el mail de activación de cuenta.",
            evento_fecha = datetime.now()
        )
        db.add(evento_mail)
        db.commit()

    except Exception as e:
        print("⚠️ No se pudo enviar el mail de activación:", e)



    return {
        "tipo_mensaje": "verde",
        "mensaje": (
            "<p>El usuario fue creado correctamente.</p>"
            "<p>Se ha registrado en el sistema y podrá acceder una vez activado.</p>"
        ),
        "tiempo_mensaje": 8,
        "next_page": "/login"
    }




@users_router.delete("/{login}", response_model = dict,
                     dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador"]))])
def delete_user(login: str, db: Session = Depends(get_db)):
    """
    Elimina un usuario y su registro en DDJJ, sec_users_groups y Moodle (si existe).
    Devuelve un resumen de lo que fue eliminado.
    """
    resumen = {
        "login": login,
        "ddjj_eliminada": False,
        "usuario_encontrado": False,
        "eliminado_en_moodle": False,
        "grupo_eliminado": False,
        "usuario_eliminado": False
    }

    try:
        # 🔹 Intentar eliminar DDJJ
        ddjj = db.query(DDJJ).filter(DDJJ.login == login).first()
        if ddjj:
            db.delete(ddjj)
            resumen["ddjj_eliminada"] = True

        # 🔹 Buscar usuario en RUA
        user = db.query(User).filter(User.login == login).first()
        if not user:
            db.commit()  # commit para confirmar si solo se eliminó la DDJJ
            return {"message": "No se encontró el usuario. Solo se eliminó la DDJJ (si existía).", **resumen}

        resumen["usuario_encontrado"] = True

        # 🔹 Intentar eliminar en Moodle (solo si el usuario tiene mail válido)
        try:
            user_id = get_idusuario_by_mail(user.mail, db)
            if user_id != -1:
                eliminar_usuario_en_moodle(user_id, db)
                sigue_existiendo = existe_mail_en_moodle(user.mail, db)
                if not sigue_existiendo:
                    resumen["eliminado_en_moodle"] = True
        except Exception:
            pass  # Moodle no es crítico, continuamos

        # 🔹 Eliminar relaciones en sec_users_groups
        borrados = db.query(UserGroup).filter(UserGroup.login == login).delete()
        if borrados:
            resumen["grupo_eliminado"] = True

        # 🔹 Eliminar usuario
        db.delete(user)
        resumen["usuario_eliminado"] = True

        db.commit()
        return {"message": "Proceso de eliminación finalizado.", **resumen}

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al eliminar el usuario: {str(e)}")

    

@users_router.put("/personal/{login}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def update_user_by_login(
    login: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Actualiza campos del usuario identificado por `login`. Solo se actualizan los siguientes campos si están presentes en el JSON:

    - nombre, apellido, celular, fecha_nacimiento, foto_perfil, calle_y_nro, depto_etc, 
    - barrio, localidad, provincia, profesion.

    ### Ejemplo de JSON:
    {
        "nombre": "María",
        "apellido": "González",
        "celular": "3512223344"
    }
    """

    usuario = db.query(User).filter(User.login == login).first()
    if not usuario:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Usuario no encontrado.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    
    # --- Extracción y normalización de campos ---

    nombre      = (payload.get("nombre")      or "").strip()
    apellido    = (payload.get("apellido")    or "").strip()
    celular     = (payload.get("celular")     or "").strip()
    calle_y_nro = (payload.get("calle_y_nro") or "").strip()
    depto_etc   = (payload.get("depto_etc")   or "").strip()
    barrio      = (payload.get("barrio")      or "").strip()
    localidad   = (payload.get("localidad")   or "").strip()
    provincia   = (payload.get("provincia")   or "").strip()

    # --- Asignaciones condicionales con transformación ---
    if nombre:
        usuario.nombre = capitalizar_nombre(nombre)
    if apellido:
        usuario.apellido = capitalizar_nombre(apellido)

    if celular:
        resultado = normalizar_celular(celular)
        if resultado["valido"]:
            usuario.celular = resultado["celular"]
        else:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"<p>Celular inválido: {resultado['motivo']}</p>",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
    if calle_y_nro:
        usuario.calle_y_nro = calle_y_nro
    if depto_etc:
        usuario.depto_etc = depto_etc
    if barrio:
        usuario.barrio = barrio
    if localidad:
        usuario.localidad = localidad
    if provincia:
        usuario.provincia = provincia

    # --- Evento de modificación ---
    evento = RuaEvento(
        login = login,
        evento_detalle = (
            f"📝 Datos personales básicos actualizados por {current_user['user'].get('nombre', '')} "
            f"{current_user['user'].get('apellido', '')}."
        ),
        evento_fecha = datetime.now()
    )
    db.add(evento)

            
    # --- Commit y manejo de errores ---
    try:
        db.commit()
        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "<p>Usuario actualizado correctamente.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    except SQLAlchemyError:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "<p>Error al actualizar usuario.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    




@users_router.put("/documentos/{login}", response_model=dict,
                  dependencies=[Depends(verify_api_key),
                                Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
def update_user_document_by_login(
    login: str,
    campo: Literal[
        "doc_adoptante_salud",
        "doc_adoptante_domicilio",
        "doc_adoptante_dni_frente",
        "doc_adoptante_dni_dorso",
        "doc_adoptante_deudores_alimentarios",
        "doc_adoptante_antecedentes",
        "doc_adoptante_migraciones"
    ] = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    Sube un solo documento para el usuario identificado por `login`.
    El campo debe ser uno de los predefinidos. Guarda el archivo en una carpeta
    individual por usuario, con fecha y hora. Permite múltiples archivos históricos,
    pero actualiza la ruta del más reciente en la base de datos.
    """

    # Validar extensión
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Extensión de archivo no permitida: {ext}")

    usuario = db.query(User).filter(User.login == login).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Mapear el campo a su nombre de archivo
    nombre_archivo_map = {
        "doc_adoptante_salud": "salud",
        "doc_adoptante_domicilio": "domicilio",
        "doc_adoptante_dni_frente": "dni_frente",
        "doc_adoptante_dni_dorso": "dni_dorso",
        "doc_adoptante_deudores_alimentarios": "deudores",
        "doc_adoptante_antecedentes": "antecedentes",
        "doc_adoptante_migraciones": "migraciones",
    }

    nombre_archivo = nombre_archivo_map[campo]

    # Crear carpeta del usuario si no existe
    user_dir = os.path.join(UPLOAD_DIR_DOC_PRETENSOS, login)
    os.makedirs(user_dir, exist_ok=True)

    # Generar nombre único con fecha y hora
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"{nombre_archivo}_{timestamp}{ext}"
    filepath = os.path.join(user_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Actualizar ruta en la base de datos
        setattr(usuario, campo, filepath)
        db.commit()

        return {"message": f"Documento '{campo}' subido como '{final_filename}'"}
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al actualizar documento: {str(e)}")
    



@users_router.get("/documentos/{login}/solicitar-revision", response_model=dict,
                  dependencies=[Depends(verify_api_key),
                                Depends(require_roles(["adoptante"]))])
def solicitar_revision_documentos(
    login: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Marca que el usuario identificado por `login` solicita revisión de documentos,
    si todos los documentos requeridos están presentes. Devuelve next_page para el flujo del usuario.
    """

    login_actual = current_user["user"]["login"]

    if login_actual != login:
        raise HTTPException(status_code=403, detail="No tiene permiso para realizar esta acción.")


    usuario = db.query(User).filter(User.login == login).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Documentos obligatorios
    documentos_requeridos = {
        "doc_adoptante_domicilio": "Certificado de domicilio",
        "doc_adoptante_dni_frente": "DNI frente",
        "doc_adoptante_dni_dorso": "DNI dorso",
        "doc_adoptante_deudores_alimentarios": "Certificado de deudores alimentarios",
        "doc_adoptante_antecedentes": "Certificado de antecedentes penales",
        "doc_adoptante_salud": "Certificado de salud"
    }

    documentos_faltantes = []

    for campo, nombre in documentos_requeridos.items():
        if not getattr(usuario, campo):
            documentos_faltantes.append(nombre)

    # Si faltan documentos
    if documentos_faltantes:
        mensaje_html = (
            "<p>Debés adjuntar los siguientes documentos antes de solicitar la revisión:</p>\n"
            "<ul>\n"
        )
        for doc in documentos_faltantes:
            mensaje_html += f"  <li>{doc}</li>\n"
        mensaje_html += "</ul>"

        return {
            "tipo_mensaje": "naranja",
            "mensaje": mensaje_html,
            "tiempo_mensaje": 10,
            "next_page": "actual"
        }

    try :

        # Si están todos los documentos
        usuario.fecha_solicitud_revision = datetime.now()
        usuario.doc_adoptante_estado = "pedido_revision"


        # ✅ Registrar evento en RuaEvento
        evento = RuaEvento(
            login = usuario.login,
            evento_detalle = "El usuario solicitó la revisión de su documentación personal.",
            evento_fecha = datetime.now()
        )
        db.add(evento)

        db.commit()


        # Enviar notificación a todas las supervisoras
        crear_notificacion_masiva_por_rol(
            db = db,
            rol = "supervisora",
            mensaje = f"El usuario {usuario.nombre} {usuario.apellido} ha solicitado revisión de su documentación.",
            link = "/menu_supervisoras/detallePretenso",
            data_json= { "dni": usuario.login },
            tipo_mensaje = "naranja"
        )



        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>La solicitud de revisión de su documentación personal fue enviada correctamente a la supervisión.</p>"
            ),
            "tiempo_mensaje": 8,
            "next_page": "menu_adoptantes/portada"
        }
    except SQLAlchemyError as e:
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



@users_router.put("/{login}/aprobar-curso", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def aprobar_curso_adoptante(
    login: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    ✅ Marca como aprobado el curso del usuario `login`, si pertenece al grupo 'adoptante'.

    Solo puede ser utilizado por roles `administrador` o `supervisora`.
    """

    usuario = db.query(User).filter(User.login == login).first()

    if not usuario:
        raise HTTPException(status_code = 404, detail = "Usuario no encontrado")

    # Validar que el usuario sea adoptante
    grupo = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .first()
    )

    if not grupo or grupo.description != "adoptante":
        return {
            "tipo_mensaje": "rojo",
            "mensaje": "<p>Solo puede aprobarse el curso de personas con rol 'adoptante'.</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Aprobar el curso
    usuario.doc_adoptante_curso_aprobado = "Y"

    try:
        db.commit()

        # Registrar evento
        evento = RuaEvento(
            login = login,
            evento_detalle = "Curso aprobado manualmente por supervisión.",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "tipo_mensaje": "verde",
            "mensaje": "<p>Curso aprobado correctamente.</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al aprobar el curso.</p>"
                f"<p>{str(e)}</p>"
            ),
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }



@users_router.get("/documentos/{login}/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
def descargar_documento_usuario(
    login: str,
    campo: Literal[
        "doc_adoptante_salud",
        "doc_adoptante_domicilio",
        "doc_adoptante_dni_frente",
        "doc_adoptante_dni_dorso",
        "doc_adoptante_deudores_alimentarios",
        "doc_adoptante_antecedentes",
        "doc_adoptante_migraciones"
    ] = Query(...),
    db: Session = Depends(get_db)
):
    """
    Descarga un documento del usuario identificado por `login`.
    El campo debe ser uno de los documentos personales almacenados.
    """

    usuario = db.query(User).filter(User.login == login).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    filepath = getattr(usuario, campo)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )




@users_router.post("/observacion/{login}", response_model=dict,
                   dependencies=[Depends(verify_api_key),
                                 Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def crear_observacion_pretenso(
    login: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Registra una nueva observación para un pretenso (usuario).
    Puede incluir una acción para cambiar el estado de documentación.

    ### Ejemplo del JSON:
    ```json
    {
        "observacion": "El pretenso no adjuntó el certificado de salud.",
        "accion": "solicitar_actualizacion"  // o "aprobar_documentacion"
    }
    ```
    """
    observacion = data.get("observacion")
    accion = data.get("accion")  # Puede ser: "aprobar_documentacion" o "solicitar_actualizacion"
    login_que_observo = current_user["user"]["login"]

    if not observacion:
        raise HTTPException(status_code = 400, detail = "Debe proporcionar el campo 'observacion'.")

    # Verificar que el login destino exista y sea adoptante
    grupo_destinatario = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .first()
    )
    if not grupo_destinatario:
        raise HTTPException(status_code = 404, detail = "El login del pretenso no existe.")
    if grupo_destinatario.description != "adoptante":
        raise HTTPException(status_code = 400, detail = "El login destino no pertenece al grupo 'adoptante'.")

    # Verificar que el observador tiene permisos
    grupo_observador = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login_que_observo)
        .first()
    )
    if not grupo_observador or grupo_observador.description not in ["supervisora", "profesional", "administrador"]:
        raise HTTPException(status_code = 400, detail = "El observador no tiene permiso para registrar observaciones.")

    try:
        # ✅ Guardar la observación
        nueva_obs = ObservacionesPretensos(
            observacion_fecha = datetime.now(),
            observacion = observacion,
            login_que_observo = login_que_observo,
            observacion_a_cual_login = login
        )
        db.add(nueva_obs)

        observacion_resumen = (observacion[:100] + "...") if len(observacion) > 100 else observacion
        nuevo_evento = RuaEvento(
            login = login,
            evento_detalle = f"Observación registrada por {login_que_observo}: {observacion_resumen}",
            evento_fecha = datetime.now()
        )
        db.add(nuevo_evento)

        # ✅ Si viene una acción válida, actualizar el estado
        if accion in ["aprobar_documentacion", "solicitar_actualizacion"]:
            usuario_destino = db.query(User).filter(User.login == login).first()
            if not usuario_destino:
                raise HTTPException(status_code = 404, detail = "Usuario no encontrado para actualizar estado.")

            nuevo_estado = {
                "aprobar_documentacion": "aprobado",
                "solicitar_actualizacion": "actualizando"
            }[accion]

            usuario_destino.doc_adoptante_estado = nuevo_estado

            # Registrar evento adicional por el cambio de estado
            evento_estado = RuaEvento(
                login = login,
                evento_detalle = f"Se cambió el estado de documentación a '{nuevo_estado}' por {login_que_observo}",
                evento_fecha = datetime.now()
            )
            db.add(evento_estado)

        db.commit()


        return {"message": "Observación registrada correctamente"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar observación: {str(e)}")





@users_router.post("/notificacion/{login}", response_model=dict,
                   dependencies=[Depends(verify_api_key),
                                 Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def notificacion_a_pretenso(
    login: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Registra una nueva observación para un pretenso (usuario).
    Puede incluir una acción para cambiar el estado de documentación.

    ### Ejemplo del JSON:
    ```json
    {
        "observacion": "El pretenso no adjuntó el certificado de salud.",
        "accion": "solicitar_actualizacion"  // o "aprobar_documentacion"
    }
    ```
    """
    observacion = data.get("observacion")
    accion = data.get("accion")  # Puede ser: "aprobar_documentacion" o "solicitar_actualizacion"
    login_que_observo = current_user["user"]["login"]

    if not observacion:
        raise HTTPException(status_code = 400, detail = "Debe proporcionar el campo 'observacion'.")

    # Verificar que el login destino exista y sea adoptante
    grupo_destinatario = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .first()
    )
    if not grupo_destinatario:
        raise HTTPException(status_code = 404, detail = "El login del pretenso no existe.")
    if grupo_destinatario.description != "adoptante":
        raise HTTPException(status_code = 400, detail = "El login destino no pertenece al grupo 'adoptante'.")

    # Verificar que el observador tiene permisos
    grupo_observador = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login_que_observo)
        .first()
    )
    if not grupo_observador or grupo_observador.description not in ["supervisora", "profesional", "administrador"]:
        raise HTTPException(status_code = 400, detail = "El observador no tiene permiso para registrar observaciones.")

    try:
        # ✅ Guardar la observación
        nueva_obs = ObservacionesPretensos(
            observacion_fecha = datetime.now(),
            observacion = observacion,
            login_que_observo = login_que_observo,
            observacion_a_cual_login = login
        )
        db.add(nueva_obs)

        observacion_resumen = (observacion[:100] + "...") if len(observacion) > 100 else observacion
        nuevo_evento = RuaEvento(
            login = login,
            evento_detalle = f"Observación registrada por {login_que_observo}: {observacion_resumen}",
            evento_fecha = datetime.now()
        )
        db.add(nuevo_evento)

        # ✅ Si viene una acción válida, actualizar el estado
        if accion in ["aprobar_documentacion", "solicitar_actualizacion"]:
            usuario_destino = db.query(User).filter(User.login == login).first()
            if not usuario_destino:
                raise HTTPException(status_code = 404, detail = "Usuario no encontrado para actualizar estado.")

            nuevo_estado = {
                "aprobar_documentacion": "aprobado",
                "solicitar_actualizacion": "actualizando"
            }[accion]

            usuario_destino.doc_adoptante_estado = nuevo_estado

            # Registrar evento adicional por el cambio de estado
            evento_estado = RuaEvento(
                login = login,
                evento_detalle = f"Se cambió el estado de documentación a '{nuevo_estado}' por {login_que_observo}",
                evento_fecha = datetime.now()
            )
            db.add(evento_estado)

        db.commit()

        # Enviar mail si tiene correo
        usuario_destino = db.query(User).filter(User.login == login).first()
        if usuario_destino and usuario_destino.mail:
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
                                Hola <strong>{usuario_destino.nombre}</strong>,
                            </td>
                            </tr>
                            <tr>
                            <td style="font-size: 16px; padding-bottom: 10px;">
                                Se ha registrado una observación en tu perfil:
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
                            </tr>"""
                
                if accion == "aprobar_documentacion":
                    cuerpo_html += """
                            <tr>
                            <td style="font-size: 17px; color: green; padding-top: 20px;">
                                📄 Tu documentación ha sido <strong>aprobada</strong>.
                            </td>
                            </tr>"""
                    
                elif accion == "solicitar_actualizacion":
                    cuerpo_html += """
                            <tr>
                            <td style="font-size: 17px; color: #d48806; padding-top: 20px;">
                                📄 Se ha solicitado que <strong>actualices</strong> tu documentación.
                            </td>
                            </tr>"""

                cuerpo_html += """
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
                    destinatario = usuario_destino.mail,
                    asunto = "Nueva observación en tu perfil",
                    cuerpo = cuerpo_html
                )


            except Exception as e:
                print(f"⚠️ Error al enviar el correo: {str(e)}")

        return {"message": "Observación registrada correctamente"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code = 500, detail = f"Error al guardar observación: {str(e)}")






@users_router.get("/observacion/{login}/listado", response_model=dict,
                  dependencies=[Depends(verify_api_key),
                                Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante", "coordinadora"]))])
def listar_observaciones_de_pretenso(
    login: str,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    """
    Devuelve un listado paginado de observaciones asociadas a un pretenso identificado por su `login`.

    - Si el usuario autenticado tiene rol 'adoptante', solo puede acceder a su propio listado.
    - Si tiene rol 'administrador', 'supervisora' o 'profesional', puede ver observaciones de cualquier adoptante.
    """
    try:
        usuario_actual_login = current_user["user"]["login"]

        # Obtener roles del usuario logueado
        roles_actual = (
            db.query(Group.description)
            .join(UserGroup, Group.group_id == UserGroup.group_id)
            .filter(UserGroup.login == usuario_actual_login)
            .all()
        )
        roles_actual = [r.description for r in roles_actual]

        # Si es adoptante, solo puede acceder a sus propias observaciones
        if "adoptante" in roles_actual and login != usuario_actual_login:
            raise HTTPException(status_code=403, detail="No tiene permiso para acceder a observaciones de otro usuario.")

        # Verificar que el login destino exista y sea adoptante
        grupo_destino = (
            db.query(Group.description)
            .join(UserGroup, Group.group_id == UserGroup.group_id)
            .filter(UserGroup.login == login)
            .first()
        )
        if not grupo_destino or grupo_destino.description != "adoptante":
            raise HTTPException(status_code=400, detail="El login indicado no corresponde a un usuario del grupo 'adoptante'.")

        # Contar total de observaciones
        total_observaciones = (
            db.query(func.count(ObservacionesPretensos.observacion_id))
            .filter(ObservacionesPretensos.observacion_a_cual_login == login)
            .scalar()
        )

        # Paginación simple
        offset = (page - 1) * limit
        observaciones = (
            db.query(ObservacionesPretensos)
            .filter(ObservacionesPretensos.observacion_a_cual_login == login)
            .order_by(ObservacionesPretensos.observacion_fecha.desc())
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
        # Convertir a diccionario {login: {nombre, apellido}}
        mapa_observadores = {u.login: {"nombre": u.nombre, "apellido": u.apellido} for u in usuarios_observadores}

        # Armar respuesta incluyendo nombre completo del observador
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
        raise HTTPException(status_code=500, detail=f"Error al obtener las observaciones: {str(e)}")




@users_router.get("/eventos/{login}", response_model=dict,
                  dependencies=[Depends(verify_api_key),
                                Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante", "coordinadora"]))])
def listar_eventos_login(
    login: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100)
):
    """
    Devuelve un listado paginado de eventos (detalle y fecha) para un usuario identificado por su `login`.

    - Si el usuario autenticado es 'adoptante' o 'profesional', solo puede acceder a sus propios eventos.
    - 'supervisora' y 'administrador' pueden acceder a eventos de cualquier login.
    """

    try:
        login_actual = current_user["user"]["login"]

        # Obtener roles del usuario autenticado
        roles = (
            db.query(Group.description)
            .join(UserGroup, Group.group_id == UserGroup.group_id)
            .filter(UserGroup.login == login_actual)
            .all()
        )
        roles = [r.description for r in roles]

        # Si es adoptante o profesional, solo puede acceder a sus propios eventos
        if any(r in ["adoptante", "profesional"] for r in roles):
            if login != login_actual:
                raise HTTPException(status_code=403, detail="No tiene permiso para ver eventos de otros usuarios.")

        query = db.query(RuaEvento).filter(RuaEvento.login == login)

        total = query.count()

        eventos = (
            query.order_by(RuaEvento.evento_fecha.desc())
            .offset((page - 1) * limit)
            .limit(limit)
            .all()
        )

        resultado = [
            {
                "detalle": evento.evento_detalle,
                "fecha": evento.evento_fecha.strftime("%Y-%m-%d %H:%M") if evento.evento_fecha else None
            }
            for evento in eventos
        ]

        return {
            "page": page,
            "limit": limit,
            "total": total,
            "eventos": resultado
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener eventos: {str(e)}")



@users_router.get("/observaciones/{login}", response_model=dict,
                  dependencies=[Depends(verify_api_key),
                                Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante", "coordinadora"]))])
def listar_observaciones_login(
    login: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100)
):
    """
    Devuelve un listado paginado de observaciones asociadas a un login de pretenso.

    - Si el usuario autenticado es 'adoptante', solo puede acceder a sus propias observaciones.
    - Si es profesional, supervisora o administrador, puede ver las observaciones de cualquier adoptante.
    """
    try:
        login_actual = current_user["user"]["login"]

        # Obtener roles del usuario autenticado
        roles = (
            db.query(Group.description)
            .join(UserGroup, Group.group_id == UserGroup.group_id)
            .filter(UserGroup.login == login_actual)
            .all()
        )
        roles = [r.description for r in roles]

        # Si es adoptante, solo puede ver sus propias observaciones
        if "adoptante" in roles:
            if login != login_actual:
                raise HTTPException(status_code=403, detail="No tiene permiso para acceder a observaciones de otro usuario.")
            modo_adoptante = True
        else:
            modo_adoptante = False

        # Verificar que el login destino exista
        if not db.query(User).filter(User.login == login).first():
            raise HTTPException(status_code=404, detail="El usuario indicado no existe.")

        total = db.query(func.count(ObservacionesPretensos.observacion_id)).filter(
            ObservacionesPretensos.observacion_a_cual_login == login
        ).scalar()

        observaciones = (
            db.query(ObservacionesPretensos)
            .filter(ObservacionesPretensos.observacion_a_cual_login == login)
            .order_by(ObservacionesPretensos.observacion_fecha.desc())
            .offset((page - 1) * limit)
            .limit(limit)
            .all()
        )

        resultado = []

        for obs in observaciones:
            item = {
                "fecha": obs.observacion_fecha.strftime("%Y-%m-%d %H:%M"),
                "observacion": obs.observacion
            }

            if not modo_adoptante:
                # Buscar nombre completo del login_que_observo
                usuario = db.query(User).filter(User.login == obs.login_que_observo).first()
                item["quien_observo"] = f"{usuario.nombre} {usuario.apellido}" if usuario else "Usuario desconocido"

            resultado.append(item)

        return {
            "page": page,
            "limit": limit,
            "total": total,
            "observaciones": resultado
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener observaciones: {str(e)}")



@users_router.put("/usuario/actualizar", response_model = dict, 
                  dependencies=[Depends(verify_api_key),
                                Depends(require_roles(["administrador", "supervisora"]))])
def actualizar_usuario_total(
    datos: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    🔄 Actualiza los datos de un usuario en Moodle y en la base local (tabla `sec_users`).

    ✅ Solo se realiza la actualización si al menos uno de los datos (DNI, email, nombre o apellido) cambió.

    📥 Entrada esperada (JSON):
    ```json
    {
        "mail_old": "actual@correo.com",
        "dni": "nuevoDNI",
        "mail": "nuevo@correo.com",
        "nombre": "Nuevo Nombre",
        "apellido": "Nuevo Apellido"
    }
    ```

    ⚠️ Se lanza un error si no se encuentra el usuario en `sec_users` por el correo anterior,
    si no existe en Moodle, o si el nuevo DNI ya está en uso en Moodle.
    """
    try:
        required_keys = ["mail_old", "dni", "mail", "nombre", "apellido"]
        for key in required_keys:
            if key not in datos:
                return {
                    "success": False,
                    "tipo_mensaje": "amarillo",
                    "mensaje": f"Falta el campo requerido: {key}",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

        dni_supervisora = current_user["user"]["login"]

        user = db.query(User).filter(User.mail == datos["mail_old"]).first()

        if not user:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "Usuario no encontrado en la base local.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if not existe_mail_en_moodle(datos["mail_old"], db):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "Usuario no encontrado en Moodle.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Verificar que el nuevo DNI no esté ya en uso por otro usuario en Moodle
        if user.login != datos["dni"] and existe_dni_en_moodle(datos["dni"], db):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "El nuevo DNI ya está en uso en Moodle. No se puede actualizar.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }


        # 🧹 Limpieza y normalización
        mail_old  = (datos["mail_old"] or "").strip().lower()

        nuevo_dni = normalizar_y_validar_dni(datos["dni"])
        if not nuevo_dni:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "El DNI ingresado no es válido.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        nuevo_mail = (datos["mail"] or "").strip().lower()
        nuevo_nombre = capitalizar_nombre((datos["nombre"] or "").strip())
        nuevo_apellido = capitalizar_nombre((datos["apellido"] or "").strip())

        if not validar_correo(nuevo_mail):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "El nuevo correo electrónico no tiene un formato válido.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }


        hubo_cambio = (
            user.login    != nuevo_dni or
            user.mail     != nuevo_mail or
            user.nombre   != nuevo_nombre or
            user.apellido != nuevo_apellido
        )


        if not hubo_cambio:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "No se detectaron cambios. No se realizó ninguna actualización.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Actualización en Moodle
        resultado_moodle = actualizar_usuario_en_moodle(
            mail_old = mail_old,
            dni      = nuevo_dni,
            mail     = nuevo_mail,
            nombre   = nuevo_nombre,
            apellido = nuevo_apellido,
            db       = db
        )

        # 🧠 Guardamos el DNI original antes de sobrescribirlo
        dni_original = user.login

        # Actualización en base local
        user.login    = nuevo_dni
        user.mail     = nuevo_mail
        user.nombre   = nuevo_nombre
        user.apellido = nuevo_apellido

        # 👉 Asegura que los cambios estén aplicados antes de continuar
        db.flush()

        # 🔄 Si el usuario estaba como login_2 en algún proyecto, actualizamos
        proyectos_afectados = db.query(Proyecto).filter(Proyecto.login_2 == dni_original).all()
        for proyecto in proyectos_afectados:
            proyecto.login_2 = nuevo_dni


        evento = RuaEvento(
            login = datos["dni"],
            evento_detalle = (
                f"📝 Datos personales críticos actualizados por supervisión {current_user['user'].get('nombre', '')} "
                f"{current_user['user'].get('apellido', '')}. "
                "Se sincronizó con Moodle."
            ),
            evento_fecha = datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Datos actualizados correctamente en Moodle y RUA.",
            "tiempo_mensaje": 5,
            "next_page": "menu_adoptantes/datosPersonales"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al actualizar en base local: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    except HTTPException as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error HTTP: {str(e.detail)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    except Exception as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error general al actualizar: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@users_router.put("/usuario/cambiar-clave", response_model = dict)
def cambiar_clave_usuario(
    datos: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    🔐 Permite al usuario autenticado cambiar su clave, sincronizando con Moodle y la base local.

    📥 Entrada esperada (JSON):
    ```json
    {
        "clave_actual": "123456",
        "nueva_clave": "654321",
        "confirmar_clave": "654321"
    }
    ```
    """
    try:
        login = current_user["user"]["login"]
        mail = current_user["user"]["email"]

        clave_actual = datos.get("clave_actual", "")
        nueva_clave = datos.get("nueva_clave", "")
        confirmar_clave = datos.get("confirmar_clave", "")

        user = db.query(User).filter(User.login == login).first()
        if not user:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "Usuario no encontrado en la base local.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Validar clave actual
        
        if not detect_hash_and_verify(clave_actual, user.clave):
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "La contraseña actual es incorrecta.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if nueva_clave != confirmar_clave:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "Las contraseñas nuevas no coinciden.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Comprobar que la nueva clave no sea igual a la actual
        if nueva_clave == clave_actual:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "La nueva contraseña es igual a la actual.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if not nueva_clave.isdigit() or len(nueva_clave) < 6:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "La nueva clave debe tener al menos 6 dígitos y ser numérica.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if check_consecutive_numbers(nueva_clave):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "La nueva clave no puede tener números consecutivos.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # 🔄 Moodle
        actualizar_clave_en_moodle(mail, nueva_clave, db)

        # 💾 Guardar en bcrypt
        hashed_clave = get_password_hash(nueva_clave)
        user.clave = hashed_clave

        evento = RuaEvento(
            login = login,
            evento_detalle = "🔐 El usuario cambió su clave de acceso.",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "La contraseña fue actualizada correctamente.",
            "tiempo_mensaje": 5,
            "next_page": "login"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al actualizar en base local: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    except Exception as e:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error general: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@users_router.get("/timeline/{login}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional", "coordinadora"]))])
def obtener_timeline_usuario(
    login: str,
    nivel: Literal["hitos", "notificaciones", "observaciones", "eventos"] = Query("hitos"),
    db: Session = Depends(get_db)
):
    """
    📅 Devuelve una línea de tiempo del usuario con distintos niveles de detalle.
    - hitos: solo los momentos clave (curso, DDJJ, proyecto, estados).
    - notificaciones: agrega notificaciones recibidas.
    - observaciones: agrega observaciones internas.
    - eventos: incluye además todos los eventos registrados.
    """
    try:
        user = db.query(User).filter(User.login == login).first()
        if not user:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        timeline = []

        if user.fecha_alta:
            timeline.append({ "fecha": user.fecha_alta, "evento": "Alta del usuario en el sistema" })

        if user.doc_adoptante_curso_aprobado == "Y":
            timeline.append({ "fecha": user.fecha_alta, "evento": "Curso de adopción aprobado" })

        if user.doc_adoptante_ddjj_firmada == "Y":
            timeline.append({ "fecha": user.fecha_alta, "evento": "Declaración Jurada firmada" })

        proyecto = db.query(Proyecto).filter(
            or_(Proyecto.login_1 == login, Proyecto.login_2 == login)
        ).first()

        if proyecto:
            timeline.append({
                "fecha": proyecto.fecha_alta if hasattr(proyecto, "fecha_alta") else datetime.now(),
                "evento": "Creación del proyecto adoptivo"
            })

            historial = db.query(ProyectoHistorialEstado)\
                .filter(ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id)\
                .order_by(ProyectoHistorialEstado.fecha_hora).all()

            for h in historial:
                timeline.append({
                    "fecha": h.fecha_hora,
                    "evento": f"Cambio de estado del proyecto: {h.estado_nuevo.replace('_', ' ').capitalize()}"
                })

            entrevistas = db.query(AgendaEntrevistas).filter(
                AgendaEntrevistas.proyecto_id == proyecto.proyecto_id
            ).order_by(AgendaEntrevistas.fecha_hora).all()

            for idx, e in enumerate(entrevistas):
                timeline.append({
                    "fecha": e.fecha_hora,
                    "evento": f"{idx+1}° Entrevista: {e.comentarios or 'Sin comentarios'}"
                })

            if proyecto.informe_profesionales:
                try:
                    fecha_info = os.path.getmtime(proyecto.informe_profesionales)
                    timeline.append({
                        "fecha": datetime.fromtimestamp(fecha_info),
                        "evento": "Presentación del informe de profesionales"
                    })
                except Exception:
                    pass

        # ➕ NOTIFICACIONES
        if nivel in ["notificaciones", "observaciones", "eventos"]:
            notificaciones = db.query(NotificacionesRUA)\
                .filter(NotificacionesRUA.login_destinatario == login)\
                .order_by(NotificacionesRUA.fecha_creacion).all()

            for n in notificaciones:
                timeline.append({
                    "fecha": n.fecha_creacion,
                    "evento": f"📢 Notificación: {n.mensaje}"
                })

        # ➕ OBSERVACIONES
        if nivel in ["observaciones", "eventos"]:
            observaciones = db.query(ObservacionesPretensos)\
                .filter(ObservacionesPretensos.observacion_a_cual_login == login)\
                .order_by(ObservacionesPretensos.observacion_fecha).all()

            for obs in observaciones:
                resumen = obs.observacion[:100] + "..." if len(obs.observacion) > 100 else obs.observacion
                timeline.append({
                    "fecha": obs.observacion_fecha,
                    "evento": f"📝 Observación registrada: {resumen}"
                })

        # ➕ EVENTOS
        if nivel == "eventos":
            eventos = db.query(RuaEvento)\
                .filter(RuaEvento.login == login)\
                .order_by(RuaEvento.evento_fecha).all()

            for evento in eventos:
                timeline.append({
                    "fecha": evento.evento_fecha,
                    "evento": evento.evento_detalle
                })

        # Ordenar cronológicamente
        timeline.sort( key=lambda x: datetime.combine(x["fecha"], dt_time.min) if isinstance(x["fecha"], date) else x["fecha"] )


        # Formatear fechas a "YYYY-MM-DD"
        for item in timeline:
            fecha = item["fecha"]
            if isinstance(fecha, datetime):
                item["fecha"] = fecha.strftime("%Y-%m-%d")
            elif isinstance(fecha, date):
                item["fecha"] = fecha.strftime("%Y-%m-%d")
            else:
                item["fecha"] = str(fecha)

        # Limpiar posibles etiquetas HTML del campo 'evento'
        for item in timeline:
            evento_original = item["evento"]
            evento_limpio = re.sub(r"<[^>]*?>", "", evento_original)  # Quita cualquier etiqueta HTML
            item["evento"] = evento_limpio.strip()
        

        return {
            "success": True,
            "timeline": timeline
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al generar la línea de tiempo: {str(e)}")



@users_router.get("/estado/{login}", response_model = dict, dependencies = [Depends(verify_api_key)])
def get_estado_usuario(
    login: str,
    db: Session = Depends(get_db)
):
    """
    Devuelve el mensaje de portada y tipo_mensaje según el estado del usuario y su proyecto.
    """

    user = db.query(User).filter(User.login == login).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    group = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .first()
    )
    group_name = group[0] if group else "Sin grupo asignado"

    ddjj = db.query(DDJJ).filter(DDJJ.login == login).first()
    proyecto = db.query(Proyecto).filter(
        or_(Proyecto.login_1 == login, Proyecto.login_2 == login)
    ).first()

    mensaje_para_portada = ""
    tipo_mensaje = "info"

    # 🧠 Curso para adoptantes
    curso_aprobado = user.doc_adoptante_curso_aprobado or "N"
    if group_name.lower() == "adoptante" and curso_aprobado == "N":
        if is_curso_aprobado(user.mail, db):
            curso_aprobado = "Y"
            user.doc_adoptante_curso_aprobado = "Y"
            db.commit()


    if group_name.lower() == "adoptante":
        if curso_aprobado == "N":
            mensaje_para_portada = """
                <h4>Curso de sensibilización</h4>
                <h5>Usted se encuentra en condiciones de iniciar el curso de sensibilización.</h5>
                <p>Por favor, ingrese a nuestro 
                <a href="https://campusvirtual2.justiciacordoba.gob.ar/login/index.php"
                target="_blank" style="color: #007bff; text-decoration: underline;">
                Campus Virtual</a> para comenzar la capacitación.</p>
                <h6>¡Muchas gracias!</h6>
            """
        elif curso_aprobado == "Y" and not ddjj :
            mensaje_para_portada = """
                <h4>Curso aprobado</h4>
                <h5>Usted tiene el curso aprobado y puede continuar con el proceso.</h5>
                <p>Acceda <a href="/menu_adoptantes/alta_ddjj"
                    style="color: #007bff; text-decoration: underline;">aquí para completar su DDJJ</a>.</p>
                <h6>¡Muchas gracias!</h6>
            """ 
        elif curso_aprobado == "Y" and ddjj and user.doc_adoptante_ddjj_firmada == "N":
            mensaje_para_portada = """
                <h4>Actualización de DDJJ</h4>
                <p>Acceda <a href="/menu_adoptantes/alta_ddjj"
                    style="color: #007bff; text-decoration: underline;">aquí para actualizar su DDJJ</a>.</p>
                <h6>¡Muchas gracias!</h6>
            """ 
        elif ddjj and user.doc_adoptante_ddjj_firmada == "Y" and \
                user.doc_adoptante_estado in ( 'inicial_cargando', 'actualizando' ) :
            mensaje_para_portada = """
                <h4>DDJJ firmada</h4>
                <h5>Documentación personal pendiente.</h5>
                <p>Complete la documentación personal <a href='/menu_adoptantes/personales'>desde aquí</a>.</p>
                <h6>¡Muchas gracias!</h6>
            """ 

        elif ddjj and user.doc_adoptante_ddjj_firmada == "Y" and \
                user.doc_adoptante_estado in ( 'pedido_revision' ) :
            mensaje_para_portada = """
                <h4>Documentación en revisión</h4>
                <h5>Aguarde la revisión de su documentación.</h5>
                <h6>¡Muchas gracias!</h6>
            """ 
        elif ddjj and user.doc_adoptante_ddjj_firmada == "Y" and \
                user.doc_adoptante_estado in ( 'aprobado' ) and not proyecto :
            mensaje_para_portada = """
                <h4>Documentación aprobada</h4>
                <h5>Puede presetar su proyecto adoptivo.</h5>
                <h6>¡Muchas gracias!</h6>
            """ 

        elif proyecto:
            estado = proyecto.estado_general
            estados_mensajes = {
                "confeccionando": (
                    "Documentación personal pendiente",
                    "Usted ya firmó su Declaración Jurada.",
                    "Complete la documentación personal <a href='/menu_adoptantes/personales'>desde aquí</a>."
                ),
                "invitacion_pendiente": (
                    "Invitación pendiente",
                    "Revise su correo o acceda al proyecto desde el menú principal.",
                    ""
                ),
                "en_revision": (
                    "Proyecto en revisión",
                    "Aguarde la revisión de su proyecto adoptivo por parte de la Supervisión del RUA.",
                    ""
                ),
                "aprobado": (
                    "Proyecto aprobado",
                    "Será contactado/a para coordinar entrevistas.",
                    ""
                ),
                "calendarizando": (
                    "Entrevistas en preparación",
                    "Pronto recibirá más información.",
                    ""
                ),
                "entrevistando": (
                    "Proceso de entrevistas",
                    "Actualmente se están realizando entrevistas.",
                    ""
                ),
                "para_valorar": (
                    "Evaluación en curso",
                    "Su proyecto se encuentra en etapa de evaluación.",
                    ""
                ),
                "viable": (
                    "Valoración favorable",
                    "Está disponible para búsqueda de NNA.",
                    ""
                ),
                "viable_no_disponible": (
                    "Valoración favorable",
                    "No está disponible actualmente.",
                    ""
                ),
                "en_suspenso": (
                    "Proyecto en suspenso",
                    "Su proyecto se encuentra temporalmente en pausa.",
                    ""
                ),
                "no_viable": (
                    "Proyecto no viable",
                    "Su proyecto no ha sido viable.",
                    "Para más información, contacte al equipo técnico."
                ),
                "en_carpeta": (
                    "Proyecto en carpeta",
                    "Su proyecto está siendo considerado actualmente.",
                    ""
                ),
                "vinculacion": (
                    "Vinculación en curso",
                    "Se encuentra en proceso de vinculación.",
                    ""
                ),
                "guarda": (
                    "Guarda otorgada",
                    "Se ha otorgado la guarda del NNA.",
                    "¡Felicitaciones!"
                ),
                "adopcion_definitiva": (
                    "Adopción definitiva",
                    "La adopción ha sido otorgada definitivamente.",
                    "¡Felicitaciones!"
                ),
            }

            if estado == "viable" and proyecto.ultimo_cambio_de_estado:
                fecha_cambio = proyecto.ultimo_cambio_de_estado

                # Convertir solo si es string (precaución extra)
                if isinstance(fecha_cambio, str):
                    try:
                        fecha_cambio = datetime.strptime(fecha_cambio, "%Y-%m-%d").date()
                    except ValueError:
                        fecha_cambio = None

                if isinstance(fecha_cambio, date):
                    dias_transcurridos = (datetime.today().date() - fecha_cambio).days
                    if dias_transcurridos >= 330:
                        tipo_mensaje = "naranja"
                        mensaje_para_portada += """
                            <div style="margin-top: 20px; border-top: 1px solid #ccc; padding-top: 10px;">
                                <h5>🔔 Ratificación necesaria</h5>
                                <p>Han pasado más de 11 meses desde su última actualización en la lista RUA.</p>
                                <p>Por favor, comuníquese con el equipo técnico para ratificar su deseo de continuar formando parte de la lista.</p>
                            </div>
                        """


            if estado in estados_mensajes:
                titulo, mensaje, detalle = estados_mensajes[estado]
                mensaje_para_portada = f"""
                    <h4>{titulo}</h4>
                    <h5>{mensaje}</h5>
                    {f"<p>{detalle}</p>" if detalle else ""}
                """
            elif estado.startswith("baja"):
                motivo_baja = estado.replace('_', ' ').capitalize()
                mensaje_para_portada = f"""
                    <h4>Proyecto dado de baja</h4>
                    <h5>Motivo: {motivo_baja}.</h5>
                    <h6>Para más información, contacte al equipo técnico.</h6>
                """




    return {
        "tipo_mensaje": tipo_mensaje,
        "mensaje_para_portada": mensaje_para_portada,
        "curso_aprobado": curso_aprobado,
        "ddjj_firmada": user.doc_adoptante_ddjj_firmada,
        "doc_adoptante_estado": user.doc_adoptante_estado,
        "datos_domicilio_faltantes": not (user.calle_y_nro or user.localidad)
    }



@users_router.get("/mi/perfil", response_model=dict,
    dependencies=[Depends(verify_api_key)])
def obtener_mis_datos_personales(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Devuelve los datos personales editables del usuario autenticado.
    """


    login = current_user["user"]["login"]

    user = db.query(User).filter(User.login == login).first()

    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    return {
        "login": user.login,
        "nombre": user.nombre,
        "apellido": user.apellido,
        "celular": user.celular,
        "mail": user.mail,
        "calle_y_nro": user.calle_y_nro,
        "depto_etc": user.depto_etc,
        "barrio": user.barrio,
        "localidad": user.localidad,
        "provincia": user.provincia,
    }



@users_router.put("/mi/perfil", response_model=dict,
    dependencies=[Depends(verify_api_key)])
def actualizar_mis_datos_personales(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    🔐 Requiere autenticación y API Key

    Actualiza los datos personales del usuario autenticado.
    Los datos deben enviarse en formato JSON (Content-Type: application/json)

    📥 Ejemplo de JSON de entrada:
    ```json
    {
    "nombre": "Lidia Angélica",
    "apellido": "de Gómez",
    "celular": "351-123-4567",
    "calle_y_nro": "Calle Falsa 123",
    "depto_etc": "Dpto B",
    "barrio": "Centro",
    "localidad": "Córdoba",
    "provincia": "Córdoba"
    }
    ```

    ⚠️ Validaciones aplicadas:
    - El campo "celular" debe tener entre 10 y 15 dígitos válidos (se permiten guiones, espacios, paréntesis).
    - Los campos "nombre", "apellido", "mail" y "celular" son obligatorios.
    - Se capitaliza nombre y apellido.
    - Se normaliza el celular al formato internacional (+54...).
    """

    login = current_user["user"]["login"]
    user = db.query(User).filter(User.login == login).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    nombre      = (payload.get("nombre")      or "").strip()
    apellido    = (payload.get("apellido")    or "").strip()
    celular     = (payload.get("celular")     or "").strip()
    calle_y_nro = (payload.get("calle_y_nro") or "").strip()
    depto_etc   = (payload.get("depto_etc")   or "").strip()
    barrio      = (payload.get("barrio")      or "").strip()
    localidad   = (payload.get("localidad")   or "").strip()
    provincia   = (payload.get("provincia")   or "").strip()



    user.nombre = capitalizar_nombre(nombre)
    user.apellido = capitalizar_nombre(apellido)

    resultado_celular = normalizar_celular(celular)
    if not resultado_celular["valido"]:
        return {
            "success": False,
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>Ingrese un número de celular válido.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    
    user.celular = resultado_celular["celular"]

    user.calle_y_nro = calle_y_nro
    user.depto_etc = depto_etc
    user.barrio = barrio
    user.localidad = localidad
    user.provincia = provincia

    db.add(RuaEvento(
        login = login,
        evento_detalle = "Actualizó sus datos personales desde el perfil.",
        evento_fecha = datetime.now()
    ))

    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "<p>Datos personales actualizados correctamente.</p>",
        "tiempo_mensaje": 4,
        "next_page": "actual"
    }



@users_router.post("/notificacion/pretenso/mensaje", response_model=dict,
                   dependencies=[Depends(verify_api_key),
                                 Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def notificar_pretenso_mensaje(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    📢 Envía una notificación completa a un pretenso:
    - Crea una notificación individual
    - Registra observación interna
    - Cambia estado de documentación si corresponde
    - Envía un correo electrónico

    ### Ejemplo del JSON esperado:
    ```json
    {
        "login_destinatario": "12345678",
        "mensaje": "Recordá subir el certificado de salud.",
        "link": "/menu_adoptantes/documentacion",
        "data_json": { "accion": "solicitar_actualizacion_doc" },
        "tipo_mensaje": "naranja"
    }
    """
    login_destinatario = data.get("login_destinatario")
    mensaje = data.get("mensaje")
    link = data.get("link")
    data_json = data.get("data_json") or {}
    tipo_mensaje = data.get("tipo_mensaje", "naranja")
    login_que_observa = current_user["user"]["login"]
    accion = data_json.get("accion")  # puede ser None, "solicitar_actualizacion_doc", "aprobar_documentacion"

    if not all([login_destinatario, mensaje, link]):
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Faltan campos requeridos: login_destinatario, mensaje o link.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    # Validar usuario destinatario
    user_destino = db.query(User).filter(User.login == login_destinatario).first()
    if not user_destino:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El usuario destinatario no existe.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    grupo = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login_destinatario)
        .first()
    )
    if not grupo or grupo.description != "adoptante":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El login destino no pertenece al grupo 'adoptante'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    try:
        # Crear notificación individual
        resultado = crear_notificacion_individual(
            db=db,
            login_destinatario=login_destinatario,
            mensaje=mensaje,
            link=link,
            data_json=data_json,
            tipo_mensaje=tipo_mensaje,
            enviar_por_whatsapp=False,
            login_que_notifico=login_que_observa
        )
        if not resultado["success"]:
            raise Exception(resultado["mensaje"])

        # Cambiar estado si la acción es válida
        nuevo_estado = None
        if accion == "solicitar_actualizacion_doc":
            nuevo_estado = "actualizando"
        elif accion == "aprobar_documentacion":
            nuevo_estado = "aprobado"

        if nuevo_estado:
            user_destino.doc_adoptante_estado = nuevo_estado

        # Registrar único evento con detalle completo
        evento_detalle = f"Notificación enviada por {login_que_observa}: {mensaje[:150]}"
        if nuevo_estado:
            evento_detalle += f" | Se cambió el estado de documentación a '{nuevo_estado}'"

        db.add(RuaEvento(
            login=login_destinatario,
            evento_detalle=evento_detalle,
            evento_fecha=datetime.now()
        ))

        db.commit()

        # Enviar correo si hay mail
        if user_destino.mail:
            try:
                if accion == "solicitar_actualizacion_doc":
                    cuerpo_mensaje_html = f"""
                    <p>Desde el equipo de supervisión del <strong>RUA</strong>, solicitamos que revise y actualice su documentación personal en el sistema.</p>
                    <p>A continuación, compartimos el mensaje enviado por el equipo:</p>
                    <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #0d6efd; border-radius: 6px; margin-top: 10px;">
                        <em>{mensaje}</em>
                    </div>
                    """
                elif accion == "aprobar_documentacion":
                    cuerpo_mensaje_html = f"""
                    <p>Desde el equipo de supervisión del <strong>RUA</strong>, informamos que su documentación personal fue <strong>revisada y aprobada</strong>.</p>
                    <p>A continuación, compartimos el mensaje enviado por el equipo:</p>
                    <div style="background-color: #f1f3f5; padding: 15px 20px; border-left: 4px solid #198754; border-radius: 6px; margin-top: 10px;">
                        <em>{mensaje}</em>
                    </div>
                    """
                else:
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
                                <strong>Hola {user_destino.nombre},</strong>
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
                    destinatario=user_destino.mail,
                    asunto="Notificación del Sistema RUA",
                    cuerpo=cuerpo
                )
            except Exception as e:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"⚠️ Error al enviar correo: {str(e)}",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }


        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Notificación enviada y registrada correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al procesar la notificación: {str(e)}",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }




@users_router.post("/observacion/{login}/registrar", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def registrar_observacion_directa(
    login: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Registra una observación interna para un pretenso, sin enviar mail ni modificar el estado documental.
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
        

    # Verificar que el usuario destino exista y sea adoptante
    grupo_destinatario = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .first()
    )
    if not grupo_destinatario:
        return  {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "El login del pretenso no existe.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
        
    if grupo_destinatario.description != "adoptante":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El login destino no pertenece al grupo 'adoptante'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    try:
        # Registrar observación
        nueva_obs = ObservacionesPretensos(
            observacion_fecha=datetime.now(),
            observacion=observacion.strip(),
            login_que_observo=login_que_observo,
            observacion_a_cual_login=login
        )
        db.add(nueva_obs)

        # Registrar evento
        resumen = observacion.strip()
        resumen = resumen[:100] + "..." if len(resumen) > 100 else resumen

        nuevo_evento = RuaEvento(
            login=login,
            evento_detalle=f"Observación registrada por {login_que_observo}: {resumen}",
            evento_fecha=datetime.now()
        )
        db.add(nuevo_evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Observación registrada correctamente.",
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



@users_router.put("/usuarios/{login}/darse-de-baja", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervisora", "adoptante"]))])
def darse_de_baja_del_sistema(
    login: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Permite dar de baja a un usuario del sistema RUA.
    
    🟢 Si el usuario autenticado es administrador o supervisora, puede dar de baja a cualquier persona.
    🔵 Si es adoptante, solo puede darse de baja a sí mismo.

    Marca el campo `operativo` en 'N' en `sec_users` y registra un evento.
    """

    login_actual = current_user["user"]["login"]
    
    # Buscar usuario a dar de baja
    user = db.query(User).filter(User.login == login).first()
    if not user:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Usuario no encontrado.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Validar grupo adoptante
    grupo = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login)
        .first()
    )
    if not grupo or grupo.description.lower() != "adoptante":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Solo los usuarios adoptantes pueden solicitar la baja.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if user.operativo == "N":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "El usuario ya se encuentra dado de baja.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    try:
        user.operativo = "N"
        db.add(RuaEvento(
            login=login,
            evento_detalle=f"El usuario fue dado de baja por {login_actual}.",
            evento_fecha=datetime.now()
        ))
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "La baja fue registrada correctamente.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }




    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error al registrar la baja: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@users_router.get("/{login}/descargar-documentos", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def descargar_documentos_usuario(
    login: str,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.login == login).first()
    if not user:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "El usuario indicado no fue encontrado.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }
        )

    try:
        output_path = os.path.join(DIR_PDF_GENERADOS, f"documentos_usuario_{login}.pdf")
        pdf_paths: List[Tuple[str, str]] = []


        def agregar_documentos(modelo, campos: List[str]):
            for campo in campos:
                ruta = getattr(modelo, campo, None)
                if ruta and os.path.exists(ruta):
                    ext = os.path.splitext(ruta)[1].lower()
                    nombre_base = f"{modelo.__class__.__name__.lower()}_{campo}_{os.path.basename(ruta)}"
                    out_pdf = os.path.join(DIR_PDF_GENERADOS, nombre_base + ".pdf")

                    if ext == ".pdf":
                        shutil.copy(ruta, out_pdf)
                        # pdf_paths.append(out_pdf)
                        pdf_paths.append((campo, out_pdf))
                    elif ext in [".jpg", ".jpeg", ".png"]:
                        Image.open(ruta).convert("RGB").save(out_pdf)
                        # pdf_paths.append(out_pdf)
                        pdf_paths.append((campo, out_pdf))
                    elif ext in [".doc", ".docx"]:
                        subprocess.run([
                            "libreoffice", "--headless", "--convert-to", "pdf", "--outdir", DIR_PDF_GENERADOS, ruta
                        ], check=True)
                        converted = os.path.join(DIR_PDF_GENERADOS, os.path.splitext(os.path.basename(ruta))[0] + ".pdf")
                        if os.path.exists(converted):
                            # pdf_paths.append(converted)
                            pdf_paths.append((campo, converted))

        # Agregamos los documentos personales del usuario
        agregar_documentos(user, [
            "doc_adoptante_salud", "doc_adoptante_dni_frente", "doc_adoptante_dni_dorso", "doc_adoptante_domicilio",
            "doc_adoptante_deudores_alimentarios", "doc_adoptante_antecedentes",
            "doc_adoptante_migraciones"
        ])

        if not pdf_paths:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "No se encontraron documentos disponibles para este usuario.",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }
            )
        
        TITULOS_DOCUMENTOS = {
            "doc_adoptante_salud": "Certificado de salud",
            "doc_adoptante_dni_frente": "DNI - Frente",
            "doc_adoptante_dni_dorso": "DNI - Dorso",
            "doc_adoptante_domicilio": "Comprobante de domicilio",
            "doc_adoptante_deudores_alimentarios": "Certificado de deudores alimentarios",
            "doc_adoptante_antecedentes": "Certificado de antecedentes penales",
            "doc_adoptante_migraciones": "Certificado de migraciones"
        }
       

        # Fusionamos todos los PDF
        merged = fitz.open()



        # Portada con estilo institucional
        page = merged.new_page(pno=0, width=595, height=842)

        # Encabezado institucional
        page.insert_textbox(
            rect=fitz.Rect(0, 40, page.rect.width, 80),
            buffer="SERVICIO DE GUARDA Y ADOPCIÓN",
            fontname="helv",
            fontsize=16,
            align=1,
            color=(0.1, 0.1, 0.3)  # azul oscuro
        )

        page.insert_textbox(
            rect=fitz.Rect(0, 65, page.rect.width, 100),
            buffer="REGISTRO ÚNICO DE ADOPCIONES Y EQUIPO TÉCNICO",
            fontname="helv",
            fontsize=13,
            align=1,
            color=(0.2, 0.2, 0.4)
        )

        page.insert_textbox(
            rect=fitz.Rect(0, 95, page.rect.width, 120),
            buffer="DOCUMENTACIÓN DEL PRETENSO ADOPTANTE",
            fontname="helv",
            fontsize=11,
            align=1,
            color=(0.4, 0.4, 0.4)
        )

        # Recuadro con fondo sutil para los datos personales
        fondo = fitz.Rect(50, 150, page.rect.width - 50, 280)
        page.draw_rect(fondo, color=(0.88, 0.93, 0.98), fill=(0.88, 0.93, 0.98))

        datos = [
            f"Nombre: {user.nombre} {user.apellido}",
            f"DNI: {user.login}",
            f"Correo electrónico: {user.mail or 'No registrado'}",
            f"Celular: {user.celular or 'No registrado'}"
        ]

        y_pos = 165
        for linea in datos:
            page.insert_textbox(
                rect=fitz.Rect(60, y_pos, page.rect.width - 60, y_pos + 25),
                buffer=linea,
                fontname="helv",
                fontsize=13,
                align=0,
                color=(0.1, 0.1, 0.1)
            )
            y_pos += 28

        # Línea decorativa inferior
        page.draw_line(p1=(60, y_pos + 10), p2=(page.rect.width - 60, y_pos + 10), color=(0.5, 0.5, 0.5), width=0.6)



        # # Página inicial con datos personales del pretenso
        # page = merged.new_page(pno=0, width=595, height=842)

        # # Encabezado centrado grande
        # page.insert_textbox(
        #     rect=fitz.Rect(0, 60, page.rect.width, 100),
        #     buffer="Documentación del Pretenso Adoptante",
        #     fontname="helv",
        #     fontsize=22,
        #     align=1
        # )

        # # Datos personales: insertados bien visibles más abajo
        # datos = [
        #     f"Nombre: {user.nombre} {user.apellido}",
        #     f"DNI: {user.login}",
        #     f"Correo electrónico: {user.mail or 'No registrado'}",
        #     f"Celular: {user.celular or 'No registrado'}"
        # ]

        # # Espaciado vertical correcto
        # y_pos = 120
        # for linea in datos:
        #     rect_linea = fitz.Rect(60, y_pos, page.rect.width - 60, y_pos + 25)
        #     page.insert_textbox(
        #         rect=rect_linea,
        #         buffer=linea,
        #         fontname="helv",
        #         fontsize=14,
        #         align=0  # alineado a la izquierda
        #     )
        #     y_pos += 35  # mayor espaciado para visibilidad

        # # Línea decorativa final
        # page.draw_line(p1=(60, y_pos), p2=(page.rect.width - 60, y_pos), color=(0.5, 0.5, 0.5), width=0.8)




        for campo, path in pdf_paths:
            print(f"➡️  Procesando: {campo} -> {path}")
            titulo_base = TITULOS_DOCUMENTOS.get(campo, "Documento")
            titulo_completo = f"{titulo_base} de {user.nombre} {user.apellido}"
            print(f"📝 Título insertado: {titulo_completo}")

            # Página de título
            page = merged.new_page(width=595, height=842)  # A4
            print("✅ Página de título creada.")

            text_rect = fitz.Rect(0, 280, page.rect.width, 320)
            page.insert_textbox(
                rect=text_rect,
                buffer=titulo_completo,
                fontname="helv",
                fontsize=20,
                align=1
            )

            print("🖋️ Texto insertado en página de título.")

            ICONO_FLECHA_PATH = "/app/recursos/imagenes/flecha_hacia_abajo.png"
            if os.path.exists(ICONO_FLECHA_PATH):
                img_rect = fitz.Rect(250, 340, 345, 440)
                page.insert_image(img_rect, filename=ICONO_FLECHA_PATH)
                print("📌 Flecha insertada.")
            else:
                print("⚠️ No se encontró el ícono de flecha:", ICONO_FLECHA_PATH)

            # Insertar el documento real
            with fitz.open(path) as doc:
                merged.insert_pdf(doc)
                print(f"📎 Documento {path} insertado.")

        print(f"📄 Total de páginas generadas: {merged.page_count}")
        merged.save(output_path)

        return FileResponse(
            path=output_path,
            filename=f"documentos_pretenso_{login}.pdf",
            media_type="application/pdf"
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"Ocurrió un error al generar el PDF: {str(e)}",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }
        )

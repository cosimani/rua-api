from fastapi import APIRouter, HTTPException, Depends, Query, Request, Body, UploadFile, File, Form

from typing import List, Dict, Optional, Literal, Tuple
from math import ceil
from database.config import SessionLocal
from helpers.utils import check_consecutive_numbers, get_user_name_by_login, \
        build_subregistro_string, parse_date, calculate_age, validar_correo, generar_codigo_para_link, \
        normalizar_y_validar_dni, capitalizar_nombre, normalizar_celular, verificar_recaptcha

from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, crear_usuario_en_moodle, get_idcurso, \
    enrolar_usuario, get_idusuario_by_mail, eliminar_usuario_en_moodle, actualizar_usuario_en_moodle, \
    actualizar_clave_en_moodle, is_curso_aprobado

from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol, crear_notificacion_individual

import base64
from fastapi import BackgroundTasks
import csv
import io

from models.users import User, Group, UserGroup 

from models.proyecto import Proyecto, ProyectoHistorialEstado, AgendaEntrevistas
from models.notif_y_observaciones import ObservacionesPretensos, NotificacionesRUA
from models.ddjj import DDJJ
import hashlib
import time
from datetime import datetime, timedelta, date, time as dt_time

import time
from bs4 import BeautifulSoup



from database.config import get_db  # Importá get_db desde config.py
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import case, func, and_, or_, select, union_all, join, literal_column, desc, text, not_
from sqlalchemy.sql import literal_column


from models.eventos_y_configs import RuaEvento, UsuarioNotificadoInactivo
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




@users_router.get("/", response_model=dict, dependencies=[Depends( verify_api_key ), 
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def get_users(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),

    operativo: Optional[Literal["Y", "N"]] = Query(
        None,
        description=(
            "Filtrar por el campo **operativo** de `sec_users`.\n"
            "- `Y` → usuarios operativos\n"
            "- `N` → usuarios NO operativos\n"
            "Si no se especifica, se asume `Y`."
        ),
    ),

    # group_description: Literal["adoptante", "supervision", "profesional", "supervisora", "administrador"] = Query(
    #     None, description="Grupo o rol del usuario"),   

    group_description: Optional[Literal["adoptante","supervision","profesional","supervisora","administrador"]] = Query(
        None, description="Grupo o rol del usuario"
    ),

    search: Optional[str] = Query(None, description="Búsqueda por al menos 3 caracteres alfanuméricos"),

    proyecto_tipo: Optional[Literal["Monoparental", "Matrimonio", "Unión convivencial"]] = Query(
        None, description="Filtrar por tipo de proyecto (Monoparental, Matrimonio, Unión convivencial)" ),
    curso_aprobado: Optional[bool] = Query(None, description="Filtrar por curso aprobado"),
    doc_adoptante_estado: Optional[Literal[
        "inicial_cargando", "pedido_revision", "actualizando", "aprobado", "rechazado", "inactivo"
    ]] = Query(None, description="Filtrar por estado de documentación personal (incluye 'inactivo')"),
                                          

    nro_orden_rua: Optional[int] = Query(None, description="Filtrar por número de orden"),

    fecha_alta_inicio: Optional[str] = Query(None, description="Filtrar por fecha de alta de usuario, inicio (AAAA-MM-DD)"),
    fecha_alta_fin: Optional[str] = Query(None, description="Filtrar por fecha de alta de usuario, fin (AAAA-MM-DD)"),
    edad_min: Optional[int] = Query(None, description="Edad mínima edad según fecha de nacimiento en DDJJ"),
    edad_max: Optional[int] = Query(None, description="Edad máxima según fecha de naciminieto en DDJJ"),
    fecha_nro_orden_inicio: Optional[str] = Query(None, 
                    description="Filtrar por fecha de asignación de nro. de orden, inicio (AAAA-MM-DD)"),
    fecha_nro_orden_fin: Optional[str] = Query(None, 
                    description="Filtrar por fecha de asignación de nro. de orden, fin (AAAA-MM-DD)"),
    ingreso_por: Literal["rua","oficio","convocatoria","todos"] = Query(
        "rua",
        description="Filtra por origen del proyecto: rua/oficio/convocatoria. 'todos' para no filtrar."
    ),
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
                User.active.label("active"),
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
        # if doc_adoptante_estado:
        #     query = query.filter(User.doc_adoptante_estado == doc_adoptante_estado)

        # Filtro por estado de documentación personal
        if doc_adoptante_estado:
            if doc_adoptante_estado == "inactivo":
                # 'inactivo' es un estado virtual: se filtra por active = 'N'
                query = query.filter(User.active == "N")
            else:
                # Estados reales del enum; además, excluimos inactivos para no mezclar
                query = query.filter(
                    User.doc_adoptante_estado == doc_adoptante_estado,
                    or_(User.active == None, User.active != "N")  # activos o null
                )


        # Filtro por nro de orden
        if nro_orden_rua:
            query = query.filter(Proyecto.nro_orden_rua == nro_orden_rua)    

        # ——— Filtro por campo operativo ——————————————————————————
        # if operativo is None:          # el cliente no mandó el parámetro
        #     operativo = "Y"            # asumimos solo operativos
        # query = query.filter(User.operativo == operativo)
        # ——————————————————————————————————————————————————————————

        # ——— Filtro por campo operativo ——————————————————————————
        # Regla:
        # - Si el cliente manda ?operativo=Y|N => respetar ese filtro.
        # - Si NO manda 'operativo' (operativo is None):
        #     * Por defecto filtrar solo Y.
        #     * EXCEPCIÓN: si doc_adoptante_estado == "rechazado", NO filtrar por operativo (incluye Y y N).
        if operativo is not None:
            query = query.filter(User.operativo == operativo)
        else:
            if not (doc_adoptante_estado and doc_adoptante_estado == "rechazado"):
                query = query.filter(User.operativo == "Y")
        # ——————————————————————————————————————————————————————————


        # ——— Filtro por ingreso_por ————————————————————————————————
        if ingreso_por != "todos":
            P_any = aliased(Proyecto)
            existe_alguno = (
                db.query(P_any.proyecto_id)
                .filter(or_(P_any.login_1 == User.login, P_any.login_2 == User.login))
                .exists()
            )

            P_origen = aliased(Proyecto)
            existe_origen = (
                db.query(P_origen.proyecto_id)
                .filter(
                    or_(P_origen.login_1 == User.login, P_origen.login_2 == User.login),
                    P_origen.ingreso_por == ingreso_por
                )
                .exists()
            )

            # Si NO tiene proyectos -> pasa. Si tiene proyectos -> debe tener al menos uno del origen pedido.
            query = query.filter(or_(not_(existe_alguno), existe_origen))
        # ————————————————————————————————————————————————————————————————

    
        if search and len(search.strip()) >= 3:
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

        # Para evitar duplicados, para que un usuario que tiene varios proyectos, aparezca una sola vez
        query = query.distinct(User.login)


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

        MAPA_ESTADOS_PROYECTO = {
            "sin_curso": "Curso pendiente",
            "ddjj_pendiente": "DDJJ pendiente",
            "inicial_cargando": "Doc. inicial",
            "pedido_revision": "Doc. en revisión",
            "actualizando": "Actualizando doc.",
            "aprobado": "Doc. aprobada",
            "rechazado": "Doc. rechazada",
            "inactivo": "Inactivo",

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
            "guarda_provisoria": "Guarda provisoria",
            "guarda_confirmada": "Guarda confirmada",
            "adopcion_definitiva": "Adopción def.",
            "baja_anulacion": "Baja anulación",
            "baja_caducidad": "Baja caducidad",
            "baja_por_convocatoria": "Baja conv.",
            "baja_rechazo_invitacion": "Baja por rechazo"
            
        }


        users_list = []

        for user in users:

            # ----- proyectos_ids con prioridad (solo Adoptantes) -----
            es_adoptante = (user.group or "").lower() == "adoptante"

            if es_adoptante:
                # Prioridad por origen
                orden_origen = case(
                    (Proyecto.ingreso_por == "rua", 0),
                    (Proyecto.ingreso_por == "oficio", 1),
                    (Proyecto.ingreso_por == "convocatoria", 2),
                    else_=3
                )
    
                # Prioridad por estado dentro de cada origen
                estados_ordenados = [
                    'aprobado',
                    'calendarizando',
                    'entrevistando',
                    'para_valorar',
                    'viable',
                    'viable_no_disponible',
                    'en_suspenso',
                    'no_viable',
                    'en_carpeta',
                    'vinculacion',
                    'guarda_provisoria',
                    'guarda_confirmada',
                    'adopcion_definitiva',
                ]

                # CASE para ranking de estado
                orden_estado = case(
                    *[(Proyecto.estado_general == e, i) for i, e in enumerate(estados_ordenados)],
                    else_=len(estados_ordenados)
                )

                # Subquery: última fecha de historial por proyecto
                hist_max_sq = (
                    db.query(
                        ProyectoHistorialEstado.proyecto_id.label("pid"),
                        func.max(ProyectoHistorialEstado.fecha_hora).label("last_hist")
                    )
                    .group_by(ProyectoHistorialEstado.proyecto_id)
                    .subquery()
                )

                # Usamos la fecha de historial SOLO para 'convocatoria'; para otros orígenes no influye
                last_hist_cond = case(
                    (Proyecto.ingreso_por == "convocatoria", hist_max_sq.c.last_hist),
                    else_=None
                )

                proyectos_rows = (
                    db.query(Proyecto.proyecto_id, Proyecto.ingreso_por)
                      .outerjoin(hist_max_sq, hist_max_sq.c.pid == Proyecto.proyecto_id)
                      .filter(or_(Proyecto.login_1 == user.login, Proyecto.login_2 == user.login))
                      .order_by(
                          orden_origen.asc(),          # 1) origen: rua > oficio > convocatoria
                          orden_estado.asc(),          # 2) estado, según el orden pedido
                          last_hist_cond.desc(),       # 3) SOLO convocatoria: último historial más reciente primero
                          Proyecto.proyecto_id.asc()   # 4) desempate estable
                      )
                      .all()
                )
                proyectos_ids = [row.proyecto_id for row in proyectos_rows]
                

            else:
                # No adoptantes: lista vacía
                proyectos_rows = []
                proyectos_ids = []

            # Garantía de lista vacía si no hay proyectos
            if not proyectos_ids:
                proyectos_ids = []

            # === Proyecto PRIMARIO: debe ser el primer proyecto con ingreso_por == "rua". Si no hay "rua", queda None. ===
            # proyecto_id_primario = next((r.proyecto_id for r in proyectos_rows if (r.ingreso_por or "") == "rua"), None)

            # Proyecto PRIMARIO: el primero de la lista ordenada (rua > oficio > convocatoria). 
            # Si no hay RUA pero hay convocatoria, será el primero de convocatoria.
            proyecto_id_primario = proyectos_ids[0] if proyectos_ids else None


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

            

            # Determinar el estado en bruto (prioriza INACTIVO)
            if user.active == "N":
                estado_raw = "inactivo"
            else:
                estado_raw = (
                    user.estado_general if user.estado_general else (
                        "sin_curso" if not user.doc_adoptante_curso_aprobado or user.doc_adoptante_curso_aprobado != "Y"
                        else "ddjj_pendiente" if not user.doc_adoptante_ddjj_firmada or user.doc_adoptante_ddjj_firmada != "Y"
                        else user.doc_adoptante_estado if user.doc_adoptante_estado in valid_states
                        else "inicial_cargando"
                    )
                )


            # Defaults por si no hay proyecto
            prim_tipo = ""
            prim_nro_orden = ""
            prim_ingreso_por = ""
            prim_operativo = False
            prim_login_1 = None
            prim_login_2 = None
            prim_fecha_asign_nro_orden = ""
            prim_ultimo_cambio = ""
            prim_estado_general = ""
            prim_subregistro_string = ""


            if proyecto_id_primario is not None:
                # Traer el proyecto primario
                proyecto_prim = (
                    db.query(Proyecto)
                      .filter(Proyecto.proyecto_id == proyecto_id_primario)
                      .first()
                )

                if proyecto_prim:
                    prim_tipo = proyecto_prim.proyecto_tipo if proyecto_prim.proyecto_tipo in valid_proyecto_tipos else ""
                    prim_nro_orden = proyecto_prim.nro_orden_rua or ""
                    prim_ingreso_por = proyecto_prim.ingreso_por or ""
                    prim_operativo = (proyecto_prim.operativo == "Y")
                    prim_login_1 = proyecto_prim.login_1
                    prim_login_2 = proyecto_prim.login_2
                    prim_fecha_asign_nro_orden = parse_date(proyecto_prim.fecha_asignacion_nro_orden)
                    prim_ultimo_cambio = parse_date(proyecto_prim.ultimo_cambio_de_estado)
                    prim_estado_general = MAPA_ESTADOS_PROYECTO.get(proyecto_prim.estado_general, proyecto_prim.estado_general)

                    # Si tu helper acepta un objeto Proyecto, podés reutilizarlo:
                    try:
                        prim_subregistro_string = build_subregistro_string(proyecto_prim)
                    except Exception:
                        # (opcional) si tu helper esperaba el row "user" con columnas de Proyecto,
                        # dejalo vacío o implementá un helper build_subregistro_string_from_proyecto(proyecto_prim)
                        prim_subregistro_string = ""



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
                # "doc_adoptante_estado": user.doc_adoptante_estado if user.doc_adoptante_estado in valid_states else "",
                "doc_adoptante_estado": (
                    "inactivo" if user.active == "N"
                    else (user.doc_adoptante_estado if user.doc_adoptante_estado in valid_states else "inicial_cargando")
                ),

                "proyecto_id": proyecto_id_primario if proyecto_id_primario is not None else "",
                "proyecto_tipo": prim_tipo,
                "nro_orden_rua": prim_nro_orden,
                "ingreso_por": prim_ingreso_por,
                "proyecto_operativo": prim_operativo,
                "login_1_info": get_user_name_by_login(db, prim_login_1) if prim_login_1 else "",
                "login_2_info": get_user_name_by_login(db, prim_login_2) if prim_login_2 else "",
                "fecha_asignacion_nro_orden": prim_fecha_asign_nro_orden,
                "ultimo_cambio_de_estado": prim_ultimo_cambio,
                "subregistro_string": prim_subregistro_string,
                "proyecto_estado_general": prim_estado_general,
                "proyectos_ids": proyectos_ids,

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




@users_router.get("/{login}", response_model=dict, dependencies=[Depends(verify_api_key)])
def get_user_by_login(
    login: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Devuelve un único usuario por su `login`.

    - 'administrador' / 'supervisora' / 'supervision' / 'profesional' / 'coordinadora' ven a cualquier usuario.
    - 'adoptante' solo puede ver su propio usuario.
    """

    # ============================================================
    # 1) CONSTANTES
    # ============================================================
    ROLES_FULL_ACCESS = {"administrador", "supervision", "supervisora", "profesional", "coordinadora"}
    VALID_STATES_USER = {"inicial_cargando", "pedido_revision", "actualizando", "aprobado", "rechazado"}
    VALID_TIPOS_PROY = {"Monoparental", "Matrimonio", "Unión convivencial"}

    SUBREG_CAMPOS = [
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

    # ============================================================
    # 2) HELPERS
    # ============================================================
    def disponibilidad_adoptiva(ddjj) -> bool:
        if not ddjj:
            return False
        return any(getattr(ddjj, c, None) == "Y" for c in SUBREG_CAMPOS)

    def tiene_datos(ddjj, campos) -> bool:
        return any(getattr(ddjj, c, None) for c in campos) if ddjj else False

    def serialize_proyecto(p) -> dict:
        """Campos útiles y compactos; no toca las claves históricas del endpoint."""
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

    # ============================================================
    # 3) AUTORIZACIÓN
    # ============================================================
    usuario_actual_login = current_user["user"]["login"]
    roles = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == usuario_actual_login)
        .all()
    )
    roles = {r.description for r in roles}

    if not (roles & ROLES_FULL_ACCESS):
        if "adoptante" in roles:
            if login != usuario_actual_login:
                raise HTTPException(status_code=403, detail="No tiene permiso para ver a otros usuarios.")
        else:
            raise HTTPException(status_code=403, detail="No tiene permisos para acceder a este recurso.")

    # ya tenés `roles` y `usuario_actual_login`
    es_autoconsulta_adoptante = ("adoptante" in roles) and (login == usuario_actual_login)

    # ============================================================
    # 4) CONSULTA PRINCIPAL (usuario + joins)
    # ============================================================
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
                User.active.label("active"),
                User.clave.label("clave"), 
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
            .join(UserGroup, User.login == UserGroup.login)
            .join(Group, UserGroup.group_id == Group.group_id)
            .outerjoin(DDJJ, User.login == DDJJ.login)
            .outerjoin(Proyecto, (User.login == Proyecto.login_1) | (User.login == Proyecto.login_2))
            .filter(User.login == login)
        )

        user = query.first()
        if not user:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        # Instancia DDJJ (para checks)
        ddjj = db.query(DDJJ).filter(DDJJ.login == user.login).first()

        # Métricas/pendientes para supervisión
        pendientes = {}
        if user.group in ['supervisora', 'supervision']:
            pendientes = {
                "doc_adoptante": db.query(User).filter(User.doc_adoptante_estado == "pedido_revision").count(),
                "doc_proyecto": db.query(Proyecto).filter(Proyecto.estado_general == "en_revision").count(),
                "proyectos_en_entrevistas": db.query(Proyecto).filter(Proyecto.estado_general.in_(["calendarizando", "entrevistando"])).count(),
                "proyectos_en_valoracion": db.query(Proyecto).filter(Proyecto.estado_general == "en_valoracion").count(),
            }

        # Checks de secciones DDJJ
        ddjj_checks = {
            "ddjj_datos_personales": tiene_datos(ddjj, [
                "ddjj_nombre", "ddjj_apellido", "ddjj_estado_civil", "ddjj_fecha_nac",
                "ddjj_nacionalidad", "ddjj_sexo", "ddjj_correo_electronico", "ddjj_telefono"
            ]),
            "ddjj_grupo_familiar_hijos": tiene_datos(ddjj, [f"ddjj_hijo{i}_nombre_completo" for i in range(1, 6)]),
            "ddjj_grupo_familiar_otros": tiene_datos(ddjj, [f"ddjj_otro{i}_nombre_completo" for i in range(1, 6)]),
            "ddjj_red_de_apoyo": tiene_datos(ddjj, [f"ddjj_apoyo{i}_nombre_completo" for i in range(1, 3)]),
            "ddjj_informacion_laboral": tiene_datos(ddjj, ["ddjj_ocupacion", "ddjj_horas_semanales", "ddjj_ingreso_mensual"]),
            "ddjj_procesos_judiciales": tiene_datos(ddjj, ["ddjj_causa_penal", "ddjj_juicios_filiacion", "ddjj_denunciado_violencia_familiar"]),
            "ddjj_disponibilidad_adoptiva": disponibilidad_adoptiva(ddjj),
            "ddjj_tramo_final": tiene_datos(ddjj, ["ddjj_guardo_1", "ddjj_guardo_2"]),
        }


        # ==============================
        # 5) PROYECTOS (mismo criterio que GET /users)
        # ==============================
        # Orden por origen
        orden_origen = case(
            (Proyecto.ingreso_por == "rua", 0),
            (Proyecto.ingreso_por == "oficio", 1),
            (Proyecto.ingreso_por == "convocatoria", 2),
            else_=3,
        )

        # Orden por estado dentro del origen
        estados_ordenados = [
            'aprobado', 'calendarizando', 'entrevistando', 'para_valorar', 'viable',
            'viable_no_disponible', 'en_suspenso', 'no_viable', 'en_carpeta',
            'vinculacion', 'guarda_provisoria', 'guarda_confirmada', 'adopcion_definitiva',
        ]
        orden_estado = case(
            *[(Proyecto.estado_general == e, i) for i, e in enumerate(estados_ordenados)],
            else_=len(estados_ordenados)
        )

        # Último historial por proyecto (para desempatar SOLO convocatoria)
        hist_max_sq = (
            db.query(
                ProyectoHistorialEstado.proyecto_id.label("pid"),
                func.max(ProyectoHistorialEstado.fecha_hora).label("last_hist"),
            )
            .group_by(ProyectoHistorialEstado.proyecto_id)
            .subquery()
        )
        last_hist_cond = case(
            (Proyecto.ingreso_por == "convocatoria", hist_max_sq.c.last_hist),
            else_=None,
        )



        proyectos_rows = (
            db.query(Proyecto.proyecto_id, Proyecto.ingreso_por)
              .outerjoin(hist_max_sq, hist_max_sq.c.pid == Proyecto.proyecto_id)
              .filter(or_(Proyecto.login_1 == user.login, Proyecto.login_2 == user.login))
              .order_by(
                  orden_origen.asc(),
                  orden_estado.asc(),
                  last_hist_cond.desc(),
                  Proyecto.proyecto_id.asc()
              )
              .all()
        )

        proyectos_ids = [r.proyecto_id for r in proyectos_rows] or []

        if es_autoconsulta_adoptante:
            # SOLO RUA. Si no hay RUA, None (no se cae a convocatoria/oficio).
            proyecto_id_primario = next(
                (r.proyecto_id for r in proyectos_rows if (r.ingreso_por or "") == "rua"),
                None
            )
        else:
            # tu comportamiento general actual (primer proyecto de la lista ordenada)
            proyecto_id_primario = proyectos_ids[0] if proyectos_ids else None



        # Defaults por si NO hay proyecto primario
        prim_tipo = ""
        prim_nro_orden = ""
        prim_ingreso_por = ""
        prim_operativo = False
        prim_login_1 = None
        prim_login_2 = None
        prim_fecha_asign_nro_orden = ""
        prim_ultimo_cambio = ""
        prim_estado_general = ""
        prim_subregistro_string = ""

        if proyecto_id_primario is not None:
            proyecto_prim = (
                db.query(Proyecto)
                  .filter(Proyecto.proyecto_id == proyecto_id_primario)
                  .first()
            )
            if proyecto_prim:
                prim_tipo = proyecto_prim.proyecto_tipo if proyecto_prim.proyecto_tipo in VALID_TIPOS_PROY else ""
                prim_nro_orden = proyecto_prim.nro_orden_rua or ""
                prim_ingreso_por = proyecto_prim.ingreso_por or ""
                prim_operativo = (proyecto_prim.operativo == "Y")
                prim_login_1 = proyecto_prim.login_1
                prim_login_2 = proyecto_prim.login_2
                prim_fecha_asign_nro_orden = parse_date(proyecto_prim.fecha_asignacion_nro_orden)
                prim_ultimo_cambio = parse_date(proyecto_prim.ultimo_cambio_de_estado)
                prim_estado_general = proyecto_prim.estado_general or ""
                try:
                    prim_subregistro_string = build_subregistro_string(proyecto_prim)
                except Exception:
                    prim_subregistro_string = ""


        # ==============================
        # 6) Estado botón (usar proyecto_id_primario/prim_estado_general)
        # ==============================
        docs_de_pretenso_presentados = all([
            user.doc_adoptante_salud, user.doc_adoptante_domicilio,
            user.doc_adoptante_dni_frente, user.doc_adoptante_dni_dorso,
            user.doc_adoptante_deudores_alimentarios, user.doc_adoptante_antecedentes,
        ])
        docs_todos_vacios = all([
            not user.doc_adoptante_salud, not user.doc_adoptante_domicilio,
            not user.doc_adoptante_dni_frente, not user.doc_adoptante_dni_dorso,
            not user.doc_adoptante_deudores_alimentarios, not user.doc_adoptante_antecedentes,
        ])

        # 👇 PRIORIDAD MÁXIMA: si no tiene clave → “SIN CLAVE”
        if not ( (user.clave or "").strip() ):
            texto_boton_estado_pretenso = "SIN CLAVE"
        elif user.active == "N":
            texto_boton_estado_pretenso = "INACTIVO"
        elif user.doc_adoptante_curso_aprobado == "N":
            texto_boton_estado_pretenso = "CURSO PENDIENTE"
        elif user.doc_adoptante_curso_aprobado == "Y" and user.doc_adoptante_ddjj_firmada == "N":
            texto_boton_estado_pretenso = "LLENANDO DDJJ"
        elif (user.doc_adoptante_curso_aprobado == "Y" and user.doc_adoptante_ddjj_firmada == "Y" and docs_todos_vacios):
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
                "guarda_provisoria": "GUARDA PROVISORIA",
                "guarda_confirmada": "GUARDA CONFIRMADA",
                "adopcion_definitiva": "ADOPCIÓN DEFINITIVA",
                "baja_anulacion": "BAJA - ANULACIÓN",
                "baja_caducidad": "BAJA - CADUCIDAD",
                "baja_interrupcion": "BAJA - INTERRUPCIÓN",
                "baja_por_convocatoria": "BAJA - CONVOCATORIA",
                "baja_rechazo_invitacion": "BAJA - RECHAZO INVITACIÓN",
                "inactivo": "INACTIVO",
            }
            if not proyecto_id_primario:
                texto_boton_estado_pretenso = estado_a_texto.get(user.doc_adoptante_estado, user.doc_adoptante_estado or "DOC. PERSONAL")
            else:
                texto_boton_estado_pretenso = estado_a_texto.get(prim_estado_general, prim_estado_general or "DOC. PROYECTO")


        # ============================================================
        # 7) RESPUESTA (mantiene todas las claves previas + agrega 4 listas)
        # ============================================================
        user_dict = {
            "login": user.login,
            "nombre": user.nombre or "",
            "apellido": user.apellido or "",
            "celular": user.celular or "",
            "operativo": user.operativo or "N",
            "mail": user.mail or "",
            "calle_y_nro": user.calle_y_nro or "",
            "depto_etc": user.depto_etc or "",
            "barrio": user.barrio or "",
            "localidad": user.localidad or "",
            "provincia": user.provincia or "",
            "cp": user.ddjj_cp_legal or (user.ddjj_cp or ""),
            "fecha_alta": parse_date(user.fecha_alta),
            "group": user.group or "Sin grupo asignado",
            "ddjj_fecha_nac": parse_date(user.ddjj_fecha_nac),
            "edad_segun_ddjj": calculate_age(user.ddjj_fecha_nac),
            "doc_adoptante_curso_aprobado": user.doc_adoptante_curso_aprobado == "Y",
            "doc_adoptante_ddjj_firmada": user.doc_adoptante_ddjj_firmada == "Y",
            "doc_adoptante_estado": (
                "inactivo" if user.active == "N"
                else (user.doc_adoptante_estado if user.doc_adoptante_estado in VALID_STATES_USER else "inicial_cargando")
            ),
            "doc_adoptante_salud": user.doc_adoptante_salud,
            "doc_adoptante_domicilio": user.doc_adoptante_domicilio,
            "doc_adoptante_dni_frente": user.doc_adoptante_dni_frente,
            "doc_adoptante_dni_dorso": user.doc_adoptante_dni_dorso,
            "doc_adoptante_deudores_alimentarios": user.doc_adoptante_deudores_alimentarios,
            "doc_adoptante_antecedentes": user.doc_adoptante_antecedentes,
            "doc_adoptante_migraciones": user.doc_adoptante_migraciones,


            # Campos de proyecto: siempre referencian al PRIMARIO (RUA). Si no hay RUA, van vacíos.
            "proyecto_id": proyecto_id_primario if proyecto_id_primario is not None else "",
            "proyecto_tipo": prim_tipo,
            "nro_orden_rua": prim_nro_orden,
            "ingreso_por": prim_ingreso_por,
            "proyecto_operativo": prim_operativo,
            "login_1_info": get_user_name_by_login(db, prim_login_1) if prim_login_1 else "",
            "login_2_info": get_user_name_by_login(db, prim_login_2) if prim_login_2 else "",
            "fecha_asignacion_nro_orden": prim_fecha_asign_nro_orden,
            "ultimo_cambio_de_estado": prim_ultimo_cambio,
            "subregistro_string": prim_subregistro_string,
            "proyecto_estado_general": prim_estado_general,

            # Lista completa ordenada
            "proyectos_ids": proyectos_ids,

            # Flags/botones que dependían de user.proyecto_id
            "mostrar_boton_proyecto": bool(proyecto_id_primario),
            "boton_ver_proyecto": bool(proyecto_id_primario),

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
            "ddjj_firmada": user.doc_adoptante_ddjj_firmada == "Y",

            # "mostrar_boton_proyecto": bool(user.proyecto_id),
            "boton_aprobar_documentacion": docs_de_pretenso_presentados and user.doc_adoptante_estado == "pedido_revision",
            "boton_solicitar_actualizacion": docs_de_pretenso_presentados and (user.doc_adoptante_estado in {"pedido_revision", "aprobado"}),
            # "boton_ver_proyecto": bool(user.proyecto_id),
            "texto_boton_estado_pretenso": texto_boton_estado_pretenso,

        }

        return user_dict

    except SQLAlchemyError as e:
        print(str(e))
        raise HTTPException(status_code=500, detail=f"Error al recuperar el usuario: {str(e)}")





@users_router.post("/", response_model=dict, dependencies=[Depends(verify_api_key)])
async def create_user(
    request: Request, 
    db: Session = Depends(get_db)    
):
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

    body = await request.json()
    recaptcha_token = body.get("recaptcha_token")
    if not recaptcha_token:
        return {
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Debe completar el reCAPTCHA.</p>"
                "<p>Intentá nuevamente.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    if not await verificar_recaptcha(recaptcha_token, request.client.host):
        return {
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>No se pudo verificar que sos humano.</p>"
                "<p>Intentá nuevamente.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    # Extraer datos de entrada

    dni = normalizar_y_validar_dni(body.get("dni")) 
    if not dni: 
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Debe indicar un DNI válido.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    clave = body.get("clave", "")
    confirm_clave = body.get("confirm_clave", "")
    nombre = capitalizar_nombre(body.get("nombre", ""))
    apellido = capitalizar_nombre(body.get("apellido", ""))
    mail = body.get("mail", "").lower()
    group_description = body.get("group_description", "")

    celular_raw = body.get("celular", "")
    resultado_validacion_celular = normalizar_celular(celular_raw)

    if resultado_validacion_celular["valido"]:
        celular = resultado_validacion_celular["celular"]
    else:
        return {
            "tipo_mensaje": "naranja",
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
                "<p>Por favor, comunicarse con personal del RUA.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if db.query(User).filter( User.mail == mail ).first() :
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Ya existe un usuario con ese mail en el Sistema RUA.</p>"
                "<p>Por favor, comunicarse con personal del RUA.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # Verificar que las contraseñas coincidan
    if clave != confirm_clave:
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Las contraseñas no coinciden.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    

    
    # —— Validación de política de contraseñas ——
    # 1) Al menos 6 dígitos en cualquier posición
    dígitos = [c for c in clave if c.isdigit()]
    if len(dígitos) < 6:
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>La contraseña debe tener al menos 6 números no consecutivos, una mayúscula y una minúscula.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # 2) Sin secuencias de 3 dígitos consecutivos
    if check_consecutive_numbers(clave):
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>La contraseña no puede contener secuencias numéricas consecutivas "
                "(por ejemplo “1234” o “4321”).</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # 3) Al menos una letra mayúscula
    if not any(c.isupper() for c in clave):
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>La contraseña debe tener al menos 6 números no consecutivos, una mayúscula y una minúscula.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    # 4) Al menos una letra minúscula
    if not any(c.islower() for c in clave):
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>La contraseña debe tener al menos 6 números no consecutivos, una mayúscula y una minúscula.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    # ——————————————————————————————

    # Validar formato de correo
    if not validar_correo(mail):
        return {
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>El correo electrónico no tiene un formato válido.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    
    
    # Validar que el grupo sea uno de los permitidos
    allowed_groups = ["adoptante", "profesional", "supervision", "supervisora", "administrador"]
    if group_description not in allowed_groups :
        raise HTTPException(status_code=400, detail=f"El grupo debe ser uno de: {', '.join(allowed_groups)}")

    # Buscar en la tabla de grupos el grupo correspondiente a la descripción
    group = db.query(Group).filter(Group.description == group_description).first()
    if not group:
        raise HTTPException(status_code=400, detail="El grupo seleccionado no existe en la base de datos.")


    
    if group_description == "adoptante":
        try:
            dni_en_moodle = existe_dni_en_moodle(dni, db)
            mail_en_moodle = existe_mail_en_moodle(mail, db)

            if dni_en_moodle and mail_en_moodle:
                # ✅ Caso 1: Ambos existen → Actualizar contraseña en Moodle
                actualizar_clave_en_moodle(mail, clave, db)

            elif dni_en_moodle and not mail_en_moodle:
                # ❌ Caso 2: Existe el usuario con ese DNI pero otro mail → error
                return {
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        "<p><strong>Ya tenés una cuenta creada en nuestro sistema de capacitación (Moodle) con este DNI.</strong></p>"
                        "<p>Para completar tu inscripción en el Sistema RUA, debés utilizar el mismo correo electrónico que está asociado a tu cuenta en Moodle.</p>"
                        "<p>Si no recordás qué correo usaste, ingresá a <a href='https://campusvirtual2.justiciacordoba.gob.ar/login/index.php' target='_blank'>https://campusvirtual2.justiciacordoba.gob.ar</a> y usá tu DNI como nombre de usuario.</p>"
                        "<p>Desde allí vas a poder acceder a tu cuenta, recuperar tu contraseña si lo necesitás, y verificar o actualizar el correo electrónico desde tu perfil.</p>"
                        "<p>Cuando tengas acceso a tu cuenta y sepas qué mail está asociado, volvé a esta página e ingresalo para continuar con tu registro en el Sistema RUA.</p>"
                    ),
                    "tiempo_mensaje": 7,
                    "next_page": "actual"
                }

            elif not dni_en_moodle and mail_en_moodle:
                # ❌ Caso 3: Existe el mail, pero no el DNI (username) → error
                return {
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        "<p><strong>El correo electrónico que ingresaste ya está siendo utilizado en nuestro sistema de capacitación (Moodle).</strong></p>"
                        "<p>Esto significa que ya existe una cuenta en Moodle asociada a ese correo, posiblemente con otro DNI como nombre de usuario.</p>"
                        "<p>Por motivos de seguridad y para evitar conflictos, necesitás registrar un correo diferente que no esté en uso en Moodle.</p>"
                        "<p>Si querés verificar qué cuentas tenés en Moodle, podés ingresar a <a href='https://campusvirtual2.justiciacordoba.gob.ar/login/index.php' target='_blank'>https://campusvirtual2.justiciacordoba.gob.ar</a> usando tus datos habituales.</p>"
                        "<p>Una vez que tengas identificado un correo alternativo, volvé a esta página e ingresalo para completar tu registro en el Sistema RUA.</p>"
                    ),
                    "tiempo_mensaje": 7,
                    "next_page": "actual"
                }

            else:
                # ✅ Caso 4: No existe el usuario en Moodle → crearlo
                crear_usuario_en_moodle(dni, clave, nombre, apellido, mail, db)

            # En todos los casos válidos: obtener ID y enrolar
            id_curso = get_idcurso(db)
            id_usuario = get_idusuario_by_mail(mail, db)
            enrolar_usuario(id_curso, id_usuario, db)

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



    # if group_description == "adoptante":
    #     try:
    #         if existe_mail_en_moodle(mail, db):
    #             return {
    #                 "tipo_mensaje": "naranja",
    #                 "mensaje": (
    #                     "<p>Ya existe un usuario con ese mail en nuestro sistema de capacitación (Moodle).</p>"
    #                     "<p>Por favor, comunicarse con personal del RUA.</p>"
    #                 ),
    #                 "tiempo_mensaje": 5,
    #                 "next_page": "actual"
    #             }

    #         if existe_dni_en_moodle(dni, db):
    #             return {
    #                 "tipo_mensaje": "naranja",
    #                 "mensaje": (
    #                     "<p>Ya existe un usuario con ese DNI en nuestro sistema de capacitación (Moodle).</p>"
    #                     "<p>Por favor, comunicarse con personal del RUA.</p>"
    #                 ),
    #                 "tiempo_mensaje": 5,
    #                 "next_page": "actual"
    #             }

    #         retorno = crear_usuario_en_moodle(dni, clave, nombre, apellido, mail, db)
    #         print('crear_usuario_en_moodle', retorno)

    #         id_curso = get_idcurso(db)
    #         id_usuario = get_idusuario_by_mail(mail, db)

    #         retorno = enrolar_usuario(id_curso, id_usuario, db)
    #         print('enrolar_usuario', retorno)

    #     except HTTPException as e:
    #         return {
    #             "tipo_mensaje": "rojo",
    #             "mensaje": (
    #                 "<p>No se pudo completar el registro en el sistema de capacitación (Moodle).</p>"
    #                 f"<p>Detalle técnico: {e.detail}</p>"
    #                 "<p>Por favor, intente más tarde o comuníquese con personal del RUA.</p>"
    #             ),
    #             "tiempo_mensaje": 10,
    #             "next_page": "actual"
    #         }


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
        # Agregar todos los objetos
        db.add(new_user)
        db.add(new_user_group)
        db.commit()              # 👈 commit primero el user y grupo
        db.refresh(new_user)


        nuevo_evento = RuaEvento(
            login=dni,
            evento_detalle="Nuevo usuario registrado.",
            evento_fecha=datetime.now()
        )
        db.add(nuevo_evento)

        # Un solo commit
        db.commit()

    
    except SQLAlchemyError as e:
        db.rollback()
        print("⚠️ Error al hacer commit:", str(e))

        return {
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al registrar el usuario.</p>"
                "<p>Por favor, intente nuevamente o comuníquese con personal del RUA.</p>"
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
                          <strong>¡Hola {nombre}!</strong>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top: 20px; font-size: 17px;">
                          <p>¡Bienvenido a la plataforma virtual del <strong>Registro Único de Adopciones de Córdoba</strong>!</p>
                            <p>Para iniciar el proceso de inscripción hace clic en el siguiente botón y completa el curso informativo obligatorio:</p>
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
                                  Confirmo mi registro en el sistema
                              </a>
                            </td>
                          </tr>
                        </table>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top: 20px; font-size: 17px;">
                        <p>Para ingresar usa el mismo usuario y contraseña que creaste en la plataforma.</p>
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
            "<p>Se ha registrado en el sistema y podrá acceder una vez activado, revise su correo electrónico.</p>"
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
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante"]))])
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
    file.file.seek(0, os.SEEK_END)   # Ir al final del archivo
    size = file.file.tell()          # Obtener posición (bytes)
    if size > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="El archivo excede el tamaño máximo permitido (5 MB)."
        )
    file.file.seek(0)  # Volver al inicio para la lectura
    

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
                "<p>La solicitud de revisión de su documentación personal fue enviada correctamente.</p>"
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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
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
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante"]))])
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
                                 Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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
    if not grupo_observador or grupo_observador.description not in ["supervision", "supervisora", "profesional", "administrador"]:
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
                                 Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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
    if not grupo_observador or grupo_observador.description not in ["supervision", "supervisora", "profesional", "administrador"]:
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
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante", "coordinadora"]))])
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
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante", "coordinadora"]))])
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
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "adoptante", "coordinadora"]))])
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
                                Depends(require_roles(["administrador", "supervision", "supervisora"]))])
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

        # —— Validación de política de contraseñas ——
        # a) Al menos 6 dígitos numéricos en cualquier posición
        dígitos = [c for c in nueva_clave if c.isdigit()]
        if len(dígitos) < 6:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "La contraseña debe contener al menos 6 dígitos numéricos.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # b) Sin secuencias de 3 dígitos consecutivos
        if check_consecutive_numbers(nueva_clave):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": (
                    "La contraseña no puede contener secuencias numéricas consecutivas "
                    "(p.ej. “1234” o “4321”)."
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # c) Al menos una letra mayúscula
        if not any(c.isupper() for c in nueva_clave):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "La contraseña debe incluir al menos una letra mayúscula.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # d) Al menos una letra minúscula
        if not any(c.islower() for c in nueva_clave):
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": "La contraseña debe incluir al menos una letra minúscula.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
        # ——————————————————————————————

        # 🔄 Moodle solo si es adoptante
        user_group = db.query(UserGroup).filter(UserGroup.login == login).first()
        if user_group:
            group = db.query(Group).filter(Group.group_id == user_group.group_id).first()
            if group and group.description and group.description.lower() == "adoptante":
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



# @users_router.post("/{login}/reenviar-activacion", response_model=dict,
#     dependencies=[ Depends(verify_api_key),
#                    Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"])) ])
# def reenviar_mail_activacion(login: str, db: Session = Depends(get_db)):
#     """
#     Envía un mail al usuario para que elija una nueva contraseña (flujo recuperar clave).
#     Si el usuario NO tiene clave aún, primero se asegura:
#       - creación del usuario en Moodle (si no existe)
#       - enrolamiento al curso correspondiente
#     """
#     user: User = db.query(User).filter(User.login == login).first()
#     if not user:
#         return {
#             "success": False,
#             "tipo_mensaje": "naranja",
#             "mensaje": "<p>No se encontró un usuario con ese login.</p>",
#             "tiempo_mensaje": 6,
#             "next_page": "actual",
#         }

#     if not user.mail:
#         return {
#             "success": False,
#             "tipo_mensaje": "naranja",
#             "mensaje": (
#                 "<p>El usuario no tiene un correo electrónico asociado.</p>"
#                 "<p>No es posible enviar el enlace para elegir contraseña.</p>"
#             ),
#             "tiempo_mensaje": 7,
#             "next_page": "actual",
#         }

#     # ---------------------------
#     # Helpers para Moodle
#     # ---------------------------

#     def _generar_password_temporal_moodle() -> str:
#         """
#         Requisitos:
#           - >= 6 dígitos
#           - dígitos no consecutivos (sin secuencias asc/desc de largo >=3)
#           - >= 1 mayúscula
#           - >= 1 minúscula
#           - largo total >= 10
#         """
#         import random, string

#         def _no_consecutivos(n=6):
#             res = []
#             while len(res) < n:
#                 d = random.choice("0123456789")
#                 if not res:
#                     res.append(d)
#                 else:
#                     # evitar vecinos ascend/descend inmediatos (…-3-4… o …-4-3…)
#                     if abs(int(d) - int(res[-1])) != 1:
#                         res.append(d)
#             return res

#         def _check_consecutive_numbers(s: str) -> bool:
#             # True si hay secuencias numéricas consecutivas asc/desc (largo >=3)
#             nums = [c for c in s if c.isdigit()]
#             if len(nums) < 3:
#                 return False
#             # revisar ventana de 3 en 3
#             for i in range(len(nums) - 2):
#                 a, b, c = int(nums[i]), int(nums[i+1]), int(nums[i+2])
#                 if (b - a == 1 and c - b == 1) or (a - b == 1 and b - c == 1):
#                     return True
#             return False

#         import random, string
#         while True:
#             digs = _no_consecutivos(6)
#             must = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase)]
#             extra = [random.choice(string.ascii_letters) for _ in range(4)]  # total ~10
#             chars = digs + must + extra
#             random.shuffle(chars)
#             pwd = "".join(chars)
#             if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) \
#                and sum(c.isdigit() for c in pwd) >= 6 and not _check_consecutive_numbers(pwd):
#                 return pwd

#     def _asegurar_moodle_y_enrolamiento(u: User):
#         """
#         - Si existe (dni y mail) -> asegurar enrolamiento.
#         - Si hay conflicto (dni con otro mail, o mail con otro dni) -> loguea evento y omite.
#         - Si no existe -> crea con clave temporal que cumple política y enrola.
#         """
#         try:
#             dni = str(u.login)
#             mail = (u.mail or "").lower()
#             nombre = u.nombre or ""
#             apellido = u.apellido or ""

#             if not dni or not mail:
#                 db.add(RuaEvento(
#                     login=u.login,
#                     evento_detalle="Moodle: omitido por datos insuficientes (dni/mail).",
#                     evento_fecha=datetime.now()
#                 ))
#                 db.commit()
#                 return

#             dni_en_moodle = existe_dni_en_moodle(dni, db)
#             mail_en_moodle = existe_mail_en_moodle(mail, db)

#             if dni_en_moodle and mail_en_moodle:
#                 # sólo enrolar
#                 id_curso = get_idcurso(db)
#                 id_usuario = get_idusuario_by_mail(mail, db)
#                 enrolar_usuario(id_curso, id_usuario, db)
#                 db.add(RuaEvento(
#                     login=u.login,
#                     evento_detalle="Moodle: usuario ya existía; enrolamiento asegurado.",
#                     evento_fecha=datetime.now()
#                 ))
#                 db.commit()
#                 return

#             if dni_en_moodle and not mail_en_moodle:
#                 db.add(RuaEvento(
#                     login=u.login,
#                     evento_detalle="Moodle: conflicto (existe DNI con otro mail). Enrolamiento omitido.",
#                     evento_fecha=datetime.now()
#                 ))
#                 db.commit()
#                 return

#             if not dni_en_moodle and mail_en_moodle:
#                 db.add(RuaEvento(
#                     login=u.login,
#                     evento_detalle="Moodle: conflicto (existe mail con otro DNI). Enrolamiento omitido.",
#                     evento_fecha=datetime.now()
#                 ))
#                 db.commit()
#                 return

#             # no existe -> crearlo y enrolar
#             clave_tmp = _generar_password_temporal_moodle()
#             crear_usuario_en_moodle(dni, clave_tmp, nombre, apellido, mail, db)

#             id_curso = get_idcurso(db)
#             id_usuario = get_idusuario_by_mail(mail, db)
#             enrolar_usuario(id_curso, id_usuario, db)

#             db.add(RuaEvento(
#                 login=u.login,
#                 evento_detalle="Moodle: usuario creado y enrolado con clave temporal.",
#                 evento_fecha=datetime.now()
#             ))
#             db.commit()

#         except HTTPException as e:
#             db.rollback()
#             db.add(RuaEvento(
#                 login=u.login,
#                 evento_detalle=f"Moodle: error HTTP al crear/enrolar ({e.detail}).",
#                 evento_fecha=datetime.now()
#             ))
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             db.add(RuaEvento(
#                 login=u.login,
#                 evento_detalle=f"Moodle: error inesperado al crear/enrolar ({str(e)}).",
#                 evento_fecha=datetime.now()
#             ))
#             db.commit()

#     try:
#         # 1) Habilitar flujo de nueva clave (activar si estaba inactivo)
#         recien_activado = False
#         if user.active != "Y":
#             user.active = "Y"
#             recien_activado = True

#         # 1.b) Si NO tiene clave, asegurar Moodle + enrolamiento (como en agendar_entrevista)
#         if not (user.clave or "").strip():
#             _asegurar_moodle_y_enrolamiento(user)

#         # 2) Usar código existente o generarlo si falta
#         rec_code = (user.recuperacion_code or "").strip()
#         if not rec_code:
#             rec_code = generar_codigo_para_link(16)
#             user.recuperacion_code = rec_code

#         db.commit()
#         db.refresh(user)

#         # 3) Link de recuperar clave
#         protocolo = get_setting_value(db, "protocolo")
#         host = get_setting_value(db, "donde_esta_alojado")
#         puerto = get_setting_value(db, "puerto_tcp")
#         endpoint = get_setting_value(db, "endpoint_recuperar_clave")

#         if endpoint and not endpoint.startswith("/"):
#             endpoint = "/" + endpoint

#         puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
#         host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host
#         link = f"{protocolo}://{host_con_puerto}{endpoint}?activacion={rec_code}"

#         # 4) Email
#         asunto = "Establecimiento de tu contraseña"
#         cuerpo = f"""
#         <html>
#           <body style="margin:0;padding:0;background-color:#f8f9fa;">
#             <table cellpadding="0" cellspacing="0" width="100%" style="background-color:#f8f9fa;padding:20px;">
#               <tr>
#                 <td align="center">
#                   <table cellpadding="0" cellspacing="0" width="600" style="background:#ffffff;border-radius:10px;padding:30px;font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;color:#343a40;box-shadow:0 0 10px rgba(0,0,0,0.1);">
#                     <tr>
#                       <td style="font-size:24px;color:#007bff;">
#                         <strong>¡Hola {user.nombre or ""}!</strong>
#                       </td>
#                     </tr>
#                     <tr>
#                       <td style="padding-top: 20px; font-size: 17px;">
#                         <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong> para que puedas colocar tu contraseña para ingresar a la plataforma.</p>
#                       </td>
#                     </tr>
#                     <tr>
#                       <td style="padding-top:18px;font-size:17px;line-height:1.5;">
#                         <p>Hacé clic en el botón para definirla. Una vez guardada, ya vas a poder ingresar con tu DNI y la clave elegida.</p>
#                       </td>
#                     </tr>
#                     <tr>
#                       <td align="center" style="padding:26px 0;">
#                         <a href="{link}" target="_blank"
#                            style="display:inline-block;padding:12px 24px;font-size:16px;color:#ffffff;background:#0d6efd;text-decoration:none;border-radius:8px;font-weight:600;">
#                           🔐 Crear mi contraseña
#                         </a>
#                       </td>
#                     </tr>
#                     <tr>
#                       <td style="font-size:14px;color:#666;line-height:1.5;">
#                         <p>El enlace es temporal. Si no solicitaste este correo, podés ignorarlo.</p>
#                       </td>
#                     </tr>
#                     <tr>
#                       <td style="padding-top:10px;font-size:13px;color:#888;">
#                         Registro Único de Adopciones de Córdoba
#                       </td>
#                     </tr>
#                   </table>
#                 </td>
#               </tr>
#             </table>
#           </body>
#         </html>
#         """

#         enviar_mail(destinatario=user.mail, asunto=asunto, cuerpo=cuerpo)

#         # 5) Evento (best effort)
#         try:
#             detalle = "Se envió enlace para elegir nueva contraseña."
#             if recien_activado:
#                 detalle += " El usuario estaba inactivo y fue activado para habilitar el cambio de clave."
#             db.add(RuaEvento(
#                 login=user.login,
#                 evento_detalle=detalle,
#                 evento_fecha=datetime.now()
#             ))
#             db.commit()
#         except Exception:
#             db.rollback()

#         return {
#             "success": True,
#             "tipo_mensaje": "verde",
#             "mensaje": "<p>Se envió un correo al pretenso para que defina su nueva contraseña.</p>",
#             "tiempo_mensaje": 7,
#             "next_page": "actual",
#         }

#     except Exception as e:
#         db.rollback()
#         return {
#             "success": False,
#             "tipo_mensaje": "rojo",
#             "mensaje": f"<p>No fue posible enviar el correo.</p><p>Detalle: {str(e)}</p>",
#             "tiempo_mensaje": 8,
#             "next_page": "actual",
#         }




@users_router.post("/{login}/reenviar-activacion", response_model=dict,
    dependencies=[ Depends(verify_api_key),
                   Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"])) ])
def reenviar_mail_activacion(login: str, db: Session = Depends(get_db)):
    """
    Envía un mail al usuario para que **elija una nueva contraseña** usando el flujo de *recuperar clave*.
    - Si ya existe `recuperacion_code` → se reutiliza (no se regenera).
    - **No** se toca `clave`.
    - Si el usuario está inactivo, se lo activa (`active="Y"`) para que el flujo de nueva clave funcione.

    Si el usuario NO tiene clave aún, primero se asegura:
    - creación del usuario en Moodle (si no existe)
    - enrolamiento al curso correspondiente
    """
    user: User = db.query(User).filter(User.login == login).first()
    if not user:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "<p>No se encontró un usuario con ese login.</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }

    if not user.mail:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "<p>El usuario no tiene un correo electrónico asociado.</p>"
                       "<p>No es posible enviar el enlace para elegir contraseña.</p>",
            "tiempo_mensaje": 7,
            "next_page": "actual",
        }

    # ---------------------------
    # Helpers para Moodle
    # ---------------------------

    def _generar_password_temporal_moodle() -> str:
        """
        Requisitos:
          - >= 6 dígitos
          - dígitos no consecutivos (sin secuencias asc/desc de largo >=3)
          - >= 1 mayúscula
          - >= 1 minúscula
          - largo total >= 10
        """
        import random, string

        def _no_consecutivos(n=6):
            res = []
            while len(res) < n:
                d = random.choice("0123456789")
                if not res:
                    res.append(d)
                else:
                    # evitar vecinos ascend/descend inmediatos (…-3-4… o …-4-3…)
                    if abs(int(d) - int(res[-1])) != 1:
                        res.append(d)
            return res

        def _check_consecutive_numbers(s: str) -> bool:
            # True si hay secuencias numéricas consecutivas asc/desc (largo >=3)
            nums = [c for c in s if c.isdigit()]
            if len(nums) < 3:
                return False
            # revisar ventana de 3 en 3
            for i in range(len(nums) - 2):
                a, b, c = int(nums[i]), int(nums[i+1]), int(nums[i+2])
                if (b - a == 1 and c - b == 1) or (a - b == 1 and b - c == 1):
                    return True
            return False

        import random, string
        while True:
            digs = _no_consecutivos(6)
            must = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase)]
            extra = [random.choice(string.ascii_letters) for _ in range(4)]  # total ~10
            chars = digs + must + extra
            random.shuffle(chars)
            pwd = "".join(chars)
            if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) \
               and sum(c.isdigit() for c in pwd) >= 6 and not _check_consecutive_numbers(pwd):
                return pwd

    def _asegurar_moodle_y_enrolamiento(u: User):
        """
        - Si existe (dni y mail) -> asegurar enrolamiento.
        - Si hay conflicto (dni con otro mail, o mail con otro dni) -> loguea evento y omite.
        - Si no existe -> crea con clave temporal que cumple política y enrola.
        """
        try:
            dni = str(u.login)
            mail = (u.mail or "").lower()
            nombre = u.nombre or ""
            apellido = u.apellido or ""

            if not dni or not mail:
                db.add(RuaEvento(
                    login=u.login,
                    evento_detalle="Moodle: omitido por datos insuficientes (dni/mail).",
                    evento_fecha=datetime.now()
                ))
                db.commit()
                return

            dni_en_moodle = existe_dni_en_moodle(dni, db)
            mail_en_moodle = existe_mail_en_moodle(mail, db)

            if dni_en_moodle and mail_en_moodle:
                # sólo enrolar
                id_curso = get_idcurso(db)
                id_usuario = get_idusuario_by_mail(mail, db)
                enrolar_usuario(id_curso, id_usuario, db)
                db.add(RuaEvento(
                    login=u.login,
                    evento_detalle="Moodle: usuario ya existía; enrolamiento asegurado.",
                    evento_fecha=datetime.now()
                ))
                db.commit()
                return

            if dni_en_moodle and not mail_en_moodle:
                db.add(RuaEvento(
                    login=u.login,
                    evento_detalle="Moodle: conflicto (existe DNI con otro mail). Enrolamiento omitido.",
                    evento_fecha=datetime.now()
                ))
                db.commit()
                return

            if not dni_en_moodle and mail_en_moodle:
                db.add(RuaEvento(
                    login=u.login,
                    evento_detalle="Moodle: conflicto (existe mail con otro DNI). Enrolamiento omitido.",
                    evento_fecha=datetime.now()
                ))
                db.commit()
                return

            # no existe -> crearlo y enrolar
            clave_tmp = _generar_password_temporal_moodle()
            crear_usuario_en_moodle(dni, clave_tmp, nombre, apellido, mail, db)

            id_curso = get_idcurso(db)
            id_usuario = get_idusuario_by_mail(mail, db)
            enrolar_usuario(id_curso, id_usuario, db)

            db.add(RuaEvento(
                login=u.login,
                evento_detalle="Moodle: usuario creado y enrolado con clave temporal.",
                evento_fecha=datetime.now()
            ))
            db.commit()

        except HTTPException as e:
            db.rollback()
            db.add(RuaEvento(
                login=u.login,
                evento_detalle=f"Moodle: error HTTP al crear/enrolar ({e.detail}).",
                evento_fecha=datetime.now()
            ))
            db.commit()
        except Exception as e:
            db.rollback()
            db.add(RuaEvento(
                login=u.login,
                evento_detalle=f"Moodle: error inesperado al crear/enrolar ({str(e)}).",
                evento_fecha=datetime.now()
            ))
            db.commit()


    try:
        # 1) Asegurar que pueda usar el flujo de nueva clave:
        #    Si está inactivo, lo activamos (tu endpoint /nueva-clave requiere active == "Y").
        recien_activado = False
        if user.active != "Y":
            user.active = "Y"
            recien_activado = True

        # 1.b) Si NO tiene clave, asegurar Moodle + enrolamiento (como en agendar_entrevista)
        if not (user.clave or "").strip():
            _asegurar_moodle_y_enrolamiento(user)


        # 2) Usar código de recuperación existente o generarlo si falta (no invalidamos correos previos).
        rec_code = (user.recuperacion_code or "").strip()
        if not rec_code:
            rec_code = generar_codigo_para_link(16)
            user.recuperacion_code = rec_code

        db.commit()
        db.refresh(user)

        # 3) Construir el link de "recuperar clave" desde settings
        protocolo = get_setting_value(db, "protocolo")
        host = get_setting_value(db, "donde_esta_alojado")
        puerto = get_setting_value(db, "puerto_tcp")
        endpoint = get_setting_value(db, "endpoint_recuperar_clave")

        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
        host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

        link = f"{protocolo}://{host_con_puerto}{endpoint}?activacion={rec_code}"

        # 4) Email (mismo estilo que /recuperar-clave)
        asunto = "Establecimiento de tu contraseña"

        cuerpo = f"""
        <html>
          <body style="margin:0;padding:0;background-color:#f8f9fa;">
            <table cellpadding="0" cellspacing="0" width="100%" style="background-color:#f8f9fa;padding:20px;">
              <tr>
                <td align="center">
                  <table cellpadding="0" cellspacing="0" width="600" style="background:#ffffff;border-radius:10px;padding:30px;font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;color:#343a40;box-shadow:0 0 10px rgba(0,0,0,0.1);">
                    <tr>
                      <td style="font-size:24px;color:#007bff;">
                        <strong>¡Hola {user.nombre or ""}!</strong>
                      </td>
                    </tr>

                    <tr>
                      <td style="padding-top: 20px; font-size: 17px;">
                          <p>Nos comunicamos desde del <strong>Registro Único de Adopciones de Córdoba</strong>
                            para que puedas colocar tu contraseña para ingresar a la plataforma.
                      </td>
                    </tr>

                    <tr>
                      <td style="padding-top:18px;font-size:17px;line-height:1.5;">
                        <p>Hacé clic en el botón para definirla. Una vez guardada, ya vas a poder ingresar con tu DNI y la clave elegida.</p>
                      </td>
                    </tr>

                    <tr>
                      <td align="center" style="padding:26px 0;">
                        <a href="{link}" target="_blank"
                          style="display:inline-block;padding:12px 24px;font-size:16px;color:#ffffff;background:#0d6efd;text-decoration:none;border-radius:8px;font-weight:600;">
                          🔐 Crear mi contraseña
                        </a>
                      </td>
                    </tr>

                    <tr>
                      <td style="font-size:14px;color:#666;line-height:1.5;">
                        <p>El enlace es temporal. Si no solicitaste este correo, podés ignorarlo.</p>
                      </td>
                    </tr>

                    <tr>
                      <td style="padding-top:10px;font-size:13px;color:#888;">
                        Registro Único de Adopciones de Córdoba
                      </td>
                    </tr>

                  </table>
                </td>
              </tr>
            </table>
          </body>
        </html>
        """

        enviar_mail(destinatario=user.mail, asunto=asunto, cuerpo=cuerpo)

        # 5) Evento (best effort)
        try:
            detalle = "Se envió enlace para elegir nueva contraseña."
            if recien_activado:
                detalle += " El usuario estaba inactivo y fue activado para habilitar el cambio de clave."
            db.add(RuaEvento(
                login=user.login,
                evento_detalle=detalle,
                evento_fecha=datetime.now()
            ))
            db.commit()
        except Exception:
            db.rollback()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "<p>Se envió un correo al pretenso para que defina su nueva contraseña.</p>",
            "tiempo_mensaje": 7,
            "next_page": "actual",
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"<p>No fue posible enviar el correo.</p><p>Detalle: {str(e)}</p>",
            "tiempo_mensaje": 8,
            "next_page": "actual",
        }






@users_router.get("/timeline/{login}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
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


    # proyecto = db.query(Proyecto).filter(
    #     or_(Proyecto.login_1 == login, Proyecto.login_2 == login)
    # ).first()

    # --- Proyecto primario SOLO si es RUA ---
    # Orden por estado (mismo criterio que usás en otros endpoints para estabilidad)
    estados_ordenados = [
        'aprobado', 'calendarizando', 'entrevistando', 'para_valorar', 'viable',
        'viable_no_disponible', 'en_suspenso', 'no_viable', 'en_carpeta',
        'vinculacion', 'guarda_provisoria', 'guarda_confirmada', 'adopcion_definitiva',
    ]
    orden_estado = case(
        *[(Proyecto.estado_general == e, i) for i, e in enumerate(estados_ordenados)],
        else_=len(estados_ordenados)
    )

    proyecto = (
        db.query(Proyecto)
          .filter(
              or_(Proyecto.login_1 == login, Proyecto.login_2 == login),
              Proyecto.ingreso_por == "rua"
          )
          .order_by(
              orden_estado.asc(),          # 1) ranking de estado
              Proyecto.proyecto_id.asc()   # 2) desempate estable
          )
          .first()
    )
    # Si no hay RUA, 'proyecto' queda en None → el código entra a las ramas de mensajes "a nivel usuario".


    mensaje_para_portada = ""
    tipo_mensaje = "info"
    en_fecha_de_ratificar = False


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
            """
        elif curso_aprobado == "Y" and not ddjj :
            mensaje_para_portada = """
                <h4>Curso aprobado</h4>
                <h5>Usted tiene el curso aprobado y puede continuar con el proceso.</h5>
                <p>Acceda <a href="/menu_adoptantes/alta_ddjj"
                    style="color: #007bff; text-decoration: underline;">aquí para completar su DDJJ</a>.</p>
            """ 
        elif curso_aprobado == "Y" and ddjj and user.doc_adoptante_ddjj_firmada == "N":
            mensaje_para_portada = """
                <h4>Actualización de DDJJ</h4>
                <p>Acceda <a href="/menu_adoptantes/alta_ddjj"
                    style="color: #007bff; text-decoration: underline;">aquí para actualizar su DDJJ</a>.</p>
            """ 
        elif ddjj and user.doc_adoptante_ddjj_firmada == "Y" and \
                user.doc_adoptante_estado in ( 'inicial_cargando', 'actualizando' ) :
            mensaje_para_portada = """
                <h4>DDJJ firmada</h4>
                <h5>Documentación personal pendiente.</h5>
                <p>Complete la documentación personal <a href='/menu_adoptantes/personales'>desde aquí</a>.</p>
            """ 

        elif ddjj and user.doc_adoptante_ddjj_firmada == "Y" and \
                user.doc_adoptante_estado in ( 'pedido_revision' ) :
            mensaje_para_portada = """
                <h4>Documentación en revisión</h4>
                <h5>Aguarde la revisión de su documentación personal.</h5>
            """ 
        elif ddjj and user.doc_adoptante_ddjj_firmada == "Y" and \
                user.doc_adoptante_estado in ( 'aprobado' ) and not proyecto :
            mensaje_para_portada = """
                <h4>Documentación aprobada</h4>
                <h5>Puede presentar su proyecto adoptivo.</h5>
            """ 
        elif proyecto:
            estado = proyecto.estado_general
            estados_mensajes = {
                "confeccionando": (
                    "Documentación personal aprobada",
                    "Puede presentar su proyecto adoptivo.",
                    "Presente su proyecto adoptivo <a href='/menu_adoptantes/proyecto'>desde aquí</a>."
                ),
                "invitacion_pendiente": (
                    "Invitación pendiente",
                    "Hay una invitación pendiente a su proyecto adoptivo. Revisen sus correos para aceptar la invitación.",
                    ""
                ),
                "actualizando": (
                    "Proyecto pendiente de actualización",
                    "Desde el RUA se le ha pedido la actualización del proyecto adoptivo.",
                    ""
                ),
                "en_revision": (
                    "Proyecto en revisión",
                    "Aguarde la revisión de su proyecto adoptivo.",
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
                "guarda_provisoria": (
                    "Guarda provisoria",
                    "Se ha otorgado la guarda provisoria del NNA.",
                    "¡Felicitaciones!"
                ),
                "guarda_confirmada": (
                    "Guarda confirmada",
                    "Se ha otorgado la guarda confirmada del NNA.",
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
                    <h6>Para más información, contacte al equipo técnico.</h6>
                """

            if estado == "viable":
                # Buscar fechas relevantes de cambio a viable
                fecha_viable_a_viable = db.query(func.max(ProyectoHistorialEstado.fecha_hora)).filter(
                    ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
                    ProyectoHistorialEstado.estado_anterior == "viable",
                    ProyectoHistorialEstado.estado_nuevo == "viable"
                ).scalar()

                fecha_para_valorar_a_viable = db.query(func.max(ProyectoHistorialEstado.fecha_hora)).filter(
                    ProyectoHistorialEstado.proyecto_id == proyecto.proyecto_id,
                    ProyectoHistorialEstado.estado_anterior == "para_valorar",
                    ProyectoHistorialEstado.estado_nuevo == "viable"
                ).scalar()

                fechas_posibles = []
                if proyecto.ultimo_cambio_de_estado:
                    fechas_posibles.append(datetime.combine(proyecto.ultimo_cambio_de_estado, dt_time.min))
                if fecha_viable_a_viable:
                    fechas_posibles.append(fecha_viable_a_viable)
                if fecha_para_valorar_a_viable:
                    fechas_posibles.append(fecha_para_valorar_a_viable)

                if fechas_posibles:
                    fecha_cambio_final = max(fechas_posibles)
                    fecha_ratificacion = fecha_cambio_final + timedelta(days=356)
                    hoy = datetime.now()

                    if hoy.date() >= fecha_ratificacion.date():
                        tipo_mensaje = "naranja"
                        mensaje_para_portada += """
                            <div style="margin-top: 20px; border-top: 1px solid #ccc; padding-top: 10px;">
                                <h5>🔔 Ratificación requerida</h5>
                                <p>Ha pasado más de un año desde la última valoración favorable de su proyecto.</p>
                                <p>Por favor, es necesario ratificar su voluntad de continuar en el Registro Único de Adopciones.</p>
                            </div>
                        """

                en_fecha_de_ratificar = hoy.date() >= fecha_ratificacion.date()

        # --- Párrafo adicional por postulaciones a CONVOCATORIAS ---
        estados_conv_validos = [
            'aprobado', 'calendarizando', 'entrevistando', 'para_valorar', 'viable',
            'viable_no_disponible', 'en_suspenso', 'no_viable', 'en_carpeta',
            'vinculacion', 'guarda_provisoria', 'guarda_confirmada', 'adopcion_definitiva',
        ]

        # Cantidad de proyectos por CONVOCATORIA en estados válidos
        num_conv = (
            db.query(func.count(Proyecto.proyecto_id))
              .filter(
                  or_(Proyecto.login_1 == login, Proyecto.login_2 == login),
                  Proyecto.ingreso_por == "convocatoria",
                  Proyecto.estado_general.in_(estados_conv_validos)
              )
              .scalar()
        ) or 0

        # ¿Tiene RUA u OFICIO?
        tiene_rua = db.query(
            db.query(Proyecto.proyecto_id)
              .filter(
                  or_(Proyecto.login_1 == login, Proyecto.login_2 == login),
                  Proyecto.ingreso_por == "rua"
              )
              .exists()
        ).scalar()

        tiene_oficio = db.query(
            db.query(Proyecto.proyecto_id)
              .filter(
                  or_(Proyecto.login_1 == login, Proyecto.login_2 == login),
                  Proyecto.ingreso_por == "oficio"
              )
              .exists()
        ).scalar()

        if num_conv > 0:
            # Solo convocatoria (sin rua ni oficio): redacción especial
            if not tiene_rua and not tiene_oficio:
                if num_conv == 1:
                    parrafo_conv = "Además, tiene postulación a una convocatoria."
                else:
                    parrafo_conv = f"Además, tiene {num_conv} postulaciones a convocatorias."
            else:
                # Tiene RUA u/oficio además de convocatoria
                if num_conv == 1:
                    parrafo_conv = "Además, tiene postulación a una convocatoria."
                else:
                    parrafo_conv = f"Además, tiene postulaciones a {num_conv} convocatorias."

            mensaje_para_portada += f"""
                ----------------------------<br>

                <h5>{parrafo_conv}</h5>
            """


    return {
        "tipo_mensaje": tipo_mensaje,
        "mensaje_para_portada": mensaje_para_portada,
        "curso_aprobado": curso_aprobado,
        "ddjj_firmada": user.doc_adoptante_ddjj_firmada,
        "doc_adoptante_estado": user.doc_adoptante_estado,
        "datos_domicilio_faltantes": not (user.calle_y_nro or user.localidad),
        "en_fecha_de_ratificar": en_fecha_de_ratificar,
        "fecha_ratificacion": fecha_ratificacion.strftime("%Y-%m-%d") if 'fecha_ratificacion' in locals() else None
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
                                 Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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
            login_destinatario=login_destinatario,
            mensaje=mensaje_texto_plano,
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
        evento_detalle = f"Notificación enviada por {login_que_observa}: {mensaje_texto_plano[:150]}"
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
                    cuerpo = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                  <td style="font-size: 24px; color: #007bff;">
                                      <strong>¡Hola {user_destino.nombre}!</strong>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 17px;">
                                    <p>Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                                    <p>Te informamos que recibiste la siguiente notificación en la plataforma con
                                    una solicitud para actualizar tu documentación personal:</p>
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
                                  <td style="padding-top: 30px; font-size: 16px; text-align: center;">
                                    <p>Ingresá al sistema para más detalles:</p>
                                    <a href="https://rua.justiciacordoba.gob.ar" target="_blank"
                                      style="display: inline-block; margin-top: 10px; padding: 12px 24px; font-size: 16px; font-weight: bold; color: #ffffff; background-color: #007bff; border-radius: 6px; text-decoration: none;">
                                      Sistema RUA
                                    </a>
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
                elif accion == "aprobar_documentacion":

                    cuerpo = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                  <td style="font-size: 24px; color: #007bff;">
                                      <strong>¡Hola {user_destino.nombre}!</strong>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="padding-top: 20px; font-size: 17px;">
                                    {mensaje}
                                  </td>
                                </tr>

                                <tr>
                                  <td style="padding-top: 30px; font-size: 16px; text-align: center;">
                                    <p>Ingresá al sistema para más detalles:</p>
                                    <a href="https://rua.justiciacordoba.gob.ar" target="_blank"
                                      style="display: inline-block; margin-top: 10px; padding: 12px 24px; font-size: 16px; font-weight: bold; color: #ffffff; background-color: #007bff; border-radius: 6px; text-decoration: none;">
                                      Sistema RUA
                                    </a>
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
                else:
                    cuerpo = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                                <tr>
                                  <td style="font-size: 24px; color: #007bff;">
                                      <strong>¡Hola {user_destino.nombre}!</strong>
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
                                  <td style="padding-top: 30px; font-size: 16px; text-align: center;">
                                    <p>Ingresá al sistema para más detalles:</p>
                                    <a href="https://rua.justiciacordoba.gob.ar" target="_blank"
                                      style="display: inline-block; margin-top: 10px; padding: 12px 24px; font-size: 16px; font-weight: bold; color: #ffffff; background-color: #007bff; border-radius: 6px; text-decoration: none;">
                                      Sistema RUA
                                    </a>
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
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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
                  Depends(require_roles(["administrador", "supervision", "supervisora", "adoptante"]))])
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
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
                    # elif ext in [".jpg", ".jpeg", ".png"]:
                    #     Image.open(ruta).convert("RGB").save(out_pdf)
                    #     # pdf_paths.append(out_pdf)
                    #     pdf_paths.append((campo, out_pdf))
                    elif ext in [".jpg", ".jpeg", ".png"]:
                        img = Image.open(ruta).convert("RGB")

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




@users_router.post( "/notificar-inactivos", # response_model=dict, 
                   dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador"])) ], )
def notificar_usuario_inactivo(
  db: Session = Depends(get_db),
):
    # fechas de referencia
    hoy = datetime.now()
    hace_180 = hoy - timedelta(days=180)
    hace_7 = hoy - timedelta(days=7)

    print("📅 Hoy:", hoy)
    print("📅 Hace 180 días:", hace_180)
    print("📅 Hace 7 días:", hace_7)

    # subconsulta: logins que tuvieron "inicio de sesión" en los últimos 180 días
    subq_activos = (
        db.query(RuaEvento.login)
          .filter(
              RuaEvento.evento_detalle.ilike("%Ingreso exitoso al sistema%"),
              RuaEvento.evento_fecha >= hace_180
          )
          .distinct()
          .subquery()
    )

    # para evitar el “Illegal mix of collations” al comparar login
    login_0900 = User.login.collate('utf8mb4_0900_ai_ci')

    # buscamos el primer usuario operativo inactivo y que no haya recibido aviso en los últimos 7 días
    usuario = (
        db.query(User)
          .outerjoin(
              UsuarioNotificadoInactivo,
              login_0900 == UsuarioNotificadoInactivo.login
          )
          .filter(User.operativo == 'Y')
          .filter(~login_0900.in_(subq_activos))
          .filter(
              or_(
                  UsuarioNotificadoInactivo.mail_enviado_1 == None,
                  and_(
                      UsuarioNotificadoInactivo.dado_de_baja == None,
                      func.greatest(
                          func.coalesce(UsuarioNotificadoInactivo.mail_enviado_1, datetime.min),
                          func.coalesce(UsuarioNotificadoInactivo.mail_enviado_2, datetime.min),
                          func.coalesce(UsuarioNotificadoInactivo.mail_enviado_3, datetime.min),
                          func.coalesce(UsuarioNotificadoInactivo.mail_enviado_4, datetime.min),
                      ) <= hace_7
                  )
              )
          )
          .order_by(User.fecha_alta.asc())
          .limit(1)
          .first()
    )

    if not usuario or not usuario.mail:
        print("❌ No se encontró ningún usuario inactivo para notificar.")
        raise HTTPException(status_code=404, detail="No hay usuarios inactivos pendientes de notificar.")

    print(f"👤 Usuario inactivo encontrado: {usuario.login} - {usuario.mail}")

    # obtener o crear registro de notificaciones
    notificacion = (
        db.query(UsuarioNotificadoInactivo)
          .filter(UsuarioNotificadoInactivo.login == usuario.login)
          .first()
    )

    if not notificacion:
        # Primer aviso
        notificacion = UsuarioNotificadoInactivo(
            login=usuario.login,
            mail_enviado_1=hoy
        )
        db.add(notificacion)
        nro_envio = 1

    elif notificacion.mail_enviado_2 is None:
        # Segundo aviso
        notificacion.mail_enviado_2 = hoy
        nro_envio = 2

    elif notificacion.mail_enviado_3 is None:
        # Tercer aviso
        notificacion.mail_enviado_3 = hoy
        nro_envio = 3

    elif notificacion.mail_enviado_4 is None:
        # Cuarto aviso
        notificacion.mail_enviado_4 = hoy
        nro_envio = 4

    else:
        # 🔴 Quinto llamado: ya recibió los 4 avisos -> se desactiva
        usuario.operativo = 'N'
        notificacion.dado_de_baja = hoy
        evento_baja = RuaEvento(
            login=usuario.login,
            evento_detalle="Usuario dado de baja por inactividad prolongada.",
            evento_fecha=hoy
        )
        db.add(evento_baja)
        db.commit()
        return {"message": f"Usuario {usuario.login} dado de baja por inactividad."}


    # enviamos el mail
    try:
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
                      <td style="font-size: 20px; color: #007bff;">
                        <strong>{nro_envio}° aviso:</strong>
                      </td>
                    </tr>

                    <tr>
                      <td style="padding-top: 20px; font-size: 17px;">
                        <p>¡Hola, <strong>{usuario.nombre}</strong>! Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.</p>
                        <p>Te contactamos porque hace más de 6 meses que no hay actividad en tu cuenta.</p>
                        <p>¿Necesitás ayuda con los pasos para continuar con tu inscripción? Comunicate con nosotros al siguiente correo: <a href="mailto:registroadopcion@justiciacordoba.gob.ar">registroadopcion@justiciacordoba.gob.ar</a> o al teléfono: (0351) 44 81 000 - interno: 13181.</p>
                        <p><strong>¡Te invitamos a que ingreses al sistema para conservar tu cuenta y continuar con el proceso de inscripción!</strong></p>
                        <p><em>Luego del cuarto aviso se desactivará automáticamente tu cuenta.</em></p>
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
                        ¡Muchas gracias!
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
            destinatario=usuario.mail,
            asunto="Aviso por inactividad - Sistema RUA",
            cuerpo=cuerpo_html
        )

        db.commit()
        return {"message": f"Notificación enviada a {usuario.login} (envío #{nro_envio})."}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500,
                            detail=f"Error al enviar mail: {str(e)}")




def procesar_envio_masivo(lineas: List[str]):
    db = SessionLocal()
    try:
        resultados = {"total": 0, "mails_enviados": 0, "errores": []}

        try:
            protocolo = get_setting_value(db, "protocolo") or "https"
            host = get_setting_value(db, "donde_esta_alojado") or "osmvision.com.ar"
            puerto = get_setting_value(db, "puerto_tcp")
            endpoint = "/reconfirmar-subregistros"

            puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
            host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

            os.makedirs(UPLOAD_DIR_DOC_PRETENSOS, exist_ok=True)
            log_path = os.path.join(UPLOAD_DIR_DOC_PRETENSOS, "envios_exitosos.txt")
        except Exception as e:
            print(f"[ERROR GLOBAL] Fallo en configuración inicial: {e}")
            return  # cortás aquí si no podés ni siquiera armar el entorno

        for idx, linea in enumerate(lineas, start=1):
            resultados["total"] += 1
            partes = linea.split("::")

            if len(partes) != 4:
                resultados["errores"].append(f"Línea {idx}: formato inválido")
                continue

            login, nombre, apellido, mail = [p.strip() for p in partes]

            if not (login and nombre and apellido and mail):
                resultados["errores"].append(f"Línea {idx}: campos vacíos")
                continue

            ddjj = db.query(DDJJ).filter(DDJJ.login == login).first()
            if not ddjj:
                resultados["errores"].append(f"{login}: no tiene DDJJ registrada")
                continue

            try:
                login_base64 = base64.b64encode(login.encode()).decode()
                link_final = f"{protocolo}://{host_con_puerto}{endpoint}?user={login_base64}"

                asunto = "Confirmación sobre flexibilidad adoptiva - RUA"
                cuerpo_html = f"""
                <html>
                  <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                    <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                      <tr>
                        <td align="center">
                          <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: Arial, sans-serif; color: #333333; box-shadow: 0 0 10px rgba(0,0,0,0.05);">
                            <tr>
                              <td style="font-size: 18px; padding-bottom: 20px;">
                                ¡Hola! nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.
                              </td>
                            </tr>
                            <tr>
                              <td style="font-size: 16px; padding-bottom: 10px; line-height: 1.6;">
                                Te contactamos porque tenemos registrado que al momento de completar el formulario de inscripción señalaste, además de tu preferencia en las condiciones de niñas, niños y adolescentes que consideraste que podrías adoptar, la opción de <strong>“flexibilidad adoptiva”</strong> en relación a otras condiciones de niñas, niños y adolescentes que están esperando una familia.
                              </td>
                            </tr>
                            <tr>
                              <td style="font-size: 16px; padding: 10px 0;">
                                Es por eso que en esta oportunidad te pedimos que nos especifiques tu elección de flexibilidad haciendo clic en el siguiente botón:
                              </td>
                            </tr>
                            <tr>
                              <td align="center" style="padding: 20px 0;">
                                <a href="{link_final}"
                                    style="display: inline-block; padding: 12px 24px; font-size: 16px;
                                          color: #ffffff; background-color: #0d6efd; text-decoration: none;
                                          border-radius: 6px; font-weight: bold;"
                                    target="_blank">
                                  Ir al formulario
                                </a>
                              </td>
                            </tr>
                            <tr>
                              <td style="font-size: 16px; padding-top: 10px;">
                                ¡Muchas gracias por continuar formando parte del Registro Único de Adopciones de Córdoba!
                              </td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>
                  </body>
                </html>
                """


                enviar_mail(destinatario=mail, asunto=asunto, cuerpo=cuerpo_html)
                resultados["mails_enviados"] += 1

                with open(log_path, "a", encoding="utf-8") as log_file:
                    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_file.write(f"[{ahora}] Enviado a {login} ({mail})\n")

                time.sleep(5)

            except Exception as e:
                resultados["errores"].append(f"{login} ({mail}): {e}")

    except Exception as fatal:
        print(f"[ERROR FATAL] {fatal}")

    finally:
        db.close()





@users_router.post( "/usuarios/notificar-desde-txt", response_model=dict, 
                   dependencies=[ Depends(verify_api_key), Depends(require_roles(["administrador"])) ], )
async def notificar_desde_txt(
    background_tasks: BackgroundTasks,  # 👈 primero los que no tienen valor por defecto
    archivo: UploadFile = File(...),
):
    if not archivo.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="El archivo debe tener extensión .txt")

    try:
        contenido = (await archivo.read()).decode("utf-8")
        lineas = [l.strip() for l in contenido.splitlines() if l.strip()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al leer el archivo: {e}")

    # Agregamos la tarea en segundo plano
    background_tasks.add_task(procesar_envio_masivo, lineas)

    return {
        "tipo_mensaje": "verde",
        "mensaje": f"Se está procesando el envío de {len(lineas)} correos en segundo plano.",
        "errores": []
    }





def procesar_envio_masivo_postulantes_desde_csv(contenido_csv: str):
    print("[TAREA] Comenzando procesamiento del CSV...")

    
    def maybe_none(val: Optional[str]) -> Optional[str]:
        v = (val or "").strip()
        return None if v == "" or v.upper() == "NULL" else v

    def parse_fecha(s: Optional[str]) -> Optional[date]:
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):  # soporta 14/10/1955 y 4/6/1956
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
        return None

    lector_csv = csv.DictReader(io.StringIO(contenido_csv), delimiter=';', quoting=csv.QUOTE_MINIMAL)

    db: Session = SessionLocal()
    try:
        resultados = {"total": 0, "mails_enviados": 0, "errores": []}

        protocolo = get_setting_value(db, "protocolo") or "https"
        host = get_setting_value(db, "donde_esta_alojado") or "osmvision.com.ar"
        puerto = get_setting_value(db, "puerto_tcp")
        endpoint = "/reconfirmar-subregistros-postulantes"
        puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
        host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

        os.makedirs(UPLOAD_DIR_DOC_PRETENSOS, exist_ok=True)
        log_path = os.path.join(UPLOAD_DIR_DOC_PRETENSOS, "envios_exitosos_postulantes.txt")
        print(f"[TAREA] UPLOAD_DIR_DOC_PRETENSOS = {UPLOAD_DIR_DOC_PRETENSOS}")

        for idx, fila in enumerate(lector_csv, start=2):  # línea 2 = primera data
            print(f"[TAREA] Procesando línea {idx}...")
            resultados["total"] += 1

            try:
                # === lecturas, normalización y NULLs ===
                login   = maybe_none(fila.get("login"))
                nombre  = maybe_none(fila.get("nombre"))
                apellido= maybe_none(fila.get("apellido"))
                mail    = maybe_none(fila.get("mail"))
                fecha_nacimiento = parse_fecha(fila.get("fecha_nacimiento"))
                nacionalidad     = maybe_none(fila.get("nacionalidad"))
                sexo             = maybe_none(fila.get("sexo"))
                estado_civil     = maybe_none(fila.get("estado_civil"))
                calle_y_nro      = maybe_none(fila.get("calle_y_nro"))
                depto            = maybe_none(fila.get("depto"))
                barrio           = maybe_none(fila.get("barrio"))
                localidad        = maybe_none(fila.get("localidad"))
                cp               = maybe_none(fila.get("cp"))
                provincia        = maybe_none(fila.get("provincia"))
                telefono_contacto= maybe_none(fila.get("telefono_contacto"))
                ocupacion        = maybe_none(fila.get("ocupacion"))


                # Validación mínima
                if not (login and nombre and apellido and mail):
                    resultados["errores"].append(f"Línea {idx}: campos obligatorios vacíos (login/nombre/apellido/mail)")
                    continue

                user_existente = db.query(User).filter(User.login == login).first()
                ddjj_existente = db.query(DDJJ).filter(DDJJ.login == login).first()

                if user_existente and ddjj_existente:
                    print(f"[TAREA] Usuario y DDJJ ya existen para {login}.")
                    continue

                if not user_existente:
                    user = User(
                        login=login,
                        nombre=nombre,
                        apellido=apellido,
                        mail=mail,
                        fecha_nacimiento=fecha_nacimiento,
                        celular=telefono_contacto,
                        calle_y_nro=calle_y_nro,
                        depto_etc=depto,
                        barrio=barrio,
                        localidad=localidad,
                        provincia=provincia,
                        profesion=ocupacion,
                        fecha_alta=datetime.now().date(),
                        active="Y",
                    )
                    db.add(user)

                    grupo = db.query(Group).filter(Group.description.ilike("%adoptante%")).first()
                    if grupo:
                        db.add(UserGroup(login=login, group_id=grupo.group_id))
                    else:
                        resultados["errores"].append(f"Línea {idx}: No se encontró el grupo 'Adoptante'")
                        db.rollback()
                        continue

                if not ddjj_existente:
                    ddjj = DDJJ(
                        login=login,
                        ddjj_nombre=nombre,
                        ddjj_apellido=apellido,
                        ddjj_correo_electronico=mail,
                        ddjj_fecha_nac=fecha_nacimiento,
                        ddjj_telefono=telefono_contacto,
                        ddjj_calle=calle_y_nro,
                        ddjj_depto=depto,
                        ddjj_barrio=barrio,
                        ddjj_localidad=localidad,
                        ddjj_cp=cp,
                        ddjj_provincia=provincia,
                        ddjj_estado_civil=estado_civil,
                        ddjj_nacionalidad=nacionalidad,
                        ddjj_sexo=sexo,
                        ddjj_ocupacion=ocupacion,
                        ddjj_fecha_ultimo_cambio=datetime.now().strftime("%Y-%m-%d"),
                    )
                    db.add(ddjj)

                db.commit()

                if not ddjj_existente:
                    login_base64 = base64.b64encode(login.encode()).decode()
                    link_final = f"{protocolo}://{host_con_puerto}{endpoint}?user={login_base64}"
                    asunto = "Consulta por disponibilidad adoptiva"

                    # cuerpo_html = f"""
                    # <html>
                    #   <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                    #     <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                    #       <tr>
                    #         <td align="center">
                    #           <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: Arial, sans-serif; color: #333333; box-shadow: 0 0 10px rgba(0,0,0,0.05);">
                    #             <tr>
                    #               <td style="font-size: 18px; padding-bottom: 20px;">
                    #                 ¡Hola! nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.
                    #               </td>
                    #             </tr>
                    #             <tr>
                    #               <td style="font-size: 16px; padding-bottom: 10px; line-height: 1.6;">
                    #                 Te contactamos porque te has postulado a una convocatoria pública de adopción. Queremos saber 
                    #                 si tienes interés en ser consultado/a para búsquedas específicas de niñas, niños o adolescentes 
                    #                 en situación de adoptabilidad.

                    #                 <br />

                    #                 Además, nos gustaría que nos indiques en qué subregistros estarías disponible para 
                    #                 ser considerado/a en futuras convocatorias o búsquedas.
                    #               </td>
                    #             </tr>
                    #             <tr>
                    #               <td style="font-size: 16px; padding: 10px 0;">
                    #                 Te invitamos a completar el siguiente formulario, donde podrás registrar tu disponibilidad:
                    #               </td>
                    #             </tr>
                    #             <tr>
                    #               <td align="center" style="padding: 20px 0;">
                    #                 <a href="{link_final}"
                    #                     style="display: inline-block; padding: 12px 24px; font-size: 16px;
                    #                           color: #ffffff; background-color: #0d6efd; text-decoration: none;
                    #                           border-radius: 6px; font-weight: bold;"
                    #                     target="_blank">
                    #                   Ir al formulario
                    #                 </a>
                    #               </td>
                    #             </tr>
                    #             <tr>
                    #               <td style="font-size: 16px; padding-top: 10px;">
                    #                 ¡Muchas gracias!
                    #               </td>
                    #             </tr>
                    #           </table>
                    #         </td>
                    #       </tr>
                    #     </table>
                    #   </body>
                    # </html>
                    # """

                    cuerpo_html = f"""
                    <html>
                      <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                          <tr>
                            <td align="center">
                              <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: Arial, sans-serif; color: #333333; box-shadow: 0 0 10px rgba(0,0,0,0.05);">
                                <tr>
                                  <td style="font-size: 18px; padding-bottom: 20px;">
                                    ¡Hola! Nos comunicamos desde el <strong>Registro Único de Adopciones de Córdoba</strong>.
                                  </td>
                                </tr>
                                <tr>
                                  <td style="font-size: 16px; padding-bottom: 10px; line-height: 1.6;">
                                    Como ya te anotaste en una convocatoria pública de adopción, nos interesa saber si querés que te contactemos
                                    para informarte de las próximas búsquedas de familias para niñas, niños y adolescentes que esperan ser adoptados.
                                    <br /><br />
                                    Si estás de acuerdo, nos gustaría que nos especifiques en qué tipo de futuros llamados estarías interesada/o.
                                  </td>
                                </tr>
                                <tr>
                                  <td style="font-size: 16px; padding: 10px 0;">
                                    Te invitamos a completar el siguiente formulario:
                                  </td>
                                </tr>
                                <tr>
                                  <td align="center" style="padding: 20px 0;">
                                    <a href="{link_final}"
                                        style="display: inline-block; padding: 12px 24px; font-size: 16px;
                                              color: #ffffff; background-color: #0d6efd; text-decoration: none;
                                              border-radius: 6px; font-weight: bold;"
                                        target="_blank">
                                      Ir al formulario
                                    </a>
                                  </td>
                                </tr>
                                <tr>
                                  <td style="font-size: 16px; padding-top: 10px;">
                                    ¡Muchas gracias!
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
                        enviar_mail(destinatario=mail, asunto=asunto, cuerpo=cuerpo_html)
                        print(f"[TAREA] Mail enviado a {mail}")
                    except Exception as mail_error:
                        resultados["errores"].append(f"Línea {idx} ({login}): error al enviar mail: {mail_error}")

                    with open(log_path, "a", encoding="utf-8") as log_file:
                        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        log_file.write(f"[{ahora}] Enviado a {login} ({mail})\n")

                    resultados["mails_enviados"] += 1
                    time.sleep(5)

            except Exception as e:
                db.rollback()
                resultados["errores"].append(f"Línea {idx} ({fila.get('login','?')}): {e}")

    except Exception as fatal:
        print(f"[ERROR FATAL] {fatal}")
    finally:
        db.close()

    print(f"[TAREA] Proceso finalizado. Mails enviados: {resultados['mails_enviados']}, Total: {resultados['total']}")
    return resultados






@users_router.post("/usuarios/notificar-desde-csv-postulantes", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador"]))], )
async def notificar_desde_csv_postulantes(
    background_tasks: BackgroundTasks,
    archivo: UploadFile = File(...),
):
    if not archivo.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe tener extensión .csv")

    try:

        raw = await archivo.read()  # bytes

        def decode_csv_bytes(data: bytes) -> Tuple[str, str]:
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    return data.decode(enc), enc
                except UnicodeDecodeError:
                    continue
            # último recurso
            return data.decode("utf-8", errors="replace"), "utf-8(replace)"

        contenido, encoding_usado = decode_csv_bytes(raw)
        print(f"[CSV] Decodificado con: {encoding_usado}")

        background_tasks.add_task(procesar_envio_masivo_postulantes_desde_csv, contenido)


        return {
            "tipo_mensaje": "verde",
            "mensaje": "Se está procesando el archivo CSV en segundo plano.",
            "errores": []
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al iniciar el procesamiento: {e}")


import hashlib
from sqlalchemy.orm import Session, aliased
from sqlalchemy import or_, func, and_, text, distinct, not_, asc, exists

from datetime import date, datetime, timedelta
from collections import defaultdict, Counter

import re
import os
import secrets, string
import bcrypt

from fpdf import FPDF

from typing import Optional

from fastapi import HTTPException

from models.carpeta import Carpeta, DetalleNNAEnCarpeta, DetalleProyectosEnCarpeta

from models.proyecto import Proyecto, ProyectoHistorialEstado
from models.notif_y_observaciones import ObservacionesProyectos, ObservacionesPretensos, NotificacionesRUA
from models.convocatorias import DetalleProyectoPostulacion, Postulacion
from models.eventos_y_configs import RuaEvento, UsuarioNotificadoRatificacion
from models.users import User, Group, UserGroup 



from models.nna import Nna
from models.ddjj import DDJJ

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

from models.eventos_y_configs import SecSettings

import httpx




RECAPTCHA_SECRET_KEY = os.getenv("RECAPTCHA_SECRET_KEY")

async def verificar_recaptcha(token: str, remote_ip: str = "", threshold: float = 0.5) -> bool:
    """
    Verifica el token de reCAPTCHA v3 contra la API de Google.
    """
    url = "https://www.google.com/recaptcha/api/siteverify"
    data = {
        "secret": RECAPTCHA_SECRET_KEY,
        "response": token,
        "remoteip": remote_ip,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=data)
            result = response.json()
            return result.get("success", False) and result.get("score", 0) >= threshold
    except Exception as e:
        print("❌ Error al verificar reCAPTCHA:", e)
        return False





# ---------------------------
# Listas "fuente de verdad"
# ---------------------------
PROYECTO_ESTADOS = [
    'invitacion_pendiente','confeccionando','en_revision','actualizando','aprobado',
    'calendarizando','entrevistando','para_valorar','viable','viable_no_disponible',
    'en_suspenso','no_viable','en_carpeta','vinculacion','guarda_provisoria',
    'guarda_confirmada','adopcion_definitiva','baja_anulacion','baja_caducidad',
    'baja_por_convocatoria','baja_rechazo_invitacion','baja_interrupcion'
]

NNA_ESTADOS = [
    'sin_ficha_sin_sentencia','con_ficha_sin_sentencia','sin_ficha_con_sentencia',
    'disponible','preparando_carpeta','enviada_a_juzgado','proyecto_seleccionado',
    'vinculacion','guarda_provisoria','guarda_confirmada','adopcion_definitiva',
    'interrupcion','mayor_sin_adopcion','en_convocatoria','no_disponible'
]

# Bajas definitivas = unión de estados baja_*
BAJAS = ('baja_anulacion','baja_caducidad','baja_por_convocatoria','baja_rechazo_invitacion','baja_interrupcion')

# ---------------------------
# Helpers de tiempo (MySQL)
# ---------------------------
def _avg_days(diff_expr):
    """Envuelve promedios de diferencias en días para devolver int."""
    return func.round(func.avg(diff_expr), 2)

def _days_between(start_col, end_col):
    """TIMESTAMPDIFF(DAY, start, end)"""
    # Nota: text('DAY') es necesario en SQLAlchemy para el primer arg de TIMESTAMPDIFF
    return func.timestampdiff(text("DAY"), start_col, end_col)

def _es_adoptante():
    # EXISTS: el usuario pertenece a un grupo cuyo description contiene "adopt"
    return exists().where(
        and_(
            UserGroup.login == User.login,
            UserGroup.group_id == Group.group_id,
            func.lower(Group.description).like("%adopt%")
        )
    )

def _tiene_clave():
    # clave no nula y no vacía (trim)
    return and_(User.clave.isnot(None), func.length(func.trim(User.clave)) > 0)

def _sin_clave():
    # clave nula o vacía (por si la columna acepta strings vacíos)
    return or_(User.clave.is_(None), func.length(func.trim(User.clave)) == 0)    

# ---------------------------
# BLOQUE USUARIOS
# ---------------------------
def _estadisticas_usuarios(db: Session) -> dict:

    # 1) usuarios_totales = activos + con clave + adoptantes
    usuarios_totales = (
        db.query(User)
        .filter(
            User.active == 'Y',
            _tiene_clave(), 
            _es_adoptante()
        )
        .count()
    )

    # EXISTS en postulaciones por dni o conyuge_dni
    postulacion_existe = exists().where(
        or_(Postulacion.dni == User.login, Postulacion.conyuge_dni == User.login)
    )

    # 2) usuarios_postulados_y_rua = adoptantes + CON clave + en postulaciones
    usuarios_postulados_y_rua = (
        db.query(User)
        .filter(
            _es_adoptante(),
            _tiene_clave(),
            postulacion_existe
        )
        .count()
    )

    # 3) usuarios_postulados_y_no_rua = adoptantes + SIN clave + en postulaciones
    usuarios_postulados_y_no_rua = (
        db.query(User)
        .filter(
            _es_adoptante(),
            _sin_clave(),
            postulacion_existe
        )
        .count()
    )

    # 4) postulaciones_totales = filas en postulaciones (un usuario puede tener varias)
    postulaciones_totales = db.query(func.count(Postulacion.postulacion_id)).scalar() or 0

    sin_activar = db.query(User).filter(User.active == 'N').count()

    # Estados documental/curso/ddjj
    sin_curso_sin_ddjj = db.query(User).filter(
        User.doc_adoptante_curso_aprobado == 'N',
        User.doc_adoptante_ddjj_firmada == 'N'
    ).count()

    con_curso_sin_ddjj = db.query(User).filter(
        User.doc_adoptante_curso_aprobado == 'Y',
        User.doc_adoptante_ddjj_firmada == 'N'
    ).count()

    con_curso_con_ddjj = db.query(User).filter(
        User.doc_adoptante_curso_aprobado == 'Y',
        User.doc_adoptante_ddjj_firmada == 'Y'
    ).count()

    pretensos_presentando = db.query(User).filter(
        User.doc_adoptante_curso_aprobado == 'Y',
        User.doc_adoptante_ddjj_firmada == 'Y',
        or_(User.doc_adoptante_estado == 'inicial_cargando',
            User.doc_adoptante_estado == 'actualizando')
    ).count()

    pretensos_aprobados = db.query(User).filter(
        User.doc_adoptante_curso_aprobado == 'Y',
        User.doc_adoptante_ddjj_firmada == 'Y',
        User.doc_adoptante_estado == 'aprobado'
    ).count()

    pretensos_rechazados = db.query(User).filter(
        User.doc_adoptante_curso_aprobado == 'Y',
        User.doc_adoptante_ddjj_firmada == 'Y',
        User.doc_adoptante_estado == 'rechazado'
    ).count()

    # Usuarios aprobados SIN proyecto o solo con proyectos en estados iniciales
    ProyectoAlias = aliased(Proyecto)
    aprobados_estado_valido = (
        db.query(User).outerjoin(
            ProyectoAlias,
            or_(ProyectoAlias.login_1 == User.login, ProyectoAlias.login_2 == User.login)
        )
        .filter(
            User.doc_adoptante_curso_aprobado == 'Y',
            User.doc_adoptante_ddjj_firmada == 'Y',
            User.doc_adoptante_estado == 'aprobado',
            or_(
                ProyectoAlias.proyecto_id.is_(None),
                ProyectoAlias.estado_general.in_([
                    'invitacion_pendiente','confeccionando','en_revision','actualizando','aprobado'
                ])
            )
        ).distinct().count()
    )

    # Usuarios sin proyecto (aprobados)
    usuarios_sin_proyecto = (
        db.query(User)
        .outerjoin(Proyecto, (User.login == Proyecto.login_1) | (User.login == Proyecto.login_2))
        .filter(User.doc_adoptante_estado == "aprobado", Proyecto.proyecto_id.is_(None))
        .count()
    )

    return {
        "usuarios_totales": usuarios_totales,
        "usuarios_postulados_y_rua": usuarios_postulados_y_rua,
        "usuarios_postulados_y_no_rua": usuarios_postulados_y_no_rua,
        "postulaciones_totales": int(postulaciones_totales),

        "sin_activar": sin_activar,
        "sin_curso_sin_ddjj": sin_curso_sin_ddjj,
        "con_curso_sin_ddjj": con_curso_sin_ddjj,
        "con_curso_con_ddjj": con_curso_con_ddjj,
        "pretensos_presentando_documentacion": pretensos_presentando,
        "pretensos_aprobados": pretensos_aprobados,
        "pretensos_rechazados": pretensos_rechazados,
        "pretensos_aprobados_con_estado_valido": aprobados_estado_valido,
        "usuarios_sin_proyecto": usuarios_sin_proyecto,
        "tasa_aprobacion": (pretensos_aprobados / max(1, (pretensos_aprobados + pretensos_rechazados)))
    }

# ---------------------------
# BLOQUE PROYECTOS
# ---------------------------
def _estadisticas_proyectos(db: Session) -> dict:
    # Conteo por estado (incluye estados nuevos)
    por_estado = {}
    for est in PROYECTO_ESTADOS:
        por_estado[est] = db.query(Proyecto).filter(Proyecto.estado_general == est).count()

    # Viables / entrevistas / vinculaciones / etc. (atajos)
    proyectos_viables = por_estado.get('viable', 0)
    proyectos_en_entrevistas = por_estado.get('calendarizando', 0) + por_estado.get('entrevistando', 0)
    proyectos_en_suspenso = por_estado.get('en_suspenso', 0)
    proyectos_no_viables = por_estado.get('no_viable', 0)
    proyectos_enviados_juzgado = por_estado.get('en_carpeta', 0)
    proyectos_en_guarda_provisoria = por_estado.get('guarda_provisoria', 0)
    proyectos_en_guarda_confirmada = por_estado.get('guarda_confirmada', 0)
    proyectos_adopcion_definitiva = por_estado.get('adopcion_definitiva', 0)
    proyectos_en_vinculacion = por_estado.get('vinculacion', 0)

    # Aprobados con nro_orden asignado (para calendarizar)
    def _aprobados_para_calendarizar(tipo_monoparental: bool):
        q = db.query(Proyecto).filter(
            Proyecto.estado_general == 'aprobado',
            and_(
                func.nullif(func.trim(Proyecto.nro_orden_rua), "") != None,
                func.trim(Proyecto.nro_orden_rua) != "0"
            )
        )
        if tipo_monoparental:
            q = q.filter(Proyecto.proyecto_tipo == 'Monoparental')
        else:
            q = q.filter(Proyecto.proyecto_tipo != 'Monoparental')
        return q.count()

    monoparentales = db.query(Proyecto).filter(Proyecto.proyecto_tipo == 'Monoparental').count()
    en_pareja = db.query(Proyecto).filter(Proyecto.proyecto_tipo != 'Monoparental').count()

    monop_subiendo = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == 'Monoparental',
        Proyecto.estado_general.in_(('confeccionando','actualizando'))
    ).count()
    pareja_subiendo = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != 'Monoparental',
        Proyecto.estado_general.in_(('confeccionando','actualizando'))
    ).count()

    monop_revision = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == 'Monoparental',
        Proyecto.estado_general == 'en_revision'
    ).count()
    pareja_revision = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != 'Monoparental',
        Proyecto.estado_general == 'en_revision'
    ).count()

    monop_aprob_calendar = _aprobados_para_calendarizar(True)
    pareja_aprob_calendar = _aprobados_para_calendarizar(False)

    entrevistando_monop = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == 'Monoparental',
        Proyecto.estado_general.in_(('confeccionando','entrevistando','para_valorar'))
    ).count()
    entrevistando_pareja = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != 'Monoparental',
        Proyecto.estado_general.in_(('confeccionando','entrevistando','para_valorar'))
    ).count()

    # Sin valorar (aprobado o en etapas previas a “viable/no_viable”)
    monop_sin_valorar = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == 'Monoparental',
        Proyecto.estado_general.in_(('aprobado','confeccionando','entrevistando','para_valorar'))
    ).count()
    pareja_sin_valorar = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != 'Monoparental',
        Proyecto.estado_general.in_(('aprobado','confeccionando','entrevistando','para_valorar'))
    ).count()
    proyectos_sin_valorar_subregistros_altos = monop_sin_valorar + pareja_sin_valorar  # ← dinámico

    # Viables por tipo
    monop_viable = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == 'Monoparental',
        Proyecto.estado_general == 'viable'
    ).count()
    pareja_viable = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != 'Monoparental',
        Proyecto.estado_general == 'viable'
    ).count()

    # Adopción definitiva por tipo
    adop_def_mono = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == "Monoparental",
        Proyecto.estado_general == "adopcion_definitiva"
    ).count()
    adop_def_pareja = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != "Monoparental",
        Proyecto.estado_general == "adopcion_definitiva"
    ).count()

    # Sin nro de orden
    mono_sin_orden = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == "Monoparental",
        or_(Proyecto.nro_orden_rua.is_(None), func.trim(Proyecto.nro_orden_rua) == "")
    ).count()
    pareja_sin_orden = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != "Monoparental",
        or_(Proyecto.nro_orden_rua.is_(None), func.trim(Proyecto.nro_orden_rua) == "")
    ).count()

    # En valoración (calendarizando)
    en_valoracion = por_estado.get('calendarizando', 0)

    # Ingreso por fuente
    por_ingreso = {
        'rua': db.query(Proyecto).filter(Proyecto.ingreso_por == 'rua').count(),
        'oficio': db.query(Proyecto).filter(Proyecto.ingreso_por == 'oficio').count(),
        'convocatoria': db.query(Proyecto).filter(Proyecto.ingreso_por == 'convocatoria').count(),
    }

    monop_baja_def = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo == 'Monoparental',
        Proyecto.estado_general.in_(BAJAS)
    ).count()

    pareja_baja_def = db.query(Proyecto).filter(
        Proyecto.proyecto_tipo != 'Monoparental',
        Proyecto.estado_general.in_(BAJAS)
    ).count()

    return {
        "por_estado": por_estado,
        "resumen": {
            "proyectos_viables": proyectos_viables,
            "proyectos_en_entrevistas": proyectos_en_entrevistas,
            "proyectos_en_suspenso": proyectos_en_suspenso,
            "proyectos_no_viables": proyectos_no_viables,
            "proyectos_en_carpeta": proyectos_enviados_juzgado,
            "proyectos_en_guarda_provisoria": proyectos_en_guarda_provisoria,
            "proyectos_en_guarda_confirmada": proyectos_en_guarda_confirmada,
            "proyectos_en_vinculacion": proyectos_en_vinculacion,
            "proyectos_adopcion_definitiva": proyectos_adopcion_definitiva,
        },
        "tipos": {
            "proyectos_monoparentales": monoparentales,
            "proyectos_en_pareja": en_pareja,
            "monoparentales_subiendo_documentacion": monop_subiendo,
            "en_pareja_subiendo_documentacion": pareja_subiendo,
            "monoparentales_en_revision": monop_revision,
            "en_pareja_en_revision": pareja_revision,
            "monoparentales_aprobados_para_calendarizar": monop_aprob_calendar,
            "en_pareja_aprobados_para_calendarizar": pareja_aprob_calendar,
            "entrevistando_monoparental": entrevistando_monop,
            "entrevistando_en_pareja": entrevistando_pareja,
            "monoparentales_sin_valorar": monop_sin_valorar,
            "en_pareja_sin_valorar": pareja_sin_valorar,
            "proyectos_sin_valorar_subregistros_altos": proyectos_sin_valorar_subregistros_altos,
            "proyectos_monoparental_viable": monop_viable,
            "proyectos_en_pareja_viable": pareja_viable,
            "proyectos_adopcion_definitiva_monoparental": adop_def_mono,
            "proyectos_adopcion_definitiva_pareja": adop_def_pareja,
            "monoparentales_sin_nro_orden": mono_sin_orden,
            "en_pareja_sin_nro_orden": pareja_sin_orden,
            "proyectos_en_valoracion": en_valoracion,
            "monoparentales_baja_definitiva": monop_baja_def,
            "en_pareja_baja_definitiva": pareja_baja_def,
        },
        "por_ingreso": por_ingreso
    }

# ---------------------------
# BLOQUE NNA
# ---------------------------
def _estadisticas_nna(db: Session) -> dict:
    # Distribución por estado
    por_estado = {}
    for est in NNA_ESTADOS:
        por_estado[est] = db.query(Nna).filter(Nna.nna_estado == est).count()

    # Edades (0–6, 7–11, 12–17, 18+)
    hoy = date.today()
    fecha_6  = date(hoy.year - 6,  hoy.month, hoy.day)
    fecha_11 = date(hoy.year - 11, hoy.month, hoy.day)
    fecha_17 = date(hoy.year - 17, hoy.month, hoy.day)
    fecha_18 = date(hoy.year - 18, hoy.month, hoy.day)

    edades = {
        "0_6":  db.query(Nna).filter(Nna.nna_fecha_nacimiento > fecha_6).count(),
        "7_11": db.query(Nna).filter(Nna.nna_fecha_nacimiento <= fecha_6, Nna.nna_fecha_nacimiento > fecha_11).count(),
        "12_17":db.query(Nna).filter(Nna.nna_fecha_nacimiento <= fecha_11, Nna.nna_fecha_nacimiento > fecha_18).count(),
        "18_mas": db.query(Nna).filter(Nna.nna_fecha_nacimiento <= fecha_18).count(),
    }

    en_convocatoria = db.query(Nna).filter(Nna.nna_en_convocatoria == 'Y').count()

    # NNA en adopción definitiva y guarda (distintos por carpeta/proyecto)
    nna_en_adopcion_def = (
        db.query(distinct(DetalleNNAEnCarpeta.nna_id))
        .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
        .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
        .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
        .filter(Carpeta.estado_carpeta == 'proyecto_seleccionado',
                Proyecto.estado_general == 'adopcion_definitiva')
        .count()
    )

    nna_en_guarda_conf = (
        db.query(distinct(DetalleNNAEnCarpeta.nna_id))
        .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
        .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
        .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
        .filter(Carpeta.estado_carpeta == 'proyecto_seleccionado',
                Proyecto.estado_general == 'guarda_confirmada')
        .count()
    )

    # NNA en RUA (menores de 18 sin estar en una carpeta seleccionada)
    nna_en_rua = (
        db.query(Nna).filter(
            Nna.nna_fecha_nacimiento > fecha_18,
            not_(
                db.query(DetalleNNAEnCarpeta.nna_id)
                .filter(DetalleNNAEnCarpeta.nna_id == Nna.nna_id)
                .exists()
            )
        ).count()
    )

    return {
        "por_estado": por_estado,
        "edades": edades,
        "nna_en_convocatoria": en_convocatoria,
        "nna_en_adopcion_definitiva": nna_en_adopcion_def,
        "nna_en_guarda_confirmada": nna_en_guarda_conf,
        "nna_en_rua": nna_en_rua,
    }

# ---------------------------
# BLOQUE DDJJ
# ---------------------------
def _estadisticas_ddjj(db: Session) -> dict:
    # Firmadas vs no firmadas (con User)
    firmadas = db.query(User).filter(User.doc_adoptante_ddjj_firmada == 'Y').count()
    no_firmadas = db.query(User).filter(User.doc_adoptante_ddjj_firmada == 'N').count()

    # Flexibilidades y condiciones (ejemplos sobre algunos campos representativos)
    flex_edad_alguna = db.query(DDJJ).filter(
        or_(DDJJ.ddjj_flex_edad_1 == 'Y', DDJJ.ddjj_flex_edad_2 == 'Y', DDJJ.ddjj_flex_edad_3 == 'Y',
            DDJJ.ddjj_flex_edad_4 == 'Y', DDJJ.ddjj_flex_edad_todos == 'Y')
    ).count()

    acepta_discapacidad = db.query(DDJJ).filter(
        or_(DDJJ.ddjj_discapacidad_1 == 'Y', DDJJ.ddjj_discapacidad_2 == 'Y')
    ).count()

    acepta_enfermedad = db.query(DDJJ).filter(
        or_(DDJJ.ddjj_enfermedad_1 == 'Y', DDJJ.ddjj_enfermedad_2 == 'Y', DDJJ.ddjj_enfermedad_3 == 'Y')
    ).count()

    acepta_hermanos = db.query(DDJJ).filter(
        or_(DDJJ.ddjj_hermanos_comp_1 == 'Y', DDJJ.ddjj_hermanos_comp_2 == 'Y', DDJJ.ddjj_hermanos_comp_3 == 'Y')
    ).count()

    return {
        "firmadas": firmadas,
        "no_firmadas": no_firmadas,
        "flex_edad_alguna": flex_edad_alguna,
        "acepta_discapacidad": acepta_discapacidad,
        "acepta_enfermedad": acepta_enfermedad,
        "acepta_grupo_hermanos": acepta_hermanos,
    }

# ---------------------------
# BLOQUE TIEMPOS (proyectos)
# ---------------------------
def _tiempos_proyectos(db: Session) -> dict:
    """
    Calcula promedios de días que pasan los proyectos en cada estado,
    usando la diferencia entre `fecha_hora` y la del siguiente cambio.
    Requiere MySQL 8+ por las window functions.
    """
    # Lead(fecha_hora) sobre cada proyecto, ordenado por fecha_hora asc
    # SELECT avg(TIMESTAMPDIFF(DAY, fh_actual, fh_siguiente)) GROUP BY estado_nuevo
    subq = db.query(
        ProyectoHistorialEstado.proyecto_id.label("pid"),
        ProyectoHistorialEstado.estado_nuevo.label("estado"),
        ProyectoHistorialEstado.fecha_hora.label("fh_actual"),
        func.lead(ProyectoHistorialEstado.fecha_hora)
            .over(partition_by=ProyectoHistorialEstado.proyecto_id,
                  order_by=ProyectoHistorialEstado.fecha_hora).label("fh_sig")
    ).subquery()

    # Sólo filas donde hay siguiente estado (fh_sig no nulo)
    filas = db.query(
        subq.c.estado,
        _avg_days(_days_between(subq.c.fh_actual, subq.c.fh_sig)).label("promedio_dias")
    ).filter(subq.c.fh_sig.isnot(None)).group_by(subq.c.estado).all()

    promedio_por_estado = {row.estado: float(row.promedio_dias) for row in filas}

    # Tiempo total desde primer estado hasta último por proyecto
    # min(fecha) y max(fecha) por proyecto → promedio de (max - min)
    rango = db.query(
        ProyectoHistorialEstado.proyecto_id.label("pid"),
        func.min(ProyectoHistorialEstado.fecha_hora).label("fh_min"),
        func.max(ProyectoHistorialEstado.fecha_hora).label("fh_max"),
    ).group_by(ProyectoHistorialEstado.proyecto_id).subquery()

    total_promedio = db.query(
        _avg_days(_days_between(rango.c.fh_min, rango.c.fh_max))
    ).scalar() or 0

    return {
        "promedio_dias_por_estado": promedio_por_estado,
        "promedio_dias_total_por_proyecto": float(total_promedio),
    }

# ---------------------------
# BLOQUE TIEMPOS (pretensos)
# ---------------------------
def _tiempos_pretensos(db: Session) -> dict:
    """
    Estima tiempos entre hitos del pretenso, basándose en RuaEvento.evento_detalle.
    Ajustá los patrones like() a los textos reales que uses en `evento_detalle`.
    """
    # Para cada login: t(curso_aprobado) -> t(ddjj_firmada) -> t(solicitud_revision) -> t(aprobado)
    # Promedio global de cada tramo.
    def _avg_diff_between(evento_a_like: str, evento_b_like: str):
        # Tomamos el primer A y el primer B posteriores por usuario
        A = aliased(RuaEvento)
        B = aliased(RuaEvento)
        # self-join por login y B.fecha > A.fecha
        pares = (
            db.query(
                A.login.label("login"),
                func.min(A.evento_fecha).label("fa"),
                func.min(B.evento_fecha).label("fb")
            )
            .join(B, and_(B.login == A.login, B.evento_fecha > A.evento_fecha))
            .filter(A.evento_detalle.like(evento_a_like),
                    B.evento_detalle.like(evento_b_like))
            .group_by(A.login)
            .subquery()
        )

        return (db.query(_avg_days(_days_between(pares.c.fa, pares.c.fb))).scalar() or 0)

    # Ajustá estos patrones a tus strings reales:
    avg_curso_a_ddjj = _avg_diff_between("%curso_aprobado%", "%ddjj_firmada%")
    avg_ddjj_a_rev   = _avg_diff_between("%ddjj_firmada%", "%solicitud_revision%")
    avg_rev_a_aprob  = _avg_diff_between("%solicitud_revision%", "%aprobado%")

    return {
        "promedio_dias_curso_a_ddjj": float(avg_curso_a_ddjj),
        "promedio_dias_ddjj_a_solicitud_revision": float(avg_ddjj_a_rev),
        "promedio_dias_revision_a_aprobado": float(avg_rev_a_aprob),
    }

# ---------------------------
# BLOQUE TIEMPOS (ratificación)
# ---------------------------
def _tiempos_ratificacion(db: Session) -> dict:
    """
    Promedio de días desde el primer mail de ratificación hasta la ratificación.
    Si hay múltiples mails, tomamos el primero disponible.
    """
    u = UsuarioNotificadoRatificacion
    primera_fecha = func.coalesce(u.mail_enviado_1, u.mail_enviado_2, u.mail_enviado_3, u.mail_enviado_4)

    avg_mail_a_rat = (
        db.query(_avg_days(_days_between(primera_fecha, u.ratificado)))
        .filter(u.ratificado.isnot(None), primera_fecha.isnot(None))
        .scalar()
    ) or 0

    pendientes = db.query(u).filter(u.ratificado.is_(None)).count()

    return {
        "promedio_dias_mail_a_ratificacion": float(avg_mail_a_rat),
        "ratificaciones_pendientes": pendientes
    }

# ---------------------------
# FUNCIÓN PRINCIPAL
# ---------------------------
def calcular_estadisticas_generales(db: Session) -> dict:
    """
    Versión modular y ampliada:
      - usuarios, proyectos, nna, ddjj
      - tiempos (proyectos por estado, pipeline pretensos, ratificación)
    """
    try:
        usuarios = _estadisticas_usuarios(db)
        proyectos = _estadisticas_proyectos(db)
        nna = _estadisticas_nna(db)
        ddjj = _estadisticas_ddjj(db)

        tiempos = {
            "proyectos": _tiempos_proyectos(db),
            "pretensos": _tiempos_pretensos(db),
            "ratificacion": _tiempos_ratificacion(db),
        }

        return {
            "usuarios": usuarios,
            "proyectos": proyectos,
            "nna": nna,
            "ddjj": ddjj,
            "tiempos": tiempos,
        }

    except Exception as e:
        # Podés envolver con logs si querés        
        raise HTTPException(status_code=500, detail=str(e))





def get_setting_value(db: Session, setting_name: str) -> str:
    """
    Obtiene el valor de una configuración desde la tabla sec_settings.
    """
    setting = db.query(SecSettings).filter(SecSettings.set_name == setting_name).first()
    return setting.set_value if setting else None




def enviar_mail(destinatario: str, asunto: str, cuerpo: str):
    # Datos del remitente y servidor SMTP desde variables de entorno
    remitente = os.getenv("MAIL_REMITENTE")  # ejemplo: sistemarua@justiciacordoba.gob.ar
    nombre_remitente = os.getenv("MAIL_NOMBRE_REMITENTE", "RUA")
    password = os.getenv("MAIL_PASSWORD")
    smtp_server = os.getenv("MAIL_SERVER", "smtp.office365.com")
    smtp_port = int(os.getenv("MAIL_PORT", 587))

    # ─────────── Lógica de destino ───────────
    # Si la variable no existe, tomamos "Y" como valor por defecto
    mail_solo_a_cesar = os.getenv("MAIL_SOLO_A_CESAR", "Y").strip().upper()

    enviar_a_cesar = mail_solo_a_cesar != "N"      # True → mandar solo a César
    destino_final  = "cesarosimani@gmail.com" if enviar_a_cesar else destinatario

    # ─────────── Construcción del mensaje ───────────
    msg = MIMEMultipart()
    msg["From"]    = formataddr((nombre_remitente, remitente))  # Ej: "RUA <sistemarua@...>"
    msg["To"]      = destino_final
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html"))

    # Enviar el correo
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(remitente, password)
            server.send_message(msg)
    except Exception as e:
        print(f"❌ Error al enviar el correo: {e}")
        raise



class EstadisticasPDF(FPDF):
    def header(self):
        self.set_font("Arial", "B", 12)
        self.set_text_color(40, 40, 40)
        self.cell(0, 10, "SERVICIO DE GUARDA Y ADOPCIÓN", ln=True, align="C")
        self.set_font("Arial", "B", 11)
        self.cell(0, 10, "REGISTRO ÚNICO DE ADOPCIONES Y EQUIPO TÉCNICO DE ADOPCIONES", ln=True, align="C")
        self.set_font("Arial", "I", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 10, "INFORME DE ESTADÍSTICAS GENERALES", ln=True, align="C")
        self.ln(4)

    def section_title(self, title):
        self.set_font("Arial", "B", 12)
        self.set_fill_color(200, 220, 255)  # azul claro
        self.set_text_color(0)
        self.cell(0, 10, title, ln=True, fill=True)
        self.ln(3)

    def add_table(self, data, col_widths=None):
        if not col_widths:
            col_widths = [190 // len(data[0])] * len(data[0])

        self.set_font("Arial", "B", 9)
        self.set_fill_color(230, 230, 230)
        self.set_text_color(0)
        for i, header in enumerate(data[0]):
            self.cell(col_widths[i], 8, header, border=1, align="C", fill=True)
        self.ln()

        self.set_font("Arial", "", 9)
        self.set_text_color(30, 30, 30)
        for row in data[1:]:
            for i, datum in enumerate(row):
                self.cell(col_widths[i], 7, str(datum), border=1, align="C")
            self.ln()
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "I", 8)
        self.set_text_color(100, 100, 100)
        self.cell(0, 10, "Informe generado automáticamente - RUA", 0, 0, "C")



# def calcular_estadisticas_generales(db: Session) -> dict:
#     try:
#         sin_activar = db.query(User).filter(User.active == 'N').count()
#         usuarios_activos = db.query(User).count()
#         sin_curso_sin_ddjj = db.query(User).filter(User.doc_adoptante_curso_aprobado == 'N', User.doc_adoptante_ddjj_firmada == 'N').count()
#         con_curso_sin_ddjj = db.query(User).filter(User.doc_adoptante_curso_aprobado == 'Y', User.doc_adoptante_ddjj_firmada == 'N').count()
#         con_curso_con_ddjj = db.query(User).filter(User.doc_adoptante_curso_aprobado == 'Y', User.doc_adoptante_ddjj_firmada == 'Y').count()

#         pretensos_presentando_documentacion = db.query(User).filter(
#             User.doc_adoptante_curso_aprobado == 'Y',
#             User.doc_adoptante_ddjj_firmada == 'Y',
#             or_(
#                 User.doc_adoptante_estado == 'inicial_cargando',
#                 User.doc_adoptante_estado == 'actualizando'
#             )
#         ).count()

#         pretensos_aprobados = db.query(User).filter(
#             User.doc_adoptante_curso_aprobado == 'Y',
#             User.doc_adoptante_ddjj_firmada == 'Y',
#             User.doc_adoptante_estado == 'aprobado',
#         ).count()

#         pretensos_rechazados = db.query(User).filter(
#             User.doc_adoptante_curso_aprobado == 'Y',
#             User.doc_adoptante_ddjj_firmada == 'Y',
#             User.doc_adoptante_estado == 'rechazado',
#         ).count()

#         # Los estados de proyectos son:
#         # ESTADOS_PROYECTO = [ "Inactivo", "Activo", "Entrevistas", "En valoración", "No viable", "En suspenso", "Viable", 
#         # "En carpeta", "En cancelación", "Cancelado","Baja definitiva", "Preparando entrevistas", "Adopción definitiva" ]


#         proyectos_monoparentales = db.query(Proyecto).filter(Proyecto.proyecto_tipo == 'Monoparental').count()
#         proyectos_en_pareja = db.query(Proyecto).filter(Proyecto.proyecto_tipo != 'Monoparental').count()

#         proyectos_monoparentales_subiendo_documentacion = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 or_(
#                     Proyecto.estado_general == 'confeccionando',
#                     Proyecto.estado_general == 'actualizando'
#                 )
#             )
#             .count()
#         )

#         proyectos_en_pareja_subiendo_documentacion = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 or_(
#                     Proyecto.estado_general == 'confeccionando',
#                     Proyecto.estado_general == 'actualizando'
#                 )
#             )
#             .count()
#         )


#         proyectos_monoparentales_en_revision_por_supervision = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'en_revision'
#             )
#             .count()
#         )

#         proyectos_en_pareja_en_revision_por_supervision = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 Proyecto.estado_general == 'en_revision'
#             )
#             .count()
#         )


#         # Un proyecto está para calendarizar es cuando la supervisión aprueba el proeycto,
#         # en este moemtno pasa al estado Preparando entrevistas y se le coloca el nro. de orden
#         proyectos_monoparentales_aprobados_para_calendarizar = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'aprobado',
#                 # Esto controla que el nro. de orden esté asignado
#                 and_(
#                     func.nullif(func.trim(Proyecto.nro_orden_rua), "") != None,
#                     func.trim(Proyecto.nro_orden_rua) != "0"
#                 )
#             )
#             .count()
#         )

#         proyectos_en_pareja_aprobados_para_calendarizar = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 Proyecto.estado_general == 'aprobado',
#                 # Esto controla que el nro. de orden esté asignado
#                 and_(
#                     func.nullif(func.trim(Proyecto.nro_orden_rua), "") != None,
#                     func.trim(Proyecto.nro_orden_rua) != "0"
#                 )
#             )
#             .count()
#         )
        
        

#         entrevistando_monoparental = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 or_(
#                     Proyecto.estado_general == 'confeccionando',
#                     Proyecto.estado_general == 'entrevistando',
#                     Proyecto.estado_general == 'para_valorar'
#                 )
#             )
#             .count()
#         )

#         entrevistando_en_pareja = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 or_(
#                     Proyecto.estado_general == 'confeccionando',
#                     Proyecto.estado_general == 'entrevistando',
#                     Proyecto.estado_general == 'para_valorar'
#                 )
#             )
#             .count()
#         )

#         proyectos_monoparentales_en_suspenso = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'en_suspenso'
#             )
#             .count()
#         )

#         proyectos_en_pareja_en_suspenso = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 Proyecto.estado_general == 'en_suspenso'
#             )
#             .count()
#         )

#         proyectos_monoparentales_no_viable = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'no_viable'
#             )
#             .count()
#         )

#         proyectos_en_pareja_no_viable = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 Proyecto.estado_general == 'no_viable'
#             )
#             .count()
#         )

#         proyectos_monoparentales_baja_definitiva = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 or_(
#                     Proyecto.estado_general == 'baja_anulacion',
#                     Proyecto.estado_general == 'baja_caducidad',
#                     Proyecto.estado_general == 'baja_por_convocatoria',
#                     Proyecto.estado_general == 'baja_rechazo_invitacion'
#                 )
#             )
#             .count()
#         )

#         proyectos_en_pareja_baja_definitiva = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 or_(
#                     Proyecto.estado_general == 'baja_anulacion',
#                     Proyecto.estado_general == 'baja_caducidad',
#                     Proyecto.estado_general == 'baja_por_convocatoria',
#                     Proyecto.estado_general == 'baja_rechazo_invitacion'
#                 )
#             )
#             .count()
#         )


#         proyectos_monoparentales_sin_valorar = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'aprobado',
#                 or_(
#                     Proyecto.estado_general == 'confeccionando',
#                     Proyecto.estado_general == 'entrevistando',
#                     Proyecto.estado_general == 'para_valorar'
#                 )
#             )
#             .count()
#         )

#         proyectos_en_pareja_sin_valorar = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 Proyecto.estado_general == 'aprobado',
#                 or_(
#                     Proyecto.estado_general == 'confeccionando',
#                     Proyecto.estado_general == 'entrevistando',
#                     Proyecto.estado_general == 'para_valorar'
#                 )
#             )
#             .count()
#         )

#         proyectos_aprobados_totales = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'aprobado'
#         ).count()



#         proyectos_aprobados_totales = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'aprobado'
#             )
#             .count()
#         )
                

#         usuarios_sin_proyecto = (
#             db.query(User)
#             .outerjoin(Proyecto, (User.login == Proyecto.login_1) | (User.login == Proyecto.login_2))
#             .filter(User.doc_adoptante_estado == "aprobado", Proyecto.proyecto_id.is_(None))
#             .count()
#         )
#         proyectos_monoparentales_sin_nro_orden = db.query(Proyecto).filter(Proyecto.proyecto_tipo == "Monoparental", 
#                                                                            Proyecto.nro_orden_rua.is_(None)).count()
        
#         proyectos_en_pareja_sin_nro_orden = (
#             db.query(Proyecto)
#             .filter(Proyecto.proyecto_tipo != "Monoparental", Proyecto.nro_orden_rua.is_(None)).count()
#         )

#         proyectos_en_valoracion = db.query(Proyecto).filter(Proyecto.estado_general == "calendarizando").count()

        
#         proyectos_monoparental_viable = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo == 'Monoparental',
#                 Proyecto.estado_general == 'viable'
#             )
#             .count() 
#         )

#         proyectos_en_pareja_viable = (
#             db.query(Proyecto)
#             .filter(
#                 Proyecto.proyecto_tipo != 'Monoparental',
#                 Proyecto.estado_general == 'viable'
#             )
#             .count() 
#         )

#         proyectos_viables = ( db.query(Proyecto).filter( Proyecto.estado_general == 'viable' ).count() )

#         proyectos_sin_valorar_subregistros_altos = (
#             proyectos_monoparentales_sin_valorar + proyectos_en_pareja_sin_valorar
#         )

#         proyectos_viables_disponibles = (
#             proyectos_monoparental_viable + proyectos_en_pareja_viable
#         )

#         proyectos_enviados_juzgado = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'en_carpeta'
#         ).count()

#         proyectos_en_guarda_provisoria = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'guarda_provisoria'
#         ).count()

#         proyectos_en_guarda_confirmada = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'guarda_confirmada'
#         ).count()

#         proyectos_adopcion_definitiva = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'adopcion_definitiva'
#         ).count()

#         proyectos_en_vinculacion = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'vinculacion'
#         ).count()

#         convocatorias_con_adopcion_definitiva = db.query(Proyecto).filter(
#             Proyecto.ingreso_por == 'convocatoria',
#             Proyecto.estado_general == 'adopcion_definitiva'
#         ).count()

#         proyectos_en_entrevistas = db.query(Proyecto).filter(
#             or_(
#                 Proyecto.estado_general == 'calendarizando',
#                 Proyecto.estado_general == 'entrevistando'
#             )
#         ).count()

#         proyectos_en_suspenso = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'en_suspenso'
#         ).count()

#         proyectos_no_viables = db.query(Proyecto).filter(
#             Proyecto.estado_general == 'no_viable'
#         ).count()

#         nna_en_adopcion_definitiva = (
#             db.query(distinct(DetalleNNAEnCarpeta.nna_id))
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'adopcion_definitiva'
#             )
#             .count()
#         )

#         ProyectoAlias = aliased(Proyecto)

#         pretensos_aprobados_con_estado_valido = (
#             db.query(User)
#             .outerjoin(
#                 ProyectoAlias,
#                 or_(
#                     ProyectoAlias.login_1 == User.login,
#                     ProyectoAlias.login_2 == User.login
#                 )
#             )
#             .filter(
#                 User.doc_adoptante_curso_aprobado == 'Y',
#                 User.doc_adoptante_ddjj_firmada == 'Y',
#                 User.doc_adoptante_estado == 'aprobado',
#                 or_(
#                     ProyectoAlias.proyecto_id.is_(None),  # no tiene ningún proyecto
#                     ProyectoAlias.estado_general.in_([
#                         'invitacion_pendiente', 'confeccionando', 'en_revision', 'actualizando', 'aprobado'
#                     ])
#                 )
#             )
#             .distinct()
#             .count()
#         )

#         nna_en_guarda = (
#             db.query(distinct(DetalleNNAEnCarpeta.nna_id))
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'guarda_confirmada'
#             )
#             .count()
#         )

#         # Fecha límite: hoy menos 18 años
#         hoy = date.today()
#         fecha_limite_18 = date(hoy.year - 18, hoy.month, hoy.day)

#         nna_en_rua = (
#             db.query(Nna)
#             .filter(
#                 Nna.nna_fecha_nacimiento > fecha_limite_18,
#                 not_(
#                     db.query(DetalleNNAEnCarpeta.nna_id)
#                     .filter(DetalleNNAEnCarpeta.nna_id == Nna.nna_id)
#                     .exists()
#                 )
#             )
#             .count()
#         )

#         proyectos_adopcion_definitiva_monoparental = db.query(Proyecto).filter(
#             Proyecto.proyecto_tipo == "Monoparental",
#             Proyecto.estado_general == "adopcion_definitiva"
#         ).count()

#         proyectos_adopcion_definitiva_pareja = db.query(Proyecto).filter(
#             Proyecto.proyecto_tipo != "Monoparental",
#             Proyecto.estado_general == "adopcion_definitiva"
#         ).count()

        

#         hoy = date.today()
#         fecha_6 = date(hoy.year - 6, hoy.month, hoy.day)
#         fecha_11 = date(hoy.year - 11, hoy.month, hoy.day)
#         fecha_17 = date(hoy.year - 17, hoy.month, hoy.day)
#         fecha_18 = date(hoy.year - 18, hoy.month, hoy.day)

#         # 0–6 años
#         guarda_grupo_0_6 = (
#             db.query(DetalleNNAEnCarpeta.nna_id)
#             .join(Nna, Nna.nna_id == DetalleNNAEnCarpeta.nna_id)
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'guarda_confirmada',
#                 Nna.nna_fecha_nacimiento > fecha_6
#             )
#             .distinct()
#             .count()
#         )

#         # 7–11 años
#         guarda_grupo_7_11 = (
#             db.query(DetalleNNAEnCarpeta.nna_id)
#             .join(Nna, Nna.nna_id == DetalleNNAEnCarpeta.nna_id)
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'guarda_confirmada',
#                 Nna.nna_fecha_nacimiento <= fecha_6,
#                 Nna.nna_fecha_nacimiento > fecha_11
#             )
#             .distinct()
#             .count()
#         )

#         # 12–17 años
#         guarda_grupo_12_17 = (
#             db.query(DetalleNNAEnCarpeta.nna_id)
#             .join(Nna, Nna.nna_id == DetalleNNAEnCarpeta.nna_id)
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'guarda_confirmada',
#                 Nna.nna_fecha_nacimiento <= fecha_11,
#                 Nna.nna_fecha_nacimiento > fecha_18
#             )
#             .distinct()
#             .count()
#         )

#         # 0–6 años
#         adopcion_grupo_0_6 = (
#             db.query(DetalleNNAEnCarpeta.nna_id)
#             .join(Nna, Nna.nna_id == DetalleNNAEnCarpeta.nna_id)
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'adopcion_definitiva',
#                 Nna.nna_fecha_nacimiento > fecha_6
#             )
#             .distinct()
#             .count()
#         )

#         # 7–11 años
#         adopcion_grupo_7_11 = (
#             db.query(DetalleNNAEnCarpeta.nna_id)
#             .join(Nna, Nna.nna_id == DetalleNNAEnCarpeta.nna_id)
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'adopcion_definitiva',
#                 Nna.nna_fecha_nacimiento <= fecha_6,
#                 Nna.nna_fecha_nacimiento > fecha_11
#             )
#             .distinct()
#             .count()
#         )

#         # 12–17 años
#         adopcion_grupo_12_17 = (
#             db.query(DetalleNNAEnCarpeta.nna_id)
#             .join(Nna, Nna.nna_id == DetalleNNAEnCarpeta.nna_id)
#             .join(Carpeta, Carpeta.carpeta_id == DetalleNNAEnCarpeta.carpeta_id)
#             .join(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id)
#             .join(Proyecto, Proyecto.proyecto_id == DetalleProyectosEnCarpeta.proyecto_id)
#             .filter(
#                 Carpeta.estado_carpeta == 'proyecto_seleccionado',
#                 Proyecto.estado_general == 'adopcion_definitiva',
#                 Nna.nna_fecha_nacimiento <= fecha_11,
#                 Nna.nna_fecha_nacimiento > fecha_18
#             )
#             .distinct()
#             .count()
#         )

        
#         return {
#             "proyectos_viables": proyectos_viables,
#             "proyectos_en_entrevistas": proyectos_en_entrevistas,
#             "pretensos_aprobados_con_estado_valido": pretensos_aprobados_con_estado_valido,

#             "nna_en_adopcion_definitiva": nna_en_adopcion_definitiva,
#             "nna_en_guarda": nna_en_guarda,
#             "nna_en_rua": nna_en_rua,

#             "proyectos_adopcion_definitiva_monoparental": proyectos_adopcion_definitiva_monoparental,
#             "proyectos_adopcion_definitiva_pareja": proyectos_adopcion_definitiva_pareja,

#             "guarda_grupo_0_6": guarda_grupo_0_6,
#             "guarda_grupo_7_11": guarda_grupo_7_11,
#             "guarda_grupo_12_17": guarda_grupo_12_17,

#             "adopcion_grupo_0_6": adopcion_grupo_0_6,
#             "adopcion_grupo_7_11": adopcion_grupo_7_11,
#             "adopcion_grupo_12_17": adopcion_grupo_12_17,


#             "sin_activar": sin_activar,
#             "usuarios_activos": usuarios_activos,
#             "sin_curso_sin_ddjj": sin_curso_sin_ddjj,
#             "con_curso_sin_ddjj": con_curso_sin_ddjj,
#             "con_curso_con_ddjj": con_curso_con_ddjj,

#             "proyectos_sin_valorar_subregistros_altos": 26,
            
            
            
#             "proyectos_no_viables": proyectos_no_viables,
#             "proyectos_en_suspenso": proyectos_en_suspenso,
#             "proyectos_enviados_juzgado": proyectos_enviados_juzgado,
#             "proyectos_en_guarda_provisoria": proyectos_en_guarda_provisoria,
#             "proyectos_en_guarda": proyectos_en_guarda_confirmada,
#             "proyectos_en_guarda_confirmada": proyectos_en_guarda_confirmada,
#             "proyectos_adopcion_definitiva": proyectos_adopcion_definitiva,
#             "proyectos_en_vinculacion": proyectos_en_vinculacion,
#             "convocatorias_con_adopcion_definitiva": convocatorias_con_adopcion_definitiva,

#             "pretensos_presentando_documentacion": pretensos_presentando_documentacion,
#             "pretensos_aprobados": pretensos_aprobados,
#             "pretensos_rechazados": pretensos_rechazados,
                        
#             "proyectos_monoparentales": proyectos_monoparentales,
#             "proyectos_en_pareja": proyectos_en_pareja,

#             "proyectos_monoparentales_subiendo_documentacion": proyectos_monoparentales_subiendo_documentacion,
#             "proyectos_en_pareja_subiendo_documentacion": proyectos_en_pareja_subiendo_documentacion,
#             "proyectos_monoparentales_en_revision_por_supervision": proyectos_monoparentales_en_revision_por_supervision,
#             "proyectos_en_pareja_en_revision_por_supervision": proyectos_en_pareja_en_revision_por_supervision,

#             "proyectos_monoparentales_aprobados_para_calendarizar": proyectos_monoparentales_aprobados_para_calendarizar,
#             "proyectos_en_pareja_aprobados_para_calendarizar": proyectos_en_pareja_aprobados_para_calendarizar,

#             "entrevistando_en_pareja": entrevistando_en_pareja,
#             "entrevistando_monoparental": entrevistando_monoparental,

#             "proyectos_monoparentales_en_suspenso": proyectos_monoparentales_en_suspenso,
#             "proyectos_en_pareja_en_suspenso": proyectos_en_pareja_en_suspenso,
#             "proyectos_monoparentales_no_viable": proyectos_monoparentales_no_viable,
#             "proyectos_en_pareja_no_viable": proyectos_en_pareja_no_viable,
#             "proyectos_monoparentales_baja_definitiva": proyectos_monoparentales_baja_definitiva,
#             "proyectos_en_pareja_baja_definitiva": proyectos_en_pareja_baja_definitiva,

#             "proyectos_monoparentales_sin_valorar": proyectos_monoparentales_sin_valorar,            
#             "proyectos_en_pareja_sin_valorar": proyectos_en_pareja_sin_valorar,

#             "usuarios_sin_proyecto": usuarios_sin_proyecto,
#             "proyectos_monoparentales_sin_nro_orden": proyectos_monoparentales_sin_nro_orden,
#             "proyectos_en_pareja_sin_nro_orden": proyectos_en_pareja_sin_nro_orden,

#             "proyectos_en_valoracion": proyectos_en_valoracion,
            
#             "proyectos_monoparental_viable": proyectos_monoparental_viable,
#             "proyectos_en_pareja_viable": proyectos_en_pareja_viable,
#         }
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))
    


def check_consecutive_numbers(password: str) -> bool:
    """
    Verifica si la contraseña contiene más de dos números consecutivos.
    Retorna True si hay números consecutivos, de lo contrario False.
    """
    for i in range(len(password) - 2):
        a, b, c = password[i], password[i+1], password[i+2]
        # Solo seguimos si los tres son dígitos
        if a.isdigit() and b.isdigit() and c.isdigit():
            if int(b) == int(a) + 1 and int(c) == int(b) + 1:
                return True
    return False



def get_user_name_by_login(db: Session, login: str):
    """
    Consulta en la tabla sec_users por el login y devuelve un nombre y apellido concatenados.
    """
    user = db.query(User.nombre, User.apellido).filter(User.login == login).first()
    if user:
        return f"{user.nombre} {user.apellido}"  # Usamos f-string para concatenar
    return ""



def build_subregistro_string(user):
    subregistros = {
        "1": user.subregistro_1,
        "2": user.subregistro_2,
        "3": user.subregistro_3,
        "4": user.subregistro_4,
        "5a": user.subregistro_5_a,
        "5b": user.subregistro_5_b,
        "5c": user.subregistro_5_c,
        "6a": user.subregistro_6_a,
        "6b": user.subregistro_6_b,
        "6c": user.subregistro_6_c,
        "6d": user.subregistro_6_d,
        "62": user.subregistro_6_2,
        "63": user.subregistro_6_3,
        "63+": user.subregistro_6_mas_de_3,
        "f": user.subregistro_flexible,
        "o": user.subregistro_otra_provincia,
    }
    return " ; ".join([key for key, value in subregistros.items() if value == "Y"])


def construir_subregistro_string(row):
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

    resultado = []

    for campo in subregistros_definitivos:
        valor = getattr(row, campo, None)
        if str(valor).upper() == "Y":
            resultado.append(campo.replace("subreg_", ""))

    return " ; ".join(resultado)



def parse_date(date_value):
    """
    Valida y devuelve una fecha en formato 'YYYY-MM-DD'.
    Puede manejar objetos date, datetime o cadenas en formato 'YYYY-MM-DD' y 'DD/MM/YYYY'.
    Si no es válida, devuelve una cadena vacía.
    """
    if isinstance(date_value, (date, datetime)):
        return date_value.strftime("%Y-%m-%d")
    elif isinstance(date_value, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(date_value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""




def calculate_age(birthdate) -> int:
    """Calcula la edad a partir de una fecha de nacimiento en formato 'YYYY-MM-DD' o tipo date."""
    if not birthdate:
        return 0

    try:
        if isinstance(birthdate, str):
            birthdate_date = datetime.strptime(birthdate, "%Y-%m-%d").date()
        elif isinstance(birthdate, date):
            birthdate_date = birthdate
        else:
            return 0  # tipo no válido

        today = date.today()
        age = today.year - birthdate_date.year - (
            (today.month, today.day) < (birthdate_date.month, birthdate_date.day)
        )
        return age
    except Exception:
        return 0




def validar_correo(correo: str) -> bool:
    """
    Valida si el email tiene un formato correcto.
    Acepta letras, números, puntos, guiones y subrayados antes del @.
    Acepta dominios válidos después del @, incluyendo subdominios.

    Ejemplos válidos:
    - usuario@mail.com
    - user.name@mail.co.uk
    - user_name123@sub.domain.org

    Retorna True si es válido, False si no.
    """
    if not correo:
        return False

    correo = correo.strip().lower()
    patron = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(patron, correo) is not None




def normalizar_y_validar_dni(dni: str) -> Optional[str]:
    """
    Normaliza y valida un DNI:
    - Elimina espacios, puntos y comas.
    - Verifica que tenga entre 6 y 9 dígitos numéricos.
    
    Retorna el DNI limpio si es válido, o None si no lo es.
    """
    if not dni:
        return None

    # Eliminar espacios, puntos y comas
    dni_limpio = re.sub(r"[ .,]", "", dni)

    if dni_limpio.isdigit() and 6 <= len(dni_limpio) <= 9:
        return dni_limpio
    return None



def generar_codigo_para_link(length: int = 10) -> str:
    """Genera un código alfanumérico aleatorio de la longitud especificada."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))



def edad_como_texto(nacimiento: date) -> str:
    today = date.today()
    años = today.year - nacimiento.year
    meses = today.month - nacimiento.month
    dias = today.day - nacimiento.day

    if dias < 0:
        meses -= 1
    if meses < 0:
        años -= 1
        meses += 12

    if años > 0:
        if años == 1:
            return "1 año"
        else:
            return f"{años} años"
    elif meses > 0:
        if meses == 1:
            return "1 mes"
        else:
            return f"{meses} meses"
    else:
        return "Menos de 1 mes"



def verify_md5(password: str, hash_md5: str) -> bool:
    """Verifica si la contraseña coincide con el hash MD5 almacenado."""
    return hashlib.md5(password.encode()).hexdigest() == hash_md5




def detect_hash_and_verify(password: str, stored_hash: str) -> bool:
    """Detecta si el hash almacenado es MD5 o Bcrypt y verifica la contraseña."""
    if re.fullmatch(r"[a-fA-F0-9]{32}", stored_hash):  # MD5 hash (32 caracteres hexadecimales)
        return verify_md5(password, stored_hash)
    elif stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):  # Bcrypt hash
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    else:
        return False  # No es un formato reconocido



def capitalizar_nombre(nombre: str) -> str:
    """
    Capitaliza un nombre completo, manteniendo preposiciones en minúscula.
    Ejemplo: "lidia angélica de gomez" → "Lidia Angélica de Gomez"
    """
    preposiciones = {"de", "del", "la", "las", "los", "y"}
    palabras = nombre.lower().split()
    return " ".join([
        palabra if palabra in preposiciones else palabra.capitalize()
        for palabra in palabras
    ])



def normalizar_celular(celular: str) -> dict:
    """
    Limpia, corrige y valida un número de celular.
    Acepta guiones pero rechaza letras u otros caracteres inválidos.

    Devuelve:
    - 'valido': True/False
    - 'celular': versión limpia si fue válido
    - 'motivo': motivo si no fue válido
    """
    if not celular:
        return {
            "valido": False,
            "motivo": "Número no proporcionado"
        }

    # ❌ Rechazar si contiene letras o símbolos extraños (emojis, comillas, etc.)
    if re.search(r"[^\d\s\-\+\(\)\.]", celular):  # permite dígitos, espacios, guiones, paréntesis y puntos
        return {
            "valido": False,
            "motivo": "El número contiene caracteres no válidos (como letras o símbolos)"
        }

    # ✅ Eliminar espacios, guiones, paréntesis y puntos
    celular_limpio = re.sub(r"[ \-\(\)\.]", "", celular)

    # Si empieza con 0, quitarlo
    if celular_limpio.startswith("0"):
        celular_limpio = celular_limpio[1:]

    # Si empieza con 549 sin +, agregar +
    if celular_limpio.startswith("549") and not celular_limpio.startswith("+"):
        celular_limpio = "+" + celular_limpio

    # Si empieza con 11, 351, etc., agregar +54
    if re.match(r"^(11|15|2\d{2}|3\d{2}|4\d{2})\d{6,7}$", celular_limpio):
        celular_limpio = "+54" + celular_limpio

    # Validar largo (solo dígitos)
    digitos = re.sub(r"[^\d]", "", celular_limpio)
    if len(digitos) < 10 or len(digitos) > 15:
        return {
            "valido": False,
            "motivo": "Cantidad de dígitos inválida (debe tener entre 10 y 15)"
        }

    return {
        "valido": True,
        "celular": celular_limpio
    }


def convertir_booleans_a_string(d: dict) -> dict:
    convertido = {}
    for k, v in d.items():
        if isinstance(v, bool):
            convertido[k] = "Y" if v else "N"
        else:
            convertido[k] = v
    return convertido

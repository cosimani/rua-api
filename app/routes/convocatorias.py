from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session, joinedload, aliased, noload
from sqlalchemy.exc import SQLAlchemyError

from sqlalchemy import and_, or_, func, literal, select, literal_column  

import re

from database.config import get_db
from security.security import get_current_user, verify_api_key, require_roles

from helpers.utils import normalizar_y_validar_dni, verificar_recaptcha, validar_correo, \
  capitalizar_nombre, enviar_mail

from datetime import datetime
from models.convocatorias import Postulacion, Convocatoria, DetalleProyectoPostulacion, DetalleNNAEnConvocatoria  
from models.notif_y_observaciones import ObservacionesProyectos
from models.eventos_y_configs import RuaEvento
from models.users import User, Group, UserGroup 
from models.ddjj import DDJJ
from models.proyecto import Proyecto, ProyectoHistorialEstado
from models.nna import Nna
from sqlalchemy.orm.exc import NoResultFound
from datetime import date, datetime
from math import ceil
from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol


import unicodedata





convocatoria_router = APIRouter()





def _parse_date_yyyy_mm_dd(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def crear_ddjj_inicial(db: Session, *, login: str, datos: dict, es_conyuge: bool = False):
    """
    Crea DDJJ básica para el login indicado tomando datos de la postulación.
    Si es_conyuge=True, usa los campos 'conyuge_*' cuando existan.
    """
    # Elegir prefijo de campos según titular/pareja
    pref = "conyuge_" if es_conyuge else ""

    nombre        = datos.get(f"{pref}nombre") or datos.get("nombre")
    apellido      = datos.get(f"{pref}apellido") or datos.get("apellido")
    fecha_nac_str = datos.get(f"{pref}fecha_nacimiento") or datos.get("fecha_nacimiento")
    mail          = datos.get(f"{pref}mail") or datos.get("mail")
    tel_contacto  = datos.get(f"{pref}telefono_contacto") or datos.get("telefono_contacto")
    ocupacion     = datos.get(f"{pref}ocupacion") or datos.get("ocupacion")

    # Dirección: la tomamos de la postulación (es la misma para ambos)
    calle         = datos.get("calle_y_nro")
    depto         = datos.get("depto")
    barrio        = datos.get("barrio")
    localidad     = datos.get("localidad")
    cp            = datos.get("cp")
    provincia     = datos.get("provincia")

    # Subregistros (copiamos sólo los que tenés en la postulación)
    subregs = {
        "subreg_3":      datos.get("subreg_3"),
        "subreg_4":      datos.get("subreg_4"),
        "subreg_5A1ET":  datos.get("subreg_5A1ET"),
        "subreg_5A2ET":  datos.get("subreg_5A2ET"),
        "subreg_5B1ET":  datos.get("subreg_5B1ET"),
        "subreg_5B2ET":  datos.get("subreg_5B2ET"),
        "subreg_5B3ET":  datos.get("subreg_5B3ET"),
        "subreg_61ET":   datos.get("subreg_61ET"),
        "subreg_62ET":   datos.get("subreg_62ET"),
        "subreg_63ET":   datos.get("subreg_63ET"),
    }

    ddjj = DDJJ(
        login = login,
        ddjj_fecha_ultimo_cambio = datetime.now().strftime("%Y-%m-%d"),
        ddjj_nombre = capitalizar_nombre(nombre or ""),
        ddjj_apellido = capitalizar_nombre(apellido or ""),
        ddjj_estado_civil = datos.get("estado_civil"),
        ddjj_calle = calle,
        ddjj_depto = depto,
        ddjj_barrio = barrio,
        ddjj_localidad = localidad,
        ddjj_cp = cp,
        ddjj_provincia = provincia,
        # duplicamos como domicilio legal (si después lo editan, lo cambian en el flujo propio)
        ddjj_calle_legal = calle,
        ddjj_depto_legal = depto,
        ddjj_barrio_legal = barrio,
        ddjj_localidad_legal = localidad,
        ddjj_cp_legal = cp,
        ddjj_provincia_legal = provincia,
        ddjj_fecha_nac = _parse_date_yyyy_mm_dd(fecha_nac_str) if fecha_nac_str else None,
        ddjj_nacionalidad = datos.get("nacionalidad"),
        ddjj_sexo = datos.get("sexo"),
        ddjj_correo_electronico = (mail or "").lower(),
        ddjj_telefono = tel_contacto,
        ddjj_ocupacion = ocupacion,
        # Subregistros “definitivos” que mapean 1:1 con los de la postulación
        subreg_3 = subregs["subreg_3"],
        subreg_4 = subregs["subreg_4"],
        subreg_5A1ET = subregs["subreg_5A1ET"],
        subreg_5A2ET = subregs["subreg_5A2ET"],
        subreg_5B1ET = subregs["subreg_5B1ET"],
        subreg_5B2ET = subregs["subreg_5B2ET"],
        subreg_5B3ET = subregs["subreg_5B3ET"],
        subreg_61ET = subregs["subreg_61ET"],
        subreg_62ET = subregs["subreg_62ET"],
        subreg_63ET = subregs["subreg_63ET"],
    )

    db.add(ddjj)
    db.flush()
    return ddjj



def _plantilla_mail_postulacion(nombre: str, convocatoria: Convocatoria) -> str:
    ref  = convocatoria.convocatoria_referencia or "-"
    llam = convocatoria.convocatoria_llamado or "-"
    edad = convocatoria.convocatoria_edad_es or "-"

    primer_nombre = nombre.split(" ")[0].capitalize() if nombre else "Postulante"

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
                    <p>¡Hola, <strong>{primer_nombre}</strong>! Nos comunicamos desde el 
                       <strong>Registro Único de Adopciones de Córdoba</strong>.</p>

                    <p>Recibimos tu <strong>postulación</strong> a la convocatoria:</p>

                    <ul style="line-height: 1.6; padding-left: 20px;">
                      <li><strong>Referencia:</strong> {ref}</li>
                      <li>{llam} - {edad}</li>
                    </ul>

                    <p>
                      En los próximos días nos vamos a contactar para coordinar una entrevista.
                    </p>

                  </td>
                </tr>
      
                <tr>
                  <td style="font-size: 17px; padding-top: 20px;">
                    ¡Saludos!
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    return cuerpo_html



def _normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower()

def _extract_age_range(text: str) -> Optional[Tuple[int, int]]:
    """
    Extrae un rango de edades desde texto libre.
    - Si hay varios numeros: devuelve [min, max]
    - Si hay uno solo: [n, n]
    - Si no hay: None
    """
    if not text:
        return None
    nums = [int(x) for x in re.findall(r"\d+", text)]
    if not nums:
        return None
    return (min(nums), max(nums))

def _overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return max(a[0], b[0]) <= min(a[1], b[1])

def _is_group_siblings(c) -> bool:
    """
    Heuristica por palabras clave en llamado/descripcion/edad_es.
    """
    t = _normalize_text(f"{c.convocatoria_llamado} {c.convocatoria_descripcion} {c.convocatoria_edad_es}")
    # claves comunes: grupo de hermanos / hermanos / hermanas / fratria
    return (
        ("grupo" in t and "herman" in t) or
        ("hermanos" in t) or
        ("hermanas" in t) or
        ("fratria" in t)  # por si algun juzgado usa esta palabra
    )


def _parse_client_ranges(ranges: List[str]) -> List[Tuple[int, int]]:
    """Convierte ["0-3","4-6"] -> [(0,3),(4,6)], ignora inválidos."""
    out: List[Tuple[int, int]] = []
    for r in ranges or []:
        m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", r)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b:
                out.append((a, b))
    return out


@convocatoria_router.get("/", response_model=dict, dependencies=[Depends(verify_api_key)])
def get_convocatorias(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None),
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    online: Optional[bool] = Query(None)
):
    try:
        query = db.query(Convocatoria)

        # Hacer join con DetalleNNAEnConvocatoria y Nna si hay búsqueda
        if search and len(search) >= 3:
            pattern = f"%{search}%"

            # Crear un alias para Nna si es necesario (opcional)
            nna_alias = aliased(Nna)

            query = query.outerjoin(Convocatoria.detalle_nnas).outerjoin(DetalleNNAEnConvocatoria.nna)

            query = query.filter(
                or_(
                    Convocatoria.convocatoria_referencia.ilike(pattern),
                    Convocatoria.convocatoria_llamado.ilike(pattern),
                    Convocatoria.convocatoria_edad_es.ilike(pattern),
                    Convocatoria.convocatoria_residencia_postulantes.ilike(pattern),
                    Convocatoria.convocatoria_descripcion.ilike(pattern),
                    Convocatoria.convocatoria_juzgado_interviniente.ilike(pattern),
                    Nna.nna_nombre.ilike(pattern),
                    Nna.nna_apellido.ilike(pattern)
                )
            )

        if fecha_inicio:
            query = query.filter(Convocatoria.convocatoria_fecha_publicacion >= fecha_inicio)
        if fecha_fin:
            query = query.filter(Convocatoria.convocatoria_fecha_publicacion <= fecha_fin)

        if online is True:
            query = query.filter(Convocatoria.convocatoria_online == "Y")
        elif online is False:
            query = query.filter(Convocatoria.convocatoria_online == "N")


        total_records = query.count()
        total_pages = ceil(total_records / limit)

        convocatorias = query \
            .options(joinedload(Convocatoria.detalle_nnas).joinedload(DetalleNNAEnConvocatoria.nna)) \
            .order_by(
                Convocatoria.convocatoria_fecha_publicacion.desc(),
                Convocatoria.convocatoria_referencia.desc()
            ) \
            .offset((page - 1) * limit) \
            .limit(limit) \
            .all()

        convocatorias_list = []
        for convocatoria in convocatorias:
            nna_asociados = []
            for detalle in convocatoria.detalle_nnas:
                nna = detalle.nna
                if nna:
                    edad = date.today().year - nna.nna_fecha_nacimiento.year - (
                        (date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
                    )
                    nna_asociados.append({
                        "nna_id": nna.nna_id,
                        "nna_nombre": nna.nna_nombre,
                        "nna_apellido": nna.nna_apellido,
                        "nna_edad": edad  # o edad_como_texto(nna.nna_fecha_nacimiento)
                    })

            convocatorias_list.append({
                "convocatoria_id": convocatoria.convocatoria_id,
                "convocatoria_referencia": convocatoria.convocatoria_referencia,
                "convocatoria_llamado": convocatoria.convocatoria_llamado,
                "convocatoria_edad_es": convocatoria.convocatoria_edad_es,
                "convocatoria_residencia_postulantes": convocatoria.convocatoria_residencia_postulantes,
                "convocatoria_descripcion": convocatoria.convocatoria_descripcion,
                "convocatoria_juzgado_interviniente": convocatoria.convocatoria_juzgado_interviniente,
                "convocatoria_fecha_publicacion": convocatoria.convocatoria_fecha_publicacion,
                "convocatoria_online": convocatoria.convocatoria_online,
                "nna_ids": [detalle.nna_id for detalle in convocatoria.detalle_nnas],
                "nna_asociados": nna_asociados
            })

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "convocatorias": convocatorias_list,
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar convocatorias: {str(e)}")



@convocatoria_router.get("/publicas", response_model=dict)
def get_convocatorias_publicas(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    ranges: List[str] = Query(default=[], description="Ej: ranges=0-3&ranges=4-6"),
    grupo: Optional[bool] = Query(default=None, description="true => sólo convocatorias con 2+ NNA vinculados")
):
    """
    Filtros:
    - Sin filtros => todas las online.
    - ranges=["a-b", ...] => incluye convocatorias que tengan AL MENOS UN NNA en alguno de esos rangos.
    - grupo=true => incluye convocatorias que tengan 2 o más NNA vinculados (sin usar hermanos_id).
    - Si ambos se envían => se exige ambos (AND).
    """
    try:
        # Base de IDs de convocatorias online (para poder DISTINCT y paginar correctamente)
        id_q = db.query(Convocatoria.convocatoria_id).filter(Convocatoria.convocatoria_online == "Y")

        parsed_ranges = _parse_client_ranges(ranges)

        # --- Filtro por edades: al menos un NNA en rango ---
        if parsed_ranges:
            age_expr = func.timestampdiff(
                literal_column("YEAR"),               # ✅ evita parametrizar 'YEAR' en MySQL
                Nna.nna_fecha_nacimiento,
                func.curdate()
            )
            age_or = or_(*[and_(age_expr >= a, age_expr <= b) for (a, b) in parsed_ranges])

            id_q = (
                id_q.join(
                    DetalleNNAEnConvocatoria,
                    DetalleNNAEnConvocatoria.convocatoria_id == Convocatoria.convocatoria_id
                )
                .join(Nna, Nna.nna_id == DetalleNNAEnConvocatoria.nna_id)
                .filter(Nna.nna_fecha_nacimiento.isnot(None))
                .filter(age_or)
            )
            # NOTA: este join garantiza "al menos un NNA en rango" por convocatoria

        # --- Filtro por "grupo de hermanos" (2+ NNA vinculados, sin hermanos_id) ---
        if grupo is True:
            # Subconsulta: convocatorias con 2 o más NNA vinculados (conteo por convocatoria)
            grupos_subq = (
                db.query(
                    DetalleNNAEnConvocatoria.convocatoria_id.label("conv_id")
                )
                .group_by(DetalleNNAEnConvocatoria.convocatoria_id)
                .having(func.count(func.distinct(DetalleNNAEnConvocatoria.nna_id)) >= 2)
                .subquery()
            )
            id_q = id_q.join(grupos_subq, grupos_subq.c.conv_id == Convocatoria.convocatoria_id)

        # Distinct IDs tras filtros aplicados (si no hubo filtros: todas online)
        id_subq = id_q.distinct().subquery()

        # Totales ya filtrados
        total_records = db.query(func.count()).select_from(id_subq).scalar() or 0
        total_pages = ceil(total_records / limit) if limit else 1

        # Orden y paginación finales
        order_cols = (
            Convocatoria.convocatoria_fecha_publicacion.desc(),
            Convocatoria.convocatoria_referencia.desc(),
        )

        page_items = (
            db.query(Convocatoria)
              .join(id_subq, id_subq.c.convocatoria_id == Convocatoria.convocatoria_id)
              .order_by(*order_cols)
              .offset((page - 1) * limit)
              .limit(limit)
              .all()
        )

        convocatorias_list = [
            {
                "convocatoria_id": c.convocatoria_id,
                "convocatoria_referencia": c.convocatoria_referencia,
                "convocatoria_llamado": c.convocatoria_llamado,
                "convocatoria_edad_es": c.convocatoria_edad_es,
                "convocatoria_residencia_postulantes": c.convocatoria_residencia_postulantes,
                "convocatoria_descripcion": c.convocatoria_descripcion,
                "convocatoria_juzgado_interviniente": c.convocatoria_juzgado_interviniente,
                "convocatoria_fecha_publicacion": c.convocatoria_fecha_publicacion,
            }
            for c in page_items
        ]

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "convocatorias": convocatorias_list
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar convocatorias públicas: {str(e)}")



@convocatoria_router.get("/{convocatoria_id}", response_model=dict, dependencies=[Depends(verify_api_key)])
def get_convocatoria_by_id(convocatoria_id: int, db: Session = Depends(get_db)):
    try:
        convocatoria = db.query(Convocatoria).filter(Convocatoria.convocatoria_id == convocatoria_id).first()
        if not convocatoria:
            raise HTTPException(status_code=404, detail="Convocatoria no encontrada")

        nna_ids = db.query(DetalleNNAEnConvocatoria.nna_id).filter(
            DetalleNNAEnConvocatoria.convocatoria_id == convocatoria.convocatoria_id
        ).all()

        return {
            "convocatoria_id": convocatoria.convocatoria_id,
            "convocatoria_referencia": convocatoria.convocatoria_referencia,
            "convocatoria_llamado": convocatoria.convocatoria_llamado,
            "convocatoria_edad_es": convocatoria.convocatoria_edad_es,
            "convocatoria_residencia_postulantes": convocatoria.convocatoria_residencia_postulantes,
            "convocatoria_descripcion": convocatoria.convocatoria_descripcion,
            "convocatoria_juzgado_interviniente": convocatoria.convocatoria_juzgado_interviniente,
            "convocatoria_fecha_publicacion": convocatoria.convocatoria_fecha_publicacion,
            "convocatoria_online": convocatoria.convocatoria_online,
            "nna_ids": [n[0] for n in nna_ids]
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar convocatoria: {str(e)}")


@convocatoria_router.get("/publicas/{convocatoria_id}", response_model=dict)
def get_convocatoria_publica_by_id(convocatoria_id: int, db: Session = Depends(get_db)):
    try:
        convocatoria = db.query(Convocatoria).filter(
            Convocatoria.convocatoria_id == convocatoria_id,
            Convocatoria.convocatoria_online == "Y"
        ).first()

        if not convocatoria:
            raise HTTPException(status_code=404, detail="Convocatoria no encontrada o no está publicada")

        nna_ids = db.query(DetalleNNAEnConvocatoria.nna_id).filter(
            DetalleNNAEnConvocatoria.convocatoria_id == convocatoria.convocatoria_id
        ).all()

        return {
            "convocatoria_id": convocatoria.convocatoria_id,
            "convocatoria_referencia": convocatoria.convocatoria_referencia,
            "convocatoria_llamado": convocatoria.convocatoria_llamado,
            "convocatoria_edad_es": convocatoria.convocatoria_edad_es,
            "convocatoria_residencia_postulantes": convocatoria.convocatoria_residencia_postulantes,
            "convocatoria_descripcion": convocatoria.convocatoria_descripcion,
            "convocatoria_juzgado_interviniente": convocatoria.convocatoria_juzgado_interviniente,
            "convocatoria_fecha_publicacion": convocatoria.convocatoria_fecha_publicacion,
            "nna_ids": [n[0] for n in nna_ids]
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar convocatoria pública: {str(e)}")




@convocatoria_router.post("/", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def upsert_convocatoria(
    convocatoria_data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        convocatoria_id = convocatoria_data.get("convocatoria_id")
        nna_ids = convocatoria_data.get("nna_id", [])
        convocatoria_online = convocatoria_data.get("convocatoria_online")

        # 🛡️ Validar existencia de todos los NNAs enviados
        if nna_ids:
            existentes = db.query(Nna.nna_id).filter(Nna.nna_id.in_(nna_ids)).all()
            existentes_ids = {nna_id for (nna_id,) in existentes}
            faltantes = [nna_id for nna_id in nna_ids if nna_id not in existentes_ids]
            if faltantes:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Los siguientes NNA no existen: {faltantes}",
                    "tiempo_mensaje": 8,
                    "next_page": "actual"
                }

        if convocatoria_id:
            # 🔁 Actualizar convocatoria existente
            convocatoria = db.query(Convocatoria).filter_by(convocatoria_id=convocatoria_id).first()
            if not convocatoria:
                raise HTTPException(status_code=404, detail="Convocatoria no encontrada")

            convocatoria.convocatoria_referencia = convocatoria_data.get("convocatoria_referencia")
            convocatoria.convocatoria_llamado = convocatoria_data.get("convocatoria_llamado")
            convocatoria.convocatoria_edad_es = convocatoria_data.get("convocatoria_edad_es")
            convocatoria.convocatoria_residencia_postulantes = convocatoria_data.get("convocatoria_residencia_postulantes")
            convocatoria.convocatoria_descripcion = convocatoria_data.get("convocatoria_descripcion")
            convocatoria.convocatoria_juzgado_interviniente = convocatoria_data.get("convocatoria_juzgado_interviniente")
            convocatoria.convocatoria_online = convocatoria_online

            # 🔄 Reemplazar los NNA asociados
            db.query(DetalleNNAEnConvocatoria).filter_by(convocatoria_id=convocatoria_id).delete()
        else:
            # 🆕 Crear nueva convocatoria
            convocatoria = Convocatoria(
                convocatoria_referencia=convocatoria_data.get("convocatoria_referencia"),
                convocatoria_llamado=convocatoria_data.get("convocatoria_llamado"),
                convocatoria_edad_es=convocatoria_data.get("convocatoria_edad_es"),
                convocatoria_residencia_postulantes=convocatoria_data.get("convocatoria_residencia_postulantes"),
                convocatoria_descripcion=convocatoria_data.get("convocatoria_descripcion"),
                convocatoria_juzgado_interviniente=convocatoria_data.get("convocatoria_juzgado_interviniente"),
                convocatoria_fecha_publicacion = datetime.now().date(),
                convocatoria_online=convocatoria_online
            )
            db.add(convocatoria)
            db.flush()  # importante para obtener el ID antes de agregar los NNA

        # Asociar NNAs nuevamente
        for nna_id in nna_ids:
            db.add(DetalleNNAEnConvocatoria(convocatoria_id=convocatoria.convocatoria_id, nna_id=nna_id))

        # 📌 Evento
        evento = RuaEvento(
            evento_detalle=(
                f"Se {'modificó' if convocatoria_id else 'creó'} la convocatoria {convocatoria.convocatoria_referencia}"
            ),
            evento_fecha=datetime.now(),
            login=current_user["user"]["login"]
        )
        db.add(evento)

        db.commit()
        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Convocatoria {'actualizada' if convocatoria_id else 'creada'} con éxito",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al guardar la convocatoria: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@convocatoria_router.delete("/{convocatoria_id}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def delete_convocatoria(convocatoria_id: int, db: Session = Depends(get_db)):
    """
    Elimina una convocatoria si existe.
    """
    try:
        convocatoria = db.query(Convocatoria).filter(Convocatoria.convocatoria_id == convocatoria_id).first()
        if not convocatoria:
            raise HTTPException(status_code=404, detail="Convocatoria no encontrada")

        db.delete(convocatoria)
        db.commit()
        return {"message": "Convocatoria eliminada exitosamente"}

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al eliminar la convocatoria: {str(e)}")



@convocatoria_router.put("/{convocatoria_id}/cerrar-y-eliminar", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def cerrar_y_eliminar_convocatoria(
    convocatoria_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Cierra una convocatoria aplicando reglas:
    - Si hay proyectos 'avanzados' (vinculación, guarda provisoria/confirmada, adopción definitiva) asociados a sus postulaciones -> BLOQUEA.
    - NNA asociados vuelven a 'disponible' y se limpian vínculos DetalleNNAEnConvocatoria.
    - Proyectos vinculados a postulaciones de esta convocatoria pasan a 'baja_por_convocatoria' (historial + observación con NNA).
    - No se eliminan las postulaciones (trazabilidad).
    - La convocatoria NO se elimina: se marca convocatoria_online = 'N'.
    """
    ADVANCED_STATES = ("vinculacion", "guarda_provisoria", "guarda_confirmada", "adopcion_definitiva")

    convocatoria = (
        db.query(Convocatoria)
          .options(noload(Convocatoria.detalle_nnas))
          .filter(Convocatoria.convocatoria_id == convocatoria_id)
          .first()
    )
    if not convocatoria:
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": "Convocatoria no encontrada.", "tiempo_mensaje": 6, "next_page": "actual",
        }

    try:
        # 1) Bloqueo si hay proyectos avanzados
        proyectos_avanzados = (
            db.query(Proyecto.proyecto_id, Proyecto.estado_general)
              .join(DetalleProyectoPostulacion, DetalleProyectoPostulacion.proyecto_id == Proyecto.proyecto_id)
              .join(Postulacion, Postulacion.postulacion_id == DetalleProyectoPostulacion.postulacion_id)
              .filter(Postulacion.convocatoria_id == convocatoria_id)
              .filter(Proyecto.estado_general.in_(ADVANCED_STATES))
              .all()
        )
        if proyectos_avanzados:
            ids = [p[0] for p in proyectos_avanzados]
            return {
                "success": False, "tipo_mensaje": "naranja",
                "mensaje": (
                    "No se puede cerrar la convocatoria porque hay proyecto(s) en estado avanzado "
                    f"(vinculación/guarda/adopción): {ids}."
                ),
                "tiempo_mensaje": 10, "next_page": "actual",
            }

        # --- Obtener NNA asociados (IDs y nombres) para usar en varios pasos
        nna_rows = db.query(DetalleNNAEnConvocatoria.nna_id).filter(
            DetalleNNAEnConvocatoria.convocatoria_id == convocatoria_id
        ).all()
        nna_ids = [x[0] for x in nna_rows]

        # Armar string de nombres "Nombre Apellido" separados por coma
        if nna_ids:
            nna_objs = db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).all()
            nna_nombres_list = []
            for n in nna_objs:
                nombre = (n.nna_nombre or "").strip()
                apellido = (n.nna_apellido or "").strip()
                full = f"{nombre} {apellido}".strip()
                if full:  # evita cadenas vacías
                    nna_nombres_list.append(full)
            nna_nombres_str = ", ".join(nna_nombres_list) if nna_nombres_list else "NNA sin nombre registrado"
        else:
            nna_nombres_str = "Sin NNA asociados"

        # 2) Liberar NNA y eliminar vínculos
        if nna_ids:
            db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).update(
                {Nna.nna_estado: "disponible", Nna.nna_en_convocatoria: "N"},
                synchronize_session=False
            )
            db.query(DetalleNNAEnConvocatoria).filter(
                DetalleNNAEnConvocatoria.convocatoria_id == convocatoria_id
            ).delete(synchronize_session=False)

            db.flush()  # materializar DELETE antes de tocar la "convocatoria"

        # 3) Proyectos asociados -> baja_por_convocatoria (no se borran postulaciones)
        proyectos_a_bajar = (
            db.query(Proyecto)
              .join(DetalleProyectoPostulacion, DetalleProyectoPostulacion.proyecto_id == Proyecto.proyecto_id)
              .join(Postulacion, Postulacion.postulacion_id == DetalleProyectoPostulacion.postulacion_id)
              .filter(Postulacion.convocatoria_id == convocatoria_id)
              .all()
        )
        for pr in proyectos_a_bajar:
            estado_anterior = pr.estado_general
            if estado_anterior != "baja_por_convocatoria":
                # Historial
                db.add(ProyectoHistorialEstado(
                    proyecto_id=pr.proyecto_id,
                    estado_anterior=estado_anterior,
                    estado_nuevo="baja_por_convocatoria",
                    fecha_hora=datetime.now()
                ))
                # Observación (incluye NNA)
                obs_text = (
                    f"[Sistema] Proyecto dado de baja por cierre de convocatoria #{convocatoria_id}. "
                    f"NNA asociados: {nna_nombres_str}."
                )
                db.add(ObservacionesProyectos(
                    observacion_a_cual_proyecto=pr.proyecto_id,
                    observacion=obs_text,
                    login_que_observo=current_user["user"]["login"],
                    observacion_fecha=datetime.now()
                ))
                pr.estado_general = "baja_por_convocatoria"

        # 4) Mantener postulaciones para trazabilidad

        # 5) Marcar convocatoria como OFFLINE (no eliminar)
        convocatoria.convocatoria_online = "N"

        # 6) Evento (solo cantidades)
        db.add(RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=(
                f"Convocatoria #{convocatoria_id} cerrada (offline). "
                f"NNA liberados: {len(nna_ids)}; proyectos dados de baja: {len(proyectos_a_bajar)}."
            ),
            evento_fecha=datetime.now()
        ))

        db.commit()

        return {
            "success": True, "tipo_mensaje": "verde",
            "mensaje": (
                f"Convocatoria cerrada (offline). NNA puestos en 'disponible': {len(nna_ids)}. "
                f"Proyectos dados de baja por convocatoria: {len(proyectos_a_bajar)}."
            ),
            "tiempo_mensaje": 8, "next_page": "actual",
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False, "tipo_mensaje": "rojo",
            "mensaje": f"Error al cerrar la convocatoria: {str(e)}",
            "tiempo_mensaje": 8, "next_page": "actual",
        }




@convocatoria_router.post("/postulacion", response_model = dict, dependencies = [Depends(verify_api_key)])
async def crear_postulacion( datos: dict = Body(...), db: Session = Depends(get_db), ):
    """
    📝 Da de alta una nueva postulación a convocatoria y crea automáticamente, el usuario principal, su ddjj y el proyecto.

    """

    try:
        # --- 1) reCAPTCHA ---
        recaptcha_token = datos.get("recaptcha_token")
        if not recaptcha_token or not await verificar_recaptcha(recaptcha_token):
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "<p>Falló la verificación reCAPTCHA. Por favor, intentá de nuevo.</p>",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        datos.pop("recaptcha_token", None)

        # --- 2) Convocatoria ---
        convocatoria_id = datos.get("convocatoria_id")

        if not convocatoria_id:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": (
                    "<p>Falta un campo obligatorio que identifica la convocatoria.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        convocatoria = db.query(Convocatoria).filter(Convocatoria.convocatoria_id == convocatoria_id).first()
        if not convocatoria:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": (
                    f"<p>No se encontró la convocatoria.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # --- 3) Reglas de campos obligatorios (titular y cónyuge) ---
        def _vacio(v) :  
            return (v is None) or (isinstance(v, str) and v.strip() == "")

        req_titular = ["nombre","apellido","dni","fecha_nacimiento",
                       "mail","telefono_contacto","localidad","provincia"]
        req_conyuge = []

        if datos.get("conyuge_convive") == "Y":
            req_conyuge = ["conyuge_nombre","conyuge_apellido","conyuge_dni",
                           "conyuge_fecha_nacimiento","conyuge_telefono_contacto"]

        faltantes = [c for c in (req_titular + req_conyuge) if _vacio(datos.get(c))]

        if faltantes:          
    
            nombres_amigables = {
                "nombre":"Nombre","apellido":"Apellido","dni":"DNI","fecha_nacimiento":"Fecha de nacimiento",
                "nacionalidad":"Nacionalidad","estado_civil":"Estado civil","calle_y_nro":"Calle y número",
                "localidad":"Localidad","provincia":"Provincia","telefono_contacto":"Teléfono de contacto",
                "mail":"Correo electrónico","ocupacion":"Ocupación / profesión","conyuge_nombre":"Nombre de la pareja",
                "conyuge_apellido":"Apellido de la pareja","conyuge_dni":"DNI de la pareja",
                "conyuge_fecha_nacimiento":"Fecha de nacimiento de la pareja","conyuge_telefono_contacto":"Teléfono de la pareja",
            }
            return {"success": False, "tipo_mensaje": "amarillo",
                    "mensaje": "<p>Faltan completar los siguientes campos obligatorios:</p><ul>"
                               + "".join(f"<li>{nombres_amigables.get(c, c.replace('_',' ').capitalize())}</li>" for c in faltantes)
                               + "</ul>",
                    "tiempo_mensaje": 5, "next_page": "actual"}
       

        # --- 4) DNI y mails ---
        dni = normalizar_y_validar_dni(datos.get("dni")) 
        if not dni: 
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": (
                    "<p>Debe indicar un DNI válido.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
      
    
        # Validar formato de correo
        if not validar_correo((datos.get("mail") or "").strip().lower()):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": (
                    "<p>El correo electrónico no tiene un formato válido.</p>"
                    "<p>Por favor, intente nuevamente.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }


        # ───── 1. Detectar y validar DNI del cónyuge lo más temprano posible ─────
        tiene_conyuge = datos.get("conyuge_convive") == "Y" and datos.get("conyuge_dni")
        conyuge_dni = None
       


        if tiene_conyuge:
            conyuge_dni = normalizar_y_validar_dni(datos["conyuge_dni"])
            if not conyuge_dni:
                return {
                    "success": False,
                    "tipo_mensaje": "amarillo",
                    "mensaje": "<p>Debe indicar un DNI válido para cónyuge.</p>",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }
            
            if conyuge_dni == dni:
                return {"success": False, "tipo_mensaje": "amarillo",
                        "mensaje": "<p>El DNI del/la cónyuge no puede ser el mismo que el titular.</p>",
                        "tiempo_mensaje": 6, "next_page": "actual"}


        # --- 5) No-viables y duplicidades ---
        dni_busqueda = [dni] + ([conyuge_dni] if conyuge_dni else [])

        proyectos_no_viables = (
            db.query(Proyecto)
            .filter(
                Proyecto.estado_general == "no_viable",
                or_(
                    Proyecto.login_1.in_(dni_busqueda),
                    Proyecto.login_2.in_(dni_busqueda)
                )
            )
            .all()
        )

        # ───── 3. Construir mensaje según quién esté involucrado ─────
        involucra_titular  = any(p.login_1 == dni or p.login_2 == dni for p in proyectos_no_viables)
        involucra_conyuge = any(
            conyuge_dni and (p.login_1 == conyuge_dni or p.login_2 == conyuge_dni)
            for p in proyectos_no_viables
        )

        if involucra_titular or involucra_conyuge:
            if involucra_titular and involucra_conyuge:
                msj = ("<p>Tanto vos como la persona indicada como pareja "
                      "forman parte de un proyecto con estado <strong>no viable</strong>. "
                      "No pueden registrar una nueva postulación.</p>")
            elif involucra_titular:
                msj = ("<p>Vos ya formás parte de un proyecto con estado "
                      "<strong>no viable</strong>. No podés registrar una nueva postulación.</p>")
            else:  # solo cónyuge
                msj = ("<p>La persona indicada como pareja ya forma parte de un proyecto "
                      "con estado <strong>no viable</strong>. No pueden registrar una nueva postulación.</p>")

            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": msj,
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }


        # Duplicidad de postulación (titular y cónyuge)
        if db.query(Postulacion).filter(Postulacion.convocatoria_id == convocatoria_id,
                                        Postulacion.dni == dni).first():
            return {"success": False, "tipo_mensaje": "amarillo",
                    "mensaje": "<p>Ya existe una postulación con tu DNI para esta convocatoria. No es posible registrar otra.</p>",
                    "tiempo_mensaje": 6, "next_page": "actual"}

        if tiene_conyuge:
            if db.query(Postulacion).filter(Postulacion.convocatoria_id == convocatoria_id,
                                            Postulacion.dni == conyuge_dni).first():
                return {"success": False, "tipo_mensaje": "amarillo",
                        "mensaje": "<p>La persona indicada como pareja ya se ha postulado a esta convocatoria. "
                                   "No es posible registrar la postulación.</p>",
                        "tiempo_mensaje": 6, "next_page": "actual"}

        # --- 6) Normalizaciones “cosméticas” y payload de Postulación ---
        datos_limpios = {**datos}
        datos_limpios["convocatoria_id"] = convocatoria_id
        datos_limpios["dni"] = dni
        if tiene_conyuge and conyuge_dni:
            datos_limpios["conyuge_dni"] = conyuge_dni
        if datos_limpios.get("nombre"): 
            datos_limpios["nombre"] = capitalizar_nombre(datos_limpios["nombre"])
        if datos_limpios.get("apellido"): 
            datos_limpios["apellido"] = capitalizar_nombre(datos_limpios["apellido"])
        if datos_limpios.get("conyuge_nombre"): 
            datos_limpios["conyuge_nombre"] = capitalizar_nombre(datos_limpios["conyuge_nombre"])
        if datos_limpios.get("conyuge_apellido"): 
            datos_limpios["conyuge_apellido"] = capitalizar_nombre(datos_limpios["conyuge_apellido"])
        if datos_limpios.get("mail"): 
            datos_limpios["mail"] = datos_limpios["mail"].strip().lower()
        if datos_limpios.get("conyuge_mail"): 
            datos_limpios["conyuge_mail"] = datos_limpios["conyuge_mail"].strip().lower()

        # Campos que acepta el modelo Postulacion (coinciden con tus columnas)
        CAMPOS_POSTULACION = [
            "convocatoria_id","nombre","apellido","dni","fecha_nacimiento","nacionalidad","sexo","estado_civil",
            "calle_y_nro","depto","barrio","localidad","cp","provincia","telefono_contacto","telefono_fijo",
            "videollamada","whatsapp","mail","movilidad_propia","obra_social","ocupacion",
            "conyuge_convive","conyuge_nombre","conyuge_apellido","conyuge_dni","conyuge_edad",
            "conyuge_fecha_nacimiento","conyuge_telefono_contacto","conyuge_telefono_fijo","conyuge_mail",
            "conyuge_ocupacion","conyuge_otros_datos","hijos","acogimiento_es","acogimiento_descripcion",
            "en_rua","subregistro_comentarios","terminaste_inscripcion_rua","otra_convocatoria",
            "otra_convocatoria_comentarios","antecedentes","antecedentes_comentarios","como_tomaron_conocimiento",
            "motivos","comunicaron_decision","otros_comentarios","inscripto_en_rua",
            "subreg_3","subreg_4","subreg_5A1ET","subreg_5A2ET","subreg_5B1ET","subreg_5B2ET","subreg_5B3ET",
            "subreg_61ET","subreg_62ET","subreg_63ET",
        ]
        payload_postulacion = {k: datos_limpios.get(k) for k in CAMPOS_POSTULACION}
        nueva_postulacion = Postulacion(**payload_postulacion)

        db.add(nueva_postulacion)
        db.flush()
        db.refresh(nueva_postulacion)

        # ========== AQUÍ EMPIEZA TU SECCIÓN 6 AJUSTADA (Usuarios + DDJJ) ==========
        # Validaciones de mail y unicidad (ANTES de crear usuarios)
        mail_titular = (datos_limpios.get("mail") or "").strip().lower()
        if not validar_correo(mail_titular):
            return {"success": False, "tipo_mensaje": "naranja",
                    "mensaje": "<p>El correo electrónico del titular no tiene un formato válido.</p>",
                    "tiempo_mensaje": 5, "next_page": "actual"}
        if db.query(User).filter(User.mail == mail_titular, User.login != dni).first():
            return {"success": False, "tipo_mensaje": "amarillo",
                    "mensaje": "<p>El correo ingresado ya está en uso por otro usuario. Por favor, indicá otro correo.</p>",
                    "tiempo_mensaje": 6, "next_page": "actual"}       

        tiene_conyuge_para_proyecto = bool(conyuge_dni and datos.get("conyuge_convive") == "Y")
        conyuge_mail_limpio = None
        if tiene_conyuge_para_proyecto and datos_limpios.get("conyuge_mail"):
            conyuge_mail_limpio = datos_limpios["conyuge_mail"]
            if not validar_correo(conyuge_mail_limpio):
                return {"success": False, "tipo_mensaje": "naranja",
                        "mensaje": "<p>El correo electrónico del/la cónyuge no tiene un formato válido.</p>",
                        "tiempo_mensaje": 5, "next_page": "actual"}
            if db.query(User).filter(User.mail == conyuge_mail_limpio, User.login != conyuge_dni).first():
                return {"success": False, "tipo_mensaje": "amarillo",
                        "mensaje": "<p>El correo ingresado para el/la cónyuge ya está en uso por otro usuario. "
                                   "Por favor, indicá otro correo.</p>",
                        "tiempo_mensaje": 6, "next_page": "actual"}




        grupo_adoptante = db.query(Group).filter(Group.description == "adoptante").first()


        # Alta usuario login_1 (postulante)
        usuario_1 = db.query(User).filter(User.login == dni).first()
        if not usuario_1:
            nuevo_usuario_1 = User(
                login=dni,
                nombre=capitalizar_nombre(datos.get("nombre", "")),
                apellido=capitalizar_nombre(datos.get("apellido", "")),
                celular=datos.get("telefono_contacto"),
                mail=mail_titular,
                profesion=(datos.get("ocupacion") or "")[:60],
                calle_y_nro=datos.get("calle_y_nro"),
                depto_etc=datos.get("depto"),
                barrio=datos.get("barrio"),
                localidad=datos.get("localidad"),
                provincia=(datos.get("provincia") or "")[:50],
                active="Y", operativo="Y",
                doc_adoptante_curso_aprobado="N",
                doc_adoptante_ddjj_firmada="N",
                fecha_alta=date.today()
            )
            db.add(nuevo_usuario_1)
            db.flush()   # <-- forzamos la inserción del usuario antes de crear eventos

            if grupo_adoptante:
                db.add(UserGroup(login = dni, group_id = grupo_adoptante.group_id))

            db.add(RuaEvento(
                login = dni,
                evento_detalle = f"Usuario generado automáticamente desde postulación a convocatoria ID {convocatoria_id}.",
                evento_fecha = datetime.now()
            ))

        # asegurar DDJJ del titular, exista o no el usuario
        if not db.query(DDJJ).filter(DDJJ.login == dni).first():
            crear_ddjj_inicial(db, login=dni, datos=datos_limpios, es_conyuge=False)

        # Usuario cónyuge (si corresponde)
        if tiene_conyuge_para_proyecto:
            usuario_2 = db.query(User).filter(User.login == conyuge_dni).first()
            if not usuario_2:
                nuevo_usuario_2 = User(
                    login=conyuge_dni,
                    nombre=capitalizar_nombre(datos.get("conyuge_nombre", "")),
                    apellido=capitalizar_nombre(datos.get("conyuge_apellido", "")),
                    celular=datos.get("conyuge_telefono_contacto"),
                    mail=conyuge_mail_limpio,
                    calle_y_nro=datos.get("calle_y_nro"),
                    depto_etc=datos.get("depto"),
                    barrio=datos.get("barrio"),
                    localidad=datos.get("localidad"),
                    provincia=(datos.get("provincia") or "")[:50],
                    active="Y", operativo="Y",
                    doc_adoptante_curso_aprobado="N",
                    doc_adoptante_ddjj_firmada="N",
                    fecha_alta=date.today()
                )
                db.add(nuevo_usuario_2)
                db.flush()

                if grupo_adoptante: 
                    db.add(UserGroup(login=conyuge_dni, group_id=grupo_adoptante.group_id))

                db.add(RuaEvento(login=conyuge_dni,
                                 evento_detalle=f"Usuario generado automáticamente (cónyuge) desde postulación a convocatoria ID {convocatoria_id}.",
                                 evento_fecha=datetime.now()))

            # DDJJ cónyuge si no existe
            if not db.query(DDJJ).filter(DDJJ.login == conyuge_dni).first():
                crear_ddjj_inicial(db, login=conyuge_dni, datos=datos_limpios, es_conyuge=True)
        # ========== FIN SECCIÓN 6 AJUSTADA ==========


        # --- 7) Proyecto y vínculos ---
        nuevo_proyecto = Proyecto(
            proyecto_tipo="Unión convivencial" if tiene_conyuge_para_proyecto else "Monoparental",
            login_1=dni, 
            login_2=conyuge_dni if tiene_conyuge_para_proyecto else None,
            proyecto_calle_y_nro=datos.get("calle_y_nro"), proyecto_depto_etc=datos.get("depto"),
            proyecto_barrio=datos.get("barrio"), proyecto_localidad=datos.get("localidad"),
            proyecto_provincia=(datos.get("provincia") or "")[:50],
            subregistro_flexible="Y", operativo="Y", aceptado="Y", aceptado_code=None,
            estado_general="aprobado", ingreso_por="convocatoria"
        )

        db.add(nuevo_proyecto)
        db.flush() 
        db.refresh(nuevo_proyecto)

        db.add(DetalleProyectoPostulacion(proyecto_id=nuevo_proyecto.proyecto_id,
                                          postulacion_id=nueva_postulacion.postulacion_id))
        db.add(RuaEvento(evento_detalle=f"Se creó una nueva postulación a convocatoria ID {convocatoria_id} "
                                        f"y el proyecto asociado (ID {nuevo_proyecto.proyecto_id}).",
                         evento_fecha=datetime.now(), login=dni))
        db.add(ProyectoHistorialEstado(proyecto_id=nuevo_proyecto.proyecto_id,
                                       estado_nuevo="aprobado", fecha_hora=datetime.now()))


        # Commit final de toda la operación
        db.commit()


        # ---- Envío de mails de confirmación ----
        try:
            # Mail al titular
            mail_titular = datos_limpios.get("mail")
            if mail_titular:
                asunto = f"Postulación recibida – Ref. {convocatoria.convocatoria_referencia or ''}".strip()
                cuerpo = _plantilla_mail_postulacion(
                    nombre=capitalizar_nombre(datos_limpios.get("nombre", "")) or "Postulante",
                    convocatoria=convocatoria
                )
                enviar_mail(mail_titular, asunto, cuerpo)

            # Mail al cónyuge (si corresponde)
            if tiene_conyuge_para_proyecto and datos_limpios.get("conyuge_mail"):
                asunto_c = f"Postulación recibida – Ref. {convocatoria.convocatoria_referencia or ''}".strip()
                cuerpo_c = _plantilla_mail_postulacion(
                    nombre=capitalizar_nombre(datos_limpios.get("conyuge_nombre", "")) or "Postulante",
                    convocatoria=convocatoria
                )
                enviar_mail(datos_limpios["conyuge_mail"], asunto_c, cuerpo_c)

        except Exception as e:
            # No bloquear si falla el mail → registrar evento
            db.add(RuaEvento(
                login=dni,
                evento_detalle=f"Error al enviar mails de confirmación: {str(e)[:400]}",
                evento_fecha=datetime.now()
            ))
            db.commit()
        # ----------------------------------------


        # --- 10) Notificación a supervisoras ---
        crear_notificacion_masiva_por_rol(
            db = db,
            rol = "supervisora",
            mensaje = f"La persona con DNI {dni} se postuló a convocatoria.",
            link = "/menu_supervisoras/detalleProyecto",
            data_json= { "proyecto_id": nuevo_proyecto.proyecto_id },
            tipo_mensaje = "naranja"
        )
        

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "<p>Postulación registrada con éxito.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual",
            "postulacion_id": nueva_postulacion.postulacion_id,
            "proyecto_id": nuevo_proyecto.proyecto_id
        }
       

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "<p>Error al registrar postulación.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }




@convocatoria_router.put("/{convocatoria_id}/online", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def actualizar_online(convocatoria_id: int, data: dict = Body(...), db: Session = Depends(get_db)):
    try:
        estado = data.get("convocatoria_online")
        convocatoria = db.query(Convocatoria).filter_by(convocatoria_id=convocatoria_id).first()
        if not convocatoria:
            raise HTTPException(status_code=404, detail="Convocatoria no encontrada")
        convocatoria.convocatoria_online = estado
        db.commit()
        return {"success": True, "message": f"Estado actualizado a {estado}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@convocatoria_router.get("/para-select/para-filtro", response_model=List[dict], 
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_convocatorias_para_filtro(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100)
):
    try:
        offset = (page - 1) * limit

        # Subconsulta con cantidad de postulaciones por convocatoria
        subq = db.query(
            Postulacion.convocatoria_id,
            func.count(Postulacion.postulacion_id).label("total_postulantes")
        ).group_by(Postulacion.convocatoria_id).subquery()

        # Traer convocatorias y unir con subconsulta
        results = db.query(
            Convocatoria,
            func.coalesce(subq.c.total_postulantes, 0).label("total_postulantes")
        ).outerjoin(subq, Convocatoria.convocatoria_id == subq.c.convocatoria_id)\
         .order_by(Convocatoria.convocatoria_fecha_publicacion.desc())\
         .offset(offset)\
         .limit(limit)\
         .all()

        return [{
            "value": c.convocatoria_id,
            "label": f"{c.convocatoria_referencia} - {c.convocatoria_llamado} \
                ({c.convocatoria_fecha_publicacion}) - Postulantes: {total_postulantes} "
        } for c, total_postulantes in results]

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener convocatorias para filtro: {str(e)}")



@convocatoria_router.get("/timeline/{convocatoria_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def obtener_timeline_convocatoria(convocatoria_id: int, db: Session = Depends(get_db)):
    """
    📅 Devuelve una línea de tiempo de una convocatoria:
    - Fecha de publicación de la convocatoria.
    - Fechas de cada postulación recibida.
    """
    try:
        convocatoria = db.query(Convocatoria).filter(
            Convocatoria.convocatoria_id == convocatoria_id
        ).first()

        if not convocatoria:
            raise HTTPException(status_code=404, detail="Convocatoria no encontrada.")

        timeline = []

        # 📌 Evento de publicación
        if convocatoria.convocatoria_fecha_publicacion:
            timeline.append({
                "fecha": convocatoria.convocatoria_fecha_publicacion,
                "evento": f"📢 Publicación de convocatoria: {convocatoria.convocatoria_referencia}"
            })

        # 📝 Postulaciones
        postulaciones = db.query(Postulacion).filter(
            Postulacion.convocatoria_id == convocatoria_id
        ).order_by(Postulacion.fecha_postulacion).all()

        for p in postulaciones:
            nombre_completo = f"{p.nombre} {p.apellido}".strip()
            timeline.append({
                "fecha": p.fecha_postulacion,
                "evento": f"📝 Postulación recibida de {nombre_completo} (DNI {p.dni})"
            })

        # Ordenar cronológicamente
        timeline.sort(
            key=lambda x: datetime.combine(x["fecha"], datetime.min.time()) if isinstance(x["fecha"], date) else x["fecha"]
        )

        # Formatear fechas a string YYYY-MM-DD
        for item in timeline:
            fecha = item["fecha"]
            if isinstance(fecha, (datetime, date)):
                item["fecha"] = fecha.strftime("%Y-%m-%d")

            # Limpiar HTML si hay
            item["evento"] = re.sub(r"<[^>]*?>", "", item["evento"]).strip()

        return {
            "success": True,
            "timeline": timeline
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al generar la línea de tiempo: {str(e)}")
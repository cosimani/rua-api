from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload, aliased
from sqlalchemy.exc import SQLAlchemyError

from sqlalchemy import func, or_
import re

from database.config import get_db
from security.security import get_current_user, verify_api_key, require_roles

from helpers.utils import normalizar_y_validar_dni, verificar_recaptcha, validar_correo, \
  capitalizar_nombre, enviar_mail

from datetime import datetime
from models.convocatorias import Postulacion, Convocatoria, DetalleProyectoPostulacion, DetalleNNAEnConvocatoria  
from models.eventos_y_configs import RuaEvento
from models.users import User, Group, UserGroup 
from models.ddjj import DDJJ
from models.proyecto import Proyecto, ProyectoHistorialEstado
from models.nna import Nna
from sqlalchemy.orm.exc import NoResultFound
from datetime import date, datetime
from math import ceil
from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol



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
    limit: int = Query(50, ge=1, le=100)
):
    try:
        query = db.query(Convocatoria).filter(Convocatoria.convocatoria_online == "Y")

        total_records = query.count()
        total_pages = ceil(total_records / limit)

        convocatorias = query.order_by(
            Convocatoria.convocatoria_fecha_publicacion.desc(),
            Convocatoria.convocatoria_referencia.desc()
        ).offset((page - 1) * limit).limit(limit).all()

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
            for c in convocatorias
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



@convocatoria_router.post("/postulacion", response_model = dict, dependencies = [Depends(verify_api_key)])
async def crear_postulacion( datos: dict = Body(...), db: Session = Depends(get_db), ):
    """
    📝 Da de alta una nueva postulación a convocatoria y crea automáticamente, el usuario principal, su ddjj y el proyecto.

    """

    try:
        
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
        

        # Diccionario de nombres amigables
        nombres_amigables = {
            "nombre": "Nombre",
            "apellido": "Apellido",
            "dni": "DNI",
            "fecha_nacimiento": "Fecha de nacimiento",
            "nacionalidad": "Nacionalidad",
            "estado_civil": "Estado civil",
            "calle_y_nro": "Calle y número",
            "localidad": "Localidad",
            "provincia": "Provincia",
            "telefono_contacto": "Teléfono de contacto",
            "mail": "Correo electrónico",
            "ocupacion": "Ocupación / profesión",
            "conyuge_nombre": "Nombre del/la conviviente",
            "conyuge_apellido": "Apellido del/la conviviente",
            "conyuge_dni": "DNI del/la conviviente",
            "conyuge_fecha_nacimiento": "Fecha de nacimiento del/la conviviente",
            "conyuge_telefono_contacto": "Teléfono del/la conviviente",
        }

        def _vacio(v):
            return (v is None) or (isinstance(v, str) and v.strip() == "")

        # Obligatorios del titular (ajustá la lista si querés sumar/quitar)
        req_titular = [
            "nombre", "apellido", "dni", "fecha_nacimiento",
            "mail", "telefono_contacto", "localidad", "provincia"
        ]

        # Si hay convivencia, obligatorios del/la cónyuge
        req_conyuge = []
        if datos.get("conyuge_convive") == "Y":
            req_conyuge = [
                "conyuge_nombre", "conyuge_apellido", "conyuge_dni",
                "conyuge_fecha_nacimiento", "conyuge_telefono_contacto"
                # si querés hacer obligatorio el mail del/la cónyuge, agregá: "conyuge_mail"
            ]

        campos_faltantes = [c for c in (req_titular + req_conyuge) if _vacio(datos.get(c))]

        if campos_faltantes:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": (
                    "<p>Faltan completar los siguientes campos obligatorios:</p><ul>"
                    + "".join(f"<li>{nombres_amigables.get(c, c.replace('_',' ').capitalize())}</li>" for c in campos_faltantes)
                    + "</ul>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual",
            }



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
        if not validar_correo(datos.get("mail", "").lower()):
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

        # ───── 2. Un solo query: proyectos “no viable” que involucren a alguno ─────
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
        involucra_titular  = any(p.login_1 == dni or p.login_2 == dni          for p in proyectos_no_viables)
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


        # ─── Verificar si ya hay una postulación del mismo DNI a esta convocatoria ───
        postulacion_existente = (
            db.query(Postulacion)
            .filter(
                Postulacion.convocatoria_id == convocatoria_id,
                Postulacion.dni == dni
            )
            .first()
        )
        if postulacion_existente:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": (
                    f"<p>Ya existe una postulación con tu DNI para esta "
                    f"convocatoria. No es posible registrar otra.</p>"
                ),
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # ─── Validar que el/la cónyuge no se haya postulado ya a esta convocatoria ───
        if tiene_conyuge:
            postulacion_conyuge_existente = (
                db.query(Postulacion)
                .filter(
                    Postulacion.convocatoria_id == convocatoria_id,
                    Postulacion.dni == conyuge_dni
                )
                .first()
            )
            
            if postulacion_conyuge_existente:
                return {
                    "success": False,
                    "tipo_mensaje": "amarillo",
                    "mensaje": (
                        f"<p>La persona indicada como pareja "
                        f"ya se ha postulado a esta convocatoria. No es posible "
                        f"registrar la postulación.</p>"
                    ),
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }


        #####################################


        # Campos que acepta el modelo Postulacion (coinciden con tus columnas)
        CAMPOS_POSTULACION = [
            # básicos
            "convocatoria_id",
            "nombre", "apellido", "dni", "fecha_nacimiento", "nacionalidad",
            "sexo", "estado_civil", "calle_y_nro", "depto", "barrio",
            "localidad", "cp", "provincia",
            "telefono_contacto", "telefono_fijo", "videollamada", "whatsapp",
            "mail", "movilidad_propia", "obra_social", "ocupacion",
            # cónyuge
            "conyuge_convive", "conyuge_nombre", "conyuge_apellido",
            "conyuge_dni", "conyuge_edad", "conyuge_fecha_nacimiento",
            "conyuge_telefono_contacto", "conyuge_telefono_fijo",
            "conyuge_mail", "conyuge_ocupacion", "conyuge_otros_datos",
            # otros
            "hijos", "acogimiento_es", "acogimiento_descripcion",
            "en_rua", "subregistro_comentarios", "terminaste_inscripcion_rua",
            "otra_convocatoria", "otra_convocatoria_comentarios",
            "antecedentes", "antecedentes_comentarios",
            "como_tomaron_conocimiento", "motivos", "comunicaron_decision",
            "otros_comentarios", "inscripto_en_rua",
            # subregistros
            "subreg_3", "subreg_4",
            "subreg_5A1ET", "subreg_5A2ET",
            "subreg_5B1ET", "subreg_5B2ET", "subreg_5B3ET",
            "subreg_61ET", "subreg_62ET", "subreg_63ET",
        ]

        # Asegurá normalizaciones previas (dni, conyuge_dni, capitalizaciones, lower de mail, etc.)
        datos_limpios = {**datos}
        datos_limpios["convocatoria_id"] = convocatoria_id
        datos_limpios["dni"] = dni
        if tiene_conyuge and conyuge_dni:
            datos_limpios["conyuge_dni"] = conyuge_dni

        # Normalizaciones “cosméticas” (opcional)
        if datos_limpios.get("nombre"):
            datos_limpios["nombre"] = capitalizar_nombre(datos_limpios["nombre"])
        if datos_limpios.get("apellido"):
            datos_limpios["apellido"] = capitalizar_nombre(datos_limpios["apellido"])
        if datos_limpios.get("conyuge_nombre"):
            datos_limpios["conyuge_nombre"] = capitalizar_nombre(datos_limpios["conyuge_nombre"])
        if datos_limpios.get("conyuge_apellido"):
            datos_limpios["conyuge_apellido"] = capitalizar_nombre(datos_limpios["conyuge_apellido"])
        if datos_limpios.get("mail"):
            datos_limpios["mail"] = datos_limpios["mail"].lower()

        # 👉 conyuge_mail (si viene)
        if datos_limpios.get("conyuge_mail"):
            datos_limpios["conyuge_mail"] = datos_limpios["conyuge_mail"].lower()
            # Validar formato, pero sin bloquear: si es inválido, lo descartamos
            if not validar_correo(datos_limpios["conyuge_mail"]):
                datos_limpios["conyuge_mail"] = None

        # Filtrar solo claves válidas para el modelo
        payload_postulacion = {k: datos_limpios.get(k) for k in CAMPOS_POSTULACION}

        nueva_postulacion = Postulacion(**payload_postulacion)

        db.add(nueva_postulacion)
        db.flush()
        db.refresh(nueva_postulacion)


        grupo_adoptante = db.query(Group).filter(Group.description == "adoptante").first()


        # Alta usuario login_1 (postulante)
        usuario_1 = db.query(User).filter(User.login == dni).first()
        if not usuario_1:
            mail_1 = datos.get("mail")
            if db.query(User).filter(User.mail == mail_1).first():
                mail_1 = None
            nuevo_usuario_1 = User(
                login = dni,
                nombre = capitalizar_nombre(datos.get("nombre", "")),  # datos.get("nombre"),
                apellido = capitalizar_nombre(datos.get("apellido", "")),  # datos.get("apellido"),
                celular = datos.get("telefono_contacto"),
                mail = mail_1,
                profesion = datos.get("ocupacion", "")[:60],
                calle_y_nro = datos.get("calle_y_nro"),
                depto_etc = datos.get("depto"),
                barrio = datos.get("barrio"),
                localidad = datos.get("localidad"),
                provincia = datos.get("provincia", "")[:50],
                active = "Y",
                operativo = "Y",
                doc_adoptante_curso_aprobado = "Y",
                doc_adoptante_ddjj_firmada = 'Y',
                fecha_alta = date.today()
            )
            db.add(nuevo_usuario_1)
            db.flush()   # <-- forzamos la inserción del usuario antes de crear eventos


            if grupo_adoptante:
                db.add(UserGroup(login = dni, group_id = grupo_adoptante.group_id))

            
            # # crear DDJJ del titular
            # if not db.query(DDJJ).filter(DDJJ.login == dni).first():
            #     crear_ddjj_inicial(db, login=dni, datos=datos_limpios, es_conyuge=False)


            db.add(RuaEvento(
                login = dni,
                evento_detalle = f"Usuario y su DDJJ generado automáticamente desde postulación a convocatoria ID {convocatoria_id}.",
                evento_fecha = datetime.now()
            ))

        # asegurar DDJJ del titular, exista o no el usuario
        if not db.query(DDJJ).filter(DDJJ.login == dni).first():
            crear_ddjj_inicial(db, login=dni, datos=datos_limpios, es_conyuge=False)

        # Alta usuario login_2 (conyuge)
        # tiene_conyuge = datos.get("conyuge_dni") and datos.get("conyuge_convive") == "Y"

        tiene_conyuge_para_proyecto = bool(conyuge_dni and datos.get("conyuge_convive") == "Y")

        if tiene_conyuge_para_proyecto:

            usuario_2 = db.query(User).filter(User.login == conyuge_dni).first()
            if not usuario_2:
                # Mail del cónyuge si vino y no está usado por otra persona
                mail_2 = datos_limpios.get("conyuge_mail")
                if mail_2 and db.query(User).filter(User.mail == mail_2).first():
                    mail_2 = None

                nuevo_usuario_2 = User(
                    login = conyuge_dni,
                    nombre = capitalizar_nombre(datos.get("conyuge_nombre", "")),
                    apellido = capitalizar_nombre(datos.get("conyuge_apellido", "")),
                    celular = datos.get("conyuge_telefono_contacto"),   # 👈 nuevo
                    mail = mail_2,                                      # 👈 nuevo
                    calle_y_nro = datos.get("calle_y_nro"),
                    depto_etc = datos.get("depto"),
                    barrio = datos.get("barrio"),
                    localidad = datos.get("localidad"),
                    provincia = datos.get("provincia", "")[:50],
                    active = "Y",
                    operativo = "Y",
                    doc_adoptante_curso_aprobado = "Y",
                    doc_adoptante_ddjj_firmada = 'Y',
                    fecha_alta = date.today()
                )
                db.add(nuevo_usuario_2)
                db.flush()

                if grupo_adoptante:
                    db.add(UserGroup(login = conyuge_dni, group_id = grupo_adoptante.group_id))

                # if not db.query(DDJJ).filter(DDJJ.login == conyuge_dni).first():
                #     crear_ddjj_inicial(db, login=conyuge_dni, datos=datos_limpios, es_conyuge=True)

                db.add(RuaEvento(
                    login = conyuge_dni,
                    evento_detalle = f"Usuario y su DDJJ generado automáticamente desde postulación (cónyuge) a convocatoria ID {convocatoria_id}.",
                    evento_fecha = datetime.now()
                ))

            # asegurar DDJJ del/la cónyuge, exista o no el usuario
            if not db.query(DDJJ).filter(DDJJ.login == conyuge_dni).first():
                crear_ddjj_inicial(db, login=conyuge_dni, datos=datos_limpios, es_conyuge=True)



        # Preparar campos comunes
        nuevo_proyecto = Proyecto(
            proyecto_tipo = "Unión convivencial" if tiene_conyuge_para_proyecto else "Monoparental",
            login_1 = dni,
            login_2 = conyuge_dni if tiene_conyuge_para_proyecto else None,
            proyecto_calle_y_nro = datos.get("calle_y_nro"),
            proyecto_depto_etc = datos.get("depto"),
            proyecto_barrio = datos.get("barrio"),
            proyecto_localidad = datos.get("localidad"),
            proyecto_provincia = datos.get("provincia", "")[:50],
            subregistro_flexible = "Y",
            operativo = "Y",
            aceptado = "Y",
            aceptado_code = None,
            estado_general = "aprobado",
            ingreso_por = "convocatoria"
        )

        db.add(nuevo_proyecto)
        db.flush()  # <-- Esto fuerza la inserción en la base de datos
        db.refresh(nuevo_proyecto)

        # Registrar la vinculación entre postulación y proyecto
        detalle_vinculo = DetalleProyectoPostulacion(
            proyecto_id = nuevo_proyecto.proyecto_id,
            postulacion_id = nueva_postulacion.postulacion_id
        )

        db.add(detalle_vinculo)


        # Registrar evento en rua_evento
        evento = RuaEvento(
            evento_detalle = f"Se creó una nueva postulación a convocatoria ID {convocatoria_id} y el proyecto asociado (ID {nuevo_proyecto.proyecto_id}).",
            evento_fecha = datetime.now(),
            login = dni
        )
        db.add(evento)


        # 🕓 Registrar en historial de estados
        db.add(ProyectoHistorialEstado(
            proyecto_id = nuevo_proyecto.proyecto_id,
            estado_nuevo = "aprobado",
            fecha_hora = datetime.now()
        ))

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
            conyuge_mail = datos_limpios.get("conyuge_mail")
            if tiene_conyuge_para_proyecto and conyuge_mail:
                asunto_c = f"Postulación recibida – Ref. {convocatoria.convocatoria_referencia or ''}".strip()
                cuerpo_c = _plantilla_mail_postulacion(
                    nombre=capitalizar_nombre(datos_limpios.get("conyuge_nombre", "")) or "Postulante",
                    convocatoria=convocatoria
                )
                enviar_mail(conyuge_mail, asunto_c, cuerpo_c)

        except Exception as e:
            # No bloquear si falla el mail → registrar evento
            db.add(RuaEvento(
                login=dni,
                evento_detalle=f"Error al enviar mails de confirmación: {str(e)[:400]}",
                evento_fecha=datetime.now()
            ))
            db.commit()
        # ----------------------------------------



        # Enviar notificación a todas las supervisoras
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
            "tipo_mensaje": "verde",
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
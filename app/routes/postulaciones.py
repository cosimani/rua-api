from fastapi import APIRouter, HTTPException, Depends, Query, Request, status, Body, UploadFile, File, Form
from typing import List, Optional, Literal, Tuple
from sqlalchemy.orm import Session, aliased, joinedload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, case, and_, or_, Integer, literal_column
import json
from sqlalchemy import or_, and_

from datetime import datetime, date
from models.proyecto import Proyecto, ProyectoHistorialEstado, DetalleEquipoEnProyecto, AgendaEntrevistas, FechaRevision
from models.carpeta import Carpeta, DetalleProyectosEnCarpeta, DetalleNNAEnCarpeta
from models.notif_y_observaciones import ObservacionesProyectos, ObservacionesPretensos, NotificacionesRUA
from models.convocatorias import DetalleProyectoPostulacion
from models.ddjj import DDJJ
from models.nna import Nna


from models.convocatorias import Postulacion
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
from math import ceil


from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol, crear_notificacion_individual




postulaciones_router = APIRouter()


@postulaciones_router.get("/postulaciones", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def get_postulaciones(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None),
    convocatoria_id: Optional[int] = Query(None)

):
    try:
        # query = db.query(Postulacion).order_by(Postulacion.fecha_postulacion.desc())
        query = db.query(Postulacion).options(joinedload(Postulacion.convocatoria)).order_by(Postulacion.fecha_postulacion.desc())

        if search :
            palabras = search.strip().split()
            condiciones = []

            for palabra in palabras:
                like = f"%{palabra}%"
                condiciones.append(
                    or_(
                        Postulacion.nombre.ilike(like),
                        Postulacion.apellido.ilike(like),
                        Postulacion.dni.ilike(like),
                        Postulacion.calle_y_nro.ilike(like),
                        Postulacion.barrio.ilike(like),
                        Postulacion.localidad.ilike(like),
                        Postulacion.provincia.ilike(like),
                        Postulacion.mail.ilike(like),
                        Postulacion.ocupacion.ilike(like),
                        Postulacion.conyuge_nombre.ilike(like),
                        Postulacion.conyuge_apellido.ilike(like),
                        Postulacion.conyuge_dni.ilike(like),
                        Postulacion.conyuge_otros_datos.ilike(like),
                    )
                )

            query = query.filter(and_(*condiciones))  # todas las palabras deben coincidir en al menos un campo

        if convocatoria_id:
            query = query.filter(Postulacion.convocatoria_id == convocatoria_id)


        total_records = query.count()
        total_pages = ceil(total_records / limit)
        postulaciones = query.offset((page - 1) * limit).limit(limit).all()

        datos = [{
            "postulacion_id": p.postulacion_id,
            "convocatoria_id": p.convocatoria_id,
            "convocatoria": {
                "convocatoria_referencia": p.convocatoria.convocatoria_referencia if p.convocatoria else None,
                "convocatoria_llamado": p.convocatoria.convocatoria_llamado if p.convocatoria else None,
            } if p.convocatoria else None,
            "fecha_postulacion": p.fecha_postulacion,
            "nombre": p.nombre,
            "apellido": p.apellido,
            "dni": p.dni,
            "localidad": p.localidad,
            "provincia": p.provincia,
            "mail": p.mail,
            "telefono_contacto": p.telefono_contacto,
            "conyuge_convive": p.conyuge_convive,
        } for p in postulaciones]

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "postulaciones": datos
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar postulaciones: {str(e)}")





@postulaciones_router.get("/postulaciones/{postulacion_id}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def get_postulacion(postulacion_id: int, db: Session = Depends(get_db)):
    try:
        # postulacion = db.query(Postulacion).filter(Postulacion.postulacion_id == postulacion_id).first()
        postulacion = db.query(Postulacion)\
            .options(joinedload(Postulacion.convocatoria))\
            .filter(Postulacion.postulacion_id == postulacion_id)\
            .first()
        
        if not postulacion:
            raise HTTPException(status_code=404, detail="Postulación no encontrada")

        # Detalle simple del proyecto vinculado (opcional)
        proyecto = None
        if postulacion.detalle_proyecto:
            proyecto = {
                "proyecto_id": postulacion.detalle_proyecto[0].proyecto_id
            }

        return {
            "postulacion_id": postulacion.postulacion_id,
            "convocatoria_id": postulacion.convocatoria_id,
            "convocatoria": {
                "convocatoria_referencia": postulacion.convocatoria.convocatoria_referencia if postulacion.convocatoria else None,
                "convocatoria_llamado": postulacion.convocatoria.convocatoria_llamado if postulacion.convocatoria else None,
            } if postulacion.convocatoria else None,
            "fecha_postulacion": postulacion.fecha_postulacion,
            "nombre": postulacion.nombre,
            "apellido": postulacion.apellido,
            "dni": postulacion.dni,
            "fecha_nacimiento": postulacion.fecha_nacimiento,
            "nacionalidad": postulacion.nacionalidad,
            "sexo": postulacion.sexo,
            "estado_civil": postulacion.estado_civil,
            "calle_y_nro": postulacion.calle_y_nro,
            "depto": postulacion.depto,
            "barrio": postulacion.barrio,
            "localidad": postulacion.localidad,
            "cp": postulacion.cp,
            "provincia": postulacion.provincia,
            "telefono_contacto": postulacion.telefono_contacto,
            "videollamada": postulacion.videollamada,
            "mail": postulacion.mail,
            "movilidad_propia": postulacion.movilidad_propia,
            "obra_social": postulacion.obra_social,
            "ocupacion": postulacion.ocupacion,
            "conyuge_convive": postulacion.conyuge_convive,
            "conyuge_nombre": postulacion.conyuge_nombre,
            "conyuge_apellido": postulacion.conyuge_apellido,
            "conyuge_dni": postulacion.conyuge_dni,
            "conyuge_edad": postulacion.conyuge_edad,
            "conyuge_otros_datos": postulacion.conyuge_otros_datos,
            "hijos": postulacion.hijos,
            "acogimiento_es": postulacion.acogimiento_es,
            "acogimiento_descripcion": postulacion.acogimiento_descripcion,
            "en_rua": postulacion.en_rua,
            "subregistro_comentarios": postulacion.subregistro_comentarios,
            "otra_convocatoria": postulacion.otra_convocatoria,
            "otra_convocatoria_comentarios": postulacion.otra_convocatoria_comentarios,
            "antecedentes": postulacion.antecedentes,
            "antecedentes_comentarios": postulacion.antecedentes_comentarios,
            "como_tomaron_conocimiento": postulacion.como_tomaron_conocimiento,
            "motivos": postulacion.motivos,
            "comunicaron_decision": postulacion.comunicaron_decision,
            "otros_comentarios": postulacion.otros_comentarios,
            "inscripto_en_rua": postulacion.inscripto_en_rua,
            "proyecto": proyecto
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar la postulación: {str(e)}")

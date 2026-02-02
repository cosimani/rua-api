from fastapi import APIRouter, HTTPException, Depends, Query, Request, status, Body, UploadFile, File, Form
from typing import List, Optional, Literal, Tuple
from sqlalchemy.orm import Session, aliased, joinedload, selectinload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, case, and_, or_, Integer, literal_column
import json
from sqlalchemy import or_, and_


from datetime import datetime, date
from models.proyecto import Proyecto, ProyectoHistorialEstado, DetalleEquipoEnProyecto, AgendaEntrevistas, FechaRevision
from models.carpeta import Carpeta, DetalleProyectosEnCarpeta, DetalleNNAEnCarpeta
from models.notif_y_observaciones import ObservacionesProyectos, ObservacionesPretensos, NotificacionesRUA
from models.convocatorias import DetalleProyectoPostulacion, DetalleNNAEnConvocatoria
from models.ddjj import DDJJ
from models.nna import Nna


from models.convocatorias import Postulacion, Convocatoria, DetalleNNAEnConvocatoria
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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_postulaciones(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None),
    convocatoria_id: Optional[int] = Query(None),
    fecha_postulacion_inicio: Optional[str] = Query(None),
    fecha_postulacion_fin: Optional[str] = Query(None),
    estado_entrevistas: Optional[Literal["calendarizando", "entrevistando", "con_agenda", "sin_agenda"]] = Query(None),
    subregistros: Optional[List[str]] = Query(None, alias="subregistro_portada")

):
    try:


        # query = db.query(Postulacion).order_by(Postulacion.fecha_postulacion.desc())
        # query = db.query(Postulacion).options(joinedload(Postulacion.convocatoria)).order_by(Postulacion.fecha_postulacion.desc())

        # query = db.query(Postulacion).options(
        #     joinedload(Postulacion.convocatoria)
        #     .joinedload(Convocatoria.detalle_nnas)
        #     .joinedload(DetalleNNAEnConvocatoria.nna)
        # )

        DDJJTitular = aliased(DDJJ)
        DDJJConyuge = aliased(DDJJ)

        query = (
            db.query(Postulacion)
              .options(
                  joinedload(Postulacion.convocatoria)
                  .joinedload(Convocatoria.detalle_nnas)
                  .joinedload(DetalleNNAEnConvocatoria.nna)
              )
              .outerjoin(DDJJTitular, DDJJTitular.login == Postulacion.dni)
              .outerjoin(DDJJConyuge, DDJJConyuge.login == Postulacion.conyuge_dni)
              .order_by(Postulacion.fecha_postulacion.desc())
        )


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

        # Filtro por rango de fechas de postulación (YYYY-MM-DD)
        if fecha_postulacion_inicio or fecha_postulacion_fin:
            try:
                fecha_inicio_dt = datetime.strptime(fecha_postulacion_inicio, "%Y-%m-%d") if fecha_postulacion_inicio else None
                fecha_fin_dt = datetime.strptime(fecha_postulacion_fin, "%Y-%m-%d") if fecha_postulacion_fin else None
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD.")

            if fecha_inicio_dt and fecha_fin_dt:
                fecha_fin_dt = datetime.combine(fecha_fin_dt.date(), datetime.max.time())
                query = query.filter(Postulacion.fecha_postulacion.between(fecha_inicio_dt, fecha_fin_dt))
            elif fecha_inicio_dt:
                query = query.filter(Postulacion.fecha_postulacion >= fecha_inicio_dt)
            elif fecha_fin_dt:
                fecha_fin_dt = datetime.combine(fecha_fin_dt.date(), datetime.max.time())
                query = query.filter(Postulacion.fecha_postulacion <= fecha_fin_dt)

        # Filtro por estado de entrevistas
        if estado_entrevistas:
            query = query.join(
                DetalleProyectoPostulacion,
                DetalleProyectoPostulacion.postulacion_id == Postulacion.postulacion_id
            )

            if estado_entrevistas in ["calendarizando", "entrevistando"]:
                query = query.join(
                    Proyecto,
                    Proyecto.proyecto_id == DetalleProyectoPostulacion.proyecto_id
                ).filter(Proyecto.estado_general == estado_entrevistas)
            else:
                agenda_subq = (
                    db.query(
                        AgendaEntrevistas.proyecto_id.label("proyecto_id"),
                        func.count(AgendaEntrevistas.id).label("cnt")
                    )
                    .group_by(AgendaEntrevistas.proyecto_id)
                    .subquery()
                )

                query = (
                    query.join(Proyecto, Proyecto.proyecto_id == DetalleProyectoPostulacion.proyecto_id)
                         .outerjoin(agenda_subq, agenda_subq.c.proyecto_id == Proyecto.proyecto_id)
                )

                if estado_entrevistas == "con_agenda":
                    query = query.filter(agenda_subq.c.cnt != None, agenda_subq.c.cnt > 0)
                elif estado_entrevistas == "sin_agenda":
                    query = query.filter(or_(agenda_subq.c.cnt == None, agenda_subq.c.cnt == 0))

            query = query.distinct(Postulacion.postulacion_id)

        def build_subregistro_field_map(ddjj_alias):
            return {
                "1": ddjj_alias.subreg_1,
                "2": ddjj_alias.subreg_2,
                "3": ddjj_alias.subreg_3,
                "4": ddjj_alias.subreg_4,
                "FE1": ddjj_alias.subreg_FE1,
                "FE2": ddjj_alias.subreg_FE2,
                "FE3": ddjj_alias.subreg_FE3,
                "FE4": ddjj_alias.subreg_FE4,
                "FET": ddjj_alias.subreg_FET,
                "5A1E1": ddjj_alias.subreg_5A1E1,
                "5A1E2": ddjj_alias.subreg_5A1E2,
                "5A1E3": ddjj_alias.subreg_5A1E3,
                "5A1E4": ddjj_alias.subreg_5A1E4,
                "5A1ET": ddjj_alias.subreg_5A1ET,
                "5A2E1": ddjj_alias.subreg_5A2E1,
                "5A2E2": ddjj_alias.subreg_5A2E2,
                "5A2E3": ddjj_alias.subreg_5A2E3,
                "5A2E4": ddjj_alias.subreg_5A2E4,
                "5A2ET": ddjj_alias.subreg_5A2ET,
                "5B1E1": ddjj_alias.subreg_5B1E1,
                "5B1E2": ddjj_alias.subreg_5B1E2,
                "5B1E3": ddjj_alias.subreg_5B1E3,
                "5B1E4": ddjj_alias.subreg_5B1E4,
                "5B1ET": ddjj_alias.subreg_5B1ET,
                "5B2E1": ddjj_alias.subreg_5B2E1,
                "5B2E2": ddjj_alias.subreg_5B2E2,
                "5B2E3": ddjj_alias.subreg_5B2E3,
                "5B2E4": ddjj_alias.subreg_5B2E4,
                "5B2ET": ddjj_alias.subreg_5B2ET,
                "5B3E1": ddjj_alias.subreg_5B3E1,
                "5B3E2": ddjj_alias.subreg_5B3E2,
                "5B3E3": ddjj_alias.subreg_5B3E3,
                "5B3E4": ddjj_alias.subreg_5B3E4,
                "5B3ET": ddjj_alias.subreg_5B3ET,
                "F5S": ddjj_alias.subreg_F5S,
                "F5E1": ddjj_alias.subreg_F5E1,
                "F5E2": ddjj_alias.subreg_F5E2,
                "F5E3": ddjj_alias.subreg_F5E3,
                "F5E4": ddjj_alias.subreg_F5E4,
                "F5ET": ddjj_alias.subreg_F5ET,
                "61E1": ddjj_alias.subreg_61E1,
                "61E2": ddjj_alias.subreg_61E2,
                "61E3": ddjj_alias.subreg_61E3,
                "61ET": ddjj_alias.subreg_61ET,
                "62E1": ddjj_alias.subreg_62E1,
                "62E2": ddjj_alias.subreg_62E2,
                "62E3": ddjj_alias.subreg_62E3,
                "62ET": ddjj_alias.subreg_62ET,
                "63E1": ddjj_alias.subreg_63E1,
                "63E2": ddjj_alias.subreg_63E2,
                "63E3": ddjj_alias.subreg_63E3,
                "63ET": ddjj_alias.subreg_63ET,
                "FQ1": ddjj_alias.subreg_FQ1,
                "FQ2": ddjj_alias.subreg_FQ2,
                "FQ3": ddjj_alias.subreg_FQ3,
                "F6E1": ddjj_alias.subreg_F6E1,
                "F6E2": ddjj_alias.subreg_F6E2,
                "F6E3": ddjj_alias.subreg_F6E3,
                "F6ET": ddjj_alias.subreg_F6ET,
            }

        if not subregistros:
            subregistros = request.query_params.getlist("subregistro_portada[]")

        if subregistros:
            subregistros = list(set(subregistros))

            tags_padres = {
                "FE": ["FE1", "FE2", "FE3", "FE4", "FET"],
                "5A": ["5A1E1", "5A1E2", "5A1E3", "5A1E4", "5A1ET", "5A2E1", "5A2E2", "5A2E3", "5A2E4", "5A2ET"],
                "5B": ["5B1E1", "5B1E2", "5B1E3", "5B1E4", "5B1ET", "5B2E1", "5B2E2", "5B2E3", "5B2E4", "5B2ET", "5B3E1", "5B3E2", "5B3E3", "5B3E4", "5B3ET"],
                "F5": ["F5S", "F5E1", "F5E2", "F5E3", "F5E4", "F5ET"],
                "6": ["61E1", "61E2", "61E3", "61ET", "62E1", "62E2", "62E3", "62ET", "63E1", "63E2", "63E3", "63ET"],
                "F6": ["F6E1", "F6E2", "F6E3", "F6ET"],
            }

            tags_excluidos = set()
            for padre, hijos in tags_padres.items():
                if padre in subregistros and any(hijo in subregistros for hijo in hijos):
                    tags_excluidos.add(padre)

            subregistros_filtrados = [sr for sr in subregistros if sr not in tags_excluidos]

            def build_condiciones(ddjj_alias):
                field_map = build_subregistro_field_map(ddjj_alias)
                condiciones_locales = [ddjj_alias.ddjj_id != None]

                for sr in subregistros_filtrados:
                    if sr in tags_padres:
                        grupo_or = [
                            field_map[subtag] == "Y"
                            for subtag in tags_padres[sr]
                            if subtag in field_map
                        ]
                        if grupo_or:
                            condiciones_locales.append(or_(*grupo_or))
                    else:
                        field = field_map.get(sr)
                        if field is not None:
                            condiciones_locales.append(field == "Y")

                return condiciones_locales

            condiciones_titular = build_condiciones(DDJJTitular)
            condiciones_conyuge = build_condiciones(DDJJConyuge)

            condiciones_or = []
            if condiciones_titular:
                condiciones_or.append(and_(*condiciones_titular))
            if condiciones_conyuge:
                condiciones_or.append(and_(*condiciones_conyuge))

            if condiciones_or:
                query = query.filter(or_(*condiciones_or))


        total_records = query.count()
        total_pages = ceil(total_records / limit)
        postulaciones = query.offset((page - 1) * limit).limit(limit).all()

        # nna_asociados = [
        #     {
        #         "nna_id": det.nna_id,
        #         "nna_nombre": det.nna.nna_nombre,
        #         "nna_apellido": det.nna.nna_apellido,
        #     }
        #     for det in p.convocatoria.detalle_nnas
        #     if det.nna is not None
        # ]

        

        datos = []
        for p in postulaciones:
            logins = [p.dni]
            if p.conyuge_dni:
                logins.append(p.conyuge_dni)

            tiene_proyecto_rua = (
                db.query(Proyecto.proyecto_id)
                .filter(
                    Proyecto.ingreso_por == "rua",
                    or_(Proyecto.login_1.in_(logins), Proyecto.login_2.in_(logins))
                )
                .first()
                is not None
            )

            tiene_otra_convocatoria = (
                db.query(Postulacion.postulacion_id)
                .filter(
                    Postulacion.postulacion_id != p.postulacion_id,
                    or_(Postulacion.dni.in_(logins), Postulacion.conyuge_dni.in_(logins))
                )
                .first()
                is not None
            )

            # si la postulación tiene convocatoria, armo la lista
            if p.convocatoria:
                # nna_asociados = [
                #     {
                #         "nna_id": det.nna_id,
                #         "nna_nombre": det.nna.nna_nombre,
                #         "nna_apellido": det.nna.nna_apellido,
                #     }
                #     for det in p.convocatoria.detalle_nnas
                #     if det.nna is not None
                # ]
                nna_asociados = [
                    {
                        "nna_id": det.nna_id,
                        "nombre_completo": f"{det.nna.nna_nombre} {det.nna.nna_apellido}",
                    }
                    for det in p.convocatoria.detalle_nnas
                    if det.nna is not None
                ]
            else:
                nna_asociados = []

            datos.append({
                "postulacion_id": p.postulacion_id,
                "convocatoria_id": p.convocatoria_id,
                "convocatoria": {
                    "convocatoria_referencia": p.convocatoria.convocatoria_referencia if p.convocatoria else None,
                    "convocatoria_llamado":     p.convocatoria.convocatoria_llamado     if p.convocatoria else None,
                    "nna_asociados": nna_asociados,
                    "total_nna": len(nna_asociados),
                } if p.convocatoria else None,
                # "convocatoria": {
                #     "convocatoria_referencia": p.convocatoria.convocatoria_referencia if p.convocatoria else None,
                #     "convocatoria_llamado":     p.convocatoria.convocatoria_llamado     if p.convocatoria else None,
                #     "nna_asociados": nna_asociados,
                #     "total_nna":     len(nna_asociados),
                # } if p.convocatoria else None,
                "fecha_postulacion":  p.fecha_postulacion,
                "nombre":             p.nombre,
                "apellido":           p.apellido,
                "dni":                p.dni,
                "localidad":          p.localidad,
                "provincia":          p.provincia,
                "mail":               p.mail,
                "telefono_contacto":  p.telefono_contacto,
                "conyuge_convive":    p.conyuge_convive,
                "conyuge_nombre":     p.conyuge_nombre,
                "conyuge_apellido":   p.conyuge_apellido,
                "conyuge_telefono_contacto": p.conyuge_telefono_contacto,
                "tiene_proyecto_rua": "Y" if tiene_proyecto_rua else "N",
                "tiene_otra_convocatoria": "Y" if tiene_otra_convocatoria else "N",
            })


        # datos = [{
        #     "postulacion_id": p.postulacion_id,
        #     "convocatoria_id": p.convocatoria_id,
        #     "convocatoria": {
        #         "convocatoria_referencia": p.convocatoria.convocatoria_referencia if p.convocatoria else None,
        #         "convocatoria_llamado": p.convocatoria.convocatoria_llamado if p.convocatoria else None,
        #         "nna_asociados": nna_asociados,
        #         "total_nna": len(nna_asociados),
        #     } if p.convocatoria else None,
        #     "fecha_postulacion": p.fecha_postulacion,
        #     "nombre": p.nombre,
        #     "apellido": p.apellido,
        #     "dni": p.dni,
        #     "localidad": p.localidad,
        #     "provincia": p.provincia,
        #     "mail": p.mail,
        #     "telefono_contacto": p.telefono_contacto,
        #     "conyuge_convive": p.conyuge_convive,
        # } for p in postulaciones]

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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_postulacion(postulacion_id: int, db: Session = Depends(get_db)):
    try:
        # 🔎 Cargar convocatoria con NNA asociados y proyecto vinculado
        postulacion = db.query(Postulacion)\
            .options(
                joinedload(Postulacion.convocatoria)
                    .selectinload(Convocatoria.detalle_nnas)
                    .joinedload(DetalleNNAEnConvocatoria.nna),
                joinedload(Postulacion.detalle_proyecto)
            )\
            .filter(Postulacion.postulacion_id == postulacion_id)\
            .first()

        if not postulacion:
            raise HTTPException(status_code=404, detail="Postulación no encontrada")

        # ✅ Proyecto vinculado
        proyecto = None
        entrevistas_agendadas = []
        if postulacion.detalle_proyecto:
            proyecto_id = postulacion.detalle_proyecto[0].proyecto_id
            proyecto_db = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
            proyecto = {
                "proyecto_id": proyecto_id,
                "estado_general": proyecto_db.estado_general if proyecto_db else None
            }

            entrevistas = (
                db.query(AgendaEntrevistas)
                .filter(AgendaEntrevistas.proyecto_id == proyecto_id)
                .order_by(AgendaEntrevistas.fecha_hora.asc())
                .all()
            )
            entrevistas_agendadas = [
                {"fecha_hora": e.fecha_hora.isoformat()} for e in entrevistas
            ]

        # ✅ NNA asociados a la convocatoria
        nna_asociados = []
        if postulacion.convocatoria and postulacion.convocatoria.detalle_nnas:
            for detalle in postulacion.convocatoria.detalle_nnas:
                nna = detalle.nna
                if nna:
                    edad = None
                    if nna.nna_fecha_nacimiento:
                        hoy = date.today()
                        edad = hoy.year - nna.nna_fecha_nacimiento.year - (
                            (hoy.month, hoy.day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
                        )

                    nna_asociados.append({
                        "nna_id": nna.nna_id,
                        "nna_nombre": nna.nna_nombre,
                        "nna_apellido": nna.nna_apellido,
                        "nna_edad": edad
                    })

        def construir_subregistros_legacy(ddjj: DDJJ) -> str:
            legacy_fields = [
                ("ddjj_subregistro_1", "1"),
                ("ddjj_subregistro_2", "2"),
                ("ddjj_subregistro_3", "3"),
                ("ddjj_subregistro_4", "4"),
                ("ddjj_subregistro_5_a", "5a"),
                ("ddjj_subregistro_5_b", "5b"),
                ("ddjj_subregistro_5_c", "5c"),
                ("ddjj_subregistro_6_a", "6a"),
                ("ddjj_subregistro_6_b", "6b"),
                ("ddjj_subregistro_6_c", "6c"),
                ("ddjj_subregistro_6_d", "6d"),
                ("ddjj_subregistro_6_2", "62"),
                ("ddjj_subregistro_6_3", "63"),
                ("ddjj_subregistro_6_mas_de_3", "63+"),
                ("ddjj_subregistro_flexible", "f"),
            ]

            resultado = [
                codigo
                for campo, codigo in legacy_fields
                if str(getattr(ddjj, campo, "")).upper() == "Y"
            ]

            return " ; ".join(resultado)

        def construir_subregistros_ddjj(ddjj: Optional[DDJJ]) -> Optional[str]:
            if not ddjj:
                return None

            subreg_definitivos = construir_subregistro_string(ddjj)
            if subreg_definitivos:
                return subreg_definitivos

            subreg_legacy = construir_subregistros_legacy(ddjj)
            if subreg_legacy:
                return subreg_legacy

            return "Sin subregistros"

        ddjj_titular = db.query(DDJJ).filter(DDJJ.login == postulacion.dni).first()
        ddjj_conyuge = None
        if postulacion.conyuge_dni:
            ddjj_conyuge = db.query(DDJJ).filter(DDJJ.login == postulacion.conyuge_dni).first()

        ddjj_subregistros = {
            "titular": construir_subregistros_ddjj(ddjj_titular),
            "conyuge": construir_subregistros_ddjj(ddjj_conyuge),
        }

        # ✅ Respuesta final
        return {
            "postulacion_id": postulacion.postulacion_id,
            "convocatoria_id": postulacion.convocatoria_id,
            "convocatoria": {
                "convocatoria_referencia": postulacion.convocatoria.convocatoria_referencia if postulacion.convocatoria else None,
                "convocatoria_llamado": postulacion.convocatoria.convocatoria_llamado if postulacion.convocatoria else None,
                "nna_asociados": nna_asociados
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
            "proyecto": proyecto,
            "tiene_entrevistas_agendadas": len(entrevistas_agendadas) > 0,
            "entrevistas_agendadas": entrevistas_agendadas,
            "ddjj_subregistros": ddjj_subregistros
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar la postulación: {str(e)}")



from fastapi import APIRouter, HTTPException, Depends, Query, Request, Body, UploadFile, File, Form

import os, shutil
from datetime import datetime

from fastapi.responses import FileResponse
from fastapi import Query

from typing import List, Optional, Literal
from database.config import get_db
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from models.nna import Nna
from models.carpeta import DetalleNNAEnCarpeta, Carpeta, DetalleProyectosEnCarpeta
from models.proyecto import Proyecto
from models.users import User
from security.security import get_current_user, verify_api_key, require_roles

from sqlalchemy import and_, func, or_, text, literal_column

from datetime import date
from helpers.utils import edad_como_texto, normalizar_y_validar_dni
from dotenv import load_dotenv

from sqlalchemy import select, exists




# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener y validar la variable
UPLOAD_DIR_DOC_NNAS = os.getenv("UPLOAD_DIR_DOC_NNAS")

if not UPLOAD_DIR_DOC_NNAS:
    raise RuntimeError("La variable de entorno UPLOAD_DIR_DOC_NNAS no está definida. Verificá tu archivo .env")

# Crear la carpeta si no existe
os.makedirs(UPLOAD_DIR_DOC_NNAS, exist_ok=True)





nna_router = APIRouter()


@nna_router.get("/", response_model=dict,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador", "supervisora", "profesional", "coordinadora"]))])
def get_nnas(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None, min_length=3),
    provincia: Optional[str] = Query(None),
    localidad: Optional[str] = Query(None),
    nna_en_convocatoria: Optional[bool] = Query(None),
    nna_archivado: Optional[bool] = Query(None),
    disponible: Optional[bool] = Query(None),
    subregistros: Optional[List[str]] = Query(None, alias="subregistro_portada"),
    estado_filtro: Optional[List[str]] = Query(None)
):
    try:
        query = db.query(Nna)

        if search:
            pattern = f"%{search}%"
            query = query.filter(
                (Nna.nna_nombre.ilike(pattern)) |
                (Nna.nna_apellido.ilike(pattern)) |
                (Nna.nna_dni.ilike(pattern))
            )
        if provincia:
            query = query.filter(Nna.nna_provincia == provincia)
        if localidad:
            query = query.filter(Nna.nna_localidad == localidad)
        if nna_en_convocatoria is not None:
            query = query.filter(Nna.nna_en_convocatoria == ("Y" if nna_en_convocatoria else "N"))
        if nna_archivado is not None:
            query = query.filter(Nna.nna_archivado == ("Y" if nna_archivado else "N"))

        # Subregistros
        subregistro_field_map = {
            "1": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 0 AND 3"),
            "2": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 4 AND 7"),
            "3": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 8 AND 12"),
            "4": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 13 AND 17"),
            "Mayor": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) >= 18"),
            "5A": Nna.nna_5A == "Y",
            "5B": Nna.nna_5B == "Y",
        }

        if subregistros:
            filtros_edad, filtros_salud = [], []
            for sr in subregistros:
                if sr in ["1", "2", "3", "4", "Mayor"]:
                    filtro = subregistro_field_map.get(sr)
                    if filtro is not None:
                        filtros_edad.append(filtro)
                elif sr in ["5A", "5B"]:
                    filtro = subregistro_field_map.get(sr)
                    if filtro is not None:
                        filtros_salud.append(filtro)
            if filtros_edad and filtros_salud:
                query = query.filter(and_(or_(*filtros_edad), or_(*filtros_salud)))
            elif filtros_edad:
                query = query.filter(or_(*filtros_edad))
            elif filtros_salud:
                query = query.filter(or_(*filtros_salud))

        ids_en_carpeta = [row[0] for row in db.query(DetalleNNAEnCarpeta.nna_id).distinct().all()]

        if disponible is not None:
            if disponible:
                query = query.filter(~Nna.nna_id.in_(ids_en_carpeta))
                edad_limite = date.today().replace(year=date.today().year - 18)
                query = query.filter(Nna.nna_fecha_nacimiento > edad_limite)
            else:
                query = query.filter(Nna.nna_id.in_(ids_en_carpeta))


        # 🔀 SI HAY FILTRO POR ESTADO → traer todo y paginar en Python
        if estado_filtro:
            nnas = query.all()
        else:
            total_records = query.count()
            total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)
            if page > total_pages:
                return {
                    "page": page, "limit": limit,
                    "total_pages": total_pages, "total_records": total_records,
                    "nnas": []
                }
            nnas = query.offset((page - 1) * limit).limit(limit).all()

        # 💡 Armar resultado con estado
        nnas_list = []
        for nna in nnas:
            edad = date.today().year - nna.nna_fecha_nacimiento.year - (
                (date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
            )

            subregistro_por_edad = (
                "1" if edad <= 3 else
                "2" if edad <= 7 else
                "3" if edad <= 12 else
                "4" if edad <= 17 else "Mayor"
            )

            estado = "Disponible"
            comentarios_estado = ""

            if nna.nna_id in ids_en_carpeta:
                carpeta = (
                    db.query(Carpeta)
                    .join(DetalleNNAEnCarpeta)
                    .filter(DetalleNNAEnCarpeta.nna_id == nna.nna_id)
                    .order_by(Carpeta.fecha_creacion.desc())
                    .first()
                )
                estado = "En carpeta"
                if carpeta:
                    if carpeta.estado_carpeta == "proyecto_seleccionado":
                        proyecto = (
                            db.query(Proyecto)
                            .join(DetalleProyectosEnCarpeta)
                            .filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta.carpeta_id)
                            .order_by(Proyecto.proyecto_id.desc())
                            .first()
                        )
                        if proyecto:
                            pretensos = []
                            for login in [proyecto.login_1, proyecto.login_2]:
                                if login:
                                    usuario = db.query(User).filter(User.login == login).first()
                                    if usuario:
                                        pretensos.append(f"{usuario.nombre} {usuario.apellido or ''}".strip())
                            estado = {
                                "vinculacion": "Vinculación",
                                "guarda": "Guarda",
                                "adopcion_definitiva": "Adopción definitiva"
                            }.get(proyecto.estado_general, proyecto.estado_general)
                            comentarios_estado = " y ".join(pretensos)
                        else:
                            estado = "Con dictamen"
                    else:
                        comentarios_estado = carpeta.estado_carpeta

            nnas_list.append({
                "nna_id": nna.nna_id,
                "nna_nombre": nna.nna_nombre,
                "nna_apellido": nna.nna_apellido,
                "nombre_completo": f"{nna.nna_nombre} {nna.nna_apellido}",
                "nna_dni": nna.nna_dni,
                "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
                "nna_edad": edad_como_texto(nna.nna_fecha_nacimiento),
                "nna_edad_num": edad,
                "subregistro_por_edad": subregistro_por_edad,
                "nna_calle_y_nro": nna.nna_calle_y_nro,
                "nna_barrio": nna.nna_barrio,
                "nna_localidad": nna.nna_localidad,
                "nna_provincia": nna.nna_provincia,
                "nna_5A": nna.nna_5A,
                "nna_5B": nna.nna_5B,
                "nna_en_convocatoria": nna.nna_en_convocatoria,
                "nna_ficha": nna.nna_ficha,
                "nna_sentencia": nna.nna_sentencia,
                "nna_archivado": nna.nna_archivado,
                "nna_disponible": nna.nna_id not in ids_en_carpeta,
                "estado": estado,
                "comentarios_estado": comentarios_estado
            })

        # Aplicar filtro por estado si corresponde
        if estado_filtro:
            estado_lower = [e.lower().strip() for e in estado_filtro]
            nnas_list = [n for n in nnas_list if n["estado"].lower().strip() in estado_lower]

            total_records = len(nnas_list)
            total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)
            if page > total_pages:
                return {
                    "page": page, "limit": limit,
                    "total_pages": total_pages, "total_records": total_records,
                    "nnas": []
                }
            start = (page - 1) * limit
            end = start + limit
            nnas_list = nnas_list[start:end]

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "nnas": nnas_list
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar NNAs: {str(e)}")


# @nna_router.get("/", response_model=dict,
#     dependencies=[Depends(verify_api_key), 
#                   Depends(require_roles(["administrador", "supervisora", "profesional", "coordinadora"]))])
# def get_nnas(
#     db: Session = Depends(get_db),
#     page: int = Query(1, ge=1),
#     limit: int = Query(10, ge=1, le=100),
#     search: Optional[str] = Query(None, min_length=3),
#     provincia: Optional[str] = Query(None),
#     localidad: Optional[str] = Query(None),
#     nna_en_convocatoria: Optional[bool] = Query(None),
#     nna_archivado: Optional[bool] = Query(None),
#     disponible: Optional[bool] = Query(None),
#     subregistros: Optional[List[str]] = Query(None, alias="subregistro_portada"),
#     estado_filtro: Optional[List[str]] = Query(None)

# ):
#     """
#     Devuelve los registros de NNA paginados.
#     Filtra por subregistros de edad y salud (nna_5A / nna_5B) según selección múltiple.
#     """
#     try:
#         query = db.query(Nna)

#         # Búsqueda por nombre, apellido o DNI
#         if search:
#             search_pattern = f"%{search}%"
#             query = query.filter(
#                 (Nna.nna_nombre.ilike(search_pattern)) |
#                 (Nna.nna_apellido.ilike(search_pattern)) |
#                 (Nna.nna_dni.ilike(search_pattern))
#             )

#         if provincia:
#             query = query.filter(Nna.nna_provincia == provincia)
#         if localidad:
#             query = query.filter(Nna.nna_localidad == localidad)
#         if nna_en_convocatoria is not None:
#             query = query.filter(Nna.nna_en_convocatoria == ("Y" if nna_en_convocatoria else "N"))
#         if nna_archivado is not None:
#             query = query.filter(Nna.nna_archivado == ("Y" if nna_archivado else "N"))

#         # Subregistro por edad o salud (nuevo: usa nna_5A / nna_5B)
#         subregistro_field_map = {
#             "1": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 0 AND 3"),
#             "2": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 4 AND 7"),
#             "3": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 8 AND 12"),
#             "4": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 13 AND 17"),
#             "Mayor": text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) >= 18"),
#             "5A": Nna.nna_5A == "Y",
#             "5B": Nna.nna_5B == "Y",
#         }

        
#         if subregistros:
#             filtros_edad = []
#             filtros_salud = []

#             for sr in subregistros:
#                 if sr in ["1", "2", "3", "4", "Mayor"]:  # ← incluimos "Mayor"
#                     filtro_edad = subregistro_field_map.get(sr)
#                     if filtro_edad is not None:
#                         filtros_edad.append(filtro_edad)
#                 elif sr in ["5A", "5B"]:
#                     filtro_salud = subregistro_field_map.get(sr)
#                     if filtro_salud is not None:
#                         filtros_salud.append(filtro_salud)

#             if filtros_edad and filtros_salud:
#                 query = query.filter(and_(
#                     or_(*filtros_edad),
#                     or_(*filtros_salud)
#                 ))
#             elif filtros_edad:
#                 query = query.filter(or_(*filtros_edad))
#             elif filtros_salud:
#                 query = query.filter(or_(*filtros_salud))


#         # Filtro por disponibilidad (si está o no en carpeta)
#         subquery_nnas_en_carpeta = db.query(DetalleNNAEnCarpeta.nna_id).distinct()
#         # if disponible is not None:
#         #     if disponible:
#         #         query = query.filter(~Nna.nna_id.in_(subquery_nnas_en_carpeta))
#         #     else:
#         #         query = query.filter(Nna.nna_id.in_(subquery_nnas_en_carpeta))

#         # Filtrar mayores de edad disponibles directamente desde la query
#         # if disponible is not None:
#         #     if disponible:
#         #         query = query.filter(~Nna.nna_id.in_(subquery_nnas_en_carpeta))
#         #     else:
#         #         query = query.filter(Nna.nna_id.in_(subquery_nnas_en_carpeta))

#         #     # 👉 Evitar mayores de edad solo si disponible=True
#         #     query = query.filter(
#         #         or_(
#         #             text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) < 18"),
#         #             Nna.nna_id.in_(subquery_nnas_en_carpeta)  # ya no están disponibles
#         #         )
#         #     )

#         if disponible is not None:
#             ids_en_carpeta = [row[0] for row in subquery_nnas_en_carpeta.all()]

#             if disponible:
#                 # Solo los que no están en carpeta
#                 query = query.filter(~Nna.nna_id.in_(ids_en_carpeta))

#                 # Excluimos mayores de edad disponibles
#                 hoy = date.today()
#                 edad_limite = hoy.replace(year=hoy.year - 18)
#                 query = query.filter(Nna.nna_fecha_nacimiento > edad_limite)
#             else:
#                 # Solo los que sí están en carpeta
#                 query = query.filter(Nna.nna_id.in_(ids_en_carpeta))


#         # Paginación
#         total_records = query.count()
#         total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)

#         if page > total_pages:
#             return {
#                 "page": page,
#                 "limit": limit,
#                 "total_pages": total_pages,
#                 "total_records": total_records,
#                 "nnas": []
#             }

#         skip = (page - 1) * limit
#         nnas = query.offset(skip).limit(limit).all()
#         subquery_ids = [row[0] for row in subquery_nnas_en_carpeta.all()]

#         nnas_list = []

#         for nna in nnas:
#             edad = date.today().year - nna.nna_fecha_nacimiento.year - (
#                 (date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
#             )

#             # Calcular subregistro por edad
#             if edad <= 3:
#                 subregistro_por_edad = "1"
#             elif 4 <= edad <= 7:
#                 subregistro_por_edad = "2"
#             elif 8 <= edad <= 12:
#                 subregistro_por_edad = "3"
#             elif 13 <= edad <= 17:
#                 subregistro_por_edad = "4"
#             else:
#                 subregistro_por_edad = "Mayor"

#             estado = "Disponible"
#             comentarios_estado = ""

#             if nna.nna_id in subquery_ids:
#                 estado_map = {
#                     "vinculacion": "Vinculación",
#                     "guarda": "Guarda",
#                     "adopcion_definitiva": "Adopción definitiva",
#                 }

#                 carpeta = (
#                     db.query(Carpeta)
#                     .join(DetalleNNAEnCarpeta)
#                     .filter(DetalleNNAEnCarpeta.nna_id == nna.nna_id)
#                     .order_by(Carpeta.fecha_creacion.desc())
#                     .first()
#                 )

#                 estado = "En carpeta"
#                 comentarios_estado = ""

#                 if carpeta:
#                     if carpeta.estado_carpeta == "proyecto_seleccionado":
#                         proyecto = (
#                             db.query(Proyecto)
#                             .join(DetalleProyectosEnCarpeta)
#                             .filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta.carpeta_id)
#                             .order_by(Proyecto.proyecto_id.desc())
#                             .first()
#                         )
#                         if proyecto:
#                             pretensos = []
#                             usuario_1 = db.query(User).filter(User.login == proyecto.login_1).first()
#                             if usuario_1:
#                                 nombre_1 = f"{usuario_1.nombre} {usuario_1.apellido or ''}".strip()
#                                 pretensos.append(nombre_1)

#                             if proyecto.login_2:
#                                 usuario_2 = db.query(User).filter(User.login == proyecto.login_2).first()
#                                 if usuario_2:
#                                     nombre_2 = f"{usuario_2.nombre} {usuario_2.apellido or ''}".strip()
#                                     pretensos.append(nombre_2)

#                             estado_legible = estado_map.get(proyecto.estado_general, proyecto.estado_general)
#                             estado = estado_legible
#                             comentarios_estado = " y ".join(pretensos)
#                         else:
#                             estado = "Con dictamen"
#                             comentarios_estado = ""
#                     else:
#                         comentarios_estado = carpeta.estado_carpeta
#             else:
#                 estado = "Disponible"
#                 comentarios_estado = ""

#             if edad >= 18:
#                 estado = f"{estado}"

#             # if edad >= 18 and estado == "Disponible":
#             #     continue  # lo salteamos


#             nnas_list.append({
#                 "nna_id": nna.nna_id,
#                 "nna_nombre": nna.nna_nombre,
#                 "nna_apellido": nna.nna_apellido,
#                 "nombre_completo": nna.nna_nombre + " " + nna.nna_apellido,
#                 "nna_dni": nna.nna_dni,
#                 "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
#                 "nna_edad": edad_como_texto(nna.nna_fecha_nacimiento),
#                 "nna_edad_num": edad,
#                 "subregistro_por_edad": subregistro_por_edad,
#                 "nna_calle_y_nro": nna.nna_calle_y_nro,
#                 "nna_barrio": nna.nna_barrio,
#                 "nna_localidad": nna.nna_localidad,
#                 "nna_provincia": nna.nna_provincia,
#                 "nna_5A": nna.nna_5A,
#                 "nna_5B": nna.nna_5B,
#                 "nna_en_convocatoria": nna.nna_en_convocatoria,
#                 "nna_ficha": nna.nna_ficha,
#                 "nna_sentencia": nna.nna_sentencia,
#                 "nna_archivado": nna.nna_archivado,
#                 "nna_disponible": nna.nna_id not in subquery_ids,
#                 "estado": estado,
#                 "comentarios_estado": comentarios_estado
#             })


#         if estado_filtro:
#             estado_lower = [e.lower().strip() for e in estado_filtro]
#             nnas_list = [
#                 n for n in nnas_list if n["estado"].lower().strip() in estado_lower
#             ]


            
#         return {
#             "page": page,
#             "limit": limit,
#             "total_pages": total_pages,
#             "total_records": total_records,
#             "nnas": nnas_list
#         }

#     except SQLAlchemyError as e:
#         raise HTTPException(status_code=500, detail=f"Error al recuperar NNAs: {str(e)}")




# @nna_router.get("/{nna_id}", response_model=dict, 
#                   dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
# def get_nna_by_id(nna_id: int, db: Session = Depends(get_db)):
#     """
#     Devuelve un único NNA por su `nna_id`.
#     """
#     try:
#         nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
#         if not nna:
#             raise HTTPException(status_code=404, detail="NNA no encontrado")

#         # Verificar si el NNA está en alguna carpeta
#         asignado_a_carpeta = db.query(DetalleNNAEnCarpeta).filter( DetalleNNAEnCarpeta.nna_id == nna_id ).first()

#         esta_disponible = asignado_a_carpeta is None


#         return {
#             "nna_id": nna.nna_id,
#             "nna_nombre": nna.nna_nombre,
#             "nna_apellido": nna.nna_apellido,
#             "nna_dni": nna.nna_dni,
#             "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
#             "nna_calle_y_nro": nna.nna_calle_y_nro,
#             "nna_barrio": nna.nna_barrio,
#             "nna_localidad": nna.nna_localidad,
#             "nna_provincia": nna.nna_provincia,
#             "nna_subregistro_salud": nna.nna_subregistro_salud,
#             "nna_en_convocatoria": nna.nna_en_convocatoria,
#             "nna_ficha": nna.nna_ficha,
#             "nna_sentencia": nna.nna_sentencia,
#             "nna_archivado": nna.nna_archivado,
#             "nna_disponible": esta_disponible
#         }

#     except SQLAlchemyError as e:
#         raise HTTPException(status_code=500, detail=f"Error al recuperar NNA: {str(e)}")


@nna_router.get("/{nna_id}", response_model=dict, 
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def get_nna_by_id(nna_id: int, db: Session = Depends(get_db)):
    """
    Devuelve un único NNA por su `nna_id`, con misma estructura que el listado paginado.
    """
    try:
        nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
        if not nna:
            raise HTTPException(status_code=404, detail="NNA no encontrado")

        edad = date.today().year - nna.nna_fecha_nacimiento.year - (
            (date.today().month, date.today().day) < (nna.nna_fecha_nacimiento.month, nna.nna_fecha_nacimiento.day)
        )

        # Subregistro por edad
        if edad <= 3:
            subregistro_por_edad = "1"
        elif 4 <= edad <= 7:
            subregistro_por_edad = "2"
        elif 8 <= edad <= 12:
            subregistro_por_edad = "3"
        elif 13 <= edad <= 17:
            subregistro_por_edad = "4"
        else:
            subregistro_por_edad = "Mayor"

        # Verificar si el NNA está en alguna carpeta
        subquery_ids = db.query(DetalleNNAEnCarpeta.nna_id).distinct()
        esta_disponible = nna.nna_id not in [row[0] for row in subquery_ids.all()]

        # Estado y comentarios
        if edad >= 18:
            estado = "Mayor de edad"
            comentarios_estado = ""
        elif not esta_disponible:
            estado_map = {
                "vinculacion": "Vinculación",
                "guarda": "Guarda",
                "adopcion_definitiva": "Adopción definitiva",
            }

            carpeta = (
                db.query(Carpeta)
                .join(DetalleNNAEnCarpeta)
                .filter(DetalleNNAEnCarpeta.nna_id == nna.nna_id)
                .order_by(Carpeta.fecha_creacion.desc())
                .first()
            )

            estado = "En carpeta"
            comentarios_estado = ""

            if carpeta:
                if carpeta.estado_carpeta == "proyecto_seleccionado":
                    proyecto = (
                        db.query(Proyecto)
                        .join(DetalleProyectosEnCarpeta)
                        .filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta.carpeta_id)
                        .order_by(Proyecto.proyecto_id.desc())
                        .first()
                    )
                    if proyecto:
                        pretensos = []
                        usuario_1 = db.query(User).filter(User.login == proyecto.login_1).first()
                        if usuario_1:
                            nombre_1 = f"{usuario_1.nombre} {usuario_1.apellido or ''}".strip()
                            pretensos.append(nombre_1)

                        if proyecto.login_2:
                            usuario_2 = db.query(User).filter(User.login == proyecto.login_2).first()
                            if usuario_2:
                                nombre_2 = f"{usuario_2.nombre} {usuario_2.apellido or ''}".strip()
                                pretensos.append(nombre_2)

                        estado_legible = estado_map.get(proyecto.estado_general, proyecto.estado_general)
                        estado = estado_legible
                        comentarios_estado = " y ".join(pretensos)
                    else:
                        estado = "Con dictamen"
                        comentarios_estado = ""
                else:
                    comentarios_estado = carpeta.estado_carpeta
        else:
            estado = "Disponible"
            comentarios_estado = ""

        return {
            "nna_id": nna.nna_id,
            "nna_nombre": nna.nna_nombre,
            "nna_apellido": nna.nna_apellido,
            "nna_dni": nna.nna_dni,
            "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
            "nna_edad": edad_como_texto(nna.nna_fecha_nacimiento),
            "nna_edad_num": edad,
            "subregistro_por_edad": subregistro_por_edad,
            "nna_calle_y_nro": nna.nna_calle_y_nro,
            "nna_barrio": nna.nna_barrio,
            "nna_localidad": nna.nna_localidad,
            "nna_provincia": nna.nna_provincia,
            "nna_5A": nna.nna_5A,
            "nna_5B": nna.nna_5B,
            "nna_en_convocatoria": nna.nna_en_convocatoria,
            "nna_ficha": nna.nna_ficha,
            "nna_sentencia": nna.nna_sentencia,
            "nna_archivado": nna.nna_archivado,
            "nna_disponible": esta_disponible,
            "estado": estado,
            "comentarios_estado": comentarios_estado
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar NNA: {str(e)}")




@nna_router.post("/", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def create_nna(nna_data: dict = Body(...), db: Session = Depends(get_db)):
    """
    Crea un nuevo registro de NNA.
    Se espera un JSON con los siguientes campos:
    - nna_nombre: str
    - nna_apellido: str
    - nna_dni: str
    - nna_fecha_nacimiento: str (formato YYYY-MM-DD)
    - nna_calle_y_nro: str
    - nna_barrio: str
    - nna_localidad: str
    - nna_provincia: str
    - nna_subregistro_salud: str
    - nna_en_convocatoria: str ("Y" o "N")
    - nna_ficha: str
    - nna_sentencia: str
    - nna_archivado: str ("Y" o "N")
    """
    try:
        new_nna = Nna(
            nna_nombre=nna_data.get("nna_nombre"),
            nna_apellido=nna_data.get("nna_apellido"),
            nna_dni=nna_data.get("nna_dni"),
            nna_fecha_nacimiento=nna_data.get("nna_fecha_nacimiento"),
            nna_calle_y_nro=nna_data.get("nna_calle_y_nro"),
            nna_barrio=nna_data.get("nna_barrio"),
            nna_localidad=nna_data.get("nna_localidad"),
            nna_provincia=nna_data.get("nna_provincia"),
            nna_subregistro_salud=nna_data.get("nna_subregistro_salud"),
            nna_en_convocatoria=nna_data.get("nna_en_convocatoria", "N"),
            nna_ficha=nna_data.get("nna_ficha"),
            nna_sentencia=nna_data.get("nna_sentencia"),
            nna_archivado=nna_data.get("nna_archivado", "N"),
        )

        db.add(new_nna)
        db.commit()
        db.refresh(new_nna)

        return {"message": "NNA creado con éxito", "nna_id": new_nna.nna_id}

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al crear el NNA: {str(e)}")


@nna_router.delete("/{nna_id}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def delete_nna(nna_id: int, db: Session = Depends(get_db)):
    """
    Elimina un NNA si existe.
    """
    try:
        nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
        if not nna:
            raise HTTPException(status_code=404, detail="NNA no encontrado")

        db.delete(nna)
        db.commit()
        return {"message": "NNA eliminado exitosamente"}

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al eliminar el NNA: {str(e)}")



@nna_router.post("/upsert", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def upsert_nna(nna_data: dict = Body(...), db: Session = Depends(get_db)):
    """
    🔁 Inserta o actualiza un NNA según el `nna_dni`.

    Si ya existe un NNA con ese DNI, se actualizan sus datos.  
    Si no existe, se crea un nuevo registro.

    📥 JSON esperado:
    ```json
    {
        "nna_nombre": "Lucía",
        "nna_apellido": "Gómez",
        "nna_dni": "30123456",
        "nna_fecha_nacimiento": "2015-07-12",
        "nna_calle_y_nro": "Av. Siempre Viva 123",
        "nna_depto_etc": "B",
        "nna_barrio": "Centro",
        "nna_localidad": "Córdoba",
        "nna_provincia": "Córdoba",
        "nna_subregistro_salud": "a",
        "nna_en_convocatoria": "Y",
        "nna_archivado": "N"
    }
    ```

    """
    try:
        # Validar y normalizar DNI
        dni = normalizar_y_validar_dni(nna_data.get("nna_dni"))
        if not dni:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe proporcionar un DNI válido de 6 a 9 dígitos",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Buscar si existe
        nna_existente = db.query(Nna).filter(Nna.nna_dni == dni).first()

        campos = [
            "nna_nombre", "nna_apellido", "nna_fecha_nacimiento", "nna_calle_y_nro",
            "nna_depto_etc", "nna_barrio", "nna_localidad", "nna_provincia",
            "nna_subregistro_salud", "nna_en_convocatoria", "nna_archivado",
            "nna_5A", "nna_5B",
        ]

        if nna_existente:
            # Actualizar campos
            for campo in campos:
                if campo in nna_data:
                    setattr(nna_existente, campo, nna_data[campo])
            db.commit()
            db.refresh(nna_existente)
            return {
                "success": True,
                "tipo_mensaje": "verde",
                "mensaje": "NNA actualizado correctamente",
                "tiempo_mensaje": 5,
                "next_page": "actual",
                "nna_id": nna_existente.nna_id
            }

        # Crear nuevo NNA
        nuevo_nna = Nna(
            **{campo: nna_data.get(campo) for campo in campos},
            nna_dni = dni
        )
        db.add(nuevo_nna)
        db.commit()
        db.refresh(nuevo_nna)
        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "NNA creado correctamente",
            "tiempo_mensaje": 5,
            "next_page": "actual",
            "nna_id": nuevo_nna.nna_id
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error en upsert de NNA: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@nna_router.put("/documentos/{nna_id}", response_model=dict,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def update_nna_document_by_id(
    nna_id: int,
    campo: Literal["nna_ficha", "nna_sentencia"] = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    Sube un documento de ficha o sentencia para el NNA identificado por `nna_id`.
    El archivo se guarda en una carpeta por NNA, con nombre único por fecha y hora.
    Actualiza la ruta del último archivo subido en la base de datos.
    """

    # Validación de extensión permitida
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Extensión de archivo no permitida: {ext}")

    nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
    if not nna:
        raise HTTPException(status_code=404, detail="NNA no encontrado")

    # Definir nombre base según campo
    nombre_archivo_map = {
        "nna_ficha": "ficha",
        "nna_sentencia": "sentencia"
    }
    nombre_archivo = nombre_archivo_map[campo]

    # Crear carpeta del NNA si no existe
    user_dir = os.path.join(UPLOAD_DIR_DOC_NNAS, str(nna_id))
    os.makedirs(user_dir, exist_ok=True)

    # Generar nombre único con timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"{nombre_archivo}_{timestamp}{ext}"
    filepath = os.path.join(user_dir, final_filename)

    try:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Actualizar campo correspondiente en DB
        setattr(nna, campo, filepath)
        db.commit()

        return {"message": f"Documento '{campo}' subido como '{final_filename}'"}
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al guardar el documento: {str(e)}")



@nna_router.get("/documentos/{nna_id}/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def descargar_documento_nna(
    nna_id: int,
    campo: Literal["nna_ficha", "nna_sentencia"] = Query(...),
    db: Session = Depends(get_db)
):
    """
    Descarga un documento del NNA identificado por `nna_id`.
    El campo debe ser 'nna_ficha' o 'nna_sentencia'.
    """

    nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
    if not nna:
        raise HTTPException(status_code=404, detail="NNA no encontrado")

    filepath = getattr(nna, campo)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path = filepath,
        filename = os.path.basename(filepath),
        media_type = "application/octet-stream"
    )
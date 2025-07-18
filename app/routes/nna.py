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

import json

import zipfile
import tempfile




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
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def get_nnas(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None),
    provincia: Optional[str] = Query(None),
    localidad: Optional[str] = Query(None),
    nna_en_convocatoria: Optional[bool] = Query(None),
    nna_archivado: Optional[bool] = Query(None),
    disponible: Optional[bool] = Query(None),
    subregistros: Optional[List[str]] = Query(None, alias="subregistro_portada"),
    estado_filtro: Optional[List[str]] = Query(None),
    excluir_nna_ids: Optional[List[int]] = Query(None),
):
    try:
        query = db.query(Nna)

        # Si tiene menos de 3 caracteres, no filtra nada extra (devuelve todo)
        if search and len(search.strip()) >= 3:
            palabras = search.strip().split()

            condiciones_por_palabra = []
            for palabra in palabras:
                patron = f"%{palabra}%"
                condiciones_por_palabra.append(
                    or_(
                        Nna.nna_nombre.ilike(patron),
                        Nna.nna_apellido.ilike(patron),
                        Nna.nna_dni.ilike(patron),
                        Nna.nna_localidad.ilike(patron)
                    )
                )

            query = query.filter(and_(*condiciones_por_palabra))
        



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

        # Filtrar por estado directamente en base de datos
        # if estado_filtro:
        #     query = query.filter(Nna.nna_estado.in_(estado_filtro))

        # —— Filtrado por estado —— 
        if estado_filtro:
            # Si me piden explícitamente 'no_disponible', lo incluyo junto a los que haya en la lista
            query = query.filter(Nna.nna_estado.in_(estado_filtro))
        else:
            # Por defecto oculto los no_disponible
            query = query.filter(Nna.nna_estado != "no_disponible")

        # Si se especifican IDs a excluir, los filtramos
        if excluir_nna_ids:
            query = query.filter(~Nna.nna_id.in_(excluir_nna_ids))


        # 👇 Ordenar por apellido y luego por nombre
        query = query.order_by(Nna.nna_apellido.asc(), Nna.nna_nombre.asc())

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

            # Ver si tiene hermanos (sin incluirse a sí mismo)
            tiene_hermanos = False
            if nna.hermanos_id is not None:
                otros_hermanos = db.query(Nna).filter(
                    Nna.hermanos_id == nna.hermanos_id,
                    Nna.nna_id != nna.nna_id
                ).first()
                tiene_hermanos = otros_hermanos is not None

            
            nnas_list.append({
                "nna_id": nna.nna_id,
                "nna_nombre": nna.nna_nombre,
                "nna_apellido": nna.nna_apellido,
                "nombre_completo": f"{nna.nna_nombre} {nna.nna_apellido}",
                "nna_dni": nna.nna_dni,
                "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
                "nna_edad": edad_como_texto(nna.nna_fecha_nacimiento),
                "nna_edad_texto": edad_como_texto(nna.nna_fecha_nacimiento),
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
                "estado": nna.nna_estado,
                "comentarios_estado": comentarios_estado,
                "tiene_hermanos": tiene_hermanos
            })

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "nnas": nnas_list
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar NNAs: {str(e)}")



@nna_router.get("/{nna_id}", response_model=dict, 
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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

        # raw_estado es el valor que viene de la columna nna_estado
        raw_estado = nna.nna_estado

        # Aquí el mapeo de los códigos a texto legible
        estado_map = {
            "no_disponible": "No disponible",
            "disponible": "Disponible",
            "sin_ficha_sin_sentencia": "Sin ficha ni sentencia de adopción",
            "con_ficha_sin_sentencia": "Sin sentencia de adopción",
            "sin_ficha_con_sentencia": "Sin ficha",
        }

        # Si el código raw_estado está en el mapa, lo uso;
        # si no, uso el texto que ya calculaste en la lógica anterior (variable `estado`)
        estado_legible = estado_map.get(raw_estado, estado)

        # Determinar el flag nna_no_disponible según el estado crudo
        nna_no_disponible = "Y" if raw_estado == "no_disponible" else "N"

        # Obtener hermanos si corresponde
        hermanos = []
        if nna.hermanos_id is not None:
            hermanos = db.query(Nna).filter(
                Nna.hermanos_id == nna.hermanos_id,
                Nna.nna_id != nna.nna_id
            ).all()


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
            
            "estado": estado_legible,
            "comentarios_estado": comentarios_estado,
            "nna_no_disponible": nna_no_disponible,

            # "estado": estado,
            # "comentarios_estado": comentarios_estado,
            # "nna_no_disponible": nna_no_disponible,

            "hermanos": [
                {
                    "nna_id": h.nna_id,
                    "nna_nombre": h.nna_nombre,
                    "nna_apellido": h.nna_apellido,
                    "nna_dni": h.nna_dni,
                    "nna_fecha_nacimiento": h.nna_fecha_nacimiento,
                    "nna_localidad": h.nna_localidad,
                    "nna_provincia": h.nna_provincia,
                    "nna_edad": (
                        date.today().year - h.nna_fecha_nacimiento.year -
                        ((date.today().month, date.today().day) < (h.nna_fecha_nacimiento.month, h.nna_fecha_nacimiento.day))
                    ),
                    "nna_edad_texto": edad_como_texto(h.nna_fecha_nacimiento)
                }
                for h in hermanos
            ]

        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar NNA: {str(e)}")



@nna_router.post("/por-ids", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_nnas_por_ids(nna_ids: List[int] = Body(...), db: Session = Depends(get_db)):
    """
    Devuelve un listado de NNAs por sus IDs, sin aplicar filtros de disponibilidad.
    """
    try:
        nnas = db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).all()

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
                "estado": nna.nna_estado,
                "comentarios_estado": ""
            })

        return {"nnas": nnas_list}
    
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener NNAs por IDs: {str(e)}")





@nna_router.post("/", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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



@nna_router.post("/upsert", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def upsert_nna(nna_data: dict = Body(...), db: Session = Depends(get_db)):
    """
    Inserta o actualiza un NNA.  
    Si viene `nna_id`, se actualiza ese registro (incluso si cambia el DNI).  
    Si no viene `nna_id`, se busca por DNI y se hace upsert.
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

        # Validar campos obligatorios con nombres amigables
        campos_obligatorios = {
            "nna_nombre": "Nombre",
            "nna_apellido": "Apellido",
            "nna_fecha_nacimiento": "Fecha de nacimiento",
            "nna_localidad": "Localidad",
            "nna_provincia": "Provincia"
        }

        faltantes = [nombre for campo, nombre in campos_obligatorios.items() if not nna_data.get(campo)]

        if faltantes:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"Faltan campos obligatorios: {', '.join(faltantes)}.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }



        campos = [
            "nna_nombre", "nna_apellido", "nna_fecha_nacimiento", "nna_calle_y_nro",
            "nna_depto_etc", "nna_barrio", "nna_localidad", "nna_provincia",
            "nna_subregistro_salud", "nna_en_convocatoria", "nna_archivado",
            "nna_5A", "nna_5B", "nna_no_disponible"
        ]

        nna_id = nna_data.get("nna_id")
        if nna_id:
            # ——— ACTUALIZAR POR ID ———
            nna_existente = db.query(Nna).filter(Nna.nna_id == nna_id).first()
            if not nna_existente:
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"No se encontró NNA con ID {nna_id}.",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }

            # Validar que el nuevo DNI no exista en otro NNA
            otro_con_igual_dni = db.query(Nna).filter(
                Nna.nna_dni == dni,
                Nna.nna_id != nna_id
            ).first()
            if otro_con_igual_dni:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"Ya existe otro NNA con este DNI.",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }


            # Verificamos si se desea marcar como no disponible
            flag = nna_data.get("nna_no_disponible")
            if flag == "Y":
                if nna_existente.nna_estado in ("disponible", "no_disponible"):
                    nna_existente.nna_estado = "no_disponible"
                else:
                    return {
                        "success": False,
                        "tipo_mensaje": "rojo",
                        "mensaje": "Sólo se puede marcar como no disponible a NNA en estado 'disponible'.",
                        "tiempo_mensaje": 6,
                        "next_page": "actual"
                    }
            elif flag == "N":
                nna_existente.nna_estado = "disponible"

            # Actualizamos todos los campos (incluido el dni)
            nna_existente.nna_dni = dni
            for campo in campos:
                if campo in ("nna_no_disponible",):
                    continue
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

        # ——— UPSERT POR DNI (como antes) ———
        nna_existente = db.query(Nna).filter(Nna.nna_dni == dni).first()
        if nna_existente:
            flag = nna_data.get("nna_no_disponible")
            if flag == "Y":
                if nna_existente.nna_estado in ("disponible", "no_disponible"):
                    nna_existente.nna_estado = "no_disponible"
                else:
                    return {
                        "success": False,
                        "tipo_mensaje": "rojo",
                        "mensaje": "Sólo se puede marcar como no disponible a NNA en estado 'disponible'.",
                        "tiempo_mensaje": 6,
                        "next_page": "actual"
                    }
            elif flag == "N":
                nna_existente.nna_estado = "disponible"

            for campo in campos:
                if campo in ("nna_no_disponible",):
                    continue
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

        # ——— CREAR NUEVO NNA ———

        # Validar que no exista otro con el mismo DNI
        otro_con_igual_dni = db.query(Nna).filter(Nna.nna_dni == dni).first()
        if otro_con_igual_dni:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"Ya existe un NNA con este DNI.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        nuevo_nna = Nna(
            nna_dni=dni,
            nna_estado="disponible",
            **{campo: nna_data.get(campo)
               for campo in campos
               if campo not in ("nna_no_disponible",)}
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
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def update_nna_document_by_id(
    nna_id: int,
    campo: Literal["nna_ficha", "nna_sentencia"] = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    _, ext = os.path.splitext(file.filename.lower())

    if ext not in allowed_extensions:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Extensión de archivo no permitida: {ext}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
    if not nna:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "NNA no encontrado.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    nombre_archivo_map = {
        "nna_ficha": "ficha",
        "nna_sentencia": "sentencia"
    }
    nombre_archivo = nombre_archivo_map[campo]

    nombre_amigable_map = {
        "nna_ficha": "Ficha de reconocimiento",
        "nna_sentencia": "Sentencia de adoptabilidad"
    }


    user_dir = os.path.join(UPLOAD_DIR_DOC_NNAS, str(nna_id))
    os.makedirs(user_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"{nombre_archivo}_{timestamp}{ext}"
    filepath = os.path.join(user_dir, final_filename)

    try:
        # Validar tamaño (máx. 5MB)
        file.file.seek(0, os.SEEK_END)
        file_size = file.file.tell()
        file.file.seek(0)

        if file_size > 5 * 1024 * 1024:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "El archivo excede el tamaño máximo permitido de 5MB.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        nuevo_archivo = {
            "ruta": filepath,
            "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Parsear valor actual del campo
        valor_actual = getattr(nna, campo)
        try:
            if valor_actual:
                if valor_actual.strip().startswith("["):
                    archivos = json.loads(valor_actual)
                else:
                    archivos = [{"ruta": valor_actual, "fecha": "desconocida"}]
            else:
                archivos = []
        except json.JSONDecodeError:
            archivos = []

        archivos.append(nuevo_archivo)
        setattr(nna, campo, json.dumps(archivos, ensure_ascii=False))
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"{nombre_amigable_map[campo]} subida correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al guardar el documento: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@nna_router.get("/documentos/{nna_id}/descargar-todos", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def descargar_todos_documentos_nna(
    nna_id: int,
    campo: Literal["nna_ficha", "nna_sentencia"] = Query(...),
    db: Session = Depends(get_db)
):
    """
    Descarga todos los documentos (de ficha o sentencia) asociados a un NNA:
    - Si hay uno solo, lo devuelve directamente.
    - Si hay más, los empaqueta en un .zip.
    También es compatible con el formato anterior (una única ruta como string plano).
    """

    print(f"🔍 Solicitando descarga de documentos para NNA ID: {nna_id}, campo: {campo}")

    nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
    if not nna:
        raise HTTPException(status_code=404, detail="NNA no encontrado")

    valor = getattr(nna, campo)

    print(f"📄 Valor del campo {campo}: {valor}")


    try:
        if valor:
            if valor.strip().startswith("["):
                archivos = json.loads(valor)
                print(f"✅ Campo contiene JSON válido: {archivos}")
            else:
                print("⚠️ Campo contiene una única ruta como string plano")
                # Es una única ruta como string plano (modo anterior)
                archivos = [{"ruta": valor}]
        else:
            archivos = []
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="El campo no contiene JSON válido ni ruta válida")

    if not archivos:
        raise HTTPException(status_code=404, detail="No hay documentos registrados")

    # Solo un archivo → lo devuelvo directamente
    if len(archivos) == 1:
        ruta = archivos[0]["ruta"]
        print(f"📁 Un solo archivo detectado: {ruta}")
        if not os.path.exists(ruta):
            raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")
        return FileResponse(path=ruta, filename=os.path.basename(ruta), media_type="application/octet-stream")

    # Más de un archivo → crear un ZIP temporal
    try:
        print(f"📦 Se encontraron múltiples archivos, creando ZIP temporal...")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zipf:
            for archivo in archivos:
                ruta = archivo.get("ruta")
                print(f"➕ Agregando al ZIP: {ruta}")
                if ruta and os.path.exists(ruta):
                    nombre_en_zip = os.path.basename(ruta)
                    zipf.write(ruta, arcname=nombre_en_zip)
                else:
                    print(f"⚠️ Ruta inexistente o vacía: {ruta}")
        print(f"✅ ZIP creado exitosamente: {tmp.name}")
        return FileResponse(
            path=tmp.name,
            filename=f"{campo}_{nna_id}.zip",
            media_type="application/zip"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al generar el ZIP: {str(e)}")




@nna_router.get("/documentos/{nna_id}/descargar", response_class=FileResponse,
    dependencies=[Depends(verify_api_key),
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
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




@nna_router.post("/definir-hermanos", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def definir_hermanos(
    nna_ids: List[int] = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """
    Asocia un grupo de NNAs como hermanos mediante un `hermanos_id` común.
    - Si ninguno tiene `hermanos_id`, se asigna uno nuevo (máximo actual + 1).
    - Si ya hay un `hermanos_id` común, se usa ese.
    - Si hay más de un `hermanos_id` distinto, se rechaza la operación.
    """
    if len(nna_ids) < 2:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Debe proporcionar al menos 2 NNA para definir hermanos.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    try:
        # Traer todos los NNA
        nnas = db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).all()
        if len(nnas) != len(nna_ids):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Uno o más NNA no fueron encontrados.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Detectar los distintos hermanos_id presentes
        hermanos_ids = set([nna.hermanos_id for nna in nnas if nna.hermanos_id is not None])

        if len(hermanos_ids) > 1:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Ya hay NNAs con diferentes grupos de hermanos definidos. No se pueden unificar. Por favor desvincule y reagrupe.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Elegir hermanos_id a asignar (el existente o uno nuevo)
        if hermanos_ids:
            nuevo_hermanos_id = hermanos_ids.pop()
        else:
            max_hermanos_id = db.query(func.max(Nna.hermanos_id)).scalar() or 0
            nuevo_hermanos_id = max_hermanos_id + 1

        for nna in nnas:
            nna.hermanos_id = nuevo_hermanos_id

        db.commit()
        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"{len(nnas)} NNAs como hermanos.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": True,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al definir hermanos.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }



@nna_router.post("/quitar-hermanos", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def quitar_hermanos(
    nna_ids: List[int] = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """
    Quita a uno o varios NNAs de su grupo de hermanos (pone hermanos_id en NULL).
    """
    if not nna_ids:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "Debe proporcionar al menos un NNA para quitar hermanos.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    try:
        nnas = db.query(Nna).filter(Nna.nna_id.in_(nna_ids)).all()
        if len(nnas) != len(nna_ids):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Uno o más NNA no fueron encontrados.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        modificados = 0
        for nna in nnas:
            if nna.hermanos_id is not None:
                nna.hermanos_id = None
                modificados += 1

        if modificados == 0:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Ninguno de los NNAs tenía grupo de hermanos asignado.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        db.commit()
        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Se quitaron {modificados} NNAs de su grupo de hermanos.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": True,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al quitar hermanos.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


@nna_router.get("/{nna_id}/hermanos", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def get_hermanos_de_nna(nna_id: int, db: Session = Depends(get_db)):
    """
    Devuelve los NNAs que tienen el mismo `hermanos_id` que el NNA dado.
    """
    try:
        nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
        if not nna:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "NNA no encontrado",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if nna.hermanos_id is None:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Este NNA no pertenece a ningún grupo de hermanos.",
                "hermanos": [],
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        hermanos = db.query(Nna).filter(
            Nna.hermanos_id == nna.hermanos_id,
            Nna.nna_id != nna_id
        ).all()

        hermanos_data = [
            {
                "nna_id": h.nna_id,
                "nna_nombre": h.nna_nombre,
                "nna_apellido": h.nna_apellido,
                "nna_dni": h.nna_dni,
                "nna_fecha_nacimiento": h.nna_fecha_nacimiento,
                "nna_localidad": h.nna_localidad,
                "nna_provincia": h.nna_provincia,
                "nna_edad_texto": edad_como_texto(h.nna_fecha_nacimiento)
            }
            for h in hermanos
        ]

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"Se encontraron {len(hermanos_data)} hermanos.",
            "hermanos": hermanos_data,
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        return {
            "success": True,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al obtener hermanos.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
        


@nna_router.delete("/documentos/{nna_id}/eliminar", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def eliminar_documento_nna(
    nna_id: int,
    campo: Literal["nna_ficha", "nna_sentencia"] = Query(...),
    ruta: str = Query(...),
    db: Session = Depends(get_db)
):
    nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
    if not nna:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "NNA no encontrado.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    try:
        valor = getattr(nna, campo)
        archivos = json.loads(valor) if valor.strip().startswith("[") else [{"ruta": valor}]
        nuevos_archivos = [a for a in archivos if a.get("ruta") != ruta]

        if len(nuevos_archivos) == len(archivos):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Archivo no encontrado.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        # Eliminar físicamente el archivo
        if os.path.exists(ruta):
            os.remove(ruta)

        setattr(nna, campo, json.dumps(nuevos_archivos, ensure_ascii=False) if nuevos_archivos else None)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Archivo eliminado correctamente.",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al eliminar archivo: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

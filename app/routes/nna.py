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
from models.carpeta import DetalleNNAEnCarpeta
from security.security import get_current_user, verify_api_key, require_roles

from sqlalchemy import func, or_, text, literal_column

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
                dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def get_nnas(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None, min_length=3, description="Búsqueda por nombre, apellido o DNI"),
    provincia: Optional[str] = Query(None, description="Filtrar por provincia"),
    localidad: Optional[str] = Query(None, description="Filtrar por localidad"),
    nna_en_convocatoria: Optional[bool] = Query(None, description="Filtrar por si está en convocatoria"),
    nna_archivado: Optional[bool] = Query(None, description="Filtrar por si está archivado"),
    subregistro_1: Optional[bool] = Query(None, description="0 a 3 años"),
    subregistro_2: Optional[bool] = Query(None, description="4 a 6 años"),
    subregistro_3: Optional[bool] = Query(None, description="7 a 11 años"),
    subregistro_4: Optional[bool] = Query(None, description="12 a 17 años"),
    subregistro_5a: Optional[bool] = Query(None, description="Dificultades motrices leves, Sindrome de Down, ..."),
    subregistro_5b: Optional[bool] = Query(None, description="Patologías crónicas y/o congénitas"),
    subregistro_5c: Optional[bool] = Query(None, description="Ceguera, sordera"),
    disponible: Optional[bool] = Query(None, description="Filtrar por disponibilidad"),
):
    """
    Devuelve los registros de NNA paginados.  
    Permite búsqueda parcial por nombre, apellido o DNI.  
    Se pueden filtrar por provincia, localidad, estado en convocatoria, archivado y subregistro por edad.
    """
    try:
        query = db.query(Nna)

        # Búsqueda por nombre, apellido o DNI
        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                (Nna.nna_nombre.ilike(search_pattern)) |
                (Nna.nna_apellido.ilike(search_pattern)) |
                (Nna.nna_dni.ilike(search_pattern))
            )

        if provincia:
            query = query.filter(Nna.nna_provincia == provincia)

        if localidad:
            query = query.filter(Nna.nna_localidad == localidad)

        if nna_en_convocatoria is not None:
            query = query.filter(Nna.nna_en_convocatoria == ("Y" if nna_en_convocatoria else "N"))

        if nna_archivado is not None:
            query = query.filter(Nna.nna_archivado == ("Y" if nna_archivado else "N"))

        # Filtro por subregistros de edad
        today = date.today()
        # Usamos CURDATE() directamente desde SQL
        edad_expr = text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE())")

        subregistro_filters = []

        if subregistro_1:
            subregistro_filters.append(text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 0 AND 3"))
        if subregistro_2:
            subregistro_filters.append(text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 4 AND 7"))
        if subregistro_3:
            subregistro_filters.append(text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 8 AND 12"))
        if subregistro_4:
            subregistro_filters.append(text("TIMESTAMPDIFF(YEAR, nna.nna_fecha_nacimiento, CURDATE()) BETWEEN 13 AND 17"))

        if subregistro_filters:
            query = query.filter(or_(*subregistro_filters))


        if subregistro_5a:
            query = query.filter(Nna.nna_subregistro_salud.ilike("%a%"))
        if subregistro_5b:
            query = query.filter(Nna.nna_subregistro_salud.ilike("%b%"))
        if subregistro_5c:
            query = query.filter(Nna.nna_subregistro_salud.ilike("%c%"))


        subquery_nnas_en_carpeta = db.query(DetalleNNAEnCarpeta.nna_id).distinct()

        if disponible is not None:
            if disponible:
                # Solo los NNAs que NO están en una carpeta
                query = query.filter(~Nna.nna_id.in_(subquery_nnas_en_carpeta))
            else:
                # Solo los NNAs que SÍ están en una carpeta
                query = query.filter(Nna.nna_id.in_(subquery_nnas_en_carpeta))


        # Paginación con total
        total_records = query.count()
        total_pages = max((total_records // limit) + (1 if total_records % limit > 0 else 0), 1)

        if page > total_pages:
            return {
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "total_records": total_records,
                "nnas": []
            }

        skip = (page - 1) * limit
        nnas = query.offset(skip).limit(limit).all()


        subquery_ids = [row[0] for row in subquery_nnas_en_carpeta.all()]

        nnas_list = []
        for nna in nnas:
            # Calcular edad
            nacimiento = nna.nna_fecha_nacimiento
            today = date.today()
            edad = today.year - nacimiento.year - ((today.month, today.day) < (nacimiento.month, nacimiento.day))

            nnas_list.append({
                "nna_id": nna.nna_id,
                "nna_nombre": nna.nna_nombre,
                "nna_apellido": nna.nna_apellido,
                "nna_dni": nna.nna_dni,
                "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
                "nna_edad": edad_como_texto(nna.nna_fecha_nacimiento),
                "nna_calle_y_nro": nna.nna_calle_y_nro,
                "nna_barrio": nna.nna_barrio,
                "nna_localidad": nna.nna_localidad,
                "nna_provincia": nna.nna_provincia,
                "nna_subregistro_salud": nna.nna_subregistro_salud,
                "nna_en_convocatoria": nna.nna_en_convocatoria,
                "nna_ficha": nna.nna_ficha,
                "nna_sentencia": nna.nna_sentencia,
                "nna_archivado": nna.nna_archivado,
                "nna_disponible": nna.nna_id not in subquery_ids
            })

        return {
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "total_records": total_records,
            "nnas": nnas_list,
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar NNAs: {str(e)}")




@nna_router.get("/{nna_id}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def get_nna_by_id(nna_id: int, db: Session = Depends(get_db)):
    """
    Devuelve un único NNA por su `nna_id`.
    """
    try:
        nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
        if not nna:
            raise HTTPException(status_code=404, detail="NNA no encontrado")

        # Verificar si el NNA está en alguna carpeta
        asignado_a_carpeta = db.query(DetalleNNAEnCarpeta).filter( DetalleNNAEnCarpeta.nna_id == nna_id ).first()

        esta_disponible = asignado_a_carpeta is None


        return {
            "nna_id": nna.nna_id,
            "nna_nombre": nna.nna_nombre,
            "nna_apellido": nna.nna_apellido,
            "nna_dni": nna.nna_dni,
            "nna_fecha_nacimiento": nna.nna_fecha_nacimiento,
            "nna_calle_y_nro": nna.nna_calle_y_nro,
            "nna_barrio": nna.nna_barrio,
            "nna_localidad": nna.nna_localidad,
            "nna_provincia": nna.nna_provincia,
            "nna_subregistro_salud": nna.nna_subregistro_salud,
            "nna_en_convocatoria": nna.nna_en_convocatoria,
            "nna_ficha": nna.nna_ficha,
            "nna_sentencia": nna.nna_sentencia,
            "nna_archivado": nna.nna_archivado,
            "nna_disponible": esta_disponible
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
            "nna_subregistro_salud", "nna_en_convocatoria", "nna_archivado"
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
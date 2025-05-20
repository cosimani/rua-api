from fastapi import APIRouter, HTTPException, Depends, status, Request, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import case

from typing import List, Optional, Literal
from datetime import datetime, date

from database.config import get_db
from security.security import verify_api_key, require_roles, get_current_user

from models.carpeta import Carpeta, DetalleProyectosEnCarpeta, DetalleNNAEnCarpeta
from models.proyecto import Proyecto
from models.users import User
from models.eventos_y_configs import RuaEvento
from fastapi.responses import FileResponse, JSONResponse
import os
import tempfile, shutil

from dotenv import load_dotenv
import fitz  # PyMuPDF
from PIL import Image
import subprocess
from pathlib import Path





# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener y validar la variable
DIR_PDF_GENERADOS = os.getenv("DIR_PDF_GENERADOS")

if not DIR_PDF_GENERADOS:
    raise RuntimeError("La variable de entorno DIR_PDF_GENERADOS no está definida. Verificá tu archivo .env")

# Crear la carpeta si no existe
os.makedirs(DIR_PDF_GENERADOS, exist_ok=True)




carpetas_router = APIRouter()




@carpetas_router.get("/", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def listar_carpetas(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge = 1),
    limit: int = Query(10, ge = 1, le = 100),
):
    try:

        orden_estado = case(
            (Carpeta.estado_carpeta == "vacia", 1),
            (Carpeta.estado_carpeta == "preparando_carpeta", 2),
            (Carpeta.estado_carpeta == "enviada_a_juzgado", 3),
            (Carpeta.estado_carpeta == "proyecto_seleccionado", 4),
            else_=5
        )

        query = db.query(Carpeta).order_by(orden_estado, Carpeta.fecha_creacion.asc())
        

        total = query.count()
        carpetas = query.offset((page - 1) * limit).limit(limit).all()

        resultado = []
        for carpeta in carpetas:
            proyectos = []
            for dp in carpeta.detalle_proyectos:
                p = dp.proyecto
                if p:
                    proyectos.append({
                        "proyecto_id": p.proyecto_id,
                        "nro_orden_rua": p.nro_orden_rua,
                        "proyecto_tipo": p.proyecto_tipo,
                        "estado_general": p.estado_general,
                        "proyecto_localidad": p.proyecto_localidad,
                        "proyecto_provincia": p.proyecto_provincia,
                        "login_1": p.login_1,
                        "login_1_name": f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else None,
                        "login_2": p.login_2,
                        "login_2_name": f"{p.usuario_2.nombre} {p.usuario_2.apellido}" if p.usuario_2 else None,
                        "fecha_asignacion": dp.fecha_asignacion
                    })

            

            proyectos = []
            proyectos_resumen = []

            for dp in carpeta.detalle_proyectos:
                p = dp.proyecto
                if p:
                    proyectos.append({
                        "proyecto_id": p.proyecto_id,
                        "nro_orden_rua": p.nro_orden_rua,
                        "proyecto_tipo": p.proyecto_tipo,
                        "estado_general": p.estado_general,
                        "proyecto_localidad": p.proyecto_localidad,
                        "proyecto_provincia": p.proyecto_provincia,
                        "login_1": p.login_1,
                        "login_1_name": f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else None,
                        "login_2": p.login_2,
                        "login_2_name": f"{p.usuario_2.nombre} {p.usuario_2.apellido}" if p.usuario_2 else None,
                        "fecha_asignacion": dp.fecha_asignacion
                    })

                    # resumen de nombres
                    if p.proyecto_tipo in ["Matrimonio", "Unión convivencial"]:
                        nombre_1 = f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else "-"
                        nombre_2 = f"{p.usuario_2.nombre} {p.usuario_2.apellido}" if p.usuario_2 else "-"
                        proyectos_resumen.append(f"{nombre_1} y {nombre_2}")
                    else:  # Monoparental
                        nombre_1 = f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else "-"
                        proyectos_resumen.append(nombre_1)

            nnas = []
            nnas_resumen = []

            for dnna in carpeta.detalle_nna:
                n = dnna.nna
                if n:
                    edad = None
                    if n.nna_fecha_nacimiento:
                        hoy = date.today()
                        edad = hoy.year - n.nna_fecha_nacimiento.year - ((hoy.month, hoy.day) < (n.nna_fecha_nacimiento.month, n.nna_fecha_nacimiento.day))

                    nnas.append({
                        "nna_id": n.nna_id,
                        "nna_nombre": n.nna_nombre,
                        "nna_apellido": n.nna_apellido,
                        "nna_dni": n.nna_dni,
                        "nna_fecha_nacimiento": n.nna_fecha_nacimiento,
                        "nna_edad": edad,
                        "nna_localidad": n.nna_localidad,
                        "nna_provincia": n.nna_provincia,
                        "nna_en_convocatoria": n.nna_en_convocatoria,
                        "nna_archivado": n.nna_archivado,
                    })

                    nombre_completo = f"{n.nna_nombre} {n.nna_apellido}"
                    if edad is not None:
                        nnas_resumen.append(f"{nombre_completo} ({edad} años)")
                    else:
                        nnas_resumen.append(nombre_completo)


            nnas = []
            for dnna in carpeta.detalle_nna:
                n = dnna.nna
                if n:
                    edad = None
                    if n.nna_fecha_nacimiento:
                        hoy = date.today()
                        edad = hoy.year - n.nna_fecha_nacimiento.year - ((hoy.month, hoy.day) < (n.nna_fecha_nacimiento.month, n.nna_fecha_nacimiento.day))

                    nnas.append({
                        "nna_id": n.nna_id,
                        "nna_nombre": n.nna_nombre,
                        "nna_apellido": n.nna_apellido,
                        "nna_dni": n.nna_dni,
                        "nna_fecha_nacimiento": n.nna_fecha_nacimiento,
                        "nna_edad": edad,
                        "nna_localidad": n.nna_localidad,
                        "nna_provincia": n.nna_provincia,
                        "nna_en_convocatoria": n.nna_en_convocatoria,
                        "nna_archivado": n.nna_archivado,
                    })

      
            estado_carpeta_map = {
                "vacia": "Vacía",
                "preparando_carpeta": "En preparación",
                "enviada_a_juzgado": "En juzgado",
                "proyecto_seleccionado": "Con dictamen"
            }


            resultado.append({
                "carpeta_id": carpeta.carpeta_id,
                "fecha_creacion": carpeta.fecha_creacion,
                "estado_carpeta": estado_carpeta_map.get(carpeta.estado_carpeta, carpeta.estado_carpeta),
                "proyectos": proyectos,
                "nnas": nnas,
                "proyectos_resumen": proyectos_resumen,
                "nnas_resumen": nnas_resumen
            })


        return {
            "page": page,
            "limit": limit,
            "total": total,
            "carpetas": resultado
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code = 500, detail = f"Error al listar carpetas: {str(e)}")




@carpetas_router.post("/", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def crear_carpeta(
    data: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    📁 Crea una nueva carpeta y puede asociarla a uno o más proyectos y/o NNAs.

    🔒 Solo pueden realizar esta acción las usuarias con rol `supervisora` o `administrador`.

    ### 📝 Campos opcionales en el JSON:
    - **proyectos_id**: `List[int]` – Lista de IDs de proyectos a asociar
    - **nna_id**: `List[int]` – Lista de IDs de NNAs a asociar

    ⚠️ Al menos uno de los dos campos debe estar presente.

    ### 📦 Ejemplo de JSON de entrada:

    ```json
    {
        "proyectos_id": [541, 542],
        "nna_id": [15, 16]
    }
    ```
    """
    try:
        proyectos_id = data.get("proyectos_id", [])
        nna_id = data.get("nna_id", [])

        if not proyectos_id and not nna_id:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe proporcionar al menos 'proyectos_id' o 'nna_id'",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        nueva_carpeta = Carpeta(
            fecha_creacion = datetime.now().date(),
            estado_carpeta = "preparando_carpeta"
        )
        db.add(nueva_carpeta)
        db.commit()
        db.refresh(nueva_carpeta)

        # Asociar proyectos
        for proyecto_id in proyectos_id:
            detalle = DetalleProyectosEnCarpeta(
                carpeta_id = nueva_carpeta.carpeta_id,
                proyecto_id = proyecto_id,
                fecha_asignacion = datetime.now().date()
            )
            db.add(detalle)

        # Asociar NNAs
        for id_nna in nna_id:
            detalle_nna = DetalleNNAEnCarpeta(
                carpeta_id = nueva_carpeta.carpeta_id,
                nna_id = id_nna
            )
            db.add(detalle_nna)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Carpeta creada exitosamente",
            "tiempo_mensaje": 5,
            "next_page": "actual",
            "carpeta_id": nueva_carpeta.carpeta_id
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al crear carpeta: {str(e)}",
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }




@carpetas_router.put("/{carpeta_id}", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def actualizar_carpeta(
    carpeta_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)  # Usá tu método real para obtener el usuario
):
    """
    🔄 Actualiza los proyectos y NNAs asociados a una carpeta.

    ### JSON de entrada:
    {
        "proyectos_id": [1, 2],
        "nna_id": [10, 11]
    }
    """
    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
        if not carpeta:
            raise HTTPException(status_code = 404, detail = "Carpeta no encontrada")

        nuevos_proyectos = set(data.get("proyectos_id", []))
        nuevos_nnas = set(data.get("nna_id", []))

        # Eliminar asignaciones actuales
        db.query(DetalleProyectosEnCarpeta).filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta_id).delete()
        db.query(DetalleNNAEnCarpeta).filter(DetalleNNAEnCarpeta.carpeta_id == carpeta_id).delete()

        # Insertar nuevas asignaciones
        for proyecto_id in nuevos_proyectos:
            db.add(DetalleProyectosEnCarpeta(
                carpeta_id = carpeta_id,
                proyecto_id = proyecto_id,
                fecha_asignacion = datetime.now().date()
            ))

        for nna_id in nuevos_nnas:
            db.add(DetalleNNAEnCarpeta(
                carpeta_id = carpeta_id,
                nna_id = nna_id
            ))

        # Actualizar estado de la carpeta según asignaciones
        if not nuevos_proyectos and not nuevos_nnas:
            carpeta.estado_carpeta = "vacia"
        else:
            carpeta.estado_carpeta = "preparando_carpeta"

        # Agregar evento
        detalle_str = f"Actualización de carpeta ID {carpeta_id} (proyectos: {list(nuevos_proyectos)}, nnas: {list(nuevos_nnas)})"
        detalle_str = (detalle_str[:252] + "...") if len(detalle_str) > 255 else detalle_str

        evento = RuaEvento(
            evento_detalle = detalle_str,
            evento_fecha = datetime.now(),
            login = current_user["user"]["login"]
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Carpeta actualizada correctamente",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al actualizar carpeta: {str(e)}",
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }


@carpetas_router.delete("/{carpeta_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def eliminar_carpeta(
    carpeta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    🗑️ Elimina una carpeta solo si su estado es 'vacia'.

    🔒 Requiere rol de administradora o supervisora.
    """
    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
        if not carpeta:
            raise HTTPException(status_code=404, detail="Carpeta no encontrada")

        if carpeta.estado_carpeta != "vacia":
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Solo se pueden eliminar carpetas vacías.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Eliminar relaciones si existieran (precaución defensiva)
        db.query(DetalleProyectosEnCarpeta).filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta_id).delete()
        db.query(DetalleNNAEnCarpeta).filter(DetalleNNAEnCarpeta.carpeta_id == carpeta_id).delete()
        db.delete(carpeta)

        # Registrar evento
        evento = RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se eliminó la carpeta ID {carpeta_id}",
            evento_fecha=datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Carpeta eliminada correctamente.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al eliminar carpeta: {str(e)}",
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }



@carpetas_router.put("/{carpeta_id}/enviar-a-juzgado")
def enviar_a_juzgado(carpeta_id: int, db: Session = Depends(get_db)):
    carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
    if not carpeta:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Carpeta no encontrada",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    if not carpeta.detalle_nna or len(carpeta.detalle_nna) == 0:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "No se puede enviar la carpeta al juzgado. Debe tener al menos un NNA asignado.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    if not carpeta.detalle_proyectos or len(carpeta.detalle_proyectos) == 0:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "No se puede enviar la carpeta al juzgado. Debe tener al menos un proyecto asignado.",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }

    carpeta.estado_carpeta = "enviada_a_juzgado"
    db.commit()

    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "Carpeta enviada correctamente al juzgado",
        "tiempo_mensaje": 5,
        "next_page": "actual"
    }





@carpetas_router.put("/{carpeta_id}/marcar-con-dictamen")
def marcar_con_dictamen(carpeta_id: int, db: Session = Depends(get_db)):
    carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
    if not carpeta:
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    carpeta.estado_carpeta = "proyecto_seleccionado"
    db.commit()
    return {"success": True, "mensaje": "Carpeta marcada como con dictamen"}




@carpetas_router.get("/{carpeta_id}/descargar-pdf", response_class=FileResponse,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def descargar_pdf_carpeta_completa(
    carpeta_id: int,
    db: Session = Depends(get_db)
):
    carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
    if not carpeta:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "La carpeta solicitada no fue encontrada.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }
        )
        

    try:
        # Ruta de salida final
        output_path = os.path.join(DIR_PDF_GENERADOS, f"carpeta_{carpeta_id}_documentos_combinados.pdf")
        pdf_paths = []

        def agregar_documentos(modelo, campos: List[str]):
            for campo in campos:
                ruta = getattr(modelo, campo, None)
                if ruta and os.path.exists(ruta):
                    ext = os.path.splitext(ruta)[1].lower()
                    nombre_base = f"{modelo.__class__.__name__.lower()}_{campo}_{os.path.basename(ruta)}"
                    out_pdf = os.path.join(DIR_PDF_GENERADOS, nombre_base + ".pdf")

                    if ext == ".pdf":
                        shutil.copy(ruta, out_pdf)
                        pdf_paths.append(out_pdf)
                    elif ext in [".jpg", ".jpeg", ".png"]:
                        Image.open(ruta).convert("RGB").save(out_pdf)
                        pdf_paths.append(out_pdf)
                    elif ext in [".doc", ".docx"]:
                        subprocess.run([
                            "libreoffice", "--headless", "--convert-to", "pdf", "--outdir", DIR_PDF_GENERADOS, ruta
                        ], check=True)
                        converted = os.path.join(DIR_PDF_GENERADOS, os.path.splitext(os.path.basename(ruta))[0] + ".pdf")
                        if os.path.exists(converted):
                            pdf_paths.append(converted)

        # Documentos de proyectos y adoptantes
        for dp in carpeta.detalle_proyectos:
            proyecto = dp.proyecto
            if not proyecto:
                continue

            agregar_documentos(proyecto, [
                "doc_proyecto_convivencia_o_estado_civil",
                "informe_profesionales"
            ])

            for login in [proyecto.login_1, proyecto.login_2]:
                if login:
                    user = db.query(User).filter(User.login == login).first()
                    if user:
                        agregar_documentos(user, [
                            "doc_adoptante_domicilio", "doc_adoptante_dni_frente", "doc_adoptante_dni_dorso",
                            "doc_adoptante_deudores_alimentarios", "doc_adoptante_antecedentes",
                            "doc_adoptante_migraciones", "doc_adoptante_salud"
                        ])

        # Documentos de NNAs
        for dnna in carpeta.detalle_nna:
            nna = dnna.nna
            if nna:
                agregar_documentos(nna, [
                    "doc_dni_frente", "doc_dni_dorso", "doc_certificado_nacimiento", "doc_certificado_discapacidad"
                ])

        if not pdf_paths:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": "No se encontraron documentos disponibles para combinar en esta carpeta.",
                    "tiempo_mensaje": 6,
                    "next_page": "actual"
                }
            )


 
        # Fusionar todos los PDF
        merged = fitz.open()
        for path in pdf_paths:
            with fitz.open(path) as doc:
                merged.insert_pdf(doc)
        merged.save(output_path)

        return FileResponse(
            path=output_path,
            filename=f"documentos_carpeta_{carpeta_id}.pdf",
            media_type="application/pdf"
        )

    except Exception as e:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"Ocurrió un error al generar el PDF combinado: {str(e)}",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }
        )




@carpetas_router.put("/{carpeta_id}/seleccionar-proyecto", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def seleccionar_proyecto(
    carpeta_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    🔄 Selecciona un único proyecto para esta carpeta.

    📥 JSON esperado:
    {
        "proyectos_id": [2]
    }
    """
    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
        if not carpeta:
            raise HTTPException(status_code = 404, detail = "Carpeta no encontrada")

        nuevos_proyectos = set(data.get("proyectos_id", []))

        if len(nuevos_proyectos) != 1:
            raise HTTPException(status_code = 400, detail = "Debe seleccionar exactamente un proyecto.")

        # Eliminar asignaciones anteriores
        db.query(DetalleProyectosEnCarpeta).filter(
            DetalleProyectosEnCarpeta.carpeta_id == carpeta_id
        ).delete()

        # Insertar nueva asignación
        for proyecto_id in nuevos_proyectos:
            db.add(DetalleProyectosEnCarpeta(
                carpeta_id = carpeta_id,
                proyecto_id = proyecto_id,
                fecha_asignacion = datetime.now().date()
            ))

        carpeta.estado_carpeta = "proyecto_seleccionado"

        # Registrar evento
        detalle_str = f"Proyecto seleccionado para carpeta ID {carpeta_id}: {list(nuevos_proyectos)}"
        if len(detalle_str) > 255:
            detalle_str = detalle_str[:252] + "..."

        evento = RuaEvento(
            login = current_user["user"]["login"],
            evento_detalle = detalle_str,
            evento_fecha = datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "Carpeta actualizada correctamente.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Error al actualizar carpeta: {str(e)}",
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }

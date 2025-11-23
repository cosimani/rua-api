from fastapi import APIRouter, HTTPException, Depends, status, Request, Query, Body
from sqlalchemy.orm import Session, joinedload, aliased
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import case

from sqlalchemy import and_, func, or_, text, literal_column

from typing import List, Optional, Literal
from datetime import datetime, date

from database.config import get_db
from security.security import verify_api_key, require_roles, get_current_user

from helpers.utils import construir_subregistro_string


from models.carpeta import Carpeta, DetalleProyectosEnCarpeta, DetalleNNAEnCarpeta
from models.proyecto import Proyecto, ProyectoHistorialEstado
from models.users import User
from models.nna import Nna, NnaHistorialEstado
from models.eventos_y_configs import RuaEvento
from fastapi.responses import FileResponse, JSONResponse
import tempfile, shutil

from zipfile import ZipFile
import os, shutil, fitz, subprocess
from PIL import Image
from io import BytesIO

from dotenv import load_dotenv
from PIL import Image
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




@carpetas_router.get("/", response_model=dict,
    dependencies=[Depends(verify_api_key), 
                  Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def listar_carpetas(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    busqueda_rapida: Optional[str] = Query(None),
    estado_filtro: Optional[str] = Query(None),
    estado_proyecto_filtro: Optional[str] = Query(None),
    ):

    try:

        # ✅ Aliased para usuarios dentro del endpoint
        User1 = aliased(User)
        User2 = aliased(User)

        orden_estado = case(
            (Carpeta.estado_carpeta == "vacia", 1),
            (Carpeta.estado_carpeta == "preparando_carpeta", 2),
            (Carpeta.estado_carpeta == "enviada_a_juzgado", 3),
            (Carpeta.estado_carpeta == "proyecto_seleccionado", 4),
            else_=5
        )

        query = db.query(Carpeta).order_by(
            orden_estado,
            Carpeta.carpeta_id.desc()
        )

        joined_proyectos = False

        # 🔍 Filtro por estado de carpeta
        if estado_filtro and estado_filtro != "todos":
            estado_db_map = {
                "Vacía": "vacia",
                "En preparación": "preparando_carpeta",
                "En juzgado": "enviada_a_juzgado",
                "Con dictamen": "proyecto_seleccionado",
            }
            estado_db_value = estado_db_map.get(estado_filtro, estado_filtro)
            query = query.filter(Carpeta.estado_carpeta == estado_db_value)

        # 🔍 Filtro por estado de proyecto
        if estado_proyecto_filtro and estado_proyecto_filtro != "todos":
            if not joined_proyectos:
                query = query \
                    .outerjoin(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id) \
                    .outerjoin(Proyecto, DetalleProyectosEnCarpeta.proyecto_id == Proyecto.proyecto_id)
                joined_proyectos = True

            query = query.filter(Proyecto.estado_general == estado_proyecto_filtro)


        # 🔍 Búsqueda rápida
        if busqueda_rapida and len(busqueda_rapida.strip()) >= 3:
            palabras = busqueda_rapida.strip().split()
            condiciones_por_palabra = []

            # ✅ Joins necesarios para búsqueda
            query = query \
                .outerjoin(DetalleNNAEnCarpeta, DetalleNNAEnCarpeta.carpeta_id == Carpeta.carpeta_id) \
                .outerjoin(Nna, DetalleNNAEnCarpeta.nna_id == Nna.nna_id)

            if not joined_proyectos:
                query = query \
                    .outerjoin(DetalleProyectosEnCarpeta, DetalleProyectosEnCarpeta.carpeta_id == Carpeta.carpeta_id) \
                    .outerjoin(Proyecto, DetalleProyectosEnCarpeta.proyecto_id == Proyecto.proyecto_id)
                joined_proyectos = True

            query = query \
                .outerjoin(User1, User1.login == Proyecto.login_1) \
                .outerjoin(User2, User2.login == Proyecto.login_2)

            for palabra in palabras:
                patron = f"%{palabra}%"
                condiciones_por_palabra.append(
                    or_(
                        Nna.nna_nombre.ilike(patron),
                        Nna.nna_apellido.ilike(patron),
                        Nna.nna_dni.ilike(patron),
                        Proyecto.nro_orden_rua.ilike(patron),
                        Proyecto.login_1.ilike(patron),
                        Proyecto.login_2.ilike(patron),
                        User1.nombre.ilike(patron),
                        User1.apellido.ilike(patron),
                        User1.login.ilike(patron),
                        User2.nombre.ilike(patron),
                        User2.apellido.ilike(patron),
                        User2.login.ilike(patron),
                    )
                )

            query = query.filter(and_(*condiciones_por_palabra)).distinct(Carpeta.carpeta_id)

        total = query.count()
        carpetas = query.offset((page - 1) * limit).limit(limit).all()


        resultado = []
        for carpeta in carpetas:
            proyectos = []
            proyectos_resumen = []

            for dp in carpeta.detalle_proyectos:
                p = dp.proyecto
                if p:
                    proyectos.append({
                        "proyecto_id": p.proyecto_id,
                        "nro_orden_rua": p.nro_orden_rua,
                        "proyecto_tipo": p.proyecto_tipo,
                        "subregistro_string": construir_subregistro_string(p),
                        "estado_general": p.estado_general,
                        "proyecto_localidad": p.proyecto_localidad,
                        "login_1": p.login_1,
                        "login_1_name": f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else None,
                        "login_2": p.login_2,
                        "login_2_name": f"{p.usuario_2.nombre} {p.usuario_2.apellido}" if p.usuario_2 else None,
                        "fecha_asignacion": dp.fecha_asignacion,
                        "doc_dictamen": p.doc_dictamen,
                    })

                    if p.proyecto_tipo in ["Matrimonio", "Unión convivencial"]:
                        nombre_1 = f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else "-"
                        nombre_2 = f"{p.usuario_2.nombre} {p.usuario_2.apellido}" if p.usuario_2 else "-"
                        proyectos_resumen.append(f"{nombre_1} y {nombre_2}")
                    else:
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
                    nnas_resumen.append(f"{nombre_completo} ({edad} años)" if edad is not None else nombre_completo)

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
        raise HTTPException(status_code=500, detail=f"Error al listar carpetas: {str(e)}")



@carpetas_router.get("/{carpeta_id}", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora", "profesional"]))])
def obtener_carpeta(
    carpeta_id: int,
    db: Session = Depends(get_db)
    ):

    """
    📁 Obtiene los detalles completos de una carpeta por su ID.
    """
    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
        if not carpeta:
            raise HTTPException(status_code=404, detail="Carpeta no encontrada")

        # Procesar proyectos
        proyectos = []
        for dp in carpeta.detalle_proyectos:
            p = dp.proyecto
            if p:
                proyectos.append({
                    "proyecto_id": p.proyecto_id,
                    "nro_orden_rua": p.nro_orden_rua,
                    "proyecto_tipo": p.proyecto_tipo,
                    "subregistro_string": construir_subregistro_string(p),
                    "estado_general": p.estado_general,
                    "proyecto_localidad": p.proyecto_localidad,
                    "login_1": p.login_1,
                    "login_1_name": f"{p.usuario_1.nombre} {p.usuario_1.apellido}" if p.usuario_1 else None,
                    "login_2": p.login_2,
                    "login_2_name": f"{p.usuario_2.nombre} {p.usuario_2.apellido}" if p.usuario_2 else None,
                    "fecha_asignacion": dp.fecha_asignacion,
                    "doc_dictamen": p.doc_dictamen,
                })

        # Procesar NNAs
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

        return {
            "success": True,
            "carpeta": {
                "carpeta_id": carpeta.carpeta_id,
                "fecha_creacion": carpeta.fecha_creacion,
                "estado_carpeta": estado_carpeta_map.get(carpeta.estado_carpeta, carpeta.estado_carpeta),
                "proyectos": proyectos,
                "nnas": nnas
            }
        }

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener carpeta: {str(e)}")




@carpetas_router.post("/", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
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
        proyectos_ids = data.get("proyectos_id", [])
        nnas_ids = data.get("nna_id", [])

        if not proyectos_ids and not nnas_ids:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Debe proporcionar al menos un proyecto o un NNA",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # 1. Validación de proyectos
        proyectos_invalidos = db.query(Proyecto).filter(
            Proyecto.proyecto_id.in_(proyectos_ids),
            Proyecto.estado_general != 'viable'
        ).all()
        if proyectos_invalidos:
            ids_invalidos = [p.proyecto_id for p in proyectos_invalidos]
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"Los siguientes proyectos no están en estado 'viable': {ids_invalidos}",
                "tiempo_mensaje": 8,
                "next_page": "actual"
            }

        # 2. Validación de NNA
        estados_permitidos = [
            'sin_ficha_sin_sentencia',
            'con_ficha_sin_sentencia',
            'sin_ficha_con_sentencia',
            'disponible'
        ]
        nnas_invalidos = db.query(Nna).filter(
            Nna.nna_id.in_(nnas_ids),
            Nna.nna_estado.notin_(estados_permitidos)
        ).all()
        if nnas_invalidos:
            ids_invalidos = [n.nna_id for n in nnas_invalidos]
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"Los siguientes NNA no están en un estado permitido: {ids_invalidos}",
                "tiempo_mensaje": 8,
                "next_page": "actual"
            }

        # 3. Crear carpeta
        nueva_carpeta = Carpeta(
            fecha_creacion = datetime.now().date(),
            estado_carpeta = "preparando_carpeta"
        )
        db.add(nueva_carpeta)
        db.commit()
        db.refresh(nueva_carpeta)

        # 4. Asociar proyectos
        for proyecto_id in proyectos_ids:
            detalle = DetalleProyectosEnCarpeta(
                carpeta_id=nueva_carpeta.carpeta_id,
                proyecto_id=proyecto_id,
                fecha_asignacion=datetime.now().date()
            )
            db.add(detalle)

            # Actualizar estado del proyecto
            proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
            if proyecto:
                estado_anterior = proyecto.estado_general
                proyecto.estado_general = "en_carpeta"
                proyecto.ultimo_cambio_de_estado = datetime.now().date()

                historial = ProyectoHistorialEstado(
                    proyecto_id=proyecto.proyecto_id,
                    estado_anterior=estado_anterior,
                    estado_nuevo="en_carpeta",
                    fecha_hora=datetime.now()
                )
                db.add(historial)

        # 5. Asociar NNAs
        for id_nna in nnas_ids:
            detalle_nna = DetalleNNAEnCarpeta(
                carpeta_id = nueva_carpeta.carpeta_id,
                nna_id = id_nna
            )
            db.add(detalle_nna)

            # Actualizar estado del NNA
            nna = db.query(Nna).filter(Nna.nna_id == id_nna).first()
            if nna:
                nna.nna_estado = 'preparando_carpeta'

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
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
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


        # Obtener los proyectos actualmente asociados antes de eliminar
        proyectos_previos = db.query(DetalleProyectosEnCarpeta.proyecto_id)\
            .filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta_id)\
            .all()
        proyectos_previos = {p.proyecto_id for p in proyectos_previos}


        # Validar proyectos nuevos que NO estuvieran antes y no estén en estado viable
        proyectos_a_validar = nuevos_proyectos - proyectos_previos

        if proyectos_a_validar:
            proyectos_invalidos = db.query(Proyecto).filter(
                Proyecto.proyecto_id.in_(proyectos_a_validar),
                Proyecto.estado_general != 'viable'
            ).all()

            if proyectos_invalidos:
                ids_invalidos = [p.proyecto_id for p in proyectos_invalidos]
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Los siguientes proyectos no están en estado 'viable': {ids_invalidos}",
                    "tiempo_mensaje": 8,
                    "next_page": "actual"
                }


        
        # # Validación previa antes de eliminar y volver a insertar
        # # Validar proyectos nuevos
        # proyectos_invalidos = db.query(Proyecto).filter(
        #     Proyecto.proyecto_id.in_(nuevos_proyectos),
        #     Proyecto.estado_general != 'viable'
        # ).all()

        # if proyectos_invalidos:
        #     ids_invalidos = [p.proyecto_id for p in proyectos_invalidos]
        #     return {
        #         "success": False,
        #         "tipo_mensaje": "rojo",
        #         "mensaje": f"Los siguientes proyectos no están en estado 'viable': {ids_invalidos}",
        #         "tiempo_mensaje": 8,
        #         "next_page": "actual"
        #     }

        # Validar NNA nuevos
        estados_permitidos = [
            'sin_ficha_sin_sentencia',
            'con_ficha_sin_sentencia',
            'sin_ficha_con_sentencia',
            'disponible'
        ]

        # Obtener NNA actualmente asociados antes de eliminar
        nnas_previos = db.query(DetalleNNAEnCarpeta.nna_id)\
            .filter(DetalleNNAEnCarpeta.carpeta_id == carpeta_id)\
            .all()
        nnas_previos = {n.nna_id for n in nnas_previos}

        # Validar solo NNAs nuevos que no estuvieran antes
        nnas_a_validar = nuevos_nnas - nnas_previos

        if nnas_a_validar:
            nnas_invalidos = db.query(Nna).filter(
                Nna.nna_id.in_(nnas_a_validar),
                Nna.nna_estado.notin_(estados_permitidos)
            ).all()
            if nnas_invalidos:
                ids_invalidos = [n.nna_id for n in nnas_invalidos]
                return {
                    "success": False,
                    "tipo_mensaje": "rojo",
                    "mensaje": f"Los siguientes NNA no están en un estado permitido: {ids_invalidos}",
                    "tiempo_mensaje": 8,
                    "next_page": "actual"
                }
            
        # nnas_invalidos = db.query(Nna).filter(
        #     Nna.nna_id.in_(nuevos_nnas),
        #     Nna.nna_estado.notin_(estados_permitidos)
        # ).all()
        # if nnas_invalidos:
        #     ids_invalidos = [n.nna_id for n in nnas_invalidos]
        #     return {
        #         "success": False,
        #         "tipo_mensaje": "rojo",
        #         "mensaje": f"Los siguientes NNA no están en un estado permitido: {ids_invalidos}",
        #         "tiempo_mensaje": 8,
        #         "next_page": "actual"
        #     }


        


        # Eliminar asignaciones actuales
        db.query(DetalleProyectosEnCarpeta).filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta_id).delete()
        db.query(DetalleNNAEnCarpeta).filter(DetalleNNAEnCarpeta.carpeta_id == carpeta_id).delete()


        # Proyectos que se eliminaron
        proyectos_eliminados = proyectos_previos - nuevos_proyectos

        # Actualizar estado de proyectos eliminados
        for proyecto_id in proyectos_eliminados:
            proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
            if proyecto:
                estado_anterior = proyecto.estado_general
                proyecto.estado_general = "viable"  # o "aprobado", o lo que corresponda
                proyecto.ultimo_cambio_de_estado = datetime.now().date()

                db.add(ProyectoHistorialEstado(
                    proyecto_id=proyecto_id,
                    estado_anterior=estado_anterior,
                    estado_nuevo="viable",
                    fecha_hora=datetime.now()
                ))


        

        # NNAs eliminados
        nnas_eliminados = nnas_previos - nuevos_nnas

        # Actualizar estado de NNAs eliminados
        for nna_id in nnas_eliminados:
            nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
            if nna:
                nna.nna_estado = 'disponible'



        # Insertar nuevas asignaciones y cambiar estado a 'en_carpeta'
        for proyecto_id in nuevos_proyectos:
            db.add(DetalleProyectosEnCarpeta(
                carpeta_id=carpeta_id,
                proyecto_id=proyecto_id,
                fecha_asignacion=datetime.now().date()
            ))

            proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyecto_id).first()
            if proyecto:
                estado_anterior = proyecto.estado_general
                proyecto.estado_general = "en_carpeta"
                proyecto.ultimo_cambio_de_estado = datetime.now().date()
                
                db.add(ProyectoHistorialEstado(
                    proyecto_id=proyecto_id,
                    estado_anterior=estado_anterior,
                    estado_nuevo="en_carpeta",
                    fecha_hora=datetime.now()
                ))


        for nna_id in nuevos_nnas:
            db.add(DetalleNNAEnCarpeta(
                carpeta_id = carpeta_id,
                nna_id = nna_id
            ))

            nna = db.query(Nna).filter(Nna.nna_id == nna_id).first()
            if nna:
                nna.nna_estado = 'preparando_carpeta'


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
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def eliminar_carpeta(
    carpeta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    🗑️ Elimina una carpeta solo si su estado es 'vacia' y no tiene NNAs ni proyectos asociados.

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

        # ✅ Verificar que no tenga NNAs asociados
        if carpeta.detalle_nna and len(carpeta.detalle_nna) > 0:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "No se puede eliminar la carpeta porque tiene NNAs asociados.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # ✅ Verificar que no tenga proyectos asociados
        if carpeta.detalle_proyectos and len(carpeta.detalle_proyectos) > 0:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "No se puede eliminar la carpeta porque tiene proyectos asociados.",
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

    # Cambiar estado de carpeta
    carpeta.estado_carpeta = "enviada_a_juzgado"

    # Armar nombres de NNAs
    nombres_nnas = [
        f"{dnna.nna.nna_nombre} {dnna.nna.nna_apellido}"
        for dnna in carpeta.detalle_nna
        if dnna.nna
    ]
    nombres_str = ", ".join(nombres_nnas)

    # Armar historial para cada proyecto
    proyectos = [dp.proyecto for dp in carpeta.detalle_proyectos if dp.proyecto]
    total_proyectos = len(proyectos)

    for proyecto in proyectos:
        otros = total_proyectos - 1
        if otros == 0:
            texto_proyectos = "sin otros proyectos en la carpeta"
        elif otros == 1:
            texto_proyectos = "junto a otro proyecto"
        else:
            texto_proyectos = f"junto a otros {otros} proyectos"

        historial = ProyectoHistorialEstado(
            proyecto_id=proyecto.proyecto_id,
            estado_anterior=proyecto.estado_general,
            estado_nuevo="enviada_a_juzgado",
            fecha_hora=datetime.now(),
            comentarios=(
                f"📤 Proyecto incluido en carpeta enviada al juzgado {texto_proyectos}. "
                f"Carpeta formada con NNAs: {nombres_str}."
            )
        )
        db.add(historial)

    db.commit()


    return {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": (
            "📤 La carpeta fue preparada para el envío al juzgado. "
            "📄 Por favor, descargue el PDF generado y continúe el procedimiento a través del SAC 🏛️."
        ),
        "tiempo_mensaje": 5,
        "next_page": "actual"
    }



@carpetas_router.put("/{carpeta_id}/volver-a-preparacion", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def volver_a_preparacion(
    carpeta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    🔄 Devuelve la carpeta a estado 'preparando_carpeta' para permitir su edición.

    ✅ Cambia el estado de 'enviada_a_juzgado' a 'preparando_carpeta'.
    ✅ Registra evento RuaEvento.
    """
    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()

        if not carpeta:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Carpeta no encontrada.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if carpeta.estado_carpeta != "enviada_a_juzgado":
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "La carpeta no está en estado 'En juzgado'.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        estado_anterior = carpeta.estado_carpeta
        carpeta.estado_carpeta = "preparando_carpeta"

        # Registrar evento
        login_actual = current_user["user"]["login"]
        evento = RuaEvento(
            login=login_actual,
            evento_detalle=f"Se devolvió la carpeta #{carpeta_id} de '{estado_anterior}' a 'preparando_carpeta' para edición.",
            evento_fecha=datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "✅ La carpeta fue devuelta a preparación para su edición.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error al devolver la carpeta: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }




@carpetas_router.put("/{carpeta_id}/marcar-con-dictamen", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def marcar_con_dictamen(
    carpeta_id: int,
    data: dict = Body(...),  # 📥 Recibe proyecto_id opcional en el body
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    📌 Marca la carpeta como 'proyecto_seleccionado' o 'desierto' según el proyecto recibido (o ninguno).
    🔁 Cambios:
    - Si recibe un proyecto_id válido, deja solo ese y marca como 'proyecto_seleccionado' a la carpeta.
    - Si no recibe ninguno, marca como 'desierto' la carpeta y elimina todos los proyectos asociados.
    - Actualiza estados de proyectos y NNAs según corresponda.
    - Registra evento RuaEvento.
    """

    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()

        if not carpeta:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "Carpeta no encontrada.",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        if carpeta.estado_carpeta in ["proyecto_seleccionado", "desierto"]:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": f"La carpeta ya está marcada como '{carpeta.estado_carpeta}'.",
                "tiempo_mensaje": 4,
                "next_page": "actual"
            }

        proyecto_id = data.get("proyecto_id")
        proyectos_asociados = [dp.proyecto_id for dp in carpeta.detalle_proyectos]

        # Procesar proyectos asociados
        proyectos_db = db.query(Proyecto).filter(Proyecto.proyecto_id.in_(proyectos_asociados)).all()
        proyectos_dict = {p.proyecto_id: p for p in proyectos_db}

        if proyecto_id:
            # ✅ Verificar que el proyecto exista en la carpeta
            if proyecto_id not in proyectos_asociados:
                return {
                    "success": False,
                    "tipo_mensaje": "naranja",
                    "mensaje": f"El proyecto seleccionado (ID {proyecto_id}) no está asociado a esta carpeta.",
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }

            # ✅ Cambiar estado del proyecto seleccionado a vinculacion
            proyecto_sel = proyectos_dict[proyecto_id]
            estado_anterior = proyecto_sel.estado_general
            proyecto_sel.estado_general = "vinculacion"
            proyecto_sel.ultimo_cambio_de_estado = datetime.now().date()
            db.add(ProyectoHistorialEstado(
                proyecto_id=proyecto_sel.proyecto_id,
                estado_anterior=estado_anterior,
                estado_nuevo="vinculacion",
                fecha_hora=datetime.now()
            ))

            # ✅ Cambiar estado de proyectos no seleccionados a viable y eliminarlos de la carpeta
            for p_id, p_obj in proyectos_dict.items():
                if p_id != proyecto_id:
                    estado_anterior = p_obj.estado_general
                    p_obj.estado_general = "viable"
                    p_obj.ultimo_cambio_de_estado = datetime.now().date()
                    db.add(ProyectoHistorialEstado(
                        proyecto_id=p_obj.proyecto_id,
                        estado_anterior=estado_anterior,
                        estado_nuevo="viable",
                        fecha_hora=datetime.now()
                    ))
                    # eliminar de carpeta
                    db.query(DetalleProyectosEnCarpeta).filter(
                        DetalleProyectosEnCarpeta.carpeta_id == carpeta_id,
                        DetalleProyectosEnCarpeta.proyecto_id == p_id
                    ).delete()

            carpeta.estado_carpeta = "proyecto_seleccionado"
            estado_nuevo = "proyecto_seleccionado"

            # ✅ Cambiar estado de todos los NNAs a vinculacion
            # for dnna in carpeta.detalle_nna:
            #     nna = dnna.nna
            #     if nna:
            #         nna.nna_estado = "vinculacion"

            for dnna in carpeta.detalle_nna:
                nna = dnna.nna
                if nna:
                    estado_anterior = nna.nna_estado
                    nna.nna_estado = "vinculacion"

                    db.add(NnaHistorialEstado(
                        nna_id = nna.nna_id,
                        estado_anterior = estado_anterior,
                        estado_nuevo = "vinculacion",
                        fecha_hora = datetime.now()
                    ))


        else:
            # ✅ Si no se recibe proyecto, carpeta queda desierta
            # ✅ Cambiar estado de todos los proyectos a viable y eliminarlos de la carpeta
            for p_obj in proyectos_db:
                estado_anterior = p_obj.estado_general
                p_obj.estado_general = "viable"
                p_obj.ultimo_cambio_de_estado = datetime.now().date()
                db.add(ProyectoHistorialEstado(
                    proyecto_id=p_obj.proyecto_id,
                    estado_anterior=estado_anterior,
                    estado_nuevo="viable",
                    fecha_hora=datetime.now()
                ))

            db.query(DetalleProyectosEnCarpeta).filter(
                DetalleProyectosEnCarpeta.carpeta_id == carpeta_id
            ).delete()

            carpeta.estado_carpeta = "desierto"
            estado_nuevo = "desierto"

            # ✅ Cambiar estado de todos los NNAs a disponible
            for dnna in carpeta.detalle_nna:
                nna = dnna.nna
                if nna:
                    nna.nna_estado = "disponible"

        # Registrar evento
        login_actual = current_user["user"]["login"]
        evento = RuaEvento(
            login=login_actual,
            evento_detalle=f"Se marcó la carpeta #{carpeta_id} como '{estado_nuevo}' por dictamen del juzgado.",
            evento_fecha=datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": f"✅ La carpeta fue marcada como '{estado_nuevo}'.",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Ocurrió un error al marcar la carpeta: {str(e)}",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@carpetas_router.put("/{carpeta_id}/seleccionar-proyecto", response_model = dict,
    dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
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




@carpetas_router.get("/{carpeta_id}/descargar-pdf", response_class=FileResponse)
def descargar_pdf_carpeta_completa(carpeta_id: int, db: Session = Depends(get_db)):
    print(f"🔍 Buscando carpeta con ID {carpeta_id}...")
    carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()

    if not carpeta:
        print("❌ Carpeta no encontrada")
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    output_folder = os.path.join(DIR_PDF_GENERADOS, f"carpeta_{carpeta_id}")
    os.makedirs(output_folder, exist_ok=True)
    print(f"📁 Carpeta de salida creada: {output_folder}")

    pdf_paths = []

    for dp in carpeta.detalle_proyectos:
        proyecto = dp.proyecto
        if not proyecto:
            print("⚠️ Proyecto no encontrado en detalle_proyectos")
            continue

        print(f"📄 Procesando proyecto ID {proyecto.proyecto_id}")
        merged = fitz.open()


        ###############
        pretenso_1 = proyecto.usuario_1
        pretenso_2 = proyecto.usuario_2 if proyecto.login_2 else None

        portada = merged.new_page(width=595, height=842)
        portada.insert_textbox(fitz.Rect(0, 50, 595, 100), "SERVICIO DE GUARDA Y ADOPCIÓN", fontname="helv", fontsize=16, align=1, color=(0.1, 0.1, 0.3))
        portada.insert_textbox(fitz.Rect(0, 75, 595, 120), "REGISTRO ÚNICO DE ADOPCIONES Y EQUIPO TÉCNICO", fontname="helv", fontsize=13, align=1, color=(0.2, 0.2, 0.4))
        portada.insert_textbox(fitz.Rect(0, 105, 595, 135), "DOCUMENTACIÓN DEL PROYECTO ADOPTIVO", fontname="helv", fontsize=11, align=1, color=(0.4, 0.4, 0.4))

        domicilio = proyecto.proyecto_calle_y_nro or ""
        if proyecto.proyecto_depto_etc:
            domicilio += f", {proyecto.proyecto_depto_etc}"
        if proyecto.proyecto_barrio:
            domicilio += f", {proyecto.proyecto_barrio}"
        if proyecto.proyecto_localidad:
            domicilio += f", {proyecto.proyecto_localidad}"

        datos = []
        if proyecto.nro_orden_rua:
            datos.append(f"N° de orden RUA: {proyecto.nro_orden_rua}")
        if proyecto.proyecto_tipo:
            datos.append(f"Tipo de proyecto: {proyecto.proyecto_tipo}")
        if pretenso_1:
            datos.append(f"Pretenso 1: {pretenso_1.nombre} {pretenso_1.apellido} - DNI: {pretenso_1.login}")
        if pretenso_2:
            datos.append(f"Pretenso 2: {pretenso_2.nombre} {pretenso_2.apellido} - DNI: {pretenso_2.login}")
        if domicilio.strip():
            datos.append(f"Domicilio: {domicilio}")
        if proyecto.proyecto_provincia:
            datos.append(f"Provincia: {proyecto.proyecto_provincia}")

        fondo = fitz.Rect(50, 160, 545, 380)
        portada.draw_rect(fondo, fill=(0.88, 0.93, 0.98))
        y = 170
        for linea in datos:
            portada.insert_textbox(fitz.Rect(60, y, 530, y + 25), linea, fontsize=13, fontname="helv", align=0, color=(0.1, 0.1, 0.1))
            y += 28

        portada.draw_line(p1=(60, y + 10), p2=(portada.rect.width - 60, y + 10), color=(0.5, 0.5, 0.5), width=0.6)

        #################

        # # Portada
        # portada = merged.new_page(width=595, height=842)
        # portada.insert_textbox(fitz.Rect(0, 50, 595, 100), "📄 Proyecto adoptivo", fontsize=22, align=1)

        # datos = [
        #     f"N° RUA: {proyecto.nro_orden_rua or '-'}",
        #     f"Tipo: {proyecto.proyecto_tipo or '-'}",
        #     f"Provincia: {proyecto.proyecto_provincia or '-'}",
        # ]

        # if proyecto.usuario_1:
        #     datos.append(f"Pretenso 1: {proyecto.usuario_1.nombre} {proyecto.usuario_1.apellido} - DNI: {proyecto.login_1}")
        # if proyecto.usuario_2:
        #     datos.append(f"Pretenso 2: {proyecto.usuario_2.nombre} {proyecto.usuario_2.apellido} - DNI: {proyecto.login_2}")

        # y = 130
        # for linea in datos:
        #     portada.insert_textbox(fitz.Rect(60, y, 530, y+25), linea, fontsize=14)
        #     y += 30






        def agregar_doc(ruta, nombre):
            if not ruta:
                print(f"⚠️ Documento '{nombre}' no definido.")
                return
            if not os.path.exists(ruta):
                print(f"❌ Ruta inexistente: {ruta}")
                return

            ext = os.path.splitext(ruta)[1].lower()
            nombre_base = f"{nombre}_{os.path.basename(ruta)}"
            out_pdf = os.path.join(output_folder, nombre_base + ".pdf")
            print(f"📎 Agregando documento '{nombre}' ({ext}) desde {ruta}")

            try:
                if ext == ".pdf":
                    shutil.copy(ruta, out_pdf)
                
                # elif ext in [".jpg", ".jpeg", ".png"]:
                #     Image.open(ruta).convert("RGB").save(out_pdf)
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
                        "libreoffice", "--headless", "--convert-to", "pdf", "--outdir", output_folder, ruta
                    ], check=True)
                    out_pdf = os.path.join(output_folder, os.path.splitext(os.path.basename(ruta))[0] + ".pdf")

                if os.path.exists(out_pdf):
                    with fitz.open(out_pdf) as doc:
                        merged.insert_pdf(doc)
                    print(f"✅ Documento agregado: {out_pdf}")
                else:
                    print(f"⚠️ No se generó el PDF para: {ruta}")
            except Exception as e:
                print(f"❌ Error procesando documento '{ruta}': {e}")

        agregar_doc(proyecto.informe_profesionales, "informe_profesionales")

        if proyecto.proyecto_tipo != "Monoparental":
            agregar_doc(proyecto.doc_proyecto_convivencia_o_estado_civil, "convivencia")

        for login in [proyecto.login_1, proyecto.login_2]:
            if login:
                user = db.query(User).filter(User.login == login).first()
                if user:
                    print(f"👤 Documentos para usuario {login}")
                    for campo in [
                        "doc_adoptante_domicilio", "doc_adoptante_dni_frente", "doc_adoptante_dni_dorso",
                        "doc_adoptante_deudores_alimentarios", "doc_adoptante_antecedentes",
                        "doc_adoptante_migraciones", "doc_adoptante_salud"
                    ]:
                        agregar_doc(getattr(user, campo, None), campo)

        for dnna in carpeta.detalle_nna:
            if dnna.nna and dnna.nna.nna_ficha:
                agregar_doc(dnna.nna.nna_ficha, f"ficha_nna_{dnna.nna.nna_id}")

        pdf_name = f"proyecto_{proyecto.proyecto_id}.pdf"
        final_pdf_path = os.path.join(output_folder, pdf_name)
        merged.save(final_pdf_path)
        pdf_paths.append(final_pdf_path)
        print(f"📄 PDF generado: {final_pdf_path}")

    if not pdf_paths:
        print("❌ No se generó ningún PDF. Abortando ZIP.")
        raise HTTPException(status_code=404, detail="No se pudieron generar documentos para esta carpeta.")

    zip_path = os.path.join(DIR_PDF_GENERADOS, f"carpeta_{carpeta_id}_documentos.zip")
    with ZipFile(zip_path, 'w') as zipf:
        for pdf in pdf_paths:
            zipf.write(pdf, os.path.basename(pdf))
            print(f"📦 Agregado al ZIP: {os.path.basename(pdf)}")

    print(f"✅ ZIP final generado: {zip_path}")
    return FileResponse(
        path=zip_path,
        filename=os.path.basename(zip_path),
        media_type="application/zip"
    )




@carpetas_router.delete("/{carpeta_id}/eliminar-proyecto-seleccionado", response_model=dict,
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervision", "supervisora"]))])
def eliminar_carpeta_proyecto_seleccionado(
    carpeta_id: int,
    dni_pretenso: str = Query(..., description="DNI del pretenso autorizado para eliminar la carpeta"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):

    """
    🗑️ Elimina una carpeta en estado 'proyecto_seleccionado' si:
    - Tiene exactamente un proyecto.
    - Tiene al menos un NNA.
    - El DNI recibido coincide con el login_1 (monoparental) o login_1/login_2 (biparental).

    🔒 Requiere rol de administradora o supervisora.
    """
    try:
        carpeta = db.query(Carpeta).filter(Carpeta.carpeta_id == carpeta_id).first()
        if not carpeta:
            raise HTTPException(status_code=404, detail="Carpeta no encontrada")

        if carpeta.estado_carpeta != "proyecto_seleccionado":
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Solo se pueden eliminar carpetas en estado 'proyecto_seleccionado'.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        proyectos = carpeta.detalle_proyectos
        nnas = carpeta.detalle_nna

        # Validaciones
        if len(proyectos) != 1:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "La carpeta debe tener exactamente un proyecto para ser eliminada.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if not nnas or len(nnas) == 0:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "La carpeta no tiene NNAs asociados.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Validar DNI pretenso
        proyecto = db.query(Proyecto).filter(Proyecto.proyecto_id == proyectos[0].proyecto_id).first()
        if not proyecto:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "Proyecto asociado no encontrado.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        es_biparental = proyecto.proyecto_tipo in ["Matrimonio", "Unión convivencial"]
        dni_valido = False

        if not es_biparental:
            # Monoparental: debe coincidir con login_1
            if proyecto.login_1 == dni_pretenso:
                dni_valido = True
        else:
            # Biparental: puede ser login_1 o login_2
            if proyecto.login_1 == dni_pretenso or (proyecto.login_2 and proyecto.login_2 == dni_pretenso):
                dni_valido = True

        if not dni_valido:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": "El DNI proporcionado no corresponde a ningún pretenso de este proyecto. Operación cancelada.",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # Cambiar estado del proyecto a viable
        estado_anterior = proyecto.estado_general
        proyecto.estado_general = "viable"
        proyecto.ultimo_cambio_de_estado = datetime.now().date()
        db.add(ProyectoHistorialEstado(
            proyecto_id=proyecto.proyecto_id,
            estado_anterior=estado_anterior,
            estado_nuevo="viable",
            fecha_hora=datetime.now()
        ))

        # Cambiar estado de NNAs a disponible
        for detalle_nna in nnas:
            nna_obj = db.query(Nna).filter(Nna.nna_id == detalle_nna.nna_id).first()
            if nna_obj:
                nna_obj.nna_estado = "disponible"

        # Eliminar relaciones y carpeta
        db.query(DetalleProyectosEnCarpeta).filter(DetalleProyectosEnCarpeta.carpeta_id == carpeta_id).delete()
        db.query(DetalleNNAEnCarpeta).filter(DetalleNNAEnCarpeta.carpeta_id == carpeta_id).delete()
        db.delete(carpeta)

        # Registrar evento
        evento = RuaEvento(
            login=current_user["user"]["login"],
            evento_detalle=f"Se eliminó la carpeta ID {carpeta_id} en estado 'proyecto_seleccionado' tras validar pretenso.",
            evento_fecha=datetime.now()
        )
        db.add(evento)

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "✅ Carpeta eliminada correctamente. Proyecto marcado como viable y NNAs disponibles.",
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

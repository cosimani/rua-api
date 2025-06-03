from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError

from database.config import get_db
from security.security import get_current_user, verify_api_key, require_roles
from helpers.utils import normalizar_y_validar_dni
from datetime import datetime
from models.convocatorias import Postulacion, Convocatoria, DetalleProyectoPostulacion, DetalleNNAEnConvocatoria  
from models.eventos_y_configs import RuaEvento
from models.users import User, Group, UserGroup 
from models.proyecto import Proyecto, ProyectoHistorialEstado
from models.nna import Nna
from sqlalchemy.orm.exc import NoResultFound
from datetime import date, datetime
from math import ceil
from helpers.notificaciones_utils import crear_notificacion_masiva_por_rol



convocatoria_router = APIRouter()



@convocatoria_router.get("/", response_model=dict, dependencies=[Depends(verify_api_key)])
def get_convocatorias(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None, min_length=3),
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    online: Optional[bool] = Query(None)
):
    try:
        query = db.query(Convocatoria)

        if search:
            pattern = f"%{search}%"
            query = query.filter(
                (Convocatoria.convocatoria_referencia.ilike(pattern)) |
                (Convocatoria.convocatoria_juzgado_interviniente.ilike(pattern))
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
                                Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
                                Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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
def crear_postulacion( datos: dict = Body(...), db: Session = Depends(get_db), ):
    """
    📝 Da de alta una nueva postulación a convocatoria y crea automáticamente el proyecto correspondiente.

    🔐 Requiere autenticación con rol adoptante.
    📅 La fecha de postulación se asigna automáticamente.
    🔗 Se genera una relación entre la postulación y el proyecto.

    📥 JSON esperado:
    ```json
    {
    "convocatoria_id": 68,
    "nombre": "Lucía",
    "apellido": "Gómez",
    "dni": "30123456",
    "fecha_nacimiento": "1985-07-12",
    "nacionalidad": "Argentina",
    "sexo": "Femenino",
    "estado_civil": "Casada",
    "calle_y_nro": "Av. Siempre Viva 123",
    "depto": "B",
    "barrio": "Centro",
    "localidad": "Córdoba",
    "cp": "5000",
    "provincia": "Córdoba",
    "telefono_contacto": "3511234567",
    "videollamada": "Y",
    "mail": "lucia.gomez@example.com",
    "movilidad_propia": "Y",
    "obra_social": "OSDE",
    "ocupacion": "Docente",
    "conyuge_convive": "Y",
    "conyuge_nombre": "Javier",
    "conyuge_apellido": "Pérez",
    "conyuge_dni": "28111222",
    "conyuge_edad": "40",
    "conyuge_otros_datos": "Trabaja como ingeniero en sistemas",
    "hijos": "Dos hijos de 8 y 10 años",
    "acogimiento_es": "N",
    "acogimiento_descripcion": "",
    "en_rua": "Y",
    "subregistro_comentarios": "Preferencia por grupos de hermanos",
    "otra_convocatoria": "N",
    "otra_convocatoria_comentarios": "",
    "antecedentes": "N",
    "antecedentes_comentarios": "",
    "como_tomaron_conocimiento": "A través de una charla informativa en el RUA",
    "motivos": "Queremos ampliar nuestra familia y sentimos vocación adoptiva",
    "comunicaron_decision": "Sí, a familiares y amistades",
    "otros_comentarios": "Disponibilidad para entrevistas y formación virtual",
    "inscripto_en_rua": "Y"
    }
    ```

    """

    try:
        convocatoria_id = datos.get("convocatoria_id")

        if not convocatoria_id:
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": (
                    "<p>Falta el campo obligatorio 'convocatoria_id'.</p>"
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
                    f"<p>No se encontró la convocatoria con ID {convocatoria_id}.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

       # Diccionario de nombres amigables
        nombres_amigables = {
            "nombre": "Nombre",
            "apellido": "Apellido",
            "fecha_nacimiento": "Fecha de nacimiento",
            "nacionalidad": "Nacionalidad",
            "estado_civil": "Estado civil",
            "calle_y_nro": "Calle y número",
            "localidad": "Localidad",
            "telefono_contacto": "Teléfono de contacto",
            "mail": "Correo electrónico",
            "ocupacion": "Ocupación / profesión",
            "conyuge_nombre": "Nombre del/la conviviente",
            "conyuge_apellido": "Apellido del/la conviviente",
            "conyuge_dni": "DNI del/la conviviente"
        }

        # Validar campos obligatorios
        campos_obligatorios = [
            "nombre", "apellido", "fecha_nacimiento", "nacionalidad", "estado_civil",
            "calle_y_nro", "localidad", "telefono_contacto", "mail", "ocupacion"
        ]

        campos_faltantes = [campo for campo in campos_obligatorios if not datos.get(campo)]

        # Validar conyuge si corresponde
        if datos.get("conyuge_convive") == "Y":
            for campo_conyuge in ["conyuge_nombre", "conyuge_apellido", "conyuge_dni"]:
                if not datos.get(campo_conyuge):
                    campos_faltantes.append(campo_conyuge)

        if campos_faltantes:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": (
                    "<p>Faltan completar los siguientes campos obligatorios:</p><ul>"
                    + "".join(f"<li>{nombres_amigables.get(campo, campo.replace('_', ' ').capitalize())}</li>" for campo in campos_faltantes)
                    + "</ul>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
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


        nueva_postulacion = Postulacion(
            fecha_postulacion = datetime.now(),
            convocatoria_id = convocatoria_id,
            nombre = datos.get("nombre"),
            apellido = datos.get("apellido"),
            dni = dni,
            fecha_nacimiento = datos.get("fecha_nacimiento"),
            nacionalidad = datos.get("nacionalidad"),
            sexo = datos.get("sexo"),
            estado_civil = datos.get("estado_civil"),
            calle_y_nro = datos.get("calle_y_nro"),
            depto = datos.get("depto"),
            barrio = datos.get("barrio"),
            localidad = datos.get("localidad"),
            cp = datos.get("cp"),
            provincia = datos.get("provincia"),
            telefono_contacto = datos.get("telefono_contacto"),
            videollamada = datos.get("videollamada"),
            mail = datos.get("mail"),
            movilidad_propia = datos.get("movilidad_propia"),
            obra_social = datos.get("obra_social"),
            ocupacion = datos.get("ocupacion"),
            conyuge_convive = datos.get("conyuge_convive"),
            conyuge_nombre = datos.get("conyuge_nombre"),
            conyuge_apellido = datos.get("conyuge_apellido"),
            conyuge_dni = datos.get("conyuge_dni"),
            conyuge_edad = datos.get("conyuge_edad"),
            conyuge_otros_datos = datos.get("conyuge_otros_datos"),
            hijos = datos.get("hijos"),
            acogimiento_es = datos.get("acogimiento_es"),
            acogimiento_descripcion = datos.get("acogimiento_descripcion"),
            en_rua = datos.get("en_rua"),
            subregistro_comentarios = datos.get("subregistro_comentarios"),
            otra_convocatoria = datos.get("otra_convocatoria"),
            otra_convocatoria_comentarios = datos.get("otra_convocatoria_comentarios"),
            antecedentes = datos.get("antecedentes"),
            antecedentes_comentarios = datos.get("antecedentes_comentarios"),
            como_tomaron_conocimiento = datos.get("como_tomaron_conocimiento"),
            motivos = datos.get("motivos"),
            comunicaron_decision = datos.get("comunicaron_decision"),
            otros_comentarios = datos.get("otros_comentarios"),
            inscripto_en_rua = datos.get("inscripto_en_rua")
        )

        db.add(nueva_postulacion)
        db.flush()  # <-- Esto fuerza la inserción en la base de datos
        db.refresh(nueva_postulacion)  # Ahora sí puede refrescarla correctamente



        # Alta usuario login_1 (postulante)
        usuario_1 = db.query(User).filter(User.login == dni).first()
        if not usuario_1:
            mail_1 = datos.get("mail")
            if db.query(User).filter(User.mail == mail_1).first():
                mail_1 = None
            nuevo_usuario_1 = User(
                login = dni,
                nombre = datos.get("nombre"),
                apellido = datos.get("apellido"),
                celular = datos.get("telefono_contacto"),
                mail = mail_1,
                profesion = datos.get("ocupacion", "")[:60],
                provincia = datos.get("provincia", "")[:50],
                active = "Y",
                operativo = "Y",
                doc_adoptante_curso_aprobado = "Y",
                doc_adoptante_ddjj_firmada = 'Y',
                fecha_alta = date.today()
            )
            db.add(nuevo_usuario_1)
            db.flush()   # <-- forzamos la inserción del usuario antes de crear eventos


            grupo_adoptante = db.query(Group).filter(Group.description == "adoptante").first()
            if grupo_adoptante:
                db.add(UserGroup(login = dni, group_id = grupo_adoptante.group_id))

            db.add(RuaEvento(
                login = dni,
                evento_detalle = f"Usuario generado automáticamente desde postulación a convocatoria ID {convocatoria_id}.",
                evento_fecha = datetime.now()
            ))

        # Alta usuario login_2 (conyuge)
        tiene_conyuge = datos.get("conyuge_dni") and datos.get("conyuge_convive") == "Y"
        if tiene_conyuge:

            conyuge_dni = normalizar_y_validar_dni(datos.get("conyuge_dni")) 
            if not conyuge_dni: 
                return {
                    "success": False,
                    "tipo_mensaje": "amarillo",
                    "mensaje": (
                        "<p>Debe indicar un DNI válido para cónyuge.</p>"
                    ),
                    "tiempo_mensaje": 5,
                    "next_page": "actual"
                }


            usuario_2 = db.query(User).filter(User.login == conyuge_dni).first()
            if not usuario_2:
                nuevo_usuario_2 = User(
                    login = conyuge_dni,
                    nombre = datos.get("conyuge_nombre"),
                    apellido = datos.get("conyuge_apellido"),
                    mail = None,
                    active = "Y",
                    operativo = "Y",
                    doc_adoptante_curso_aprobado = "Y",
                    doc_adoptante_ddjj_firmada = 'Y',
                    fecha_alta = date.today()
                )
                db.add(nuevo_usuario_2)
                db.flush()   # <-- forzamos la inserción del usuario antes de crear eventos

                if grupo_adoptante:
                    db.add(UserGroup(login = conyuge_dni, group_id = grupo_adoptante.group_id))

                db.add(RuaEvento(
                    login = conyuge_dni,
                    evento_detalle = f"Usuario generado automáticamente desde postulación (cónyuge) a convocatoria ID {convocatoria_id}.",
                    evento_fecha = datetime.now()
                ))



        # Preparar campos comunes
        nuevo_proyecto = Proyecto(
            proyecto_tipo = "Unión convivencial" if tiene_conyuge else "Monoparental",
            login_1 = dni,
            login_2 = conyuge_dni if tiene_conyuge else None,
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
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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



# @convocatoria_router.get("/para-select/para-filtro", response_model=List[dict], 
#     dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
# def get_convocatorias_para_filtro(
#     db: Session = Depends(get_db),
#     page: int = Query(1, ge=1),
#     limit: int = Query(30, ge=1, le=100)
# ):
#     try:
#         offset = (page - 1) * limit

#         convocatorias = db.query(Convocatoria)\
#             .order_by(Convocatoria.convocatoria_fecha_publicacion.desc())\
#             .offset(offset)\
#             .limit(limit)\
#             .all()

#         return [{
#             "value": c.convocatoria_id,
#             "label": f"{c.convocatoria_referencia} - {c.convocatoria_llamado} ( {c.convocatoria_fecha_publicacion} )"
#         } for c in convocatorias]

#     except SQLAlchemyError as e:
#         raise HTTPException(status_code=500, detail=f"Error al obtener convocatorias para filtro: {str(e)}")


from sqlalchemy.orm import aliased
from sqlalchemy import func

@convocatoria_router.get("/para-select/para-filtro", response_model=List[dict], 
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
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

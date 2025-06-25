from fastapi import APIRouter, HTTPException, Depends, Query, Request, Body
from sqlalchemy.orm import Session
from typing import Optional, Literal
from models.ddjj import DDJJ
from models.users import User, Group, UserGroup 
from database.config import SessionLocal, get_db
from security.security import verify_api_key
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime
from models.eventos_y_configs import RuaEvento
from security.security import get_current_user, require_roles, verify_api_key, get_password_hash

from helpers.utils import convertir_booleans_a_string, normalizar_celular, capitalizar_nombre


ddjj_router = APIRouter()



@ddjj_router.post("/upsert", response_model = dict, 
                  dependencies = [Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", 
                                                                                   "profesional", "adoptante"]))])
def upsert_ddjj(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    """
    📝 Crea o actualiza una Declaración Jurada (DDJJ) usando el `login` como identificador.

    - Si **no existe** una DDJJ para ese login, se creará una nueva.
    - Si **ya existe**, se actualizarán únicamente los campos proporcionados (excepto `login`).

    📦 **Ejemplo para crear una nueva DDJJ**:

    ```json
    {
      "login": "12345678",
      "ddjj_nombre": "Juan",
      "ddjj_apellido": "Pérez",
      "ddjj_telefono": "3511234567"
    }
    ```

    🔄 **Ejemplo para actualizar un solo campo**:

    ```json
    {
      "login": "12345678",
      "ddjj_telefono": "3519876543"
    }
    ```

    🔁 **Ejemplo para modificar todos los campos**:

    ```json
    {
    "login": "00123456",
    "ddjj_nombre": "Carlos",
    "ddjj_apellido": "Ponce",
    "ddjj_estado_civil": "Casada",
    "ddjj_calle": "Av. Siempre Viva 123",
    "ddjj_depto": "B",
    "ddjj_barrio": "Centro",
    "ddjj_localidad": "Córdoba",
    "ddjj_cp": "5000",
    "ddjj_provincia": "Córdoba",
    "ddjj_fecha_nac": "1985-07-12",
    "ddjj_nacionalidad": "Argentina",
    "ddjj_sexo": "Femenino",
    "ddjj_correo_electronico": "lucia.gomez@example.com",
    "ddjj_telefono": "3511234567",
    "ddjj_ocupacion": "Docente",
    "ddjj_horas_semanales": "30",
    "ddjj_ingreso_mensual": "200000",
    "ddjj_existe_otro_ingreso": "N",
    "ddjj_ingreso_grupo_familiar": "350000",
    "ddjj_analizaron": "Sí",
    "ddjj_horas_ocio": "10",
    "ddjj_extra_laborales": "Teatro y caminatas",
    "ddjj_denunciado_violencia_familiar": "N",
    "ddjj_causa_penal": "N",
    "ddjj_juicios_filiacion": "N",
    "ddjj_subregistro_1": "Y",
    "ddjj_subregistro_2": "N",
    "ddjj_subregistro_3": "Y",
    "ddjj_subregistro_4": "N",
    "ddjj_subregistro_5_a": "Y",
    "ddjj_subregistro_5_b": "N",
    "ddjj_subregistro_5_c": "N",
    "ddjj_subregistro_6_a": "N",
    "ddjj_subregistro_6_b": "N",
    "ddjj_subregistro_6_c": "N",
    "ddjj_subregistro_6_d": "N",
    "ddjj_subregistro_6_2": "N",
    "ddjj_subregistro_6_3": "N",
    "ddjj_subregistro_6_mas_de_3": "N",
    "ddjj_subregistro_flexible": "N",

    "ddjj_acepto_1": "Y",
    "ddjj_acepto_2": "Y",
    "ddjj_acepto_3": "Y",
    "ddjj_acepto_4": "Y",

    "ddjj_hijo1_tiene": "Y",
    "ddjj_hijo1_nombre_completo": "Mateo Gómez",
    "ddjj_hijo1_estado_civil": "Soltero",
    "ddjj_hijo1_dni": "50123456",
    "ddjj_hijo1_domicilio_real": "Av. Siempre Viva 123",
    "ddjj_hijo1_edad": "12",
    "ddjj_hijo1_nacionalidad": "Argentina",
    "ddjj_hijo1_ocupacion": "Estudiante",
    "ddjj_hijo1_convive_con_usted": "Y",

    "ddjj_otro1_tiene": "Y",
    "ddjj_otro1_nombre_completo": "María Pérez",
    "ddjj_otro1_estado_civil": "Viuda",
    "ddjj_otro1_dni": "40111222",
    "ddjj_otro1_domicilio_real": "Av. Siempre Viva 123",
    "ddjj_otro1_nacionalidad": "Argentina",
    "ddjj_otro1_edad": "67",
    "ddjj_otro1_ocupacion": "Jubilada",
    "ddjj_otro1_convive_con_usted": "Y",

    "ddjj_apoyo1_tiene": "Y",
    "ddjj_apoyo1_nombre_completo": "Mariano Ruiz",
    "ddjj_apoyo1_estado_civil": "Casado",
    "ddjj_apoyo1_dni": "30112233",
    "ddjj_apoyo1_domicilio_real": "Av. Mitre 456",
    "ddjj_apoyo1_nacionalidad": "Argentina",
    "ddjj_apoyo1_edad": "45",
    "ddjj_apoyo1_ocupacion": "Psicólogo",

    "ddjj_guardo_1": "Y",
    "ddjj_guardo_2": "Y",
    "ddjj_guardo_3": "Y",
    "ddjj_guardo_4": "Y",
    "ddjj_guardo_5": "Y",
    "ddjj_guardo_6": "Y",
    "ddjj_guardo_7": "Y",
    "ddjj_guardo_8": "Y"
    }

    ```

    ✅ **Respuesta exitosa**:

    ```json
    {
      "tipo_mensaje": "verde",
      "mensaje": "<p>DDJJ actualizada exitosamente.</p>",
      "tiempo_mensaje": 5,
      "next_page": "actual"
    }
    ```

    ❌ **Respuesta con error**:

    ```json
    {
      "tipo_mensaje": "rojo",
      "mensaje": "<p>Error al actualizar DDJJ.</p>",
      "tiempo_mensaje": 5,
      "next_page": "actual"
    }
    ```
    """


    login = data.get("login")
    if not login:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "<p>El campo 'login' es obligatorio.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    data = convertir_booleans_a_string(data)

    usuario_actual_login = current_user["user"]["login"]

    roles_actual = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == usuario_actual_login)
        .all()
    )
    roles_actual = [r.description for r in roles_actual]

    if "adoptante" in roles_actual and usuario_actual_login != login:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "<p>No tiene permisos para modificar la DDJJ de otro usuario.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    usuario = db.query(User).filter(User.login == login).first()
    if not usuario:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "<p>No existe un usuario con ese DNI.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


    if usuario.doc_adoptante_ddjj_firmada == 'Y':
        return {
            "success": False,
            "tipo_mensaje": "amarillo",
            "mensaje": "<p>Su Declaración Jurada ya fue firmada previamente.</p>"
                    "<p>Si necesita realizar modificaciones, puede reabrirla desde y volver a firmarla. "
                    "La nueva firma será notificada al equipo de supervisión del RUA para su revisión.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }



    # Validar campos obligatorios
    campos_requeridos = [
        "ddjj_nombre", "ddjj_apellido", "ddjj_fecha_nac", "ddjj_nacionalidad",
        "ddjj_sexo", "ddjj_estado_civil", "ddjj_correo_electronico",
        "ddjj_telefono", "ddjj_calle", "ddjj_localidad"
    ]

    # Diccionario con nombres amigables para campos requeridos
    nombres_amigables = {
        "ddjj_nombre": "Nombre",
        "ddjj_apellido": "Apellido",
        "ddjj_fecha_nac": "Fecha de nacimiento",
        "ddjj_nacionalidad": "Nacionalidad",
        "ddjj_sexo": "Sexo",
        "ddjj_estado_civil": "Estado civil",
        "ddjj_correo_electronico": "Correo electrónico",
        "ddjj_telefono": "Celular",
        "ddjj_calle": "Calle",
        "ddjj_localidad": "Localidad",
        "al_menos_un_subregistro": "Al menos un subregistro en Disponibilidad adoptiva"
    }


    campos_faltantes = [campo for campo in campos_requeridos if not data.get(campo)]

    # Validar que al menos un subregistro esté en "Y", True o similar
    subregistros = [
        # Subregistros principales
        "ddjj_subregistro_flexible",
        "ddjj_subregistro_1", "ddjj_subregistro_2", "ddjj_subregistro_3", "ddjj_subregistro_4",
        "ddjj_subregistro_5_a", "ddjj_subregistro_5_b", "ddjj_subregistro_5_c",
        "ddjj_subregistro_6_a", "ddjj_subregistro_6_b", "ddjj_subregistro_6_c",
        "ddjj_subregistro_6_d", "ddjj_subregistro_6_2", "ddjj_subregistro_6_3", "ddjj_subregistro_6_mas_de_3",

        # Flexibilidad edad
        "ddjj_flex_edad_1", "ddjj_flex_edad_2", "ddjj_flex_edad_3", "ddjj_flex_edad_4", "ddjj_flex_edad_todos",

        # Discapacidad
        "ddjj_discapacidad_1", "ddjj_discapacidad_2",
        "ddjj_edad_discapacidad_0", "ddjj_edad_discapacidad_1", "ddjj_edad_discapacidad_2",
        "ddjj_edad_discapacidad_3", "ddjj_edad_discapacidad_4",

        # Enfermedad
        "ddjj_enfermedad_1", "ddjj_enfermedad_2", "ddjj_enfermedad_3",
        "ddjj_edad_enfermedad_0", "ddjj_edad_enfermedad_1", "ddjj_edad_enfermedad_2",
        "ddjj_edad_enfermedad_3", "ddjj_edad_enfermedad_4",

        # Flexibilidad salud
        "ddjj_flex_condiciones_salud",
        "ddjj_flex_salud_edad_0", "ddjj_flex_salud_edad_1", "ddjj_flex_salud_edad_2",
        "ddjj_flex_salud_edad_3", "ddjj_flex_salud_edad_4",

        # Hermanos
        "ddjj_hermanos_comp_1", "ddjj_hermanos_comp_2", "ddjj_hermanos_comp_3",
        "ddjj_hermanos_edad_0", "ddjj_hermanos_edad_1", "ddjj_hermanos_edad_2", "ddjj_hermanos_edad_3",

        # Flexibilidad hermanos
        "ddjj_flex_hermanos_comp_1", "ddjj_flex_hermanos_comp_2", "ddjj_flex_hermanos_comp_3",
        "ddjj_flex_hermanos_edad_0", "ddjj_flex_hermanos_edad_1", "ddjj_flex_hermanos_edad_2", "ddjj_flex_hermanos_edad_3",

        # Nuevos subregistros definitivos
        "subreg_1", "subreg_2", "subreg_3", "subreg_4",
        "subreg_FE1", "subreg_FE2", "subreg_FE3", "subreg_FE4", "subreg_FET",
        "subreg_5A1E1", "subreg_5A1E2", "subreg_5A1E3", "subreg_5A1E4", "subreg_5A1ET",
        "subreg_5A2E1", "subreg_5A2E2", "subreg_5A2E3", "subreg_5A2E4", "subreg_5A2ET",
        "subreg_5B1E1", "subreg_5B1E2", "subreg_5B1E3", "subreg_5B1E4", "subreg_5B1ET",
        "subreg_5B2E1", "subreg_5B2E2", "subreg_5B2E3", "subreg_5B2E4", "subreg_5B2ET",
        "subreg_5B3E1", "subreg_5B3E2", "subreg_5B3E3", "subreg_5B3E4", "subreg_5B3ET",
        "subreg_F5E1", "subreg_F5E2", "subreg_F5E3", "subreg_F5E4", "subreg_F5ET",
        "subreg_61E1", "subreg_61E2", "subreg_61E3", "subreg_61ET",
        "subreg_62E1", "subreg_62E2", "subreg_62E3", "subreg_62ET",
        "subreg_63E1", "subreg_63E2", "subreg_63E3", "subreg_63ET",
        "subreg_FQ1", "subreg_FQ2", "subreg_FQ3",
        "subreg_F6E1", "subreg_F6E2", "subreg_F6E3", "subreg_F6ET"
    ]



    if not any(data.get(s) in ["Y", True, "true", "True"] for s in subregistros):
        campos_faltantes.append("al_menos_un_subregistro")

    if campos_faltantes:
        lista = "".join(f"<li>{nombres_amigables.get(campo, campo)}</li>" for campo in campos_faltantes)

        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": f"<p>Faltan completar los siguientes campos obligatorios:</p><ul>{lista}</ul>",
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }
    

    # Capitalizar nombre y apellido usando función existente
    if "ddjj_nombre" in data:
        data["ddjj_nombre"] = capitalizar_nombre(data["ddjj_nombre"])

    if "ddjj_apellido" in data:
        data["ddjj_apellido"] = capitalizar_nombre(data["ddjj_apellido"])

    # Normalizar celular usando función existente
    celular_raw = data.get("ddjj_telefono", "")
    resultado_validacion_celular = normalizar_celular(celular_raw)

    if resultado_validacion_celular["valido"]:
        data["ddjj_telefono"] = resultado_validacion_celular["celular"]
    else:
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": (
                "<p>Ingrese un número de celular válido en Datos personales.</p>"
                "<p>Por favor, intente nuevamente.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    

    # Normalizar fecha de nacimiento si existe
    if "ddjj_fecha_nac" in data:
        valor_original = data["ddjj_fecha_nac"]
        try:
            # Detectar si viene en formato DD/MM/YYYY y convertir
            if isinstance(valor_original, str) and "/" in valor_original:
                fecha = datetime.strptime(valor_original, "%d/%m/%Y").date()
                data["ddjj_fecha_nac"] = fecha.isoformat()  # convierte a YYYY-MM-DD
        except ValueError:
            return {
                "tipo_mensaje": "amarillo",
                "mensaje": "<p>La fecha de nacimiento no tiene un formato válido (esperado: DD/MM/YYYY o YYYY-MM-DD).</p>",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

    ddjj = db.query(DDJJ).filter(DDJJ.login == login).first()


    if not all(data.get(f"ddjj_acepto_{i}") == "Y" for i in range(1, 5)):
        return {
            "success": False,
            "tipo_mensaje": "amarillo",
            "mensaje": "<p>Debe aceptar las declaraciones formales y legales del tramo final para firmar la DDJJ.</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    
    
    if not ddjj:

        nueva_ddjj = DDJJ(**data)
        db.add(nueva_ddjj)

        usuario.doc_adoptante_ddjj_firmada = "Y"

        es_creacion = not ddjj
        es_mismo_usuario = usuario_actual_login == login

        evento_detalle = (
            "DDJJ creada y firmada" if es_creacion else "DDJJ actualizada y firmada"
        )
        evento_detalle += " por el mismo usuario." if es_mismo_usuario else f" por {usuario_actual_login}."

        db.add(RuaEvento(
            login=login,
            evento_detalle=evento_detalle,
            evento_fecha=datetime.now()
        ))

  
        try:
            db.commit()
            db.refresh(nueva_ddjj)
            return {
                "success": True,
                "tipo_mensaje": "verde",
                "mensaje": "<p>DDJJ firmada exitosamente.</p>",
                "tiempo_mensaje": 5,
                "next_page": "menu_adoptantes/portada"
            }
        except SQLAlchemyError as e:
            db.rollback()
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"<p>Error al crear DDJJ: {str(e)}</p>",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }
    
    else:
        
        campos_actualizables = {
            k: v for k, v in data.items() if k != "login" and hasattr(ddjj, k)
        }

        # Diccionario que guardará valores ya transformados y listos para usar
        valores_normalizados = {}

        # Capitalizar nombre y apellido
        if "ddjj_nombre" in campos_actualizables:
            valores_normalizados["ddjj_nombre"] = capitalizar_nombre((campos_actualizables["ddjj_nombre"] or "").strip())

        if "ddjj_apellido" in campos_actualizables:
            valores_normalizados["ddjj_apellido"] = capitalizar_nombre((campos_actualizables["ddjj_apellido"] or "").strip())

        # Normalizar celular
        if "ddjj_telefono" in campos_actualizables:
            celular = (campos_actualizables["ddjj_telefono"] or "").strip()
            resultado = normalizar_celular(celular)
            if resultado["valido"]:
                valores_normalizados["ddjj_telefono"] = resultado["celular"]

        # Copiar sin modificación pero con limpieza
        for campo in ["ddjj_fecha_nac", "ddjj_calle", "ddjj_depto", "ddjj_barrio", "ddjj_localidad", "ddjj_provincia"]:
            if campo in campos_actualizables:
                valores_normalizados[campo] = (campos_actualizables[campo] or "").strip()

        # Asignar los valores transformados a la DDJJ
        # for campo, valor in valores_normalizados.items():
        #     setattr(ddjj, campo, valor)

        for campo, valor in campos_actualizables.items():
            if isinstance(valor, bool):
                valor = "Y" if valor else "N"
            elif isinstance(valor, str) and valor.strip().lower() in ["true", "false"]:
                valor = "Y" if valor.strip().lower() == "true" else "N"
            setattr(ddjj, campo, valor)


        # Asignar también a sec_users
        mapeo_ddjj_a_user = {
            "ddjj_nombre": "nombre",
            "ddjj_apellido": "apellido",
            "ddjj_telefono": "celular",
            "ddjj_fecha_nac": "fecha_nacimiento",
            "ddjj_calle": "calle_y_nro",
            "ddjj_depto": "depto_etc",
            "ddjj_barrio": "barrio",
            "ddjj_localidad": "localidad",
            "ddjj_provincia": "provincia"
        }

        for campo_ddjj, campo_user in mapeo_ddjj_a_user.items():
            if campo_ddjj in valores_normalizados:
                setattr(usuario, campo_user, valores_normalizados[campo_ddjj])

        try:
            usuario.doc_adoptante_ddjj_firmada = "Y"

            es_creacion = not ddjj
            es_mismo_usuario = usuario_actual_login == login

            evento_detalle = (
                "DDJJ creada y firmada" if es_creacion else "DDJJ actualizada y firmada"
            )
            evento_detalle += " por el mismo usuario." if es_mismo_usuario else f" por {usuario_actual_login}."

            db.add(RuaEvento(
                login=login,
                evento_detalle=evento_detalle,
                evento_fecha=datetime.now()
            ))

            db.commit()

            return {
                "tipo_mensaje": "verde",
                "mensaje": "<p>DDJJ actualizada exitosamente.</p>",
                "tiempo_mensaje": 5,
                "next_page": "menu_adoptantes/portada"
            }
        except SQLAlchemyError as e:
            db.rollback()
            return {
                "tipo_mensaje": "rojo",
                "mensaje": f"<p>Error al actualizar DDJJ: {str(e)}</p>",
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }




@ddjj_router.get("/{login}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", 
                                                                                  "profesional", "adoptante"]))])
def get_ddjj_by_login(
    login: str,
    seccion: Optional[Literal[
        "datos_personales",
        "grupo_familiar_hijos",
        "grupo_familiar_otros",
        "red_de_apoyo",
        "informacion_laboral",
        "procesos_judiciales",
        "disponibilidad_adoptiva",
        "tramo_final",
        "todas"
    ]] = Query( None, description = "Sección específica de la DDJJ a devolver" ),
    db: Session = Depends(get_db)
):
    """
    Devuelve todos los datos de la Declaración Jurada (DDJJ) de un usuario a partir de su `login`.
    """
    try:
        query = (
            db.query(DDJJ)
            .filter(DDJJ.login == login)
        )

        ddjj = query.first()

        if not ddjj:
            return {
                "success": False,
                "tipo_mensaje": "amarillo",
                "mensaje": (
                    "<p>No hay registros de DDJJ para este usuario.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "actual"
            }

        ddjj_dict = {
            "ddjj_id": ddjj.ddjj_id,
            "ddjj_fecha_ultimo_cambio": ddjj.ddjj_fecha_ultimo_cambio,
            
            "datos_personales": {
                "nombre": ddjj.ddjj_nombre,
                "apellido": ddjj.ddjj_apellido,
                "dni": ddjj.login,
                "estado_civil": ddjj.ddjj_estado_civil,
                "fecha_nac": ddjj.ddjj_fecha_nac,
                "nacionalidad": ddjj.ddjj_nacionalidad,
                "sexo": ddjj.ddjj_sexo,
                "correo_electronico": ddjj.ddjj_correo_electronico,
                "telefono": ddjj.ddjj_telefono,
                "inscripto_programa_familias": ddjj.ddjj_inscripto_programa_familias,
                "domicilio_real": {
                    "calle": ddjj.ddjj_calle,
                    "depto": ddjj.ddjj_depto,
                    "barrio": ddjj.ddjj_barrio,
                    "localidad": ddjj.ddjj_localidad,
                    "cp": ddjj.ddjj_cp,
                    "provincia": ddjj.ddjj_provincia,
                },
                "domicilio_legal": {
                    "calle": ddjj.ddjj_calle_legal,
                    "depto": ddjj.ddjj_depto_legal,
                    "barrio": ddjj.ddjj_barrio_legal,
                    "localidad": ddjj.ddjj_localidad_legal,
                    "cp": ddjj.ddjj_cp_legal,
                    "provincia": ddjj.ddjj_provincia_legal,
                },
            },

            "grupo_familiar_hijos": [
                {
                    "tiene": getattr(ddjj, f"ddjj_hijo{i}_tiene"),
                    "nombre": getattr(ddjj, f"ddjj_hijo{i}_nombre_completo"),
                    "estado_civil": getattr(ddjj, f"ddjj_hijo{i}_estado_civil"),
                    "dni": getattr(ddjj, f"ddjj_hijo{i}_dni"),
                    "domicilio": getattr(ddjj, f"ddjj_hijo{i}_domicilio_real"),
                    "edad": getattr(ddjj, f"ddjj_hijo{i}_edad"),
                    "nacionalidad": getattr(ddjj, f"ddjj_hijo{i}_nacionalidad"),
                    "ocupacion": getattr(ddjj, f"ddjj_hijo{i}_ocupacion"),
                    "convive_con_usted": getattr(ddjj, f"ddjj_hijo{i}_convive_con_usted"),
                }
                for i in range(1, 6)
                if any([
                    getattr(ddjj, f"ddjj_hijo{i}_tiene"),
                    getattr(ddjj, f"ddjj_hijo{i}_nombre_completo"),
                    getattr(ddjj, f"ddjj_hijo{i}_estado_civil"),
                    getattr(ddjj, f"ddjj_hijo{i}_dni"),
                    getattr(ddjj, f"ddjj_hijo{i}_domicilio_real"),
                    getattr(ddjj, f"ddjj_hijo{i}_edad"),
                    getattr(ddjj, f"ddjj_hijo{i}_nacionalidad"),
                    getattr(ddjj, f"ddjj_hijo{i}_ocupacion"),
                ])
            ],


            "grupo_familiar_otros": [
                {
                    "nombre": getattr(ddjj, f"ddjj_otro{i}_nombre_completo"),
                    "estado_civil": getattr(ddjj, f"ddjj_otro{i}_estado_civil"),
                    "dni": getattr(ddjj, f"ddjj_otro{i}_dni"),
                    "domicilio": getattr(ddjj, f"ddjj_otro{i}_domicilio_real"),
                    "edad": getattr(ddjj, f"ddjj_otro{i}_edad"),
                    "nacionalidad": getattr(ddjj, f"ddjj_otro{i}_nacionalidad"),
                    "ocupacion": getattr(ddjj, f"ddjj_otro{i}_ocupacion"),
                    "convive_con_usted": getattr(ddjj, f"ddjj_otro{i}_convive_con_usted"),
                }
                for i in range(1, 6)
                if any([
                    getattr(ddjj, f"ddjj_otro{i}_nombre_completo"),
                    getattr(ddjj, f"ddjj_otro{i}_estado_civil"),
                    getattr(ddjj, f"ddjj_otro{i}_dni"),
                    getattr(ddjj, f"ddjj_otro{i}_domicilio_real"),
                    getattr(ddjj, f"ddjj_otro{i}_edad"),
                    getattr(ddjj, f"ddjj_otro{i}_nacionalidad"),
                    getattr(ddjj, f"ddjj_otro{i}_ocupacion"),
                ])
            ],

            "red_de_apoyo": [
                {
                    "nombre": getattr(ddjj, f"ddjj_apoyo{i}_nombre_completo"),
                    "estado_civil": getattr(ddjj, f"ddjj_apoyo{i}_estado_civil"),
                    "dni": getattr(ddjj, f"ddjj_apoyo{i}_dni"),
                    "domicilio": getattr(ddjj, f"ddjj_apoyo{i}_domicilio_real"),
                    "edad": getattr(ddjj, f"ddjj_apoyo{i}_edad"),
                    "nacionalidad": getattr(ddjj, f"ddjj_apoyo{i}_nacionalidad"),
                    "ocupacion": getattr(ddjj, f"ddjj_apoyo{i}_ocupacion"),
                }
                for i in range(1, 3)
                if any([
                    getattr(ddjj, f"ddjj_apoyo{i}_nombre_completo"),
                    getattr(ddjj, f"ddjj_apoyo{i}_estado_civil"),
                    getattr(ddjj, f"ddjj_apoyo{i}_dni"),
                    getattr(ddjj, f"ddjj_apoyo{i}_domicilio_real"),
                    getattr(ddjj, f"ddjj_apoyo{i}_edad"),
                    getattr(ddjj, f"ddjj_apoyo{i}_nacionalidad"),
                    getattr(ddjj, f"ddjj_apoyo{i}_ocupacion"),
                ])
            ],


            "informacion_laboral": {
                "educativo_maximo": ddjj.ddjj_educativo_maximo,
                "ocupacion": ddjj.ddjj_ocupacion,
                "horas_semanales": ddjj.ddjj_horas_semanales,
                "ingreso_mensual": ddjj.ddjj_ingreso_mensual,
                "existe_otro_ingreso": ddjj.ddjj_existe_otro_ingreso,
                "ingreso_grupo_familiar": ddjj.ddjj_ingreso_grupo_familiar,
                "analizaron": ddjj.ddjj_analizaron,
                "horas_ocio": ddjj.ddjj_horas_ocio,
                "extra_laborales": ddjj.ddjj_extra_laborales,
            },

            "procesos_judiciales": {
                "denunciado_violencia_familiar": ddjj.ddjj_denunciado_violencia_familiar,
                "descripcion_violencia_familiar": ddjj.ddjj_descripcion_violencia_familiar,
                "causa_penal": ddjj.ddjj_causa_penal,
                "descripcion_causa_penal": ddjj.ddjj_descripcion_causa_penal,
                "juicios_filiacion": ddjj.ddjj_juicios_filiacion,
                "descripcion_juicios_filiacion": ddjj.ddjj_descripcion_juicios_filiacion,
            },

            "disponibilidad_adoptiva": {
                key.replace("ddjj_", ""): value
                for key, value in ddjj.__dict__.items()
                if key.startswith("ddjj_subregistro_") or
                key.startswith("ddjj_flex_") or
                key.startswith("ddjj_discapacidad_") or
                key.startswith("ddjj_edad_discapacidad_") or
                key.startswith("ddjj_enfermedad_") or
                key.startswith("ddjj_edad_enfermedad_") or
                key.startswith("ddjj_flex_condiciones_salud") or
                key.startswith("ddjj_flex_salud_edad_") or
                key.startswith("ddjj_hermanos_comp_") or
                key.startswith("ddjj_hermanos_edad_") or
                key.startswith("ddjj_flex_hermanos_comp_") or
                key.startswith("ddjj_flex_hermanos_edad_") or
                key.startswith("subreg_")
            },


            "tramo_final": {
                "acepto_1": ddjj.ddjj_acepto_1,
                "acepto_2": ddjj.ddjj_acepto_2,
                "acepto_3": ddjj.ddjj_acepto_3,
                "acepto_4": ddjj.ddjj_acepto_4,
                "ddjj_firmada": all([
                    ddjj.ddjj_acepto_1 == "Y",
                    ddjj.ddjj_acepto_2 == "Y",
                    ddjj.ddjj_acepto_3 == "Y",
                    ddjj.ddjj_acepto_4 == "Y"
                ]),
                "guardado": {
                    "guardo_1": ddjj.ddjj_guardo_1,
                    "guardo_2": ddjj.ddjj_guardo_2,
                    "guardo_3": ddjj.ddjj_guardo_3,
                    "guardo_4": ddjj.ddjj_guardo_4,
                    "guardo_5": ddjj.ddjj_guardo_5,
                    "guardo_6": ddjj.ddjj_guardo_6,
                    "guardo_7": ddjj.ddjj_guardo_7,
                    "guardo_8": ddjj.ddjj_guardo_8
                },
            }
        }

        if seccion and seccion != "todas":

            mapa_secciones = {
                "datos_personales": ddjj_dict.get("datos_personales"),
                "grupo_familiar_hijos": ddjj_dict.get("grupo_familiar_hijos"),
                "grupo_familiar_otros": ddjj_dict.get("grupo_familiar_otros"),
                "red_de_apoyo": ddjj_dict.get("red_de_apoyo"),
                "informacion_laboral": ddjj_dict.get("informacion_laboral"),
                "procesos_judiciales": ddjj_dict.get("procesos_judiciales"),
                "disponibilidad_adoptiva": ddjj_dict.get("disponibilidad_adoptiva"),
                "tramo_final": ddjj_dict.get("tramo_final")
            }

            seccion_data = mapa_secciones.get(seccion)
            if seccion_data is None:
                raise HTTPException(status_code = 400, detail = "Sección sin datos")

            return { "seccion": seccion, "datos": seccion_data }



        return ddjj_dict

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Error al recuperar la DDJJ: {str(e)}")



@ddjj_router.post("/reabrir", response_model=dict, 
    dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora", "profesional", "adoptante"]))])
def reabrir_ddjj(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    🔓 Reabre una Declaración Jurada (DDJJ) firmada, permitiendo su modificación y nueva firma.

    - Adoptantes solo pueden reabrir su propia DDJJ.
    - Otros roles (administrador, supervisora, profesional) pueden reabrir cualquier DDJJ.
    """

    login = data.get("login")
    if not login:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "<p>El campo 'login' es obligatorio.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    login_actual = current_user["user"]["login"]

    # Obtener roles del usuario actual
    roles_actual = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == login_actual)
        .all()
    )
    roles_actual = [r.description for r in roles_actual]

    # Si es adoptante, solo puede reabrir su propia DDJJ
    if "adoptante" in roles_actual and login != login_actual:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": "<p>No tiene permiso para reabrir la DDJJ de otro usuario.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    usuario = db.query(User).filter(User.login == login).first()
    if not usuario:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "<p>Usuario no encontrado.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if usuario.doc_adoptante_ddjj_firmada != "Y":
        return {
            "success": False,
            "tipo_mensaje": "amarillo",
            "mensaje": "<p>La DDJJ ya se encuentra abierta para edición.</p>",
            "tiempo_mensaje": 4,
            "next_page": "actual"
        }

    try:

        usuario.doc_adoptante_ddjj_firmada = "N"

        db.add(RuaEvento(
            login=login,
            evento_detalle="DDJJ reabierta por el mismo usuario." if login_actual == login else f"DDJJ reabierta por {login_actual}",
            evento_fecha=datetime.now()
        ))

        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": "<p>La DDJJ fue reabierta correctamente. Ahora puede editar y firmar nuevamente.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"<p>Error al reabrir la DDJJ: {str(e)}</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

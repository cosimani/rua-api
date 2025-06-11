

import bcrypt
from fastapi import APIRouter, HTTPException, Depends, status, Form, Query, Request, Body
from sqlalchemy.orm import Session
from database.config import get_db
from helpers.utils import check_consecutive_numbers, detect_hash_and_verify, generar_codigo_para_link, enviar_mail, \
    verificar_recaptcha

from datetime import timedelta, datetime
from security.security import verify_password, create_access_token, get_password_hash, verify_api_key
from helpers.moodle import existe_mail_en_moodle, existe_dni_en_moodle, is_curso_aprobado, get_setting_value, \
    actualizar_clave_en_moodle

from security.security import get_current_user, require_roles, verify_api_key


import os
import re
from models.users import User, Group, UserGroup 
from models.ddjj import DDJJ
from models.proyecto import Proyecto
from models.eventos_y_configs import RuaEvento, LoginIntentoIP
from sqlalchemy.exc import SQLAlchemyError

import html

import hashlib
import re
from datetime import datetime
from typing import Optional

from helpers.utils import check_consecutive_numbers


# # Importa el limiter que definiste en main.py
from main import limiter   



login_router = APIRouter()


# Cargar variables de entorno
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))  # 1 hora por defecto

MAX_INTENTOS = 5
TIEMPO_BLOQUEO_MINUTOS = 30

MAX_INTENTOS_IP = 3
CANTIDAD_USUARIOS_DISTINTOS_PARA_BLOQUEAR_IP = 3
TIEMPO_BLOQUEO_IP_MINUTOS = 30



@login_router.post("/login", response_model = dict)
@limiter.limit("5/minute")
async def login(    
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    bypass_recaptcha: str = Form("Y")
):
    """
    Verifica las credenciales del usuario y devuelve un token si son correctas.
    """

    ip = request.client.host
    now = datetime.now()

    # # ⚠️ Extraer el form primero
    form = await request.form()
    recaptcha_token = form.get("recaptcha_token")

    if not bypass_recaptcha:
      if not recaptcha_token or not await verificar_recaptcha(recaptcha_token, ip):
          return {
              "success": False,
              "tipo_mensaje": "rojo",
              "mensaje": "No se pudo verificar que sos humano.",
              "tiempo_mensaje": 6,
              "next_page": "actual",
          }


    # Buscar registro de esa IP
    intento_ip = db.query(LoginIntentoIP).filter_by(ip=ip).first()
   

    if intento_ip and intento_ip.bloqueo_hasta and intento_ip.bloqueo_hasta > now:
        minutos_restantes = int((intento_ip.bloqueo_hasta - now).total_seconds() / 60)
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"IP bloqueada por múltiples intentos fallidos. Intente nuevamente en {minutos_restantes} minutos.",
            "tiempo_mensaje": 8,
            "next_page": "actual",
        }


    user = db.query(User).filter(User.login == username).first()


    if not user:
        if not intento_ip:
            intento_ip = LoginIntentoIP(ip=ip, usuarios=username, ultimo_intento=now)
            db.add(intento_ip)
        else:
            # Agregar el username a la lista de usuarios si no estaba
            usuarios_actuales = set(intento_ip.usuarios.split(",")) if intento_ip.usuarios else set()
            usuarios_actuales.add(username)
            intento_ip.usuarios = ",".join(usuarios_actuales)

            intento_ip.ultimo_intento = now

            if len(usuarios_actuales) >= CANTIDAD_USUARIOS_DISTINTOS_PARA_BLOQUEAR_IP:
                intento_ip.bloqueo_hasta = now + timedelta(minutes=30)
                intento_ip.usuarios = ""

                evento_bloqueo_ip = RuaEvento(
                    login = "IP",
                    evento_detalle = f"⚠️ La IP {ip} fue bloqueada por intentos fallidos con múltiples usuarios.",
                    evento_fecha = now
                )
                db.add(evento_bloqueo_ip)


        db.commit()

        return {
            "success": False,
            "tipo_mensaje": "rojo",
            # "mensaje": "Usuario no encontrado.",
            "mensaje": "Credenciales inválidas.",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }
        

    # ⛔️ Verificar si está bloqueado
    now = datetime.now()
    if user.bloqueo_hasta and user.bloqueo_hasta > now:
        minutos_restantes = int((user.bloqueo_hasta - now).total_seconds() / 60)
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": f"Usuario bloqueado por intentos fallidos. Intente nuevamente en {minutos_restantes} minutos.",
            "tiempo_mensaje": 8,
            "next_page": "actual",
        }

    # 🔒 Validar si aún no activó su cuenta
    if user.active == "N" and user.activation_code:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "La cuenta aún no fue activada. Por favor revise su correo electrónico.",
            "tiempo_mensaje": 7,
            "next_page": "actual",
        }

    # ❌ No permitir ingreso a usuarios dados de baja
    if user.operativo != "Y":
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "Tu cuenta está dada de baja y no podrás acceder al sistema.",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }

    # 🚨 Si la clave está vacía, considerarla vencida
    if not user.clave or user.clave.strip() == "":
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            # "mensaje": "Tu contraseña ha vencido. Debes recuperarla para asignar una nueva.",
            "mensaje": "Credenciales inválidas.",
            "tiempo_mensaje": 8,
            "next_page": "actual",  # o la ruta que uses
        }

        
    # 🔑 Verificar contraseña
    if not detect_hash_and_verify(password, user.clave):
        user.intentos_login = (user.intentos_login or 0) + 1

        if user.intentos_login >= MAX_INTENTOS:
            user.bloqueo_hasta = now + timedelta(minutes=TIEMPO_BLOQUEO_MINUTOS)
            user.intentos_login = 0  # reiniciar contador después del bloqueo
            db.commit()
            return {
                "success": False,
                "tipo_mensaje": "rojo",
                "mensaje": f"Usuario bloqueado por exceder los intentos fallidos. Espere {TIEMPO_BLOQUEO_MINUTOS} minutos.",
                "tiempo_mensaje": 8,
                "next_page": "actual",
            }

        db.commit()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            # "mensaje": f"Contraseña incorrecta. Intento {user.intentos_login} de {MAX_INTENTOS}.",
            "mensaje": f"Credenciales inválidas. Intento {user.intentos_login} de {MAX_INTENTOS}.",
            "tiempo_mensaje": 6,
            "next_page": "actual",
        }

    # ✅ Login exitoso: resetear intentos
    user.intentos_login = 0
    user.bloqueo_hasta = None
    db.commit()


    # 🧾 Obtener grupo
    group = (
        db.query(Group.description)
        .join(UserGroup, Group.group_id == UserGroup.group_id)
        .filter(UserGroup.login == username)
        .first()
    )
    group_name = group[0] if group else "Sin grupo asignado"

    
    # 🔐 Generar token
    access_token_expires = timedelta(minutes = ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(str(user.login), expires_delta = access_token_expires)

    # 🕓 Último login exitoso
    last_login = (
        db.query(RuaEvento.evento_fecha)
        .filter(RuaEvento.login == username, RuaEvento.evento_detalle.like("%Ingreso exitoso al sistema%"))
        .order_by(RuaEvento.evento_fecha.desc())
        .first()
    )
    last_login_date = last_login[0] if last_login else None

    # 📝 Registrar evento actual
    nuevo_evento = RuaEvento(
        login = username,
        evento_detalle = "Ingreso exitoso al sistema.",
        evento_fecha = datetime.now()
    )
    db.add(nuevo_evento)
    db.commit()

    # 🧾 Construir respuesta base
    response_data = {
        "success": True,
        "tipo_mensaje": "verde",
        "mensaje": "Inicio de sesión exitoso.",
        "tiempo_mensaje": 0,
        "next_page": "portada",

        "access_token": access_token,
        "token_type": "bearer",
        "login": user.login,
        "nombre": user.nombre,
        "apellido": user.apellido,
        "mail": user.mail,
        "group": group_name,
        "last_login": last_login_date,
    }


    return response_data




@login_router.post("/change-password", response_model=dict, dependencies=[Depends(verify_api_key)])
def change_password(
    username: str = Form(...),
    old_password: str = Form(..., description="Ingrese su contraseña actual"),
    new_password: str = Form(..., description="Ingrese la nueva contraseña"),
    confirm_new_password: str = Form(..., description="Confirme la nueva contraseña"),
    db: Session = Depends(get_db)
):
    """
    - La nueva contraseña **siempre** se guarda en bcrypt por más que anteriormenta haya sido md5.
    """
    # Buscar el usuario en la base de datos
    user = db.query(User).filter(User.login == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")


    is_valid_password = detect_hash_and_verify(old_password, user.clave)


    if not is_valid_password:
        raise HTTPException(status_code=401, detail="La contraseña actual es incorrecta.")

    # Verificar que las contraseñas nuevas coincidan
    if new_password != confirm_new_password:
        raise HTTPException(status_code=400, detail="Las contraseñas nuevas no coinciden.")
    
    # ——— Validación de política de contraseñas ———
    # 1) Al menos 6 dígitos numéricos
    dígitos = [c for c in new_password if c.isdigit()]
    if len(dígitos) < 6:
        raise HTTPException(
            status_code=400,
            detail="La contraseña debe contener al menos 6 dígitos numéricos."
        )

    # 2) Sin secuencias de 3 dígitos consecutivos
    if check_consecutive_numbers(new_password):
        raise HTTPException(
            status_code=400,
            detail="La contraseña no puede contener secuencias numéricas consecutivas (p.ej. “1234” o “4321”)."
        )

    # 3) Al menos una letra mayúscula
    if not any(c.isupper() for c in new_password):
        raise HTTPException(
            status_code=400,
            detail="La contraseña debe incluir al menos una letra mayúscula."
        )

    # 4) Al menos una letra minúscula
    if not any(c.islower() for c in new_password):
        raise HTTPException(
            status_code=400,
            detail="La contraseña debe incluir al menos una letra minúscula."
        )
    # ——————————————————————————————————————————

    # Guardar la nueva contraseña en bcrypt (migración de MD5 a bcrypt)
    hashed_new_password = get_password_hash(new_password)
    user.clave = hashed_new_password  # Se sobrescribe la contraseña en bcrypt
    db.commit()


    if re.fullmatch(r"[a-fA-F0-9]{32}", user.clave):
        evento_detalle = "Contraseña cambiada exitosamente. Migrada de MD5 a bcrypt."
    else:
        evento_detalle = "Contraseña cambiada exitosamente."

    # Registrar el evento
    nuevo_evento = RuaEvento(
        login=username,
        evento_detalle=evento_detalle,
        evento_fecha=datetime.now()
    )
    db.add(nuevo_evento)
    db.commit()

    return {
        "message": "Contraseña cambiada exitosamente."
    } 




@login_router.get("/activar-cuenta", response_model = dict)
def activar_cuenta(activacion: str = Query(...), db: Session = Depends(get_db)):
    """
    Activa la cuenta de un usuario si el código de activación es válido.
    Devuelve siempre una respuesta estructurada.
    """
    try:
        user = db.query(User).filter(User.activation_code == activacion).first()
        if not user:
            return {
                "tipo_mensaje": "rojo",
                "mensaje": (
                    "<p>El código de activación no es válido o ya fue usado.</p>"
                    "<p>Si ya activaste tu cuenta, podés ingresar con tu usuario y contraseña.</p>"
                ),
                "tiempo_mensaje": 5,
                "next_page": "login"
            }

        user.active = "Y"
        user.activation_code = None
        db.commit()

        evento = RuaEvento(
            login = user.login,
            evento_detalle = "El usuario activó su cuenta correctamente.",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>Tu cuenta ha sido activada correctamente.</p>"
                "<p>Ya podés ingresar con tu usuario y contraseña.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "login"
        }

    except SQLAlchemyError as e:
        db.rollback()
        return {
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al activar tu cuenta.</p>"
                "<p>Por favor, intentá nuevamente más tarde.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "login"
        }





@login_router.get("/aceptar-invitacion", response_model=dict)
def aceptar_invitacion(
    invitacion: str = Query(..., description="Código único de invitación"),
    respuesta: str = Query(..., regex="^[YN]$", description="Y para aceptar, N para rechazar"),
    db: Session = Depends(get_db)
):
    """
    Permite que el segundo adoptante acepte o rechace la invitación a un proyecto adoptivo.
    Se usa el código único aceptado_code, que se borra una vez procesada la respuesta.
    """

    try:
        proyecto = db.query(Proyecto).filter(Proyecto.aceptado_code == invitacion).first()

        if not proyecto:
            return {
                "tipo_mensaje": "amarillo",
                "mensaje": "<p>El código de invitación caducó o ya fue utilizado.</p><p>Consultá con personal del RUA.</p>",
                "tiempo_mensaje": 6,
                "next_page": "login"
            }

        login_2 = proyecto.login_2
        user2 = db.query(User).filter(User.login == login_2).first()

        if respuesta == "N":
            proyecto.aceptado = "N"
            proyecto.aceptado_code = None
            proyecto.estado_general = "baja_rechazo_invitacion"
            db.commit()

            evento = RuaEvento(
                login=login_2,
                evento_detalle="El usuario rechazó la invitación al proyecto.",
                evento_fecha=datetime.now()
            )
            db.add(evento)
            db.commit()

        elif respuesta == "Y":
            if not user2 or getattr(user2, "doc_adoptante_curso_aprobado", "N") != "Y":
                return {
                    "tipo_mensaje": "naranja",
                    "mensaje": (
                        "<p>No podés aceptar la invitación porque aún no tenés aprobado el Curso Obligatorio.</p>"
                        "<p>Completalo y volvé a ingresar desde el enlace de la invitación.</p>"
                    ),
                    "tiempo_mensaje": 6,
                    "next_page": "login"
                }

            proyecto.aceptado = "Y"
            proyecto.aceptado_code = None
            proyecto.estado_general = "confeccionando"
            db.commit()

            evento = RuaEvento(
                login=login_2,
                evento_detalle="El usuario aceptó la invitación al proyecto.",
                evento_fecha=datetime.now()
            )
            db.add(evento)
            db.commit()

        # Enviar correo a login_1
        try:
            user1 = db.query(User).filter(User.login == proyecto.login_1).first()
            protocolo = get_setting_value(db, "protocolo")
            host = get_setting_value(db, "donde_esta_alojado")
            puerto = get_setting_value(db, "puerto_tcp")

            puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
            host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host
            link = f"{protocolo}://{host_con_puerto}/login"

            if user1 and user1.mail:
                estado_respuesta = "aceptado" if respuesta == "Y" else "rechazado"
                color = "#28a745" if respuesta == "Y" else "#dc3545"
                texto_botón = "Ir al sistema" if respuesta == "Y" else "Ir al sistema"
                
                if respuesta == "Y":
                    mensaje_personalizado = (
                        "<p>Luego de esta aceptación, se procederá a solicitar a la Supervisión del RUA la revisión del proyecto adoptivo.</p>"
                        "<p>Si lo deseás, podés volver a ingresar al sistema para continuar el proceso.</p>"
                    )
                else:
                    mensaje_personalizado = (
                        "<p>Con esta decisión, el proyecto adoptivo ha sido cancelado.</p>"
                        "<p>Te invitamos a ingresar al sistema si deseás presentar un nuevo proyecto adoptivo.</p>"
                    )

                cuerpo = f"""
                <html>
                <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                    <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                    <tr>
                        <td align="center">
                        <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                            <tr>
                            <td style="font-size: 22px; color: #007bff;">
                                <strong>Respuesta a la invitación</strong>
                            </td>
                            </tr>
                            <tr>
                            <td style="padding-top: 20px; font-size: 16px;">
                                <p>{user2.nombre} {user2.apellido} (DNI: {login_2}) ha <strong>{estado_respuesta}</strong> la invitación al proyecto adoptivo.</p>
                                {mensaje_personalizado}
                            </td>
                            </tr>
                            <tr>
                            <td align="center" style="padding: 20px 0;">
                                <a href="{link}"
                                    style="display: inline-block; padding: 12px 25px; font-size: 16px; color: #ffffff; background-color: {color}; text-decoration: none; border-radius: 8px; font-weight: bold;">
                                    {texto_botón}
                                </a>
                            </td>
                            </tr>
                            <tr>
                            <td align="center" style="font-size: 16px;">
                                <p><strong>Muchas gracias</strong></p>
                            </td>
                            </tr>
                            <tr>
                            <td style="padding-top: 30px;">
                                <hr style="border: none; border-top: 1px solid #dee2e6;">
                                <p style="font-size: 14px; color: #6c757d; margin-top: 20px;">
                                <strong>Registro Único de Adopción (RUA) de Córdoba</strong>
                                </p>
                            </td>
                            </tr>
                        </table>
                        </td>
                    </tr>
                    </table>
                </body>
                </html>
                """

                enviar_mail(destinatario=user1.mail, asunto="Respuesta a la invitación - RUA", cuerpo=cuerpo)


        except Exception as e:
            print("⚠️ Error al enviar notificación a login_1:", str(e))
            

        # Respuesta final para quien aceptó o rechazó
        return {
            "tipo_mensaje": "verde" if respuesta == "Y" else "amarillo",
            "mensaje": "<p>Has aceptado la invitación. Ya pueden continuar el proceso en el sistema RUA.</p>" if respuesta == "Y" else "<p>Has rechazado la invitación al proyecto adoptivo.</p>",
            "tiempo_mensaje": 6,
            "next_page": "login"
        }

    except SQLAlchemyError:
        db.rollback()
        return {
            "tipo_mensaje": "amarillo",
            "mensaje": "<p>Ocurrió un error al procesar tu respuesta.</p><p>Por favor, intentá nuevamente más tarde.</p>",
            "tiempo_mensaje": 6,
            "next_page": "login"
        }





@login_router.post("/recuperar-clave", response_model = dict)
async def recuperar_clave(
    dni: str = Form(...),
    mail: str = Form(...),
    recaptcha_token: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    📩 Solicita recuperación de contraseña.
    Valida DNI y correo, y envía un mail con un enlace para restablecer la clave.
    """

    # ✅ Verificar reCAPTCHA
    if not await verificar_recaptcha(recaptcha_token):
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "<p>Falló la verificación reCAPTCHA. Por favor, intentá de nuevo.</p>",
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }
    
    
    user = db.query(User).filter(User.login == dni, User.mail == mail).first()

    if not user:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>No se encontró ningún usuario con ese DNI y correo.</p>"
                "<p>Verificá los datos o contactá con RUA.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if user.active != "Y" and user.activation_code:
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Tu cuenta aún no fue activada.</p>"
                "<p>Revisá el correo de activación que recibiste o solicitá que te lo reenviemos.</p>"
            ),
            "tiempo_mensaje": 8,
            "next_page": "actual"
        }


    try:
        # Asunto del correo
        asunto = "Recuperación de contraseña - Sistema RUA"

        # Generar código único de recuperación
        act_code = generar_codigo_para_link(16)
        user.recuperacion_code = act_code
        db.commit()


        # Configuración del sistema
        protocolo = get_setting_value(db, "protocolo")
        host = get_setting_value(db, "donde_esta_alojado")
        puerto = get_setting_value(db, "puerto_tcp")
        endpoint = get_setting_value(db, "endpoint_recuperar_clave")

        # Asegurar formato correcto del endpoint
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint


        # Determinar si incluir el puerto en la URL
        puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
        host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

        # Construir el enlace final
        link = f"{protocolo}://{host_con_puerto}{endpoint}?activacion={act_code}"


        # Cuerpo del mail en estilo institucional
        cuerpo = f"""
            <html>
            <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                <tr>
                    <td align="center">
                    <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                        <tr>
                        <td style="font-size: 24px; color: #007bff; padding-bottom: 20px;">
                            <strong>Recuperación de contraseña</strong>
                        </td>
                        </tr>
                        <tr>
                        <td style="font-size: 17px; padding-bottom: 10px;">
                            Hola,
                        </td>
                        </tr>
                        <tr>
                        <td style="font-size: 17px; padding-bottom: 10px;">
                            Desde este mail podrás cambiar tu contraseña de acceso al Sistema RUA.
                        </td>
                        </tr>
                        <tr>
                        <td style="font-size: 17px; padding-bottom: 10px;">
                            Hacé clic en el siguiente botón:
                        </td>
                        </tr>
                        <tr>
                        <td align="center" style="padding: 20px 0 30px 0;">
                            <!-- BOTÓN RESPONSIVE -->
                            <table cellpadding="0" cellspacing="0" border="0" style="border-radius: 8px;">
                            <tr>
                                <td align="center" bgcolor="#0d6efd" style="border-radius: 8px;">
                                <a href="{link}"
                                    target="_blank"
                                    style="display: inline-block; padding: 12px 20px; font-size: 16px; color: #ffffff; background-color: #0d6efd; text-decoration: none; border-radius: 8px; font-weight: bold;">
                                    🔐 Elegir nueva contraseña
                                </a>
                                </td>
                            </tr>
                            </table>
                        </td>
                        </tr>
                        <tr>
                        <td style="font-size: 17px; padding-bottom: 30px;">
                            Este enlace tiene validez limitada.
                        </td>
                        </tr>
                        <tr>
                        <td>
                            <hr style="border: none; border-top: 1px solid #dee2e6; margin: 40px 0;">
                            <p style="font-size: 15px; color: #6c757d;">
                            <strong>Registro Único de Adopción (RUA) de Córdoba</strong>
                            </p>
                        </td>
                        </tr>
                    </table>
                    </td>
                </tr>
                </table>
            </body>
            </html>
            """


        enviar_mail(destinatario = mail, asunto = asunto, cuerpo = cuerpo)

        evento = RuaEvento(
            login = dni,
            evento_detalle = "Se solicitó el mail para recuperación de contraseña.",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>Se envió un correo para recuperar tu contraseña.</p>"
                "<p>Revisá tu bandeja de entrada y correo no deseado.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "login"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al enviar el correo de recuperación.</p>"
                f"<p>{str(e)}</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }


@login_router.post("/reenviar-activacion", response_model=dict)
async def reenviar_activacion(
    dni: str = Form(...),
    mail: str = Form(...),
    recaptcha_token: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    📩 Reenvía el correo de activación si el usuario existe y aún no activó su cuenta.
    """

    # ✅ Validar reCAPTCHA
    if not await verificar_recaptcha(recaptcha_token, threshold=0.3):
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>No pudimos validar que seas una persona real.</p>"
                "<p>Por favor, actualizá la página e intentá nuevamente.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    
    user = db.query(User).filter(User.login == dni, User.mail == mail).first()

    if not user:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>No se encontró ningún usuario con ese DNI y correo.</p>"
                "<p>Verificá los datos o contactá con RUA.</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

    if user.active == "Y":
        return {
            "success": False,
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>Tu cuenta ya está activada.</p>"
                "<p>Ingresá con tu usuario y contraseña.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "login"
        }

    # Generar código de activación si no tiene (por seguridad)
    if not user.activation_code:
        user.activation_code = generar_codigo_para_link(16)
        db.commit()

    try:

        # Preparar link y mail
        protocolo = get_setting_value(db, "protocolo")
        host = get_setting_value(db, "donde_esta_alojado")
        puerto = get_setting_value(db, "puerto_tcp")
        endpoint = get_setting_value(db, "endpoint_alta_adoptante")

        # Asegurar formato correcto del endpoint
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        # Determinar si incluir el puerto en la URL
        puerto_predeterminado = (protocolo == "http" and puerto == "80") or (protocolo == "https" and puerto == "443")
        host_con_puerto = f"{host}:{puerto}" if puerto and not puerto_predeterminado else host

        # Construir el enlace final
        link_activacion = f"{protocolo}://{host_con_puerto}{endpoint}?activacion={user.activation_code}"




        asunto = "Activación de cuenta - Sistema RUA"

        cuerpo = f"""
            <html>
            <body style="margin: 0; padding: 0; background-color: #f8f9fa;">
                <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8f9fa; padding: 20px;">
                <tr>
                    <td align="center">
                    <table cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 10px; padding: 30px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #343a40; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                        <tr>
                        <td style="font-size: 24px; color: #007bff; padding-bottom: 20px;">
                            <strong>Hola {user.nombre} {user.apellido}</strong>
                        </td>
                        </tr>
                        <tr>
                        <td style="font-size: 17px; padding-bottom: 10px;">
                            El sistema ha creado tu cuenta en <strong>RUA</strong>, el Registro Único de Adopción de Córdoba.
                        </td>
                        </tr>
                        <tr>
                        <td style="font-size: 17px; padding-bottom: 10px;">
                            Para completar tu registro y comenzar a utilizar la plataforma, activá tu cuenta haciendo clic en el siguiente botón:
                        </td>
                        </tr>
                        <tr>
                        <td align="center" style="padding: 30px 0;">
                            <!-- BOTÓN RESPONSIVE -->
                            <table cellpadding="0" cellspacing="0" border="0" style="border-radius: 8px;">
                            <tr>
                                <td align="center" bgcolor="#0d6efd" style="border-radius: 8px;">
                                <a href="{link_activacion}"
                                    target="_blank"
                                    style="display: inline-block; padding: 12px 25px; font-size: 16px; color: #ffffff; background-color: #0d6efd; text-decoration: none; border-radius: 8px; font-weight: bold;">
                                    🔓 Activar mi cuenta
                                </a>
                                </td>
                            </tr>
                            </table>
                        </td>
                        </tr>
                        <tr>
                        <td align="center" style="font-size: 17px; padding-bottom: 30px;">
                            <strong>Muchas gracias</strong>
                        </td>
                        </tr>
                        <tr>
                        <td>
                            <hr style="border: none; border-top: 1px solid #dee2e6; margin: 40px 0;">
                            <p style="font-size: 15px; color: #6c757d;">
                            <strong>Registro Único de Adopción (RUA) - Poder Judicial de Córdoba</strong>
                            </p>
                        </td>
                        </tr>
                    </table>
                    </td>
                </tr>
                </table>
            </body>
            </html>
            """



        enviar_mail(user.mail, asunto, cuerpo)

        evento = RuaEvento(
            login=user.login,
            evento_detalle="Se reenvió el mail de activación de cuenta.",
            evento_fecha=datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>Se envió nuevamente el correo de activación.</p>"
                "<p>Revisá tu bandeja de entrada y correo no deseado.</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "/login"
        }

    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al reenviar el correo de activación.</p>"
                f"<p>{str(e)}</p>"
            ),
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }



@login_router.post("/nueva-clave", response_model = dict)
async def establecer_nueva_clave(
    activacion: str = Form(..., description = "Código de recuperación enviado por correo"),
    clave: str = Form(..., description = "Nueva contraseña"),
    recaptcha_token: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    ✅ Establece una nueva contraseña usando el código de recuperación.
    Hashea y actualiza la contraseña en la base local y en Moodle si corresponde.
    """

    # 👮‍♂️ Validar reCAPTCHA
    if not recaptcha_token or not await verificar_recaptcha(recaptcha_token):
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": "<p>Error en la validación reCAPTCHA. Por favor intentá de nuevo.</p>",
            "tiempo_mensaje": 6,
            "next_page": "actual"
        }
    

    user = db.query(User).filter(User.recuperacion_code == activacion).first()

    if not user:
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>El enlace de recuperación no es válido o ya fue utilizado.</p>"
                "<p>Solicitá uno nuevo desde la opción '¿Querés recuperar tu contraseña?'.</p>"
            ),
            "tiempo_mensaje": 8,
            "next_page": "login"
        }

    if user.active != "Y":
        return {
            "success": False,
            "tipo_mensaje": "naranja",
            "mensaje": (
                "<p>Tu cuenta aún no fue activada.</p>"
                "<p>Por favor activala desde el mail que recibiste antes de cambiar tu contraseña.</p>"
            ),
            "tiempo_mensaje": 8,
            "next_page": "login"
        }

    try:
        # 🔒 Validación básica
        if not clave.isdigit() or len(clave) < 6:
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": (
                    "<p>La contraseña debe tener al menos 6 dígitos y estar compuesta solo por números.</p>"
                ),
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        if check_consecutive_numbers(clave):
            return {
                "success": False,
                "tipo_mensaje": "naranja",
                "mensaje": "<p>La contraseña no puede tener números consecutivos (como 123456 o 654321).</p>",
                "tiempo_mensaje": 6,
                "next_page": "actual"
            }

        # 🔐 Guardar nueva contraseña (bcrypt)
        user.clave = get_password_hash(clave)
        user.recuperacion_code = None  # ya fue usada
        db.commit()

        # 🌐 Actualizar también en Moodle si es adoptante
        grupo = (
            db.query(Group.description)
            .join(UserGroup, Group.group_id == UserGroup.group_id)
            .filter(UserGroup.login == user.login)
            .first()
        )


        # 🌐 Intentar actualizar también en Moodle si es adoptante, pero no interrumpir si falla
        mensaje_extra = ""
        if grupo and grupo.description == "adoptante":
            try:
                actualizar_clave_en_moodle(user.mail, clave, db)
            except Exception as e:
                mensaje_extra = (
                    "<p>⚠️ La contraseña se actualizó en el sistema, pero no fue posible sincronizarla con el campus virtual (Moodle).</p>"
                    "<p>Si necesitás acceder al campus, por favor contactá con RUA.</p>"
                )


        # 📝 Registrar evento
        evento = RuaEvento(
            login = user.login,
            evento_detalle = "El usuario estableció una nueva contraseña mediante recuperación.",
            evento_fecha = datetime.now()
        )
        db.add(evento)
        db.commit()

        return {
            "success": True,
            "tipo_mensaje": "verde",
            "mensaje": (
                "<p>Tu contraseña fue actualizada exitosamente.</p>"
                "<p>Ya podés ingresar al sistema RUA con tu nueva clave.</p>" +
                mensaje_extra
            ),
            "tiempo_mensaje": 10 if mensaje_extra else 6,
            "next_page": "login"
        }


    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "tipo_mensaje": "rojo",
            "mensaje": (
                "<p>Ocurrió un error al guardar la nueva contraseña.</p>"
                f"<p>{str(e)}</p>"
            ),
            "tiempo_mensaje": 5,
            "next_page": "actual"
        }

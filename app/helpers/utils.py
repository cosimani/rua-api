import hashlib
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, and_
from models.users import User
from datetime import datetime, date
import re
import os
import secrets, string
import bcrypt

from fpdf import FPDF

from typing import Optional

from fastapi import HTTPException

from models.users import User
from models.proyecto import Proyecto


import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

from models.eventos_y_configs import SecSettings



def get_setting_value(db: Session, setting_name: str) -> str:
    """
    Obtiene el valor de una configuración desde la tabla sec_settings.
    """
    setting = db.query(SecSettings).filter(SecSettings.set_name == setting_name).first()
    return setting.set_value if setting else None




def enviar_mail(destinatario: str, asunto: str, cuerpo: str):
    # Datos del remitente y servidor SMTP desde variables de entorno
    remitente = os.getenv("MAIL_REMITENTE")  # ejemplo: sistemarua@justiciacordoba.gob.ar
    nombre_remitente = os.getenv("MAIL_NOMBRE_REMITENTE", "RUA")
    password = os.getenv("MAIL_PASSWORD")
    smtp_server = os.getenv("MAIL_SERVER", "smtp.office365.com")
    smtp_port = int(os.getenv("MAIL_PORT", 587))

    # Crear el mensaje
    msg = MIMEMultipart()
    msg["From"] = formataddr((nombre_remitente, remitente))  # Ej: "RUA <sistemarua@...>"
    # msg["To"] = destinatario
    msg["To"] = "cesarosimani@gmail.com"
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html")) 

    # Enviar el correo
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(remitente, password)
            server.send_message(msg)
    except Exception as e:
        print(f"❌ Error al enviar el correo: {e}")
        raise




class EstadisticasPDF(FPDF):

    def header(self):
        self.set_font("Arial", "B", 12)
        self.cell(0, 10, "SERVICIO DE GUARDA Y ADOPCIÓN", ln=True, align="C")
        self.set_font("Arial", "B", 11)
        self.cell(0, 10, "REGISTRO ÚNICO DE ADOPCIONES Y EQUIPO TÉCNICO DE ADOPCIONES", ln=True, align="C")
        self.set_font("Arial", "I", 9)
        self.cell(0, 10, "INFORME DE ESTADÍSTICAS GENERALES", ln=True, align="C")
        self.ln(4)


    def section_title(self, title):
        self.set_font("Arial", "B", 12)
        self.set_fill_color(230, 230, 230)
        self.cell(0, 8, title, ln=True, fill=True)
        self.ln(2)


    def add_table(self, data, col_widths=None):
        if not col_widths:
            col_widths = [190 // len(data[0])] * len(data[0])
        self.set_font("Arial", "B", 9)
        for i, header in enumerate(data[0]):
            self.cell(col_widths[i], 7, header, border=1, align="C")
        self.ln()
        self.set_font("Arial", "", 9)
        for row in data[1:]:
            for i, datum in enumerate(row):
                self.cell(col_widths[i], 7, str(datum), border=1, align="C")
            self.ln()
        self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "I", 8)
        self.cell(0, 10, "Informe generado automáticamente - RUA", 0, 0, "C")



def calcular_estadisticas_generales(db: Session) -> dict:
    try:
        sin_activar = db.query(User).filter(User.active == 'N').count()
        usuarios_activos = db.query(User).count()
        sin_curso_sin_ddjj = db.query(User).filter(User.doc_adoptante_curso_aprobado == 'N', User.doc_adoptante_ddjj_firmada == 'N').count()
        con_curso_sin_ddjj = db.query(User).filter(User.doc_adoptante_curso_aprobado == 'Y', User.doc_adoptante_ddjj_firmada == 'N').count()
        con_curso_con_ddjj = db.query(User).filter(User.doc_adoptante_curso_aprobado == 'Y', User.doc_adoptante_ddjj_firmada == 'Y').count()

        pretensos_presentando_documentacion = db.query(User).filter(
            User.doc_adoptante_curso_aprobado == 'Y',
            User.doc_adoptante_ddjj_firmada == 'Y',
            or_(
                User.doc_adoptante_estado == 'inicial_cargando',
                User.doc_adoptante_estado == 'actualizando'
            )
        ).count()

        pretensos_aprobados = db.query(User).filter(
            User.doc_adoptante_curso_aprobado == 'Y',
            User.doc_adoptante_ddjj_firmada == 'Y',
            User.doc_adoptante_estado == 'aprobado',
        ).count()

        pretensos_rechazados = db.query(User).filter(
            User.doc_adoptante_curso_aprobado == 'Y',
            User.doc_adoptante_ddjj_firmada == 'Y',
            User.doc_adoptante_estado == 'rechazado',
        ).count()

        # Los estados de proyectos son:
        # ESTADOS_PROYECTO = [ "Inactivo", "Activo", "Entrevistas", "En valoración", "No viable", "En suspenso", "Viable", 
        # "En carpeta", "En cancelación", "Cancelado","Baja definitiva", "Preparando entrevistas", "Adopción definitiva" ]


        proyectos_monoparentales = db.query(Proyecto).filter(Proyecto.proyecto_tipo == 'Monoparental').count()
        proyectos_en_pareja = db.query(Proyecto).filter(Proyecto.proyecto_tipo != 'Monoparental').count()

        proyectos_monoparentales_subiendo_documentacion = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                or_(
                    Proyecto.estado_general == 'confeccionando',
                    Proyecto.estado_general == 'actualizando'
                )
            )
            .count()
        )

        proyectos_en_pareja_subiendo_documentacion = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                or_(
                    Proyecto.estado_general == 'confeccionando',
                    Proyecto.estado_general == 'actualizando'
                )
            )
            .count()
        )


        proyectos_monoparentales_en_revision_por_supervision = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'en_revision'
            )
            .count()
        )

        proyectos_en_pareja_en_revision_por_supervision = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                Proyecto.estado_general == 'en_revision'
            )
            .count()
        )


        # Un proyecto está para calendarizar es cuando la supervisión aprueba el proeycto,
        # en este moemtno pasa al estado Preparando entrevistas y se le coloca el nro. de orden
        proyectos_monoparentales_aprobados_para_calendarizar = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'aprobado',
                # Esto controla que el nro. de orden esté asignado
                and_(
                    func.nullif(func.trim(Proyecto.nro_orden_rua), "") != None,
                    func.trim(Proyecto.nro_orden_rua) != "0"
                )
            )
            .count()
        )

        proyectos_en_pareja_aprobados_para_calendarizar = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                Proyecto.estado_general == 'aprobado',
                # Esto controla que el nro. de orden esté asignado
                and_(
                    func.nullif(func.trim(Proyecto.nro_orden_rua), "") != None,
                    func.trim(Proyecto.nro_orden_rua) != "0"
                )
            )
            .count()
        )
        
        

        entrevistando_monoparental = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                or_(
                    Proyecto.estado_general == 'confeccionando',
                    Proyecto.estado_general == 'entrevistando',
                    Proyecto.estado_general == 'para_valorar'
                )
            )
            .count()
        )

        entrevistando_en_pareja = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                or_(
                    Proyecto.estado_general == 'confeccionando',
                    Proyecto.estado_general == 'entrevistando',
                    Proyecto.estado_general == 'para_valorar'
                )
            )
            .count()
        )

        proyectos_monoparentales_en_suspenso = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'en_suspenso'
            )
            .count()
        )

        proyectos_en_pareja_en_suspenso = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                Proyecto.estado_general == 'en_suspenso'
            )
            .count()
        )

        proyectos_monoparentales_no_viable = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'no_viable'
            )
            .count()
        )

        proyectos_en_pareja_no_viable = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                Proyecto.estado_general == 'no_viable'
            )
            .count()
        )

        proyectos_monoparentales_baja_definitiva = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                or_(
                    Proyecto.estado_general == 'baja_anulacion',
                    Proyecto.estado_general == 'baja_caducidad',
                    Proyecto.estado_general == 'baja_por_convocatoria',
                    Proyecto.estado_general == 'baja_rechazo_invitacion'
                )
            )
            .count()
        )

        proyectos_en_pareja_baja_definitiva = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                or_(
                    Proyecto.estado_general == 'baja_anulacion',
                    Proyecto.estado_general == 'baja_caducidad',
                    Proyecto.estado_general == 'baja_por_convocatoria',
                    Proyecto.estado_general == 'baja_rechazo_invitacion'
                )
            )
            .count()
        )


        proyectos_monoparentales_sin_valorar = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'aprobado',
                or_(
                    Proyecto.estado_general == 'confeccionando',
                    Proyecto.estado_general == 'entrevistando',
                    Proyecto.estado_general == 'para_valorar'
                )
            )
            .count()
        )

        proyectos_en_pareja_sin_valorar = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                Proyecto.estado_general == 'aprobado',
                or_(
                    Proyecto.estado_general == 'confeccionando',
                    Proyecto.estado_general == 'entrevistando',
                    Proyecto.estado_general == 'para_valorar'
                )
            )
            .count()
        )

        proyectos_aprobados_totales = db.query(Proyecto).filter(
            Proyecto.estado_general == 'aprobado'
        ).count()



        proyectos_aprobados_totales = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'aprobado'
            )
            .count()
        )
                

        usuarios_sin_proyecto = (
            db.query(User)
            .outerjoin(Proyecto, (User.login == Proyecto.login_1) | (User.login == Proyecto.login_2))
            .filter(User.doc_adoptante_estado == "aprobado", Proyecto.proyecto_id.is_(None))
            .count()
        )
        proyectos_monoparentales_sin_nro_orden = db.query(Proyecto).filter(Proyecto.proyecto_tipo == "Monoparental", 
                                                                           Proyecto.nro_orden_rua.is_(None)).count()
        
        proyectos_en_pareja_sin_nro_orden = (
            db.query(Proyecto)
            .filter(Proyecto.proyecto_tipo != "Monoparental", Proyecto.nro_orden_rua.is_(None)).count()
        )

        proyectos_en_valoracion = db.query(Proyecto).filter(Proyecto.estado_general == "calendarizando").count()

        
        proyectos_monoparental_viable = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo == 'Monoparental',
                Proyecto.estado_general == 'viable_disponible'
            )
            .count() 
        )

        proyectos_en_pareja_viable = (
            db.query(Proyecto)
            .filter(
                Proyecto.proyecto_tipo != 'Monoparental',
                Proyecto.estado_general == 'viable_disponible'
            )
            .count() 
        )



        return {
            "sin_activar": sin_activar,
            "usuarios_activos": usuarios_activos,
            "sin_curso_sin_ddjj": sin_curso_sin_ddjj,
            "con_curso_sin_ddjj": con_curso_sin_ddjj,
            "con_curso_con_ddjj": con_curso_con_ddjj,

            "proyectos_sin_valorar_subregistros_altos": 26,
            "proyectos_viables_disponibles": 52,
            "proyectos_enviados_juzgado": 4,
            "proyectos_en_guarda": 37,
            "proyectos_adopcion_definitiva": 23,
            "proyectos_en_vinculacion": 26,       
            "convocatorias_con_adopcion_definitiva_": 7,

            "pretensos_presentando_documentacion": pretensos_presentando_documentacion,
            "pretensos_aprobados": pretensos_aprobados,
            "pretensos_rechazados": pretensos_rechazados,
                        
            "proyectos_monoparentales": proyectos_monoparentales,
            "proyectos_en_pareja": proyectos_en_pareja,

            "proyectos_monoparentales_subiendo_documentacion": proyectos_monoparentales_subiendo_documentacion,
            "proyectos_en_pareja_subiendo_documentacion": proyectos_en_pareja_subiendo_documentacion,
            "proyectos_monoparentales_en_revision_por_supervision": proyectos_monoparentales_en_revision_por_supervision,
            "proyectos_en_pareja_en_revision_por_supervision": proyectos_en_pareja_en_revision_por_supervision,

            "proyectos_monoparentales_aprobados_para_calendarizar": proyectos_monoparentales_aprobados_para_calendarizar,
            "proyectos_en_pareja_aprobados_para_calendarizar": proyectos_en_pareja_aprobados_para_calendarizar,

            "entrevistando_en_pareja": entrevistando_en_pareja,
            "entrevistando_monoparental": entrevistando_monoparental,

            "proyectos_monoparentales_en_suspenso": proyectos_monoparentales_en_suspenso,
            "proyectos_en_pareja_en_suspenso": proyectos_en_pareja_en_suspenso,
            "proyectos_monoparentales_no_viable": proyectos_monoparentales_no_viable,
            "proyectos_en_pareja_no_viable": proyectos_en_pareja_no_viable,
            "proyectos_monoparentales_baja_definitiva": proyectos_monoparentales_baja_definitiva,
            "proyectos_en_pareja_baja_definitiva": proyectos_en_pareja_baja_definitiva,

            "proyectos_monoparentales_sin_valorar": proyectos_monoparentales_sin_valorar,            
            "proyectos_en_pareja_sin_valorar": proyectos_en_pareja_sin_valorar,

            "usuarios_sin_proyecto": usuarios_sin_proyecto,
            "proyectos_monoparentales_sin_nro_orden": proyectos_monoparentales_sin_nro_orden,
            "proyectos_en_pareja_sin_nro_orden": proyectos_en_pareja_sin_nro_orden,

            "proyectos_en_valoracion": proyectos_en_valoracion,
            
            "proyectos_monoparental_viable": proyectos_monoparental_viable,
            "proyectos_en_pareja_viable": proyectos_en_pareja_viable,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    


def check_consecutive_numbers(password: str) -> bool:
    """
    Verifica si la contraseña contiene más de dos números consecutivos.
    Retorna True si hay números consecutivos, de lo contrario False.
    """
    for i in range(len(password) - 2):
        if (
            int(password[i + 1]) == int(password[i]) + 1
            and int(password[i + 2]) == int(password[i + 1]) + 1
        ):
            return True
    return False

def get_user_name_by_login(db: Session, login: str):
    """
    Consulta en la tabla sec_users por el login y devuelve un nombre y apellido concatenados.
    """
    user = db.query(User.nombre, User.apellido).filter(User.login == login).first()
    if user:
        return f"{user.nombre} {user.apellido}"  # Usamos f-string para concatenar
    return ""



def build_subregistro_string(user):
    subregistros = {
        "1": user.subregistro_1,
        "2": user.subregistro_2,
        "3": user.subregistro_3,
        "4": user.subregistro_4,
        "5a": user.subregistro_5_a,
        "5b": user.subregistro_5_b,
        "5c": user.subregistro_5_c,
        "6a": user.subregistro_6_a,
        "6b": user.subregistro_6_b,
        "6c": user.subregistro_6_c,
        "6d": user.subregistro_6_d,
        "62": user.subregistro_6_2,
        "63": user.subregistro_6_3,
        "63+": user.subregistro_6_mas_de_3,
        "f": user.subregistro_flexible,
        "o": user.subregistro_otra_provincia,
    }
    return " ; ".join([key for key, value in subregistros.items() if value == "Y"])



def parse_date(date_value):
    """
    Valida y devuelve una fecha en formato 'YYYY-MM-DD'.
    Puede manejar objetos date, datetime o cadenas en formato 'YYYY-MM-DD' y 'DD/MM/YYYY'.
    Si no es válida, devuelve una cadena vacía.
    """
    if isinstance(date_value, (date, datetime)):
        return date_value.strftime("%Y-%m-%d")
    elif isinstance(date_value, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(date_value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""



def calculate_age(birthdate: str) -> int:
    """Calcula la edad a partir de una fecha de nacimiento en formato 'YYYY-MM-DD'."""
    if not birthdate:
        return 0  # Si no hay fecha, devuelve 0
    try:
        birthdate_date = datetime.strptime(birthdate, "%Y-%m-%d").date()
        today = datetime.now().date()
        age = today.year - birthdate_date.year - ((today.month, today.day) < (birthdate_date.month, birthdate_date.day))
        return age
    except ValueError:
        return 0  # Si la fecha no tiene el formato correcto, devuelve 0
    

def validar_correo(correo: str) -> bool:
    # Expresión regular básica para validar correos electrónicos
    patron = r"(^[\w\.\-]+@[\w\-]+\.[\w\.\-]+$)"
    return re.match(patron, correo) is not None


def normalizar_y_validar_dni(dni: str) -> Optional[str]:
    """
    Normaliza y valida un DNI:
    - Elimina espacios, puntos y comas.
    - Verifica que tenga entre 6 y 9 dígitos numéricos.
    
    Retorna el DNI limpio si es válido, o None si no lo es.
    """
    if not dni:
        return None

    # Eliminar espacios, puntos y comas
    dni_limpio = re.sub(r"[ .,]", "", dni)

    if dni_limpio.isdigit() and 6 <= len(dni_limpio) <= 9:
        return dni_limpio
    return None



def generar_codigo_para_link(length: int = 10) -> str:
    """Genera un código alfanumérico aleatorio de la longitud especificada."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))



def edad_como_texto(nacimiento: date) -> str:
    today = date.today()
    años = today.year - nacimiento.year
    meses = today.month - nacimiento.month
    dias = today.day - nacimiento.day

    if dias < 0:
        meses -= 1
    if meses < 0:
        años -= 1
        meses += 12

    if años > 0:
        if años == 1:
            return "1 año"
        else:
            return f"{años} años"
    elif meses > 0:
        if meses == 1:
            return "1 mes"
        else:
            return f"{meses} meses"
    else:
        return "Menos de 1 mes"



def verify_md5(password: str, hash_md5: str) -> bool:
    """Verifica si la contraseña coincide con el hash MD5 almacenado."""
    return hashlib.md5(password.encode()).hexdigest() == hash_md5




def detect_hash_and_verify(password: str, stored_hash: str) -> bool:
    """Detecta si el hash almacenado es MD5 o Bcrypt y verifica la contraseña."""
    if re.fullmatch(r"[a-fA-F0-9]{32}", stored_hash):  # MD5 hash (32 caracteres hexadecimales)
        return verify_md5(password, stored_hash)
    elif stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):  # Bcrypt hash
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    else:
        return False  # No es un formato reconocido



def capitalizar_nombre(nombre: str) -> str:
    """
    Capitaliza un nombre completo, manteniendo preposiciones en minúscula.
    Ejemplo: "lidia angélica de gomez" → "Lidia Angélica de Gomez"
    """
    preposiciones = {"de", "del", "la", "las", "los", "y"}
    palabras = nombre.lower().split()
    return " ".join([
        palabra if palabra in preposiciones else palabra.capitalize()
        for palabra in palabras
    ])



def normalizar_celular(celular: str) -> dict:
    """
    Limpia, corrige y valida un número de celular.
    Acepta guiones pero rechaza letras u otros caracteres inválidos.

    Devuelve:
    - 'valido': True/False
    - 'celular': versión limpia si fue válido
    - 'motivo': motivo si no fue válido
    """
    if not celular:
        return {
            "valido": False,
            "motivo": "Número no proporcionado"
        }

    # ❌ Rechazar si contiene letras o símbolos extraños (emojis, comillas, etc.)
    if re.search(r"[^\d\s\-\+\(\)\.]", celular):  # permite dígitos, espacios, guiones, paréntesis y puntos
        return {
            "valido": False,
            "motivo": "El número contiene caracteres no válidos (como letras o símbolos)"
        }

    # ✅ Eliminar espacios, guiones, paréntesis y puntos
    celular_limpio = re.sub(r"[ \-\(\)\.]", "", celular)

    # Si empieza con 0, quitarlo
    if celular_limpio.startswith("0"):
        celular_limpio = celular_limpio[1:]

    # Si empieza con 549 sin +, agregar +
    if celular_limpio.startswith("549") and not celular_limpio.startswith("+"):
        celular_limpio = "+" + celular_limpio

    # Si empieza con 11, 351, etc., agregar +54
    if re.match(r"^(11|15|2\d{2}|3\d{2}|4\d{2})\d{6,7}$", celular_limpio):
        celular_limpio = "+54" + celular_limpio

    # Validar largo (solo dígitos)
    digitos = re.sub(r"[^\d]", "", celular_limpio)
    if len(digitos) < 10 or len(digitos) > 15:
        return {
            "valido": False,
            "motivo": "Cantidad de dígitos inválida (debe tener entre 10 y 15)"
        }

    return {
        "valido": True,
        "celular": celular_limpio
    }


def convertir_booleans_a_string(d: dict) -> dict:
    convertido = {}
    for k, v in d.items():
        if isinstance(v, bool):
            convertido[k] = "Y" if v else "N"
        else:
            convertido[k] = v
    return convertido

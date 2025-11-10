from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.mysql import JSON
from models.users import User
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from models.base import Base



   

class ObservacionesProyectos(Base):
    __tablename__ = "observaciones_proyectos"

    observacion_id = Column(Integer, primary_key=True, autoincrement=True)
    observacion_fecha = Column(DateTime, nullable=False, default="CURRENT_TIMESTAMP")
    observacion = Column(Text, nullable=True)
    login_que_observo = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)
    observacion_a_cual_proyecto = Column(Integer, ForeignKey("proyecto.proyecto_id", ondelete="SET NULL"), nullable=True)



class ObservacionesPretensos(Base):
    __tablename__ = "observaciones_pretensos"

    observacion_id = Column(Integer, primary_key=True, autoincrement=True)
    observacion_fecha = Column(DateTime, nullable=False, default="CURRENT_TIMESTAMP")
    observacion = Column(Text, nullable=True)
    login_que_observo = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)
    observacion_a_cual_login = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)


class ObservacionesNNAs(Base):
    __tablename__ = "observaciones_nnas"

    observacion_id = Column(Integer, primary_key=True, autoincrement=True)
    observacion_fecha = Column(DateTime, nullable=False, default="CURRENT_TIMESTAMP")
    observacion = Column(Text, nullable=True)
    login_que_observo = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)
    observacion_a_cual_nna = Column(Integer, ForeignKey("nna.nna_id", ondelete="SET NULL"), nullable=True)




class NotificacionesRUA(Base):
    __tablename__ = "notificaciones_rua"

    notificacion_id = Column(Integer, primary_key=True, autoincrement=True)

    # Fecha automática al momento de crear
    fecha_creacion = Column(DateTime, nullable=False, server_default=func.now())

    # Usuario que recibirá esta notificación
    login_destinatario = Column(String(190), ForeignKey("sec_users.login", ondelete="CASCADE"), nullable=False)

    # Mensaje a mostrar (puede ser breve o detallado)
    mensaje = Column(Text, nullable=False)

    # Link al cual se puede redirigir desde el frontend
    link = Column(String(500), nullable=False)

    data_json = Column(JSON, nullable=True)

    # Si fue o no visualizada (lo puede marcar el frontend cuando se abre el panel o al hacer clic)
    vista = Column(Boolean, default=False)

    # Opcional: tipo de notificación para que el frontend muestre íconos o colores distintos
    tipo_mensaje = Column(String(50), nullable=True)  # ejemplo: 'verde', 'amarillo', 'naranja', 'rojo', 'azul'

    # 🆕 Nuevo campo: usuario que generó la notificación
    login_que_notifico = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)

    # 🔗 Relación hacia el modelo User
    login_que_notifico_rel = relationship("User", foreign_keys=[login_que_notifico])




class Mensajeria(Base):
    __tablename__ = "mensajeria"

    mensaje_id = Column(Integer, primary_key=True, autoincrement=True)

    # Fecha y hora del envío
    fecha_envio = Column(DateTime, nullable=False, server_default=func.now())

    # Tipo de mensaje: 'whatsapp' o 'email'
    tipo = Column(Enum('whatsapp', 'email'), nullable=False)

    # Usuario que generó el envío
    login_emisor = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)

    # Usuario o destinatario (login si existe, o texto libre si fue externo)
    login_destinatario = Column(String(190), ForeignKey("sec_users.login", ondelete="SET NULL"), nullable=True)
    destinatario_texto = Column(String(255), nullable=True)  # Ej: "Juan Pérez (37630123)" o correo

    # Asunto (solo relevante para correo, opcional en WhatsApp)
    asunto = Column(String(255), nullable=True)

    # Contenido del mensaje
    contenido = Column(Text, nullable=True)

    # Estado del mensaje (según tipo WhatsApp o email)
    # WhatsApp: no_enviado / enviado / recibido / leido / error
    # Email: enviado / entregado / error
    estado = Column(
        Enum('no_enviado', 'enviado', 'recibido', 'leido', 'entregado', 'error', name='estado_mensaje_enum'),
        default='enviado',
        nullable=False
    )

    # Identificador externo (por ejemplo, ID devuelto por la API de WhatsApp o Mail)
    mensaje_externo_id = Column(String(255), nullable=True)

    # Información adicional (por ejemplo, logs, respuesta de API, metadatos de envío)
    data_json = Column(JSON, nullable=True)

    # Si el mensaje fue reenviado manualmente
    reenviado = Column(Boolean, default=False)

    # Relación a los usuarios
    emisor_rel = relationship("User", foreign_keys=[login_emisor])
    destinatario_rel = relationship("User", foreign_keys=[login_destinatario])

from sqlalchemy import Column, Integer, String, DateTime, Enum, ForeignKey
from datetime import datetime
from sqlalchemy.orm import relationship

from models.base import Base


class RuaEvento(Base):
    __tablename__ = "rua_evento"

    evento_id = Column(Integer, primary_key=True, autoincrement=True)
    evento_detalle = Column(String(255), nullable=False)
    evento_fecha = Column(DateTime, nullable=True)
    login = Column(String(190), nullable=True)


class SecSettings(Base):
    __tablename__ = "sec_settings"

    set_name = Column(String(255), primary_key=True)  # Clave primaria
    set_value = Column(String(255), nullable=True)  # Puede ser NULL


class LoginIntentoIP(Base):
    __tablename__ = "login_intentos_ip"

    ip = Column(String(64), primary_key=True)
    ultimo_intento = Column(DateTime, default=datetime.now)
    bloqueo_hasta = Column(DateTime, nullable=True)
    usuarios = Column(String(1024), default="") 


class UsuarioNotificadoInactivo(Base):
    __tablename__ = "usuarios_notificados_inactivos"

    login = Column(String(190), ForeignKey("sec_users.login"), primary_key=True)
    mail_enviado_1 = Column(DateTime, nullable=True)
    mail_enviado_2 = Column(DateTime, nullable=True)
    mail_enviado_3 = Column(DateTime, nullable=True)
    mail_enviado_4 = Column(DateTime, nullable=True)
    dado_de_baja = Column(DateTime, nullable=True)

    user = relationship("User", backref="notificacion_inactividad")


class UsuarioNotificadoRatificacion(Base):
    __tablename__ = "usuarios_notificados_ratificacion"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id"), nullable=False)
    login = Column(String(190), ForeignKey("sec_users.login"), nullable=False)
    mail_enviado_1 = Column(DateTime, nullable=True)
    mail_enviado_2 = Column(DateTime, nullable=True)
    mail_enviado_3 = Column(DateTime, nullable=True)
    mail_enviado_4 = Column(DateTime, nullable=True)
    ratificado = Column(DateTime, nullable=True)

    user = relationship("User", backref="notificaciones_ratificacion")
    proyecto = relationship("Proyecto", backref="notificaciones_ratificacion")

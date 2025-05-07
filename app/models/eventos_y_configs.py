from sqlalchemy import Column, Integer, String, DateTime, Enum
from datetime import datetime

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
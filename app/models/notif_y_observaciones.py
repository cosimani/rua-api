from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.mysql import JSON
from models.users import User
from sqlalchemy.sql import func

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

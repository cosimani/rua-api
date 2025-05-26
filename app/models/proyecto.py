
from sqlalchemy import Column, Integer, String, ForeignKey, Enum, Date, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from models.users import User
from datetime import datetime

from models.notif_y_observaciones import ObservacionesProyectos


from models.base import Base


class Proyecto(Base):
    __tablename__ = "proyecto"

    # Clave primaria
    proyecto_id = Column(Integer, primary_key=True, autoincrement=True)

    # Datos generales del proyecto
    proyecto_tipo = Column(Enum('Monoparental', 'Matrimonio', 'Unión convivencial'), nullable=True)
    proyecto_calle_y_nro = Column(String(50), nullable=True)
    proyecto_depto_etc = Column(String(50), nullable=True)
    proyecto_barrio = Column(String(50), nullable=True)
    proyecto_localidad = Column(String(150), nullable=True)
    proyecto_provincia = Column(String(20), nullable=True)

    # Subregistros
    subregistro_1 = Column(String(1), nullable=True)
    subregistro_2 = Column(String(1), nullable=True)
    subregistro_3 = Column(String(1), nullable=True)
    subregistro_4 = Column(String(1), nullable=True)
    subregistro_5_a = Column(String(1), nullable=True)
    subregistro_5_b = Column(String(1), nullable=True)
    subregistro_5_c = Column(String(1), nullable=True)
    subregistro_6_a = Column(String(1), nullable=True)
    subregistro_6_b = Column(String(1), nullable=True)
    subregistro_6_c = Column(String(1), nullable=True)
    subregistro_6_d = Column(String(1), nullable=True)
    subregistro_6_2 = Column(String(1), nullable=True)
    subregistro_6_3 = Column(String(1), nullable=True)
    subregistro_6_mas_de_3 = Column(String(1), nullable=True)
    subregistro_flexible = Column(String(1), nullable=True)
    subregistro_otra_provincia = Column(String(1), nullable=True)

    # Datos de usuarios relacionados
    login_1 = Column(String(190), ForeignKey(User.login), nullable=True)
    login_2 = Column(String(190), ForeignKey(User.login), nullable=True)

    # Estado del proyecto
    aceptado = Column(String(1), nullable=True)
    aceptado_code = Column(String(50), nullable=True)
    ultimo_cambio_de_estado = Column(Date, nullable=True)
    nro_orden_rua = Column(String(11), nullable=True)
    fecha_asignacion_nro_orden = Column(Date, nullable=True)
    operativo = Column(String(1), nullable=True)
    ratificacion_code = Column(String(32), nullable=True)

    # Campos migrados de doc_proyecto
    doc_proyecto_convivencia_o_estado_civil = Column(String(1024), nullable=True)
    
    estado_general = Column(Enum( 'invitacion_pendiente', 'confeccionando', 'en_revision', 'actualizando', 'aprobado', 
                                  'calendarizando', 'entrevistando', 'para_valorar',
                                  'viable', 'viable_no_disponible', 'en_suspenso', 'no_viable', 'en_carpeta', 
                                  'vinculacion', 'guarda', 'adopcion_definitiva', 'baja_anulacion', 'baja_caducidad', 
                                  'baja_por_convocatoria', 'baja_rechazo_invitacion' ), nullable=True)             

    ingreso_por = Column(Enum( 'rua', 'oficio', 'convocatoria' ), nullable=True)         

    informe_profesionales = Column(String(1024), nullable=True)
    
    doc_dictamen = Column(String(1024), nullable=True)
    doc_informe_vinculacion = Column(String(1024), nullable=True)
    doc_informe_seguimiento_guarda = Column(String(1024), nullable=True)
    doc_sentencia_guarda = Column(String(1024), nullable=True)
    doc_sentencia_adopcion = Column(String(1024), nullable=True)
    

    # Campos nuevos ############

    # Flexibilidad edad (Subregistro general)
    flex_edad_1 = Column(String(1))
    flex_edad_2 = Column(String(1))
    flex_edad_3 = Column(String(1))
    flex_edad_4 = Column(String(1))
    flex_edad_todos = Column(String(1))

    # Condiciones especiales: Discapacidad
    discapacidad_1 = Column(String(1))  # sin necesidad de apoyos
    discapacidad_2 = Column(String(1))  # con necesidad de apoyos

    edad_discapacidad_0 = Column(String(1))  # 0 a 3
    edad_discapacidad_1 = Column(String(1))  # 4 a 6
    edad_discapacidad_2 = Column(String(1))  # 7 a 11
    edad_discapacidad_3 = Column(String(1))  # 12 a 17
    edad_discapacidad_4 = Column(String(1))  # 0 a 17

    # Condiciones especiales: Enfermedades
    enfermedad_1 = Column(String(1))  # no afecta calidad de vida
    enfermedad_2 = Column(String(1))  # afecta relativamente
    enfermedad_3 = Column(String(1))  # afecta significativamente

    edad_enfermedad_0 = Column(String(1))  # 0 a 3
    edad_enfermedad_1 = Column(String(1))  # 4 a 6
    edad_enfermedad_2 = Column(String(1))  # 7 a 11
    edad_enfermedad_3 = Column(String(1))  # 12 a 17
    edad_enfermedad_4 = Column(String(1))  # 0 a 17

    # Flexibilidad condiciones de salud
    flex_condiciones_salud = Column(String(1))
    flex_salud_edad_0 = Column(String(1))
    flex_salud_edad_1 = Column(String(1))
    flex_salud_edad_2 = Column(String(1))
    flex_salud_edad_3 = Column(String(1))
    flex_salud_edad_4 = Column(String(1))

    # Subregistro N°6: Grupo de hermanos
    hermanos_comp_1 = Column(String(1))  # hasta 2
    hermanos_comp_2 = Column(String(1))  # hasta 3
    hermanos_comp_3 = Column(String(1))  # 4 o más

    hermanos_edad_0 = Column(String(1))  # 0 a 6
    hermanos_edad_1 = Column(String(1))  # 7 a 11
    hermanos_edad_2 = Column(String(1))  # 12 a 17
    hermanos_edad_3 = Column(String(1))  # 0 a 17

    flex_hermanos_comp_1 = Column(String(1))
    flex_hermanos_comp_2 = Column(String(1))
    flex_hermanos_comp_3 = Column(String(1))

    flex_hermanos_edad_0 = Column(String(1))
    flex_hermanos_edad_1 = Column(String(1))
    flex_hermanos_edad_2 = Column(String(1))
    flex_hermanos_edad_3 = Column(String(1))

    # Los subregistros definitivos
    subreg_1 = Column(String(1))
    subreg_2 = Column(String(1))
    subreg_3 = Column(String(1))
    subreg_4 = Column(String(1))
    subreg_FE1 = Column(String(1))
    subreg_FE2 = Column(String(1))
    subreg_FE3 = Column(String(1))
    subreg_FE4 = Column(String(1))
    subreg_FET = Column(String(1))
    subreg_5A1E1 = Column(String(1))
    subreg_5A1E2 = Column(String(1))
    subreg_5A1E3 = Column(String(1))
    subreg_5A1E4 = Column(String(1))
    subreg_5A1ET = Column(String(1))
    subreg_5A2E1 = Column(String(1))
    subreg_5A2E2 = Column(String(1))
    subreg_5A2E3 = Column(String(1))
    subreg_5A2E4 = Column(String(1))
    subreg_5A2ET = Column(String(1))
    subreg_5B1E1 = Column(String(1))
    subreg_5B1E2 = Column(String(1))
    subreg_5B1E3 = Column(String(1))
    subreg_5B1E4 = Column(String(1))
    subreg_5B1ET = Column(String(1))
    subreg_5B2E1 = Column(String(1))
    subreg_5B2E2 = Column(String(1))
    subreg_5B2E3 = Column(String(1))
    subreg_5B2E4 = Column(String(1))
    subreg_5B2ET = Column(String(1))
    subreg_5B3E1 = Column(String(1))
    subreg_5B3E2 = Column(String(1))
    subreg_5B3E3 = Column(String(1))
    subreg_5B3E4 = Column(String(1))
    subreg_5B3ET = Column(String(1))
    subreg_F5E1 = Column(String(1))
    subreg_F5E2 = Column(String(1))
    subreg_F5E3 = Column(String(1))
    subreg_F5E4 = Column(String(1))
    subreg_F5ET = Column(String(1))
    subreg_61E1 = Column(String(1))
    subreg_61E2 = Column(String(1))
    subreg_61E3 = Column(String(1))
    subreg_61ET = Column(String(1))
    subreg_62E1 = Column(String(1))
    subreg_62E2 = Column(String(1))
    subreg_62E3 = Column(String(1))
    subreg_62ET = Column(String(1))
    subreg_63E1 = Column(String(1))
    subreg_63E2 = Column(String(1))
    subreg_63E3 = Column(String(1))
    subreg_63ET = Column(String(1))
    subreg_FQ1 = Column(String(1))
    subreg_FQ2 = Column(String(1))
    subreg_FQ3 = Column(String(1))
    subreg_F6E1 = Column(String(1))
    subreg_F6E2 = Column(String(1))
    subreg_F6E3 = Column(String(1))
    subreg_F6ET = Column(String(1))
   



    # Relaciones con sec_users
    # usuario_1 = relationship(User, foreign_keys=[login_1], backref="proyectos_login_1")
    # usuario_2 = relationship(User, foreign_keys=[login_2], backref="proyectos_login_2")

    usuario_1 = relationship("User", foreign_keys=[login_1], backref="proyectos_login_1")
    usuario_2 = relationship("User", foreign_keys=[login_2], backref="proyectos_login_2")


    detalle_proyectos = relationship("DetalleProyectosEnCarpeta", back_populates="proyecto")
    detalle_equipo_proyecto = relationship("DetalleEquipoEnProyecto", back_populates="proyecto")
    historial_cambios = relationship("ProyectoHistorialEstado", back_populates="proyecto")
    detalle_postulaciones = relationship("DetalleProyectoPostulacion", backref="proyecto", cascade="all, delete-orphan")
    fechas_revision = relationship("FechaRevision", back_populates = "proyecto", cascade = "all, delete-orphan")






class ProyectoHistorialEstado(Base):
    __tablename__ = "proyecto_historial_estado"

    historial_id = Column(Integer, primary_key=True, autoincrement=True)
    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id"), nullable=False)
    estado_anterior = Column(String(100), nullable=True)
    estado_nuevo = Column(String(100), nullable=False)
    comentarios = Column(Text, nullable=True)
    fecha_hora = Column(DateTime, default=datetime.now)

    proyecto = relationship("Proyecto", back_populates="historial_cambios")





class DetalleEquipoEnProyecto(Base):
    __tablename__ = "detalle_equipo_en_proyecto"

    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id"), primary_key=True)
    login = Column(String(190), ForeignKey("sec_users.login"), primary_key=True)
    fecha_asignacion = Column(Date, nullable=True)

    proyecto = relationship("Proyecto", back_populates="detalle_equipo_proyecto")
    user = relationship("User", back_populates="detalle_equipo_proyecto")





class AgendaEntrevistas(Base):
    __tablename__ = "agenda_entrevistas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id"), nullable=False)
    login_que_agenda = Column(String(190), ForeignKey("sec_users.login"), nullable=False)
    fecha_hora = Column(DateTime, nullable=False)
    comentarios = Column(Text, nullable=True)
    evaluaciones = Column(Text, nullable=True)  # ✅ JSON string
    evaluacion_comentarios = Column(Text, nullable=True)  # ✅ NUEVO campo
    creada_en = Column(DateTime, default=datetime.now)    

    # Relaciones (opcional si necesitás acceso desde otras entidades)
    # proyecto = relationship("Proyecto", back_populates="agenda_entrevistas")
    # quien_agenda = relationship("User", backref="entrevistas_agendadas")




class FechaRevision(Base):
    __tablename__ = "fecha_revision"

    fecha_revision_id = Column(Integer, primary_key = True, autoincrement = True)
    fecha_atencion = Column(Date, nullable = True)

    observacion_id = Column(Integer, ForeignKey("observaciones_proyectos.observacion_id", ondelete = "SET NULL"), index = True, nullable = True)
    login_que_registro = Column(String(190), ForeignKey("sec_users.login", ondelete = "SET NULL"), index = True, nullable = True)
    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id", ondelete = "SET NULL"), index = True, nullable = True)

    fecha_revision_resuelto = Column(Date, nullable = True)
    cantidad_notificaciones = Column(Integer, nullable = True)

    # Relaciones
    proyecto = relationship("Proyecto", back_populates = "fechas_revision")
    observacion = relationship("ObservacionesProyectos", backref = "fechas_revision")  # back_populates opcional
    usuario_que_registro = relationship("User", backref = "fechas_revision_registradas")
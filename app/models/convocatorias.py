from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy import DateTime
from datetime import datetime
from models.nna import Nna

from models.base import Base



class Convocatoria(Base):
    __tablename__ = "convocatorias"

    convocatoria_id = Column(Integer, primary_key=True, autoincrement=True)
    convocatoria_referencia = Column(String(50), nullable=True)
    convocatoria_llamado = Column(String(50), nullable=True)
    convocatoria_edad_es = Column(String(50), nullable=True)
    convocatoria_residencia_postulantes = Column(String(50), nullable=True)
    convocatoria_descripcion = Column(Text, nullable=True)
    convocatoria_juzgado_interviniente = Column(String(250), nullable=True)
    convocatoria_fecha_publicacion = Column(Date, nullable=True)
    convocatoria_online = Column(String(1), nullable=True)

    detalle_nnas = relationship("DetalleNNAEnConvocatoria", back_populates="convocatoria", lazy="joined")





class Postulacion(Base):
    __tablename__ = "postulaciones"

    postulacion_id = Column(Integer, primary_key=True, autoincrement=True)
    fecha_postulacion = Column(DateTime, default=datetime.now)

    # 🔗 Relación con la convocatoria
    convocatoria_id = Column(Integer, ForeignKey("convocatorias.convocatoria_id"), nullable=False)
    convocatoria = relationship("Convocatoria", backref="postulaciones")

    # 👤 Datos personales
    nombre = Column(String(64), nullable=True)
    apellido = Column(String(64), nullable=True)
    dni = Column(String(15), nullable=True)
    fecha_nacimiento = Column(String(20), nullable=True)
    nacionalidad = Column(String(64), nullable=True)
    sexo = Column(String(64), nullable=True)
    estado_civil = Column(String(64), nullable=True)
    calle_y_nro = Column(String(100), nullable=True)
    depto = Column(String(64), nullable=True)
    barrio = Column(String(64), nullable=True)
    localidad = Column(String(64), nullable=True)
    cp = Column(String(15), nullable=True)
    provincia = Column(String(64), nullable=True)
    telefono_contacto = Column(String(20), nullable=True)
    telefono_fijo = Column(String(20), nullable=True)
    videollamada = Column(String(1), nullable=True)
    whatsapp = Column(String(1), nullable=True)
    mail = Column(String(100), nullable=True)
    movilidad_propia = Column(String(1), nullable=True)
    obra_social = Column(String(64), nullable=True)
    ocupacion = Column(String(100), nullable=True)

    # 👥 Cónyuge
    conyuge_convive = Column(String(1), nullable=True)
    conyuge_nombre = Column(String(64), nullable=True)
    conyuge_apellido = Column(String(64), nullable=True)
    conyuge_dni = Column(String(15), nullable=True)
    conyuge_edad = Column(String(15), nullable=True)
    conyuge_fecha_nacimiento = Column(String(20), nullable=True)
    conyuge_telefono_contacto = Column(String(20), nullable=True)
    conyuge_telefono_fijo = Column(String(20), nullable=True)
    conyuge_mail = Column(String(100), nullable=True)
    conyuge_ocupacion = Column(String(100), nullable=True)
    conyuge_otros_datos = Column(Text, nullable=True)

    # 👶 Hijos y situación
    hijos = Column(Text, nullable=True)
    acogimiento_es = Column(String(1), nullable=True)
    acogimiento_descripcion = Column(Text, nullable=True)
    en_rua = Column(String(1), nullable=True)
    subregistro_comentarios = Column(Text, nullable=True)
    terminaste_inscripcion_rua = Column(String(1), nullable=True)
    otra_convocatoria = Column(String(1), nullable=True)
    otra_convocatoria_comentarios = Column(Text, nullable=True)
    antecedentes = Column(String(1), nullable=True)
    antecedentes_comentarios = Column(Text, nullable=True)
    como_tomaron_conocimiento = Column(Text, nullable=True)
    motivos = Column(Text, nullable=True)
    comunicaron_decision = Column(Text, nullable=True)
    otros_comentarios = Column(Text, nullable=True)
    inscripto_en_rua = Column(String(1), nullable=True)

    # 🔁 Relación con proyecto
    detalle_proyecto = relationship("DetalleProyectoPostulacion", backref="postulacion", cascade="all, delete-orphan")



class DetalleProyectoPostulacion(Base):
    __tablename__ = "detalle_proyecto_postulacion"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id"), nullable=False)
    postulacion_id = Column(Integer, ForeignKey("postulaciones.postulacion_id"), nullable=False)



class DetalleNNAEnConvocatoria(Base):
    __tablename__ = "detalle_nna_en_convocatoria"

    convocatoria_id = Column(Integer, ForeignKey("convocatorias.convocatoria_id"), primary_key=True)
    nna_id = Column(Integer, ForeignKey("nna.nna_id"), primary_key=True)

    convocatoria = relationship("Convocatoria", back_populates="detalle_nnas")
    nna = relationship("Nna", back_populates="detalle_convocatorias", lazy="joined")    

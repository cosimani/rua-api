
from sqlalchemy import Column, Integer, String, ForeignKey, Enum, Date, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

from models.base import Base


class Nna(Base):
    __tablename__ = "nna"

    # Clave primaria
    nna_id = Column(Integer, primary_key=True, autoincrement=True)

    # Datos personales
    nna_nombre = Column(String(64), nullable=True)
    nna_apellido = Column(String(64), nullable=True)
    nna_dni = Column(String(15), nullable=True)
    nna_fecha_nacimiento = Column(Date, nullable=True)

    # Dirección
    nna_calle_y_nro = Column(String(50), nullable=True)
    nna_depto_etc = Column(String(50), nullable=True)
    nna_barrio = Column(String(50), nullable=True)
    nna_localidad = Column(String(50), nullable=True)
    nna_provincia = Column(String(50), nullable=True)

    nna_otra_jurisdiccion = Column(String(1), nullable=True)

    # Información adicional
    nna_subregistro_salud = Column(String(50), nullable=True)
    nna_en_convocatoria = Column(String(1), nullable=True, default="N")
    nna_ficha = Column(Text, nullable=True)
    nna_sentencia = Column(String(1024), nullable=True) 
    nna_archivado = Column(String(1), nullable=False, default="N")

    nna_5A = Column(String(1))
    nna_5B = Column(String(1))

    # nna_estado = Column(Enum( 'sin_ficha_sin_sentencia', 'con_ficha_sin_sentencia', 'sin_ficha_con_sentencia', 
    #                           'disponible', 'preparando_carpeta', 'enviada_a_juzgado', 'proyecto_seleccionado', 
    #                           'vinculacion', 'guarda_provisoria', 'guarda_confirmada', 'adopcion_definitiva', 
    #                           'interrupcion', 'mayor_sin_adopcion', 'en_convocatoria', 'no_disponible' ), nullable=True)

    # nna_estado = Column(Enum(
    #     'sin_ficha_sin_sentencia',
    #     'con_ficha_sin_sentencia',
    #     'sin_ficha_con_sentencia',
    #     'disponible',
    #     'preparando_carpeta',
    #     'enviada_a_juzgado',
    #     'proyecto_seleccionado',
    #     'vinculacion',
    #     'guarda_provisoria',
    #     'guarda_confirmada',
    #     'adopcion_definitiva',
    #     'vinculacion_no_inscriptos',   
    #     'guarda_provisoria_no_inscriptos',
    #     'guarda_confirmada_no_inscriptos',
    #     'adopcion_definitiva_no_inscriptos',
    #     'interrupcion',
    #     'mayor_sin_adopcion',
    #     'en_convocatoria',
    #     'no_disponible'
    # ), nullable=True)

    nna_estado = Column(Enum(
        'sin_ficha_sin_sentencia',
        'con_ficha_sin_sentencia',
        'sin_ficha_con_sentencia',
        'disponible',
        'preparando_carpeta',
        'enviada_a_juzgado',
        'proyecto_seleccionado',
        'vinculacion',
        'guarda_provisoria',
        'guarda_confirmada',
        'adopcion_definitiva',
        'vinculacion_no_inscriptos',   
        'guarda_provisoria_no_inscriptos',
        'guarda_confirmada_no_inscriptos',
        'adopcion_definitiva_no_inscriptos',
        'valorando_excepcion_no_inscriptos', 
        'sin_disponibilidad_adoptiva',       
        'interrupcion',
        'mayor_sin_adopcion',
        'en_convocatoria',
        'no_disponible'
    ), nullable=True)


    hermanos_id = Column(Integer, nullable=True)
    
   
    # # Relación con otra tabla (si existe)
    detalle_nna = relationship("DetalleNNAEnCarpeta", back_populates="nna", lazy="joined")
    detalle_convocatorias = relationship("DetalleNNAEnConvocatoria", back_populates="nna", lazy="joined")

    historial_estados = relationship("NnaHistorialEstado", back_populates="nna")







class NnaHistorialEstado(Base):
    __tablename__ = "nna_historial_estado"

    historial_id = Column(Integer, primary_key=True, autoincrement=True)
    nna_id = Column(Integer, ForeignKey("nna.nna_id"), nullable=False)

    estado_anterior = Column(String(100), nullable=True)
    estado_nuevo = Column(String(100), nullable=False)
    comentarios = Column(Text, nullable=True)
    fecha_hora = Column(DateTime, default=datetime.now)

    nna = relationship("Nna", back_populates="historial_estados")






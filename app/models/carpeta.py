
from sqlalchemy import Column, Integer, String, Date, ForeignKey, Enum
from sqlalchemy.orm import relationship
from models.nna import Nna

from models.base import Base


class Carpeta(Base):
    __tablename__ = "carpeta"

    carpeta_id = Column(Integer, primary_key=True, autoincrement=True)
    fecha_creacion = Column(Date, nullable=True)
    estado_carpeta = Column(Enum('vacia', 'preparando_carpeta', 'enviada_a_juzgado', 'proyecto_seleccionado', 'desierto'), nullable=False)

    detalle_proyectos = relationship("DetalleProyectosEnCarpeta", back_populates="carpeta", lazy="joined")
    detalle_nna = relationship("DetalleNNAEnCarpeta", back_populates="carpeta", lazy="joined")


class DetalleProyectosEnCarpeta(Base):
    __tablename__ = "detalle_proyectos_en_carpeta"

    carpeta_id = Column(Integer, ForeignKey("carpeta.carpeta_id"), primary_key=True)
    proyecto_id = Column(Integer, ForeignKey("proyecto.proyecto_id"), primary_key=True)
    fecha_asignacion = Column(Date, nullable=True)

    carpeta = relationship("Carpeta", back_populates="detalle_proyectos")
    proyecto = relationship("Proyecto", back_populates="detalle_proyectos")



class DetalleNNAEnCarpeta(Base):
    __tablename__ = "detalle_nna_en_carpeta"

    carpeta_id = Column(Integer, ForeignKey("carpeta.carpeta_id"), primary_key=True)
    nna_id = Column(Integer, ForeignKey("nna.nna_id"), primary_key=True)

    carpeta = relationship("Carpeta", back_populates="detalle_nna")
    nna = relationship("Nna", back_populates="detalle_nna", lazy="joined")


from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.orm import relationship

from sqlalchemy.ext.declarative import declarative_base

from models.base import Base

# class DetalleNnaEnCarpeta(Base):
#     __tablename__ = "detalle_nna_en_carpeta"

#     carpeta_id = Column(Integer, ForeignKey("carpeta.carpeta_id"), primary_key=True, nullable=False)
#     nna_id = Column(Integer, ForeignKey("nna.nna_id"), primary_key=True, nullable=False)

#     # Relaciones
#     carpeta = relationship("Carpeta", back_populates="detalles_nna")
#     nna = relationship("Nna", back_populates="detalles_carpeta")

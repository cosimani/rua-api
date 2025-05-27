from sqlalchemy import Column, Integer, String, Date, LargeBinary, ForeignKey, Index, Enum, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

from models.base import Base



class User(Base):
    __tablename__ = "sec_users"

    login = Column(String(190), primary_key=True, nullable=False)  # login varchar(190)
    clave = Column(String(255), nullable=False)  # clave varchar(255)
    nombre = Column(String(64), nullable=False)  # nombre varchar(64)
    apellido = Column(String(64), nullable=True)  # apellido varchar(64)
    celular = Column(String(20), nullable=True)  # celular varchar(20)
    mail = Column(String(255), nullable=True)  # mail varchar(255)
    fecha_nacimiento = Column(Date, nullable=True)  # fecha_nacimiento date
    active = Column(String(1), nullable=True)  # active varchar(1)
    activation_code = Column(String(32), nullable=True)  # activation_code varchar(32)
    convocatoria = Column(String(1), nullable=True, default="N")  # convocatoria varchar(1)
    foto_perfil = Column(String(255), nullable=True)  # foto_perfil varchar(255), contendrá la ruta al archivo
    calle_y_nro = Column(String(50), nullable=True)  # calle_y_nro varchar(50)
    depto_etc = Column(String(50), nullable=True)  # depto_etc varchar(50)
    barrio = Column(String(50), nullable=True)  # barrio varchar(50)
    localidad = Column(String(50), nullable=True)  # localidad varchar(50)
    provincia = Column(String(50), nullable=True)  # provincia varchar(50)
    recuperacion_code = Column(String(32), nullable=True)  # recuperacion_code varchar(32)
    profesion = Column(String(64), nullable=True)  # profesion varchar(64)
    fecha_alta = Column(Date, nullable=True)  # fecha_alta date
    operativo = Column(String(1), nullable=True, default="Y")  # operativo varchar(1)

    # Campos provenientes de doc_adoptante:
    doc_adoptante_domicilio = Column(String(255), nullable=True)  # ruta al domicilio
    doc_adoptante_dni_frente = Column(String(255), nullable=True)  # ruta al dni
    doc_adoptante_dni_dorso = Column(String(255), nullable=True)  # ruta al dni dorso
    doc_adoptante_deudores_alimentarios = Column(String(255), nullable=True)
    doc_adoptante_antecedentes = Column(String(255), nullable=True)
    doc_adoptante_migraciones = Column(String(255), nullable=True)
    doc_adoptante_salud = Column(String(255), nullable=True)
    doc_adoptante_curso_aprobado = Column(String(1), nullable=True, default="N")
    doc_adoptante_estado = Column(
        Enum('inicial_cargando', 'pedido_revision', 'actualizando', 'aprobado', 'rechazado', name='doc_adoptante_estado_enum'),
        nullable=False,
        default='inicial_cargando'
    )
    doc_adoptante_ddjj_firmada = Column(String(1), nullable=False, default='N')
    fecha_solicitud_revision = Column(Date, nullable=True) 

    intentos_login = Column(Integer, default=0)  # contador de intentos fallidos
    bloqueo_hasta = Column(DateTime, nullable=True)  # fecha hasta la cual está bloqueado

    __table_args__ = (
        Index('ix_users_login', 'login'),
        Index('ix_users_nombre', 'nombre'),
        Index('ix_users_apellido', 'apellido'),
        Index('ix_users_mail', 'mail'),
        Index('ix_users_celular', 'celular'),
        Index('ix_users_calle_y_nro', 'calle_y_nro'),
        Index('ix_users_barrio', 'barrio'),
        Index('ix_users_localidad', 'localidad'),
        Index('ix_users_provincia', 'provincia'),
        Index('ix_users_fecha_alta', 'fecha_alta'),
    )

    detalle_equipo_proyecto = relationship("DetalleEquipoEnProyecto", back_populates="user")

    


    
class Group(Base):
    __tablename__ = "sec_groups"
    group_id = Column(Integer, primary_key=True, autoincrement=True)
    description = Column(String(255), nullable=True)

    __table_args__ = (
        Index('ix_groups_group_id', 'group_id'),
        Index('ix_groups_description', 'description'),
    )


class UserGroup(Base):
    __tablename__ = "sec_users_groups"
    login = Column(String(190), ForeignKey("sec_users.login"), primary_key=True)
    group_id = Column(Integer, ForeignKey("sec_groups.group_id"), primary_key=True)



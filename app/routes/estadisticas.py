from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database.config import get_db  # Importá get_db desde config.py

from models.users import User
from models.proyecto import Proyecto
# from models.detalles import DetalleNnaEnCarpeta
from models.nna import Nna

from helpers.utils import parse_date
from models.eventos_y_configs import RuaEvento
from security.security import get_current_user, verify_api_key, require_roles

from sqlalchemy.sql import func, or_
from security.security import get_current_user, require_roles, verify_api_key

from fastapi.responses import FileResponse
from helpers.utils import EstadisticasPDF, calcular_estadisticas_generales



estadisticas_router = APIRouter()


@estadisticas_router.get("/generales", response_model=dict, 
                         dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora", "profesional"]))])
def get_estadisticas(db: Session = Depends(get_db)):
    return calcular_estadisticas_generales(db)



@estadisticas_router.get("/informe_general", 
                         dependencies=[Depends(verify_api_key), Depends(require_roles(["administrador", "supervisora"]))])
def generar_pdf_estadisticas(db: Session = Depends(get_db)):
    """
    Genera un informe en PDF con estadísticas del sistema con formato fijo.
    """

    stats = calcular_estadisticas_generales(db)


    pdf = EstadisticasPDF()
    pdf.add_page()


    # Sección 1
    pdf.section_title("1) Estadísticas en relación a los pretensos")
    pretensos_data = [
        ["Indicador", "Cantidad"],
        ["Logueados en el sistema", stats["usuarios_activos"]],
        ["Con Curso aprobado", stats["con_curso_sin_ddjj"]],
        ["Con Curso y DDJJ firmada", stats["con_curso_con_ddjj"]],
        ["Presentando documentación", stats["pretensos_presentando_documentacion"]],
        ["Aprobados", stats["pretensos_aprobados"]],
        ["Rechazados", stats["pretensos_rechazados"]],
        ["Usuarios inactivos (sólo con usuario creado)", stats["sin_activar"]],
    ]
    pdf.add_table(pretensos_data)

    pdf.section_title("2) Estadísticas en relación a los proyectos aprobados")

    proyectos_resumen = [
        ["Tipo de Proyecto", "Presentando", "En revisión", "Calendarizables", "Entrevistando"],

        ["Pareja", 
        stats["proyectos_en_pareja_subiendo_documentacion"], 
        stats["proyectos_en_pareja_en_revision_por_supervision"], 
        stats["proyectos_en_pareja_aprobados_para_calendarizar"], 
        stats["entrevistando_en_pareja"]],       

        ["Monoparental", 
        stats["proyectos_monoparentales_subiendo_documentacion"], 
        stats["proyectos_monoparentales_en_revision_por_supervision"], 
        stats["proyectos_monoparentales_aprobados_para_calendarizar"], 
        stats["entrevistando_monoparental"]],

    ]
    pdf.add_table(proyectos_resumen)


    proyectos_resumen = [
        ["Tipo de Proyecto", "En suspenso", "No viables", "Baja definitiva"],

        ["Pareja", 
        stats["proyectos_en_pareja_en_suspenso"], 
        stats["proyectos_en_pareja_no_viable"], 
        stats["proyectos_en_pareja_baja_definitiva"]],
        
        ["Monoparental", 
        stats["proyectos_monoparentales_en_suspenso"], 
        stats["proyectos_monoparentales_no_viable"], 
        stats["proyectos_monoparentales_baja_definitiva"]],
    ]
    pdf.add_table(proyectos_resumen)


    proyectos_resumen = [
        ["Tipo de Proyecto", "Viables TND", "Viables disponibles", "Adopción definitiva"],

        ["Pareja", 
        "127", 
        "35", 
        "15"],       

        ["Monoparental", 
        "59", 
        "17", 
        "8"],

    ]
    pdf.add_table(proyectos_resumen)


    subregistros = [
        ["Subregistro", "Cantidad"],
        ["Uno (0 a 3 años)", 94],
        ["Dos (4 a 6 años)", 83],
        ["Tres (7 a 11 años)", 8],
        ["Cuatro (12 a 17 años)", 1],
        ["Cinco a (discapacidad)", 0],
        ["Cinco b (condición específica)", 0],
        ["Cinco c (otras; sordera, ceguera)", 0],
    ]
    pdf.add_table(subregistros)

    pdf.add_page()

    # Sección 3
    pdf.section_title("3) Estadísticas en relación a los NNA con sentencia de adoptabilidad")
    pdf.add_table([
        ["Edad / Estado", "Con Sentencia", "En Vinculación", "En Guarda", "En Adopción"],
        ["0-3 años", 23, 19, 13, 2],
        ["4-6 años", 20, 16, 10, 7],
        ["7-11 años", 65, 18, 6, 2],
        ["12-17 años", 69, 11, 11, 1],
    ])

    # Sección 4
    pdf.section_title("4) Estadísticas en relación a los proyectos en procesos de guarda y adopción (RUA)")
    pdf.add_table([
        ["Estado del Proyecto", "Cantidad"],
        ["En vinculación", 53],
        ["En guarda", 37],
        ["Con adopción definitiva", 11],
    ])

    # Sección 5
    pdf.section_title("5) Estadísticas en relación a las convocatorias")
    pdf.add_table([
        ["Fecha", "Nº", "Unidad Judicial", "NNA", "Subregistro", "Inscriptos", "Viables", "No viables"],
        ["01/03/2024", "001", "Unidad Centro", "3", "1, 2", "14", "5", "9"],
        ["10/03/2024", "002", "Unidad Norte", "2", "3", "10", "3", "7"],
    ])



    # Guardar y devolver
    pdf_path = "/tmp/estadisticas_adopciones.pdf"
    pdf.output(pdf_path)
    return FileResponse(pdf_path, filename="estadisticas_adopciones.pdf", media_type="application/pdf")



@estadisticas_router.get("/historial/{login}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), Depends(require_roles(["administrador", "supervisora"]))])
def get_user_timeline(
    login: str,
    db: Session = Depends(get_db)
):
    """
    Devuelve el historial de actividades de un usuario basado en su login (DNI).
    Incluye fechas clave para la línea de tiempo.
    """
    try:

        return {
            "login": "login",
            "fecha_alta": "fecha_alta",
            "fecha_curso_aprobado": "fecha_curso_aprobado",
            "fecha_firma_ddjj": "fecha_firma_ddjj",
            "fecha_aprobacion_doc_personal": "fecha_aprobacion_doc_personal",
            "fecha_presentacion_proyecto": "fecha_presentacion_proyecto",
            "fecha_aprobacion_doc_proyecto": "fecha_aprobacion_doc_proyecto",
            "fecha_asignacion_nro_orden": "fecha_asignacion_nro_orden"
        }

        
        # Buscar usuario
        user = db.query(User).filter(User.login == login).first()
        if not user:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        # Buscar fechas en la tabla User
        fecha_alta = parse_date(user.fecha_alta)
        # fecha_curso_aprobado = parse_date(user.doc_adoptante_curso_aprobado_fecha)
        fecha_firma_ddjj = parse_date(user.doc_adoptante_ddjj_firmada_fecha)
        fecha_aprobacion_doc_personal = parse_date(user.doc_adoptante_estado_fecha)

        # Buscar proyecto asociado (puede tener más de uno, tomamos el más reciente)
        proyecto = (
            db.query(Proyecto)
            .filter((Proyecto.login_1 == login) | (Proyecto.login_2 == login))
            .order_by(Proyecto.fecha_asignacion_nro_orden.desc())  # El más reciente primero
            .first()
        )

        fecha_presentacion_proyecto = parse_date(proyecto.fecha_presentacion) if proyecto else None
        fecha_aprobacion_doc_proyecto = parse_date(proyecto.fecha_aprobacion_doc_proyecto) if proyecto else None
        fecha_asignacion_nro_orden = parse_date(proyecto.fecha_asignacion_nro_orden) if proyecto else None

        # Buscar fechas adicionales en RuaEvento si no están en las otras tablas
        eventos = (
            db.query(RuaEvento.evento_fecha, RuaEvento.evento_detalle)
            .filter(RuaEvento.login == login)
            .all()
        )

        # Mapear eventos específicos a sus fechas
        eventos_dict = {evento.evento_detalle: parse_date(evento.evento_fecha) for evento in eventos}

        # fecha_curso_aprobado = fecha_curso_aprobado or eventos_dict.get("Curso aprobado")
        fecha_firma_ddjj = fecha_firma_ddjj or eventos_dict.get("DDJJ firmada")
        fecha_aprobacion_doc_personal = fecha_aprobacion_doc_personal or eventos_dict.get("Documentación personal aprobada")
        fecha_presentacion_proyecto = fecha_presentacion_proyecto or eventos_dict.get("Proyecto presentado")
        fecha_aprobacion_doc_proyecto = fecha_aprobacion_doc_proyecto or eventos_dict.get("Documentación de proyecto aprobada")
        fecha_asignacion_nro_orden = fecha_asignacion_nro_orden or eventos_dict.get("Número de orden asignado")

        return {
            "login": login,
            "fecha_alta": fecha_alta,
            "fecha_curso_aprobado": fecha_curso_aprobado,
            "fecha_firma_ddjj": fecha_firma_ddjj,
            "fecha_aprobacion_doc_personal": fecha_aprobacion_doc_personal,
            "fecha_presentacion_proyecto": fecha_presentacion_proyecto,
            "fecha_aprobacion_doc_proyecto": fecha_aprobacion_doc_proyecto,
            "fecha_asignacion_nro_orden": fecha_asignacion_nro_orden
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener el historial del usuario: {str(e)}")

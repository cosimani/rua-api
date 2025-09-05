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


def g(stats: dict, path: str, default=0):
    """
    Lee claves anidadas con path tipo 'proyectos.resumen.proyectos_viables'.
    Si no existe, devuelve default.
    """
    cur = stats
    try:
        for k in path.split("."):
            cur = cur[k]
        return cur
    except Exception:
        return default
    


@estadisticas_router.get("/generales", response_model=dict, 
                         dependencies=[Depends( verify_api_key ), 
                                       Depends(require_roles(["administrador", "supervision", "supervisora", "profesional", "coordinadora"]))])
def get_estadisticas(db: Session = Depends(get_db)):
    return calcular_estadisticas_generales(db)



@estadisticas_router.get(
    "/informe_general",
    dependencies=[
        Depends(verify_api_key),
        Depends(require_roles(["administrador", "supervision", "supervisora", "coordinadora"]))
    ],
)
def generar_pdf_estadisticas(db: Session = Depends(get_db)):
    stats = calcular_estadisticas_generales(db)

    pdf = EstadisticasPDF()
    pdf.add_page()

    # =========================================================
    # RESUMEN GENERAL
    # =========================================================
    pdf.section_title("Resumen General de Indicadores Clave")

    resumen_general = [
        ["Indicador", "Cantidad"],
        ["Proyectos viables disponibles", g(stats, "proyectos.resumen.proyectos_viables")],
        ["Proyectos en entrevistas (ingresado por RUA)", g(stats, "proyectos.resumen.proyectos_en_entrevistas_rua")],
        ["Proyectos en entrevistas (por convocatoria)", g(stats, "proyectos.resumen.proyectos_en_entrevistas_convocatoria")],
        ["NNAs en Guarda Provisoria", g(stats, "nna.nna_en_guarda_provisoria")],
        ["NNAs en Guarda Confirmada", g(stats, "nna.nna_en_guarda_confirmada")],
        ["NNAs con Adopción Definitiva", g(stats, "nna.nna_en_adopcion_definitiva")],
        ["Postulaciones totales (todas las convocatorias)", g(stats, "usuarios.postulaciones_totales")],  # ← NUEVO
    ]
    pdf.add_table(resumen_general)

    # =========================================================
    # 1) PROYECTOS
    # =========================================================
    pdf.section_title("1) Estadísticas de Proyectos ingresados por RUA")

    tabla_1 = [
        ["Tipo", "Presentando doc.", "En revisión", "Calendarizables", "Entrevistando"],
        [
            "Pareja",
            g(stats, "proyectos.tipos.en_pareja_subiendo_documentacion"),
            g(stats, "proyectos.tipos.en_pareja_en_revision"),
            g(stats, "proyectos.tipos.en_pareja_aprobados_para_calendarizar"),
            g(stats, "proyectos.tipos.entrevistando_en_pareja"),
        ],
        [
            "Monoparental",
            g(stats, "proyectos.tipos.monoparentales_subiendo_documentacion"),
            g(stats, "proyectos.tipos.monoparentales_en_revision"),
            g(stats, "proyectos.tipos.monoparentales_aprobados_para_calendarizar"),
            g(stats, "proyectos.tipos.entrevistando_monoparental"),
        ],
    ]
    pdf.add_table(tabla_1)

    tabla_2 = [
        ["Tipo", "En suspenso", "No viables", "Baja definitiva"],
        [
            "Pareja",
            g(stats, "proyectos.tipos.en_pareja_en_suspenso"),  # total
            g(stats, "proyectos.tipos.en_pareja_no_viables"),   # total
            g(stats, "proyectos.tipos.en_pareja_baja_definitiva"),  # ⚠️ ver nota abajo
        ],
        [
            "Monoparental",
            g(stats, "proyectos.tipos.monoparentales_en_suspenso"),  # total
            g(stats, "proyectos.tipos.monoparentales_no_viables"),   # total
            g(stats, "proyectos.tipos.monoparentales_baja_definitiva"),  # ⚠️ ver nota abajo
        ],
    ]
    pdf.add_table(tabla_2)

    tabla_3 = [
        ["Tipo", "Viables disponibles", "Adopción definitiva"],
        [
            "Pareja",
            g(stats, "proyectos.tipos.proyectos_en_pareja_viable"),  # ← corregido
            g(stats, "proyectos.tipos.proyectos_adopcion_definitiva_pareja"),
        ],
        [
            "Monoparental",
            g(stats, "proyectos.tipos.proyectos_monoparental_viable"),
            g(stats, "proyectos.tipos.proyectos_adopcion_definitiva_monoparental"),
        ],
    ]
    pdf.add_table(tabla_3)

    tabla_4 = [
        ["Ingreso por", "Cantidad"],
        ["RUA", g(stats, "proyectos.por_ingreso.rua")],
        ["Oficio", g(stats, "proyectos.por_ingreso.oficio")],
        ["Convocatoria", g(stats, "proyectos.por_ingreso.convocatoria")],
    ]
    pdf.add_table(tabla_4)

    # (Opcional) Proyectos por estado (muestra principales)
    por_estado = g(stats, "proyectos.por_estado", {})
    if isinstance(por_estado, dict) and por_estado:
        pdf.section_title("Proyectos por estado (principales)")
        # Ordeno por cantidad desc y muestro top 10
        top = sorted(por_estado.items(), key=lambda x: x[1], reverse=True)[:10]
        tabla_estados = [["Estado", "Cantidad"]] + [[k, v] for k, v in top]
        pdf.add_table(tabla_estados)

    pdf.add_page()

    # =========================================================
    # 2) NNA
    # =========================================================

    pdf.section_title("2) NNA")
    tabla_nna_clip = [
        ["Indicador", "Cantidad"],
        ["NNAs en Proyecto con Guarda Provisoria", g(stats, "nna.nna_en_guarda_provisoria")],
        ["NNAs en Proyecto con Guarda Confirmada", g(stats, "nna.nna_en_guarda_confirmada")],
        ["NNAs en Proyecto con Adopción Definitiva", g(stats, "nna.nna_en_adopcion_definitiva")],
    ]
    pdf.add_table(tabla_nna_clip)
    
    # NNA: distribución por estado
    nna_por_estado = g(stats, "nna.por_estado", {})
    if isinstance(nna_por_estado, dict) and nna_por_estado:
        pdf.section_title("NNA por estado")
        tabla_nna_estado = [["Estado", "Cantidad"]] + [[k, v] for k, v in nna_por_estado.items()]
        pdf.add_table(tabla_nna_estado)

    # NNA: distribución por edades
    edades = g(stats, "nna.edades", {})
    if isinstance(edades, dict) and edades:
        pdf.section_title("Distribución de NNA por edades (todos los estados)")
        tabla_edades = [
            ["Rango", "Cantidad"],
            ["0-6", edades.get("0_6", 0)],
            ["7-11", edades.get("7_11", 0)],
            ["12-17", edades.get("12_17", 0)],
            ["18+", edades.get("18_mas", 0)],
        ]
        pdf.add_table(tabla_edades)

    pdf.add_page()

    # =========================================================
    # 3) PRETENSOS
    # =========================================================
    pdf.section_title("3) Estadísticas de Pretensos")

    pretensos_data = [
        ["Indicador", "Cantidad"],
        ["Activos (Pretensos con clave y activación con mail)", g(stats, "usuarios.usuarios_totales")],  # usuarios_totales redefinido
        ["Usuarios inactivos (aún no activaron con mail)", g(stats, "usuarios.sin_activar")],
        ["Con Curso y DDJJ firmada", g(stats, "usuarios.con_curso_con_ddjj")],
        
        ["Con documentación aprobada pero sin proyecto", g(stats, "usuarios.usuarios_sin_proyecto")],
        ["Postulados y CON usuario activo en RUA", g(stats, "usuarios.usuarios_postulados_y_rua")],            # ← NUEVO
        ["Postulados y SIN usuario en RUA (sólo se postularon)", g(stats, "usuarios.usuarios_postulados_y_no_rua")],        # ← NUEVO
        ["Postulaciones (incluyendo pretensos en varias convocatorias)", g(stats, "usuarios.postulaciones_totales")],                   # ← NUEVO
    ]
    pdf.add_table(pretensos_data)

    # DDJJ
    pdf.section_title("DDJJ (flexibilidades y condiciones)")
    ddjj = g(stats, "ddjj", {})
    tabla_ddjj = [
        ["Indicador", "Cantidad"],
        ["DDJJ firmadas", ddjj.get("firmadas", 0)],
        ["DDJJ no firmadas", ddjj.get("no_firmadas", 0)],
        ["Flexibilidad de edad (alguna)", ddjj.get("flex_edad_alguna", 0)],
        ["Aceptan discapacidad", ddjj.get("acepta_discapacidad", 0)],
        ["Aceptan enfermedades", ddjj.get("acepta_enfermedad", 0)],
        ["Aceptan grupo de hermanos", ddjj.get("acepta_grupo_hermanos", 0)],
    ]
    pdf.add_table(tabla_ddjj)

    pdf.add_page()

    # =========================================================
    # 4) TIEMPOS
    # =========================================================
    pdf.section_title("4) Tiempos (Proyectos y Pretensos)")

    # Proyectos: promedio de días por estado
    tiempos_proy = g(stats, "tiempos.proyectos", {})
    prom_por_estado = tiempos_proy.get("promedio_dias_por_estado", {})
    if isinstance(prom_por_estado, dict) and prom_por_estado:
        pdf.section_title("Tiempos de proyectos por estado (promedio en días)")
        tabla_tiempos_estado = [["Estado", "Promedio días"]] + [
            [k, round(v, 2)] for k, v in sorted(prom_por_estado.items(), key=lambda x: x[0])
        ]
        pdf.add_table(tabla_tiempos_estado)

    # Proyectos: tiempo total promedio por proyecto
    pdf.add_table([
        ["Métrica", "Valor"],
        ["Promedio de días por proyecto (ciclo completo)", round(g(stats, "tiempos.proyectos.promedio_dias_total_por_proyecto", 0), 2)]
    ])

    # Pretensos: pipeline de tiempos
    tiempos_pret = g(stats, "tiempos.pretensos", {})
    pdf.section_title("Pipeline de pretensos (promedios en días)")
    tabla_pret = [
        ["Tramo", "Promedio días"],
        ["Curso aprobado -> DDJJ firmada", round(tiempos_pret.get("promedio_dias_curso_a_ddjj", 0), 2)],
        ["DDJJ firmada -> Solicitud de revisión", round(tiempos_pret.get("promedio_dias_ddjj_a_solicitud_revision", 0), 2)],
        ["Solicitud de revisión -> Aprobado", round(tiempos_pret.get("promedio_dias_revision_a_aprobado", 0), 2)],
    ]
    pdf.add_table(tabla_pret)

    # Ratificación
    tiempos_rat = g(stats, "tiempos.ratificacion", {})
    pdf.section_title("Ratificación")
    tabla_rat = [
        ["Métrica", "Valor"],
        ["Promedio días: 1er mail -> ratificación", round(tiempos_rat.get("promedio_dias_mail_a_ratificacion", 0), 2)],
        ["Ratificaciones pendientes", tiempos_rat.get("ratificaciones_pendientes", 0)],
    ]
    pdf.add_table(tabla_rat)

    # =========================================================
    # Salida
    # =========================================================
    pdf_path = "/tmp/estadisticas_adopciones.pdf"
    pdf.output(pdf_path)
    return FileResponse(pdf_path, filename="estadisticas_adopciones.pdf", media_type="application/pdf")




@estadisticas_router.get("/historial/{login}", response_model=dict, 
                  dependencies=[Depends( verify_api_key ), 
                                Depends(require_roles(["administrador", "supervision", "supervisora", "coordinadora"]))])
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

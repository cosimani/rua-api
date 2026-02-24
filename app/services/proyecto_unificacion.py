import json
import os
import shutil
from datetime import datetime
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from models.eventos_y_configs import RuaEvento
from models.notif_y_observaciones import ObservacionesProyectos
from models.proyecto import Proyecto, ProyectoHistorialEstado


load_dotenv()


DOC_FIELDS = [
    "doc_proyecto_convivencia_o_estado_civil",
    "doc_dictamen",
    "doc_sentencia_guarda",
    "doc_informe_vinculacion",
    "doc_informe_seguimiento_guarda",
    "doc_informe_conclusivo",
    "doc_sentencia_adopcion",
    "doc_interrupcion",
    "doc_baja_convocatoria",
]

RUA_VALID_STATES = [
    "aprobado",
    "calendarizando",
    "entrevistando",
    "para_valorar",
    "viable",
]


def _get_docs_base_dir() -> str:
    base_dir = os.getenv("UPLOAD_DIR_DOC_PROYECTOS")
    if not base_dir:
        raise RuntimeError("La variable de entorno UPLOAD_DIR_DOC_PROYECTOS no está definida. Verificá tu archivo .env")
    base_dir = os.path.abspath(base_dir)
    if os.path.basename(base_dir) != "proyectos":
        base_dir = os.path.join(base_dir, "proyectos")
    os.makedirs(base_dir, exist_ok = True)
    return base_dir


def _is_empty_text(value: str) -> bool:
    return value is None or str(value).strip() == ""


def _is_empty_doc_value(value: str) -> bool:
    if _is_empty_text(value):
        return True
    raw = str(value).strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except Exception:
            return False
        return not data
    return False


def _is_monoparental(proyecto: Proyecto) -> bool:
    if (proyecto.proyecto_tipo or "").strip() == "Monoparental":
        return True
    return _is_empty_text(proyecto.login_2)


def _query_rua_por_grupo(db: Session, proyecto_convocatoria: Proyecto):
    base = db.query(Proyecto).filter(
        Proyecto.ingreso_por == "rua",
        Proyecto.estado_general.in_(RUA_VALID_STATES),
    )

    if _is_monoparental(proyecto_convocatoria):
        return base.filter(
            Proyecto.login_1 == proyecto_convocatoria.login_1,
            or_(Proyecto.login_2.is_(None), Proyecto.login_2 == "")
        )

    login_1 = (proyecto_convocatoria.login_1 or "").strip()
    login_2 = (proyecto_convocatoria.login_2 or "").strip()

    return base.filter(
        or_(
            and_(Proyecto.login_1 == login_1, Proyecto.login_2 == login_2),
            and_(Proyecto.login_1 == login_2, Proyecto.login_2 == login_1)
        )
    )


def _parse_doc_entries(raw_value: str):
    raw = str(raw_value).strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            entries = []
            for item in data:
                if isinstance(item, dict):
                    src = (item.get("ruta") or "").strip()
                    if src:
                        entries.append({"src": src, "meta": item})
                elif isinstance(item, str) and item.strip():
                    entries.append({"src": item.strip(), "meta": {"ruta": item.strip()}})
            return "json", entries
    return "single", [{"src": raw, "meta": None}]


def _serialize_proyecto_compacto(proyecto: Proyecto) -> dict:
    return {
        "proyecto_id": proyecto.proyecto_id,
        "estado_general": proyecto.estado_general,
        "ingreso_por": proyecto.ingreso_por,
        "login_1": proyecto.login_1,
        "login_2": proyecto.login_2,
        "nro_orden_rua": proyecto.nro_orden_rua,
        "fecha_asignacion_nro_orden": proyecto.fecha_asignacion_nro_orden,
    }


def _query_convocatoria_por_grupo(db: Session, proyecto_convocatoria: Proyecto):
    base = db.query(Proyecto).filter(Proyecto.ingreso_por == "convocatoria")

    if _is_monoparental(proyecto_convocatoria):
        return base.filter(
            Proyecto.login_1 == proyecto_convocatoria.login_1,
            or_(Proyecto.login_2.is_(None), Proyecto.login_2 == "")
        )

    login_1 = (proyecto_convocatoria.login_1 or "").strip()
    login_2 = (proyecto_convocatoria.login_2 or "").strip()

    return base.filter(
        or_(
            and_(Proyecto.login_1 == login_1, Proyecto.login_2 == login_2),
            and_(Proyecto.login_1 == login_2, Proyecto.login_2 == login_1)
        )
    )


def _build_docs_preview(
    proyecto_convocatoria: Proyecto,
    proyecto_rua: Proyecto,
) -> list:
    preview = []
    for field_name in DOC_FIELDS:
        conv_value = getattr(proyecto_convocatoria, field_name)
        rua_value = getattr(proyecto_rua, field_name)

        if not _is_empty_doc_value(conv_value) or _is_empty_doc_value(rua_value):
            continue

        _, entries = _parse_doc_entries(rua_value)
        if not entries:
            continue

        archivos = []
        for entry in entries:
            src_path = entry["src"]
            archivos.append({
                "ruta": src_path,
                "existe": os.path.exists(src_path),
            })

        preview.append({
            "field_name": field_name,
            "archivos": archivos,
            "cantidad": len(archivos),
        })

    return preview


def get_unificacion_info(db: Session, proyecto_convocatoria_id: int) -> dict:
    proyecto_convocatoria = (
        db.query(Proyecto)
        .filter(Proyecto.proyecto_id == proyecto_convocatoria_id)
        .first()
    )

    if not proyecto_convocatoria:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado.")

    if proyecto_convocatoria.ingreso_por != "convocatoria":
        return {
            "can_unify": False,
            "reason": "El proyecto no ingresó por convocatoria.",
            "proyecto_convocatoria": _serialize_proyecto_compacto(proyecto_convocatoria),
            "proyecto_rua": None,
            "rua_candidatos": [],
            "otros_proyectos_convocatoria": [],
            "docs_to_copy": [],
            "estado_final": None,
        }

    if proyecto_convocatoria.estado_general not in ("vinculacion", "guarda_provisoria"):
        return {
            "can_unify": False,
            "reason": "El proyecto no está en estado vinculacion ni guarda provisoria.",
            "proyecto_convocatoria": _serialize_proyecto_compacto(proyecto_convocatoria),
            "proyecto_rua": None,
            "rua_candidatos": [],
            "otros_proyectos_convocatoria": [],
            "docs_to_copy": [],
            "estado_final": None,
        }

    rua_candidatos = _query_rua_por_grupo(db, proyecto_convocatoria).all()
    rua_resumen = [_serialize_proyecto_compacto(p) for p in rua_candidatos]

    otros_convocatoria = (
        _query_convocatoria_por_grupo(db, proyecto_convocatoria)
        .filter(Proyecto.proyecto_id != proyecto_convocatoria.proyecto_id)
        .all()
    )

    otros_convocatoria_resumen = [
        _serialize_proyecto_compacto(p) for p in otros_convocatoria
    ]

    if not rua_candidatos:
        if _is_monoparental(proyecto_convocatoria):
            reason = "No hay proyectos RUA aptos para la unificacion para este pretenso adoptante."
        else:
            reason = "No hay proyectos RUA aptos para la unificacion para esta pareja de pretensos adoptantes."
        return {
            "can_unify": False,
            "reason": reason,
            "proyecto_convocatoria": _serialize_proyecto_compacto(proyecto_convocatoria),
            "proyecto_rua": None,
            "rua_candidatos": rua_resumen,
            "otros_proyectos_convocatoria": otros_convocatoria_resumen,
            "docs_to_copy": [],
            "estado_final": None,
        }

    if len(rua_candidatos) > 1:
        return {
            "can_unify": False,
            "reason": (
                "Inconsistencia: existe más de un proyecto RUA aprobado/viable para el mismo grupo. "
                "Revisión manual."
            ),
            "proyecto_convocatoria": _serialize_proyecto_compacto(proyecto_convocatoria),
            "proyecto_rua": None,
            "rua_candidatos": rua_resumen,
            "otros_proyectos_convocatoria": otros_convocatoria_resumen,
            "docs_to_copy": [],
            "estado_final": None,
        }

    proyecto_rua = rua_candidatos[0]

    docs_to_copy = _build_docs_preview(proyecto_convocatoria, proyecto_rua)

    copiar_orden = (
        (_is_empty_text(proyecto_convocatoria.nro_orden_rua) and not _is_empty_text(proyecto_rua.nro_orden_rua))
        or (not proyecto_convocatoria.fecha_asignacion_nro_orden and proyecto_rua.fecha_asignacion_nro_orden)
    )

    return {
        "can_unify": True,
        "reason": None,
        "proyecto_convocatoria": _serialize_proyecto_compacto(proyecto_convocatoria),
        "proyecto_rua": _serialize_proyecto_compacto(proyecto_rua),
        "rua_candidatos": rua_resumen,
        "otros_proyectos_convocatoria": otros_convocatoria_resumen,
        "docs_to_copy": docs_to_copy,
        "copiar_orden": copiar_orden,
        "estado_final": {
            "convocatoria": proyecto_convocatoria.estado_general,
            "rua": "baja_por_convocatoria",
        },
    }


def _build_dest_dir(base_dir: str, proyecto_id: int, field_name: str) -> str:
    dest_dir = os.path.join(base_dir, str(proyecto_id))
    os.makedirs(dest_dir, exist_ok = True)
    return dest_dir


def _copy_doc_field(
    proyecto_convocatoria: Proyecto,
    proyecto_rua: Proyecto,
    field_name: str,
    base_dir: str,
    created_paths: list,
) -> int:
    conv_value = getattr(proyecto_convocatoria, field_name)
    rua_value = getattr(proyecto_rua, field_name)

    if not _is_empty_doc_value(conv_value) or _is_empty_doc_value(rua_value):
        return 0

    mode, entries = _parse_doc_entries(rua_value)
    if not entries:
        return 0

    dest_dir = _build_dest_dir(base_dir, proyecto_convocatoria.proyecto_id, field_name)
    new_entries = []

    for entry in entries:
        src_path = entry["src"]
        if not os.path.exists(src_path):
            continue

        filename = os.path.basename(src_path)
        dest_path = os.path.abspath(os.path.join(dest_dir, filename))

        shutil.copy2(src_path, dest_path)
        created_paths.append(dest_path)

        if mode == "single":
            new_entries.append(dest_path)
        else:
            meta = dict(entry["meta"]) if isinstance(entry["meta"], dict) else {"ruta": src_path}
            meta["ruta"] = dest_path
            new_entries.append(meta)

    if not new_entries:
        return 0

    if mode == "single":
        setattr(proyecto_convocatoria, field_name, new_entries[0])
    else:
        setattr(proyecto_convocatoria, field_name, json.dumps(new_entries, ensure_ascii = False))

    return len(new_entries)


def _format_docs_copiados(detalles_docs: list) -> str:
    if not detalles_docs:
        return "documentos: no se copiaron"
    partes = [f"{item['field_name']}({item['cantidad']})" for item in detalles_docs]
    return "documentos: " + ", ".join(partes)


def unify_on_enter_vinculacion(
    db: Session,
    proyecto_convocatoria_id: int,
    login_usuario: str,
) -> None:
    proyecto_convocatoria = (
        db.query(Proyecto)
        .filter(Proyecto.proyecto_id == proyecto_convocatoria_id)
        .first()
    )

    if not proyecto_convocatoria:
        raise HTTPException(status_code = 404, detail = "Proyecto no encontrado.")

    if proyecto_convocatoria.ingreso_por != "convocatoria":
        return

    if proyecto_convocatoria.estado_general not in ("vinculacion", "guarda_provisoria"):
        return

    proyectos_rua = _query_rua_por_grupo(db, proyecto_convocatoria).all()

    if not proyectos_rua:
        return

    if len(proyectos_rua) > 1:
        raise HTTPException(
            status_code = 409,
            detail = (
                "Inconsistencia: existe más de un proyecto RUA aprobado/viable para el mismo grupo. "
                "Revisión manual."
            )
        )

    proyecto_rua = proyectos_rua[0]

    copio_nro_orden = False
    copio_fecha_orden = False
    if _is_empty_text(proyecto_convocatoria.nro_orden_rua) and not _is_empty_text(proyecto_rua.nro_orden_rua):
        proyecto_convocatoria.nro_orden_rua = proyecto_rua.nro_orden_rua
        copio_nro_orden = True
    if not proyecto_convocatoria.fecha_asignacion_nro_orden and proyecto_rua.fecha_asignacion_nro_orden:
        proyecto_convocatoria.fecha_asignacion_nro_orden = proyecto_rua.fecha_asignacion_nro_orden
        copio_fecha_orden = True

    base_dir = _get_docs_base_dir()
    created_paths = []
    detalles_docs = []
    estado_rua_anterior = proyecto_rua.estado_general

    try:
        for field_name in DOC_FIELDS:
            cantidad_copiada = _copy_doc_field(
                proyecto_convocatoria = proyecto_convocatoria,
                proyecto_rua = proyecto_rua,
                field_name = field_name,
                base_dir = base_dir,
                created_paths = created_paths,
            )
            if cantidad_copiada:
                detalles_docs.append({
                    "field_name": field_name,
                    "cantidad": cantidad_copiada,
                })

        detalles_orden = []
        if copio_nro_orden:
            detalles_orden.append(f"nro_orden_rua={proyecto_convocatoria.nro_orden_rua}")
        if copio_fecha_orden:
            detalles_orden.append(
                f"fecha_asignacion_nro_orden={proyecto_convocatoria.fecha_asignacion_nro_orden}"
            )
        texto_orden = (
            "orden: sin cambios"
            if not detalles_orden
            else "orden: " + ", ".join(detalles_orden)
        )

        texto_docs = _format_docs_copiados(detalles_docs)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        estado_convocatoria = proyecto_convocatoria.estado_general

        db.add(RuaEvento(
            login = login_usuario,
            evento_detalle = (
                "Unificacion proyectos (convocatoria): "
                f"proyecto {proyecto_convocatoria.proyecto_id} mantiene estado {estado_convocatoria}, "
                f"fuente RUA {proyecto_rua.proyecto_id}; "
                f"{texto_orden}; {texto_docs}; fecha={timestamp}"
            ),
            evento_fecha = datetime.now(),
        ))

        db.add(ObservacionesProyectos(
            observacion = (
                f"Unificacion proyectos: mantiene estado {estado_convocatoria}; "
                f"fuente RUA {proyecto_rua.proyecto_id}; "
                f"{texto_orden}; {texto_docs}; fecha={timestamp}"
            ),
            observacion_fecha = datetime.now(),
            login_que_observo = login_usuario,
            observacion_a_cual_proyecto = proyecto_convocatoria.proyecto_id,
        ))

        proyecto_rua.estado_general = "baja_por_convocatoria"

        db.add(RuaEvento(
            login = login_usuario,
            evento_detalle = (
                "Unificacion proyectos (RUA): "
                f"proyecto {proyecto_rua.proyecto_id} cambio estado "
                f"{estado_rua_anterior} -> baja_por_convocatoria por convocatoria "
                f"{proyecto_convocatoria.proyecto_id}; fecha={timestamp}"
            ),
            evento_fecha = datetime.now(),
        ))

        db.add(ObservacionesProyectos(
            observacion = (
                "Unificacion proyectos: cambio estado a baja_por_convocatoria; "
                f"convocatoria {proyecto_convocatoria.proyecto_id}; "
                f"{texto_orden}; {texto_docs}; fecha={timestamp}"
            ),
            observacion_fecha = datetime.now(),
            login_que_observo = login_usuario,
            observacion_a_cual_proyecto = proyecto_rua.proyecto_id,
        ))

        db.add(ProyectoHistorialEstado(
            proyecto_id = proyecto_rua.proyecto_id,
            estado_anterior = estado_rua_anterior,
            estado_nuevo = "baja_por_convocatoria",
            comentarios = (
                "Unificacion proyectos: cambio estado a baja_por_convocatoria; "
                f"convocatoria {proyecto_convocatoria.proyecto_id}; "
                f"{texto_orden}; {texto_docs}; fecha={timestamp}"
            ),
        ))

    except Exception:
        for path in created_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        raise

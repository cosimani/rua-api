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
from models.proyecto import Proyecto


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


def _build_dest_dir(base_dir: str, proyecto_id: int, field_name: str) -> str:
    dest_dir = os.path.join(base_dir, str(proyecto_id), field_name)
    os.makedirs(dest_dir, exist_ok = True)
    return dest_dir


def _copy_doc_field(
    proyecto_convocatoria: Proyecto,
    proyecto_rua: Proyecto,
    field_name: str,
    base_dir: str,
    created_paths: list,
) -> None:
    conv_value = getattr(proyecto_convocatoria, field_name)
    rua_value = getattr(proyecto_rua, field_name)

    if not _is_empty_doc_value(conv_value) or _is_empty_doc_value(rua_value):
        return

    mode, entries = _parse_doc_entries(rua_value)
    if not entries:
        return

    dest_dir = _build_dest_dir(base_dir, proyecto_convocatoria.proyecto_id, field_name)
    new_entries = []

    for entry in entries:
        src_path = entry["src"]
        if not os.path.exists(src_path):
            raise FileNotFoundError(f"Archivo no encontrado para {field_name}: {src_path}")

        ext = os.path.splitext(src_path)[1]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{field_name}_{timestamp}_{uuid4().hex}{ext}"
        dest_path = os.path.abspath(os.path.join(dest_dir, filename))

        shutil.copy2(src_path, dest_path)
        created_paths.append(dest_path)

        if mode == "single":
            new_entries.append(dest_path)
        else:
            meta = dict(entry["meta"]) if isinstance(entry["meta"], dict) else {"ruta": src_path}
            meta["ruta"] = dest_path
            new_entries.append(meta)

    if mode == "single":
        setattr(proyecto_convocatoria, field_name, new_entries[0])
    else:
        setattr(proyecto_convocatoria, field_name, json.dumps(new_entries, ensure_ascii = False))


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

    if proyecto_convocatoria.estado_general != "vinculacion":
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

    if _is_empty_text(proyecto_convocatoria.nro_orden_rua) and _is_empty_text(proyecto_convocatoria.fecha_asignacion_nro_orden):
        if not _is_empty_text(proyecto_rua.nro_orden_rua):
            proyecto_convocatoria.nro_orden_rua = proyecto_rua.nro_orden_rua
        if proyecto_rua.fecha_asignacion_nro_orden:
            proyecto_convocatoria.fecha_asignacion_nro_orden = proyecto_rua.fecha_asignacion_nro_orden

    base_dir = _get_docs_base_dir()
    created_paths = []

    try:
        for field_name in DOC_FIELDS:
            _copy_doc_field(
                proyecto_convocatoria = proyecto_convocatoria,
                proyecto_rua = proyecto_rua,
                field_name = field_name,
                base_dir = base_dir,
                created_paths = created_paths,
            )

        db.add(RuaEvento(
            login = login_usuario,
            evento_detalle = (
                "Unificación proyectos: convocatoria "
                f"{proyecto_convocatoria.proyecto_id} copió orden/documentación desde RUA "
                f"{proyecto_rua.proyecto_id} por paso a vinculación"
            ),
            evento_fecha = datetime.now(),
        ))

        proyecto_rua.estado_general = "baja_por_convocatoria"

    except Exception:
        for path in created_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        raise

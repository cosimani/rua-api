import os
import secrets  # Para comparar contraseñas de forma segura
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.responses import Response

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from dotenv import load_dotenv



# Creamos y exponemos el limiter ANTES de importar routers
# Limiter global (usa IP remota para la key)
limiter = Limiter(key_func=get_remote_address)



app = FastAPI(
    title="RUA API",
    version="2.0",
    root_path="/api",  # 👈 muy importante
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)


#  ——— Configuración de slowapi ——————————————————————————————
# Exponer el limiter en el estado de la app
app.state.limiter = limiter
# Middleware para inyectar la lógica de rate-limit
app.add_middleware(SlowAPIMiddleware)
# Excepciones de límite excedido devuelven 429 JSON
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# ———————————————————————————————————————————————————————————



# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]  # <- necesario para que el frontend lo vea
)


# Cargar variables de entorno
load_dotenv()
security = HTTPBasic()



@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)

    # 1) Clickjacking
    response.headers["X-Frame-Options"] = "DENY"

    # 2) Content Security Policy
    nonce = secrets.token_urlsafe(16)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self'; "
        f"img-src 'self' data:; "
        f"object-src 'none'; "
        f"frame-ancestors 'none'; "
        f"report-uri /csp-report"
    )
    # Si sirves HTML, debes inyectar este mismo `nonce` en tus <script nonce="...">

    # 3) HSTS
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

    # 4) No MIME-sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"

    # 5) Referrer Policy
    response.headers["Referrer-Policy"] = "no-referrer-when-downgrade"

    # 6) Permissions Policy
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), fullscreen=(self)"

    # 7) XSS Protection (legacy)
    response.headers["X-XSS-Protection"] = "1; mode=block"

    return response




from routes.login import login_router
from routes.check import check_router
from routes.users import users_router
from routes.carpeta import carpetas_router
from routes.proyectos import proyectos_router
from routes.ddjj import ddjj_router
from routes.estadisticas import estadisticas_router
from routes.notificaciones import notificaciones_router
from routes.nna import nna_router
from routes.convocatorias import convocatoria_router
from routes.postulaciones import postulaciones_router



# Rutas de la aplicación
app.include_router(login_router, prefix="/auth", tags=["Auth"])
app.include_router(check_router, prefix="/check", tags=["Checks"])
app.include_router(users_router, prefix="/users", tags=["Users"])
app.include_router(carpetas_router, prefix="/carpetas", tags=["Carpetas"])
app.include_router(proyectos_router, prefix="/proyectos", tags=["Proyectos"])
app.include_router(nna_router, prefix="/nnas", tags=["NNAs"])
app.include_router(convocatoria_router, prefix="/convocatorias", tags=["Convocatorias"])
app.include_router(ddjj_router, prefix="/ddjj", tags=["Ddjj"])
app.include_router(estadisticas_router, prefix="/estadisticas", tags=["Estadísticas"])
app.include_router(notificaciones_router, prefix="/notificaciones", tags=["Notificaciones"])
app.include_router(postulaciones_router, prefix="/postulaciones", tags=["Postulaciones"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)



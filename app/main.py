import os
import secrets  # Para comparar contraseñas de forma segura
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

security = HTTPBasic()


app = FastAPI(title="RUA API", version="2.0")

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]  # <- necesario para que el frontend lo vea
)

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)



# RUA API - Backend FastAPI para el Sistema RUA

Este repositorio contiene la definición del servicio backend del sistema RUA (Registro Único de Adopciones), desarrollado con **FastAPI** y desplegado mediante **Docker Compose**.

---

## 🚀 Tecnologías utilizadas

- 🐍 **FastAPI** – framework moderno, rápido y tipado
- 🐳 **Docker** – para contenerización del servicio
- 📂 **Montaje de volúmenes** – para acceso a documentos PDF y archivos cargados
- 🧠 **Uvicorn** – servidor ASGI de alto rendimiento
- 🌐 **Nginx (externo)** – para proxy inverso en entorno de producción

---

## 🧱 Estructura del contenedor

El contenedor expone la API en el puerto `8000`, pero no se publica directamente al host. Se recomienda el uso de Nginx como proxy inverso.

La app se lanza con:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

---

## 📦 Variables de entorno

Las variables sensibles se cargan desde un archivo `.env`. Algunas de las más importantes:

```dotenv
UPLOAD_DIR_DOC_PRETENSOS=/docs/pretensos
UPLOAD_DIR_DOC_PROYECTOS=/docs/proyectos
UPLOAD_DIR_DOC_INFORMES=/docs/informes
UPLOAD_DIR_DOC_NNAS=/docs/nnas
DIR_PDF_GENERADOS=/docs/pdfs
```

---

## 🧪 Comandos útiles

```bash
# Levantar contenedor
docker compose up -d

# Detener contenedor
docker compose down

# Ver logs
docker compose logs -f

# Reconstruir imagen
docker compose build
```

---

## 📂 Montaje de volúmenes

El contenedor monta las siguientes carpetas del host para acceso a documentos y generación de PDFs:

```
/home/ubuntu/docs-rua/pretensos
/home/ubuntu/docs-rua/proyectos
/home/ubuntu/docs-rua/informes
/home/ubuntu/docs-rua/nnas
/home/ubuntu/docs-rua/pdfs
```

Estas deben existir en el host antes de ejecutar el contenedor.

---

## 🧠 Recomendaciones

- Usá un proxy inverso como Nginx para enrutar peticiones a `/api` → `rua_api:8000`
- No utilices `--reload` en producción
- No expongas el puerto 8000 al exterior directamente (usá `expose`)

---

## 📌 Red Docker

El contenedor debe estar conectado a la red Docker `app-network`, compartida con otros servicios del ecosistema RUA.

---

## 🛡️ Seguridad

- No subas tu archivo `.env` al repositorio.
- Asegurate de que los directorios montados no expongan datos sensibles si compartís el host.

---

## 🧩 Licencia

Sistema RUA

---
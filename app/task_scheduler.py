import asyncio
import time
import requests
import os
from database.config import SessionLocal
from models.users import User
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()
FASTAPI_HOST = os.getenv("FASTAPI_HOST", "http://localhost:8000")  # Valor por defecto si no está en .env


async def check_moodle_course_completion(wait_time: int):
    """
    Verifica si los usuarios han completado un curso en Moodle, procesando uno por uno,
    con una pausa entre cada usuario basada en `wait_time`.
    
    Args:
        wait_time (int): Cantidad de segundos a esperar entre cada consulta.
    """
    db = SessionLocal()
    
    try:
        users = db.query(User).filter(User.doc_adoptante_curso_aprobado == "N").all()
    finally:
        db.close()

    if not users:
        print("✅ No hay usuarios pendientes de verificación en Moodle.")
        return

    print(f"🔄 Verificando cursos de {len(users)} usuarios en Moodle...")

    loop = asyncio.get_event_loop()  # Obtener el loop de eventos

    for user in users:
        url = f"{FASTAPI_HOST}/check/api_moodle_curso_aprobado"
        params = {"mail": user.mail}

        # Ejecutar la consulta de Moodle sin bloquear asyncio
        await loop.run_in_executor(None, call_api, url, params)

        print(f"⏳ Esperando {wait_time} segundos antes del próximo usuario...")
        await asyncio.sleep(wait_time)  # Pausa antes de procesar el siguiente usuario

def call_api(url, params):
    """
    Llama a un endpoint específico con parámetros y mide el tiempo de respuesta.
    """
    try:
        start_time = time.perf_counter()
        response = requests.get(url, params=params)
        response_time = time.perf_counter() - start_time

        if response.status_code == 200:
            message = response.json().get("message", "OK")
            print(f"[✓] {url} → {message} | Tiempo: {response_time:.2f} seg")
        else:
            print(f"[✗] {url} → Error: {response.status_code} | Tiempo: {response_time:.2f} seg")

    except Exception as e:
        print(f"[ERROR] {url} → {str(e)}")


async def run_tasks(wait_time: int = 60):
    """
    Programa y ejecuta la verificación de Moodle periódicamente con el tiempo de espera definido.
    
    Args:
        wait_time (int): Segundos a esperar entre cada usuario (por defecto 900s = 15 minutos).
    """
    while True:
        print('run_tasks', flush=True)
        await check_moodle_course_completion(wait_time)  # Ejecuta la consulta para todos los usuarios
        print(f"🔄 Ciclo completado. Esperando {wait_time} segundos antes de comenzar de nuevo...")
        await asyncio.sleep(wait_time)  # Espera antes de iniciar el siguiente ciclo



if __name__ == "__main__":

    print('task_scheduler.py iniciado', flush=True)

    wait_seconds = int(os.getenv("MOODLE_CHECK_WAIT", 60))  # Permite definir el tiempo en el .env
    asyncio.run(run_tasks(wait_seconds))

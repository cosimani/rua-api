# Usa una imagen base con Python 3.8
FROM python:3.8

# Configura variables de entorno para evitar la creación de archivos .pyc y asegurar logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Argentina/Cordoba

# Instala paquetes del sistema necesarios (mc, tcpdump, ifconfig)
RUN apt-get update && apt-get install -y \
    mc \
    tcpdump \
    net-tools \
    poppler-utils \
    libgl1 \
    libreoffice \    
    && rm -rf /var/lib/apt/lists/*

# Define el directorio de trabajo en el contenedor
WORKDIR /app

# Copia primero el archivo de dependencias para aprovechar la caché de Docker
COPY requirements.txt /app/

# Instala las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Luego copia el resto de los archivos de la aplicación
COPY app /app

# Expone el puerto 8000
EXPOSE 8000

# Comando de inicio dinámico según entorno (para que se use --reload en desarrollo)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000"]

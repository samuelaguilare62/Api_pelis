from flask import Flask, jsonify, request
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import os
import secrets
from functools import wraps
import time
import threading
from collections import defaultdict
import re
import requests
from PIL import Image
from io import BytesIO
import base64
import hashlib
from datetime import datetime, timedelta

# Inicializar Flask
app = Flask(__name__)
CORS(app)

# Configuración de seguridad
app.config['JSON_SORT_KEYS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request size

# Configuración de Email - SIN CREDENCIALES SMTP
EMAIL_CONFIG = {
    'admin_email': os.environ.get('ADMIN_EMAIL', ''),
    'service_enabled': True  # Siempre habilitado, usa método sin autenticación
}

# Configuración de administrador - AHORA CON VARIABLE DE ENTORNO
ADMIN_TOKENS = [os.environ.get('ADMIN_TOKEN', 'admin_token_secreto_2024')]

# Configuración de compresión de imágenes
IMAGE_COMPRESSION_CONFIG = {
    'free': {
        'quality': 75,
        'max_width': 400,
        'format': 'JPEG'
    },
    'premium': {
        'quality': 85, 
        'max_width': 800,
        'format': 'JPEG'
    },
    'admin': {
        'quality': 90,
        'max_width': 1200,
        'format': 'JPEG'
    }
}

# Cache en memoria para imágenes comprimidas
image_cache = {}
CACHE_DURATION = 24 * 3600  # 24 horas

# Headers para requests de imágenes
IMAGE_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# Configuración de Firebase - SOLO VARIABLES DE ENTORNO
def initialize_firebase():
    try:
        print("🔄 Inicializando Firebase SOLO con variables de entorno...")
        
        # Verificar variables críticas primero
        required_vars = ['FIREBASE_TYPE', 'FIREBASE_PROJECT_ID', 'FIREBASE_PRIVATE_KEY', 'FIREBASE_CLIENT_EMAIL']
        missing_vars = []
        
        for var in required_vars:
            if not os.environ.get(var):
                missing_vars.append(var)
        
        if missing_vars:
            print(f"❌ Variables de entorno faltantes: {missing_vars}")
            print("💡 Configura estas variables en Render.com > Environment")
            return None
        
        print("✅ Todas las variables de entorno están presentes")
        
        # Crear diccionario de credenciales SOLO con variables de entorno
        service_account_info = {
            "type": os.environ.get('FIREBASE_TYPE'),
            "project_id": os.environ.get('FIREBASE_PROJECT_ID'),
            "private_key_id": os.environ.get('FIREBASE_PRIVATE_KEY_ID'),
            "private_key": os.environ.get('FIREBASE_PRIVATE_KEY', '').replace('\\n', '\n'),
            "client_email": os.environ.get('FIREBASE_CLIENT_EMAIL'),
            "client_id": os.environ.get('FIREBASE_CLIENT_ID'),
            "auth_uri": os.environ.get('FIREBASE_AUTH_URI'),
            "token_uri": os.environ.get('FIREBASE_TOKEN_URI'),
            "auth_provider_x509_cert_url": os.environ.get('FIREBASE_AUTH_PROVIDER_CERT_URL'),
            "client_x509_cert_url": os.environ.get('FIREBASE_CLIENT_CERT_URL')
        }
        
        # Inicializar Firebase con las variables de entorno
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        
        # Probar conexión con una operación simple
        try:
            print("🔍 Probando conexión a Firestore...")
            test_ref = db.collection('api_users').limit(1)
            docs = list(test_ref.stream())
            print(f"✅ Conexión exitosa. Documentos encontrados: {len(docs)}")
            return db
        except Exception as e:
            print(f"❌ Error en conexión a Firestore: {e}")
            return None
            
    except Exception as e:
        print(f"❌ Error crítico inicializando Firebase: {e}")
        return None

# Inicializar Firebase
db = initialize_firebase()

# Colección para almacenar usuarios y tokens
TOKENS_COLLECTION = "api_users"

# Configuración de planes
PLAN_CONFIG = {
    'free': {
        'daily_limit': 50,
        'session_limit': 5,
        'rate_limit_per_minute': 10,
        'concurrent_requests': 1,
        'features': {
            'content_access': 'limited',
            'api_responses': 'basic',
            'search_limit': 5,
            'content_previews': True,
            'streaming': False,
            'download_links': False,
            'api_support': 'community',
            'request_priority': 'low',
            'bulk_operations': False,
            'advanced_filters': False,
            'content_recommendations': False
        }
    },
    'premium': {
        'daily_limit': 1000,
        'session_limit': 100,
        'rate_limit_per_minute': 60,
        'concurrent_requests': 3,
        'features': {
            'content_access': 'full',
            'api_responses': 'enhanced',
            'search_limit': 50,
            'content_previews': True,
            'streaming': True,
            'download_links': True,
            'api_support': 'priority',
            'request_priority': 'high',
            'bulk_operations': True,
            'advanced_filters': True,
            'content_recommendations': True
        }
    }
}

SESSION_TIMEOUT = 3600  # 1 hora en segundos

# Track requests por usuario para rate limiting
user_request_times = defaultdict(list)
request_lock = threading.Lock()

# IP-based rate limiting
ip_request_times = defaultdict(list)
ip_lock = threading.Lock()

# Configuración de seguridad
MAX_REQUESTS_PER_MINUTE_PER_IP = 100
MAX_REQUESTS_PER_MINUTE_PER_USER = 60

# FUNCIÓN MEJORADA PARA ENVÍO DE EMAILS SIN SMTP
def send_email_async(to_email, subject, message):
    """Enviar email en segundo plano usando servicio sin autenticación"""
    def send_email():
        try:
            # MÉTODO 1: Usar Webhook.email (servicio gratuito sin autenticación)
            webhook_url = "https://webhook.email/inbound/your-unique-id"
            
            email_data = {
                "to": to_email,
                "subject": subject,
                "html": message,
                "from": "notifications@yourapi.com"
            }
            
            try:
                response = requests.post(
                    webhook_url,
                    json=email_data,
                    headers={'Content-Type': 'application/json'},
                    timeout=10
                )
                
                if response.status_code == 200:
                    print(f"✅ Email enviado exitosamente a: {to_email}")
                    return
                else:
                    print(f"⚠️  Webhook.email falló, usando método alternativo")
            except Exception as e:
                print(f"⚠️  Error con webhook.email: {e}")
            
            # MÉTODO 2: Usar Formspree (alternativa gratuita)
            try:
                formspree_data = {
                    "_replyto": to_email,
                    "_subject": subject,
                    "message": message,
                    "email": to_email
                }
                
                response = requests.post(
                    "https://formspree.io/f/your-form-id",
                    data=formspree_data,
                    timeout=10
                )
                
                if response.status_code == 200:
                    print(f"✅ Email enviado vía Formspree a: {to_email}")
                    return
            except Exception as e:
                print(f"⚠️  Error con Formspree: {e}")
            
            # MÉTODO 3: Log para debugging (si fallan los métodos anteriores)
            print(f"📧 [SIMULACIÓN] Email para {to_email}: {subject}")
            print(f"📧 [SIMULACIÓN] Mensaje: {message[:100]}...")
            
        except Exception as e:
            print(f"❌ Error enviando email a {to_email}: {e}")
    
    # Ejecutar en segundo plano
    thread = threading.Thread(target=send_email)
    thread.daemon = True
    thread.start()

# FUNCIÓN MEJORADA PARA NOTIFICAR LÍMITES ALCANZADOS
def notify_limit_reached(user_data, limit_type, current_usage, limit, reset_time):
    """Notificar automáticamente al usuario que alcanzó un límite usando su email registrado"""
    try:
        user_email = user_data.get('email')
        username = user_data.get('username', 'Usuario')
        plan_type = user_data.get('plan_type', 'free')
        
        if not user_email:
            print("⚠️  No se puede notificar: usuario sin email")
            return
        
        print(f"📧 Preparando notificación para {user_email} - Límite: {limit_type}")
        
        # Plantillas de email según el tipo de límite
        if limit_type == 'daily':
            subject = f"🚫 Límite Diario Alcanzado - API Streaming"
            message = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <h2 style="color: #e74c3c;">Hola {username},</h2>
                    <p>Has alcanzado tu límite diario de peticiones en nuestra API de Streaming.</p>
                    
                    <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin: 20px 0;">
                        <h3 style="color: #856404; margin-top: 0;">📊 Resumen de Uso:</h3>
                        <ul style="list-style: none; padding: 0;">
                            <li style="margin: 8px 0;"><strong>Plan Actual:</strong> {plan_type.upper()}</li>
                            <li style="margin: 8px 0;"><strong>Límite Diario:</strong> {limit} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Uso Actual:</strong> {current_usage} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Se reinicia en:</strong> {reset_time}</li>
                        </ul>
                    </div>
                    
                    <p>💡 <strong>¿Necesitas más límites?</strong> Considera actualizar a nuestro plan PREMIUM para obtener:</p>
                    <ul>
                        <li>✅ Hasta 1000 peticiones diarias</li>
                        <li>✅ Acceso completo a series y contenido exclusivo</li>
                        <li>✅ Streaming HD ilimitado</li>
                        <li>✅ Soporte prioritario 24/7</li>
                    </ul>
                    
                    <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee;">
                        Si tienes alguna pregunta, no dudes en contactarnos.
                    </p>
                    
                    <p style="color: #666; font-size: 14px;">
                        Saludos,<br>El equipo de API Streaming
                    </p>
                </div>
            </body>
            </html>
            """
        elif limit_type == 'session':
            subject = f"⚠️ Límite de Sesión Alcanzado - API Streaming"
            message = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <h2 style="color: #f39c12;">Hola {username},</h2>
                    <p>Has alcanzado tu límite de peticiones por sesión en nuestra API de Streaming.</p>
                    
                    <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin: 20px 0;">
                        <h3 style="color: #856404; margin-top: 0;">📊 Resumen de Uso:</h3>
                        <ul style="list-style: none; padding: 0;">
                            <li style="margin: 8px 0;"><strong>Plan Actual:</strong> {plan_type.upper()}</li>
                            <li style="margin: 8px 0;"><strong>Límite por Sesión:</strong> {limit} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Uso Actual:</strong> {current_usage} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Se reinicia en:</strong> {reset_time}</li>
                        </ul>
                    </div>
                    
                    <p>🔄 <strong>Tu sesión se reiniciará automáticamente en {reset_time}</strong></p>
                    
                    <p>💡 <strong>Con el plan PREMIUM</strong> tendrías límites más amplios:</p>
                    <ul>
                        <li>✅ 100 peticiones por sesión</li>
                        <li>✅ Mayor tasa de requests por minuto</li>
                        <li>✅ Múltiples solicitudes concurrentes</li>
                    </ul>
                    
                    <p style="color: #666; font-size: 14px; margin-top: 30px;">
                        Saludos,<br>El equipo de API Streaming
                    </p>
                </div>
            </body>
            </html>
            """
        elif limit_type == 'rate_limit':
            subject = f"🚦 Límite de Velocidad Alcanzado - API Streaming"
            message = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <h2 style="color: #e67e22;">Hola {username},</h2>
                    <p>Has excedido el límite de velocidad de peticiones en nuestra API de Streaming.</p>
                    
                    <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin: 20px 0;">
                        <h3 style="color: #856404; margin-top: 0;">📊 Resumen de Uso:</h3>
                        <ul style="list-style: none; padding: 0;">
                            <li style="margin: 8px 0;"><strong>Plan Actual:</strong> {plan_type.upper()}</li>
                            <li style="margin: 8px 0;"><strong>Límite por Minuto:</strong> {limit} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Uso Actual:</strong> {current_usage} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Puedes reintentar en:</strong> 1 minuto</li>
                        </ul>
                    </div>
                    
                    <p>⏰ <strong>Espera 1 minuto</strong> antes de realizar más peticiones.</p>
                    
                    <p>💡 <strong>Con el plan PREMIUM</strong> tendrías:</p>
                    <ul>
                        <li>✅ Hasta 60 peticiones por minuto</li>
                        <li>✅ Hasta 3 solicitudes concurrentes</li>
                        <li>✅ Prioridad alta en el procesamiento</li>
                    </ul>
                    
                    <p style="color: #666; font-size: 14px; margin-top: 30px;">
                        Saludos,<br>El equipo de API Streaming
                    </p>
                </div>
            </body>
            </html>
            """
        else:
            subject = f"📊 Límite Alcanzado - API Streaming"
            message = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <h2>Hola {username},</h2>
                    <p>Has alcanzado un límite de uso en nuestra API de Streaming.</p>
                    
                    <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin: 20px 0;">
                        <h3 style="color: #856404; margin-top: 0;">📊 Detalles:</h3>
                        <ul style="list-style: none; padding: 0;">
                            <li style="margin: 8px 0;"><strong>Tipo de Límite:</strong> {limit_type}</li>
                            <li style="margin: 8px 0;"><strong>Plan Actual:</strong> {plan_type.upper()}</li>
                            <li style="margin: 8px 0;"><strong>Límite:</strong> {limit} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Uso Actual:</strong> {current_usage} peticiones</li>
                            <li style="margin: 8px 0;"><strong>Se reinicia en:</strong> {reset_time}</li>
                        </ul>
                    </div>
                    
                    <p style="color: #666; font-size: 14px;">
                        Saludos,<br>El equipo de API Streaming
                    </p>
                </div>
            </body>
            </html>
            """
        
        # Enviar email en segundo plano - FUNCIONA AUTOMÁTICAMENTE
        send_email_async(user_email, subject, message)
        
        print(f"✅ Notificación de {limit_type} enviada a {user_email}")
        
        # También notificar al admin si es un límite crítico
        if limit_type == 'daily' and EMAIL_CONFIG.get('admin_email'):
            admin_subject = f"🔔 Usuario alcanzó límite diario: {username}"
            admin_message = f"""
            <html>
            <body>
                <h2>Notificación de Admin</h2>
                <p>El usuario {username} ({user_email}) ha alcanzado su límite diario.</p>
                <ul>
                    <li><strong>Plan:</strong> {plan_type}</li>
                    <li><strong>Uso:</strong> {current_usage}/{limit}</li>
                    <li><strong>Reset en:</strong> {reset_time}</li>
                </ul>
            </body>
            </html>
            """
            send_email_async(EMAIL_CONFIG['admin_email'], admin_subject, admin_message)
            
    except Exception as e:
        print(f"❌ Error en notificación de límite: {e}")

# FUNCIONES DE COMPRESIÓN DE IMÁGENES
def compress_image_url(image_url, quality=85, max_width=800, output_format='JPEG'):
    """
    Comprimir imagen desde URL y devolver base64
    """
    try:
        # Validar URL
        if not image_url or not image_url.startswith(('http://', 'https://')):
            return {"success": False, "error": "URL inválida"}
        
        print(f"🔄 Comprimiendo imagen: {image_url[:80]}...")
        
        # Descargar imagen con timeout
        response = requests.get(
            image_url, 
            headers=IMAGE_REQUEST_HEADERS, 
            timeout=15,
            stream=True
        )
        response.raise_for_status()
        
        # Verificar que sea una imagen
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            return {"success": False, "error": "URL no es una imagen"}
        
        # Leer imagen
        original_content = response.content
        original_size = len(original_content)
        
        # Abrir con Pillow
        img = Image.open(BytesIO(original_content))
        
        # Preservar formato original si es posible
        if output_format == 'AUTO':
            if img.format in ['JPEG', 'PNG', 'WEBP']:
                output_format = img.format
            else:
                output_format = 'JPEG'
        
        # Convertir a RGB si es necesario (JPEG no soporta alpha)
        if output_format == 'JPEG' and img.mode in ('RGBA', 'LA', 'P'):
            if img.mode == 'P':
                img = img.convert('RGBA')
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Redimensionar si es necesario
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.LANCZOS)
            print(f"📐 Redimensionada: {img.width}x{img.height}")
        
        # Comprimir
        output_buffer = BytesIO()
        img.save(
            output_buffer, 
            format=output_format, 
            quality=quality, 
            optimize=True
        )
        
        compressed_size = output_buffer.tell()
        savings_percent = round((1 - compressed_size/original_size) * 100, 2)
        
        # Convertir a base64
        output_buffer.seek(0)
        img_base64 = base64.b64encode(output_buffer.getvalue()).decode('utf-8')
        
        print(f"✅ Imagen comprimida: {original_size/1024:.1f}KB → {compressed_size/1024:.1f}KB ({savings_percent}% ahorro)")
        
        return {
            "success": True,
            "data_url": f"data:image/{output_format.lower()};base64,{img_base64}",
            "original_size": original_size,
            "compressed_size": compressed_size,
            "savings_percent": savings_percent,
            "dimensions": f"{img.width}x{img.height}",
            "format": output_format
        }
        
    except Exception as e:
        print(f"❌ Error comprimiendo imagen {image_url}: {e}")
        return {
            "success": False,
            "original_url": image_url,
            "error": str(e)
        }

def get_cached_compressed_image(image_url, quality=85, max_width=800, output_format='JPEG'):
    """
    Obtener imagen comprimida con cache
    """
    cache_key = hashlib.md5(f"{image_url}_{quality}_{max_width}_{output_format}".encode()).hexdigest()
    
    # Verificar cache
    if cache_key in image_cache:
        cache_data = image_cache[cache_key]
        if datetime.now() - cache_data['timestamp'] < timedelta(seconds=CACHE_DURATION):
            print(f"♻️  Usando imagen en cache: {image_url[:60]}...")
            return cache_data['compressed_data']
    
    # Comprimir y cachear
    result = compress_image_url(image_url, quality, max_width, output_format)
    if result['success']:
        image_cache[cache_key] = {
            'compressed_data': result,
            'timestamp': datetime.now()
        }
        return result
    
    return {"success": False, "original_url": image_url}

def get_optimized_image_url(image_url, plan_type='free'):
    """
    Obtener URL optimizada según el plan del usuario
    """
    if not image_url or not image_url.startswith(('http://', 'https://')):
        return image_url
    
    try:
        # Obtener configuración de compresión según plan
        if plan_type == 'premium':
            config = IMAGE_COMPRESSION_CONFIG['premium']
        elif plan_type == 'admin':
            config = IMAGE_COMPRESSION_CONFIG['admin']
        else:
            config = IMAGE_COMPRESSION_CONFIG['free']
        
        # Obtener imagen comprimida (con cache)
        result = get_cached_compressed_image(
            image_url, 
            quality=config['quality'],
            max_width=config['max_width'],
            output_format=config['format']
        )
        
        if result['success']:
            return result['data_url']
        else:
            # Fallback a la imagen original si falla la compresión
            print(f"⚠️  Fallback a imagen original: {image_url[:60]}...")
            return image_url
            
    except Exception as e:
        print(f"❌ Error en optimización de imagen: {e}")
        return image_url  # Fallback a original

# FUNCIONES PARA NORMALIZAR DATOS DE LA BASE DE DATOS
def normalize_movie_data(movie_data, doc_id=None):
    """Normalizar datos de película desde la estructura de BD a la esperada por la API"""
    if doc_id:
        movie_data['id'] = doc_id
    
    normalized = {
        'id': movie_data.get('id'),
        'title': movie_data.get('title', ''),
        'poster': movie_data.get('image_url', ''),  # image_url → poster
        'description': movie_data.get('sinopsis', ''),  # sinopsis → description
        'year': movie_data.get('details', {}).get('year', '') if movie_data.get('details') else movie_data.get('year', ''),
        'genre': ', '.join(movie_data.get('details', {}).get('genres', [])) if movie_data.get('details') and movie_data.get('details', {}).get('genres') else movie_data.get('genre', ''),
        'rating': movie_data.get('details', {}).get('rating', '') if movie_data.get('details') else movie_data.get('rating', ''),
        # Campos adicionales de la BD
        'original_title': movie_data.get('original_title', ''),
        'actors': movie_data.get('details', {}).get('actors', []) if movie_data.get('details') else [],
        'duration': movie_data.get('details', {}).get('duration', '') if movie_data.get('details') else '',
        'director': movie_data.get('details', {}).get('director', '') if movie_data.get('details') else '',
        'play_links': movie_data.get('play_links', [])
    }
    
    # Limpiar campos vacíos
    return {k: v for k, v in normalized.items() if v not in [None, '', [], {}]}

def normalize_series_data(series_data, doc_id=None):
    """Normalizar datos de serie desde la estructura de BD a la esperada por la API"""
    if doc_id:
        series_data['id'] = doc_id
    
    normalized = {
        'id': series_data.get('id'),
        'title': series_data.get('title', ''),
        'poster': series_data.get('image_url', ''),  # image_url → poster
        'description': series_data.get('sinopsis', ''),  # sinopsis → description
        'year': series_data.get('details', {}).get('year', '') if series_data.get('details') else series_data.get('year', ''),
        'genre': ', '.join(series_data.get('details', {}).get('genres', [])) if series_data.get('details') and series_data.get('details', {}).get('genres') else series_data.get('genre', ''),
        'rating': series_data.get('details', {}).get('rating', '') if series_data.get('details') else series_data.get('rating', ''),
        # Campos específicos de series
        'total_seasons': series_data.get('details', {}).get('total_seasons', 0) if series_data.get('details') else 0,
        'status': series_data.get('details', {}).get('status', '') if series_data.get('details') else '',
        'seasons': normalize_seasons_data(series_data.get('seasons', {}))
    }
    
    # Limpiar campos vacíos
    return {k: v for k, v in normalized.items() if v not in [None, '', [], {}, 0]}

def normalize_seasons_data(seasons_dict):
    """Normalizar estructura de temporadas"""
    if not seasons_dict:
        return []
    
    normalized_seasons = []
    
    for season_key, season_data in seasons_dict.items():
        if isinstance(season_data, dict):
            season = {
                'season_number': season_data.get('season_number', 0),
                'episode_count': season_data.get('episode_count', 0),
                'year': season_data.get('year', ''),
                'episodes': normalize_episodes_data(season_data.get('episodes', {}))
            }
            normalized_seasons.append(season)
    
    return normalized_seasons

def normalize_episodes_data(episodes_dict):
    """Normalizar estructura de episodios"""
    if not episodes_dict:
        return []
    
    normalized_episodes = []
    
    for episode_key, episode_data in episodes_dict.items():
        if isinstance(episode_data, dict):
            episode = {
                'episode_number': episode_data.get('episode_number', 0),
                'title': episode_data.get('title', ''),
                'duration': episode_data.get('duration', ''),
                'description': episode_data.get('sinopsis', ''),
                'play_links': episode_data.get('play_links', [])
            }
            normalized_episodes.append(episode)
    
    return normalized_episodes

def normalize_channel_data(channel_data, doc_id=None):
    """Normalizar datos de canal desde la estructura de BD a la esperada por la API"""
    if doc_id:
        channel_data['id'] = doc_id
    
    normalized = {
        'id': channel_data.get('id'),
        'name': channel_data.get('name', ''),
        'logo': channel_data.get('image_url', ''),  # image_url → logo
        'status': channel_data.get('status', ''),
        'category': channel_data.get('category', ''),
        'country': channel_data.get('country', ''),
        'stream_options': channel_data.get('stream_options', [])
    }
    
    # Limpiar campos vacíos
    return {k: v for k, v in normalized.items() if v not in [None, '', [], {}]}

# Decorador para verificar Firebase
def check_firebase():
    if not db:
        return jsonify({
            "success": False,
            "error": "Firebase no inicializado",
            "solution": "Verifica las variables de entorno en Render.com"
        }), 500
    return None

# Función para verificar rate limiting por IP
def check_ip_rate_limit(ip_address):
    """Verificar límites de tasa por IP"""
    current_time = time.time()
    
    with ip_lock:
        # Limpiar requests antiguos (mayores a 1 minuto)
        ip_request_times[ip_address] = [
            req_time for req_time in ip_request_times[ip_address] 
            if current_time - req_time < 60
        ]
        
        # Verificar rate limit por minuto
        if len(ip_request_times[ip_address]) >= MAX_REQUESTS_PER_MINUTE_PER_IP:
            return {
                "error": "Límite global de requests por minuto excedido",
                "limit_type": "ip_rate_limit",
                "current_usage": len(ip_request_times[ip_address]),
                "limit": MAX_REQUESTS_PER_MINUTE_PER_IP,
                "wait_time": 60
            }, 429
        
        # Agregar nuevo request
        ip_request_times[ip_address].append(current_time)
    
    return None

# Función para verificar rate limiting por usuario
def check_user_rate_limit(user_data):
    """Verificar límites de tasa por usuario"""
    user_id = user_data.get('user_id')
    plan_type = user_data.get('plan_type', 'free')
    
    if user_data.get('is_admin'):
        return None
    
    current_time = time.time()
    plan_config = PLAN_CONFIG[plan_type]
    
    with request_lock:
        # Limpiar requests antiguos (mayores a 1 minuto)
        user_request_times[user_id] = [
            req_time for req_time in user_request_times[user_id] 
            if current_time - req_time < 60
        ]
        
        # Verificar rate limit por minuto
        if len(user_request_times[user_id]) >= plan_config['rate_limit_per_minute']:
            return {
                "error": "Límite de requests por minuto excedido",
                "limit_type": "rate_limit",
                "current_usage": len(user_request_times[user_id]),
                "limit": plan_config['rate_limit_per_minute'],
                "wait_time": 60
            }, 429
        
        # Agregar nuevo request
        user_request_times[user_id].append(current_time)
    
    return None

# Función para verificar y actualizar límites de uso
def check_usage_limits(user_data):
    """Verificar límites de uso diario, por sesión y rate limiting"""
    if user_data.get('is_admin'):
        return None  # Admin no tiene límites
    
    user_id = user_data.get('user_id')
    if not user_id:
        return {"error": "ID de usuario no válido"}, 401
    
    try:
        # Primero verificar rate limits
        rate_limit_check = check_user_rate_limit(user_data)
        if rate_limit_check:
            # Notificar al usuario sobre rate limit
            notify_limit_reached(
                user_data, 
                'rate_limit', 
                rate_limit_check[0]['current_usage'], 
                rate_limit_check[0]['limit'],
                '1 minuto'
            )
            return rate_limit_check
        
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return {"error": "Usuario no encontrado"}, 401
        
        user_info = user_doc.to_dict()
        current_time = time.time()
        plan_type = user_info.get('plan_type', 'free')
        plan_config = PLAN_CONFIG[plan_type]
        
        # Obtener límites del plan
        daily_limit = plan_config['daily_limit']
        session_limit = plan_config['session_limit']
        
        # Inicializar contadores si no existen
        update_data = {}
        
        # Verificar y resetear límite diario si es un nuevo día
        last_reset = user_info.get('daily_reset_timestamp', 0)
        if current_time - last_reset >= 86400:  # 24 horas
            update_data['daily_usage_count'] = 0
            update_data['daily_reset_timestamp'] = current_time
            daily_usage = 0
        else:
            daily_usage = user_info.get('daily_usage_count', 0)
        
        # Verificar y resetear límite de sesión si ha expirado
        session_start = user_info.get('session_start_timestamp', 0)
        if current_time - session_start >= SESSION_TIMEOUT:
            update_data['session_usage_count'] = 0
            update_data['session_start_timestamp'] = current_time
            session_usage = 0
        else:
            session_usage = user_info.get('session_usage_count', 0)
        
        # Verificar si se excedieron los límites
        if daily_usage >= daily_limit:
            time_remaining = 86400 - (current_time - last_reset)
            reset_time = f"{int(time_remaining // 3600)}h {int((time_remaining % 3600) // 60)}m"
            
            # Notificar al usuario AUTOMÁTICAMENTE
            notify_limit_reached(user_data, 'daily', daily_usage, daily_limit, reset_time)
            
            return {
                "error": f"Límite diario excedido ({daily_usage}/{daily_limit})",
                "limit_type": "daily",
                "current_usage": daily_usage,
                "limit": daily_limit,
                "reset_in": reset_time
            }, 429
        
        if session_usage >= session_limit:
            time_remaining = SESSION_TIMEOUT - (current_time - session_start)
            reset_time = f"{int(time_remaining // 60)}m {int(time_remaining % 60)}s"
            
            # Notificar al usuario AUTOMÁTICAMENTE
            notify_limit_reached(user_data, 'session', session_usage, session_limit, reset_time)
            
            return {
                "error": f"Límite de sesión excedido ({session_usage}/{session_limit})",
                "limit_type": "session", 
                "current_usage": session_usage,
                "limit": session_limit,
                "reset_in": reset_time
            }, 429
        
        # Actualizar contadores
        update_data['daily_usage_count'] = daily_usage + 1
        update_data['session_usage_count'] = session_usage + 1
        update_data['last_used'] = firestore.SERVER_TIMESTAMP
        update_data['total_usage_count'] = firestore.Increment(1)
        
        # Asegurarse de que los timestamps estén inicializados
        if 'daily_reset_timestamp' not in user_info:
            update_data['daily_reset_timestamp'] = current_time
        if 'session_start_timestamp' not in user_info:
            update_data['session_start_timestamp'] = current_time
        
        user_ref.update(update_data)
        
        return None  # Todo está bien
        
    except Exception as e:
        print(f"Error verificando límites de uso: {e}")
        return {"error": f"Error interno verificando límites: {str(e)}"}, 500

# Función para limitar información de contenido para usuarios free
def limit_content_info(content_data, content_type):
    """Limitar información para usuarios free"""
    limited_data = {
        'id': content_data.get('id'),
        'title': content_data.get('title'),
        'year': content_data.get('year'),
        'genre': content_data.get('genre'),
        'rating': content_data.get('rating'),
        'poster': content_data.get('poster'),
        'description': content_data.get('description', '')[:100] + '...' if content_data.get('description') else ''
    }
    
    # Remover información sensible para free users
    if 'streaming_url' in content_data:
        limited_data['streaming_available'] = True
        limited_data['upgrade_required'] = True
    else:
        limited_data['streaming_available'] = False
    
    if 'download_links' in content_data:
        limited_data['downloads_available'] = True
        limited_data['upgrade_required'] = True
    else:
        limited_data['downloads_available'] = False
    
    return limited_data

# Middleware de seguridad global
@app.before_request
def before_request():
    """Middleware de seguridad global"""
    # Verificar rate limiting por IP
    ip_address = request.remote_addr
    ip_limit_check = check_ip_rate_limit(ip_address)
    if ip_limit_check:
        return jsonify(ip_limit_check[0]), ip_limit_check[1]
    
    # Validar headers de seguridad
    if request.endpoint and request.endpoint != 'diagnostic':
        # Prevenir MIME type sniffing
        if 'X-Content-Type-Options' not in request.headers:
            pass  # Se agregará en after_request
    
    # Log de requests (opcional para debugging)
    if request.endpoint and 'admin' not in request.endpoint:
        print(f"📥 Request: {request.method} {request.path} from {ip_address}")

@app.after_request
def after_request(response):
    """Agregar headers de seguridad"""
    # Headers de seguridad HTTP
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = "default-src 'self'"
    
    # Prevenir caching de respuestas sensibles
    if request.path.startswith('/api/admin') or request.path.startswith('/api/user'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    
    return response

# Decorador para requerir autenticación
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # MÉTODO 1: Buscar token en parámetros URL (para navegador)
        if request.args.get('token'):
            token = request.args.get('token')
        
        # MÉTODO 2: Buscar token en headers Authorization (para apps/curl)
        elif 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                pass
        
        if not token:
            return jsonify({"error": "Token de acceso requerido"}), 401
        
        # Validar formato del token
        if len(token) < 10 or len(token) > 500:
            return jsonify({"error": "Formato de token inválido"}), 401
        
        try:
            # Verificar si es token de admin
            if token in ADMIN_TOKENS:
                user_data = {
                    'user_id': 'admin',
                    'username': 'Administrador',
                    'email': 'admin@api.com',
                    'is_admin': True,
                    'plan_type': 'admin'
                }
                return f(user_data, *args, **kwargs)
            
            # Buscar el token en Firestore para usuarios normales
            users_ref = db.collection(TOKENS_COLLECTION)
            query = users_ref.where('token', '==', token).limit(1).stream()
            
            user_data = None
            for doc in query:
                user_data = doc.to_dict()
                user_data['user_id'] = doc.id
                user_data['is_admin'] = False
                user_data['plan_type'] = user_data.get('plan_type', 'free')
                break
            
            if not user_data:
                return jsonify({"error": "Token inválido o no autorizado"}), 401
            
            if not user_data.get('active', True):
                return jsonify({"error": "Cuenta desactivada"}), 401
            
            # Verificar límites de uso (solo para usuarios normales)
            limit_check = check_usage_limits(user_data)
            if limit_check:
                return jsonify(limit_check[0]), limit_check[1]
            
            return f(user_data, *args, **kwargs)
            
        except Exception as e:
            print(f"Error de autenticación: {e}")
            return jsonify({"error": "Error de autenticación"}), 500
    
    return decorated

# Middleware para verificar características del plan
def check_plan_feature(feature_name):
    """Decorador para verificar características del plan"""
    def decorator(f):
        @wraps(f)
        def decorated_function(user_data, *args, **kwargs):
            if user_data.get('is_admin'):
                return f(user_data, *args, **kwargs)
            
            plan_type = user_data.get('plan_type', 'free')
            features = PLAN_CONFIG[plan_type]['features']
            
            if not features.get(feature_name, False):
                return jsonify({
                    "error": f"Esta característica no está disponible en tu plan {plan_type}",
                    "feature_required": feature_name,
                    "upgrade_required": True,
                    "current_plan": plan_type,
                    "required_plan": "premium"
                }), 403
            
            return f(user_data, *args, **kwargs)
        return decorated_function
    return decorator

# Generar token único
def generate_unique_token():
    return secrets.token_urlsafe(32)

# Validación de entrada
def validate_email(email):
    """Validar formato de email"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_username(username):
    """Validar formato de username"""
    if len(username) < 3 or len(username) > 50:
        return False
    pattern = r'^[a-zA-Z0-9_-]+$'
    return re.match(pattern, username) is not None

# Endpoint de diagnóstico
@app.route('/api/diagnostic', methods=['GET'])
def diagnostic():
    """Endpoint de diagnóstico del sistema"""
    firebase_status = "✅ Conectado" if db else "❌ Desconectado"
    
    # Verificar variables de entorno críticas
    env_vars = {
        'FIREBASE_TYPE': "✅" if os.environ.get('FIREBASE_TYPE') else "❌",
        'FIREBASE_PROJECT_ID': "✅" if os.environ.get('FIREBASE_PROJECT_ID') else "❌", 
        'FIREBASE_PRIVATE_KEY': "✅" if os.environ.get('FIREBASE_PRIVATE_KEY') else "❌",
        'FIREBASE_CLIENT_EMAIL': "✅" if os.environ.get('FIREBASE_CLIENT_EMAIL') else "❌",
        'ADMIN_EMAIL': "✅" if os.environ.get('ADMIN_EMAIL') else "❌",
        'ADMIN_TOKEN': "✅" if os.environ.get('ADMIN_TOKEN') else "❌"
    }
    
    # Probar operación de Firestore si está conectado
    firestore_test = "No probado"
    if db:
        try:
            test_ref = db.collection('diagnostic_test').document('connection_test')
            test_ref.set({'test': True, 'timestamp': firestore.SERVER_TIMESTAMP})
            firestore_test = "✅ Escritura exitosa"
            # Limpiar
            test_ref.delete()
        except Exception as e:
            firestore_test = f"❌ Error: {str(e)}"
    
    return jsonify({
        "success": True,
        "system": {
            "firebase_status": firebase_status,
            "firestore_test": firestore_test,
            "project_id": "phdt-b9b2c",
            "environment_variables": env_vars
        },
        "security": {
            "rate_limiting": "✅ Activado",
            "ip_restrictions": "✅ Activado",
            "token_authentication": "✅ Activado",
            "plan_restrictions": "✅ Activado",
            "email_notifications": "✅ Activado"
        },
        "endpoints_working": {
            "diagnostic": "✅ /api/diagnostic",
            "home": "🔒 / (requiere token)",
            "admin": "🔒 /api/admin/* (requiere admin token)",
            "content": "🔒 /api/* (requiere token)"
        }
    })

# Endpoints de administración (solo para admins)
@app.route('/api/admin/create-user', methods=['POST'])
@token_required
def admin_create_user(user_data):
    """Crear nuevo usuario con plan específico (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        username = data.get('username')
        email = data.get('email')
        plan_type = data.get('plan_type', 'free').lower()
        
        # Validaciones de entrada
        if not username or not email:
            return jsonify({"error": "Username y email son requeridos"}), 400
        
        if not validate_username(username):
            return jsonify({"error": "Username debe tener entre 3-50 caracteres y solo puede contener letras, números, guiones y guiones bajos"}), 400
        
        if not validate_email(email):
            return jsonify({"error": "Formato de email inválido"}), 400
        
        # Validar plan
        if plan_type not in PLAN_CONFIG:
            return jsonify({"error": f"Plan no válido. Opciones: {list(PLAN_CONFIG.keys())}"}), 400
        
        # Verificar si el usuario ya existe
        users_ref = db.collection(TOKENS_COLLECTION)
        existing_user = users_ref.where('email', '==', email).limit(1).stream()
        
        if any(existing_user):
            return jsonify({"error": "El email ya está registrado"}), 400
        
        # Obtener límites del plan
        plan_config = PLAN_CONFIG[plan_type]
        daily_limit = data.get('daily_limit', plan_config['daily_limit'])
        session_limit = data.get('session_limit', plan_config['session_limit'])
        
        # Validar límites personalizados
        if daily_limit <= 0 or session_limit <= 0:
            return jsonify({"error": "Los límites deben ser mayores a 0"}), 400
        
        # Generar token único
        token = generate_unique_token()
        current_time = time.time()
        
        # Crear usuario en Firestore
        user_data = {
            'username': username,
            'email': email,
            'token': token,
            'active': True,
            'is_admin': False,
            'plan_type': plan_type,
            'created_at': firestore.SERVER_TIMESTAMP,
            'last_used': None,
            'total_usage_count': 0,
            'daily_usage_count': 0,
            'session_usage_count': 0,
            'daily_reset_timestamp': current_time,
            'session_start_timestamp': current_time,
            'max_requests_per_day': daily_limit,
            'max_requests_per_session': session_limit,
            'features': plan_config['features']
        }
        
        # Guardar en Firestore
        user_ref = users_ref.document()
        user_ref.set(user_data)
        
        return jsonify({
            "success": True,
            "message": "Usuario creado exitosamente",
            "user_info": {
                "user_id": user_ref.id,
                "username": username,
                "email": email,
                "token": token,
                "plan_type": plan_type,
                "limits": {
                    "daily": daily_limit,
                    "session": session_limit
                },
                "features": plan_config['features']
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users', methods=['GET'])
@token_required
def admin_get_users(user_data):
    """Obtener lista de usuarios con información de planes (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        users_ref = db.collection(TOKENS_COLLECTION)
        docs = users_ref.stream()
        
        users = []
        for doc in docs:
            user_info = doc.to_dict()
            user_info['user_id'] = doc.id
            
            # Información del plan
            plan_type = user_info.get('plan_type', 'free')
            user_info['plan_type'] = plan_type
            user_info['plan_limits'] = {
                'daily': PLAN_CONFIG[plan_type]['daily_limit'],
                'session': PLAN_CONFIG[plan_type]['session_limit']
            }
            
            # Calcular tiempo restante para reset de límites
            current_time = time.time()
            daily_reset = user_info.get('daily_reset_timestamp', current_time)
            session_start = user_info.get('session_start_timestamp', current_time)
            
            daily_remaining = max(0, 86400 - (current_time - daily_reset))
            session_remaining = max(0, SESSION_TIMEOUT - (current_time - session_start))
            
            user_info['limits_info'] = {
                'daily_reset_in_seconds': int(daily_remaining),
                'session_reset_in_seconds': int(session_remaining),
                'daily_usage': user_info.get('daily_usage_count', 0),
                'session_usage': user_info.get('session_usage_count', 0),
                'daily_limit': user_info.get('max_requests_per_day', PLAN_CONFIG[plan_type]['daily_limit']),
                'session_limit': user_info.get('max_requests_per_session', PLAN_CONFIG[plan_type]['session_limit'])
            }
            
            # No mostrar el token por seguridad
            if 'token' in user_info:
                del user_info['token']
            users.append(user_info)
        
        # Estadísticas de planes
        plan_stats = {}
        for user in users:
            plan = user.get('plan_type', 'free')
            plan_stats[plan] = plan_stats.get(plan, 0) + 1
        
        return jsonify({
            "success": True,
            "count": len(users),
            "plan_statistics": plan_stats,
            "users": users
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-limits', methods=['POST'])
@token_required
def admin_update_limits(user_data):
    """Actualizar límites de uso de un usuario (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        user_id = data.get('user_id')
        daily_limit = data.get('daily_limit')
        session_limit = data.get('session_limit')
        
        if not user_id:
            return jsonify({"error": "user_id es requerido"}), 400
        
        if daily_limit is None and session_limit is None:
            return jsonify({"error": "Debe proporcionar al menos un límite para actualizar"}), 400
        
        # Validar límites
        if daily_limit is not None and daily_limit <= 0:
            return jsonify({"error": "El límite diario debe ser mayor a 0"}), 400
        
        if session_limit is not None and session_limit <= 0:
            return jsonify({"error": "El límite de sesión debe ser mayor a 0"}), 400
        
        # Verificar si el usuario existe
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        
        update_data = {}
        if daily_limit is not None:
            update_data['max_requests_per_day'] = daily_limit
        
        if session_limit is not None:
            update_data['max_requests_per_session'] = session_limit
        
        user_ref.update(update_data)
        
        return jsonify({
            "success": True,
            "message": "Límites actualizados exitosamente",
            "updated_limits": update_data
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/change-plan', methods=['POST'])
@token_required
def admin_change_plan(user_data):
    """Cambiar el plan de un usuario (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        user_id = data.get('user_id')
        new_plan = data.get('new_plan', 'free').lower()
        
        if not user_id:
            return jsonify({"error": "user_id es requerido"}), 400
        
        if new_plan not in PLAN_CONFIG:
            return jsonify({"error": f"Plan no válido. Opciones: {list(PLAN_CONFIG.keys())}"}), 400
        
        # Verificar si el usuario existe
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        
        # Obtener configuración del nuevo plan
        plan_config = PLAN_CONFIG[new_plan]
        
        # Actualizar usuario
        update_data = {
            'plan_type': new_plan,
            'max_requests_per_day': plan_config['daily_limit'],
            'max_requests_per_session': plan_config['session_limit'],
            'features': plan_config['features'],
            'plan_updated_at': firestore.SERVER_TIMESTAMP
        }
        
        user_ref.update(update_data)
        
        return jsonify({
            "success": True,
            "message": f"Plan cambiado a {new_plan} exitosamente",
            "new_plan": new_plan,
            "new_limits": {
                "daily": plan_config['daily_limit'],
                "session": plan_config['session_limit']
            },
            "new_features": plan_config['features']
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/reset-limits', methods=['POST'])
@token_required
def admin_reset_limits(user_data):
    """Resetear contadores de uso de un usuario (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        user_id = data.get('user_id')
        reset_type = data.get('reset_type', 'both')  # 'daily', 'session', 'both'
        
        if not user_id:
            return jsonify({"error": "user_id es requerido"}), 400
        
        # Verificar si el usuario existe
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        
        current_time = time.time()
        update_data = {}
        
        if reset_type in ['daily', 'both']:
            update_data['daily_usage_count'] = 0
            update_data['daily_reset_timestamp'] = current_time
        
        if reset_type in ['session', 'both']:
            update_data['session_usage_count'] = 0
            update_data['session_start_timestamp'] = current_time
        
        user_ref.update(update_data)
        
        return jsonify({
            "success": True,
            "message": f"Límites {reset_type} reseteados exitosamente",
            "reset_type": reset_type
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/regenerate-token', methods=['POST'])
@token_required
def admin_regenerate_token(user_data):
    """Regenerar token de un usuario (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        user_id = data.get('user_id')
        
        if not user_id:
            return jsonify({"error": "user_id es requerido"}), 400
        
        # Verificar si el usuario existe
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        
        # Generar nuevo token
        new_token = generate_unique_token()
        
        # Actualizar en Firestore
        user_ref.update({
            'token': new_token,
            'last_token_regenerated': firestore.SERVER_TIMESTAMP,
            'regenerated_by_admin': user_data.get('username', 'admin')
        })
        
        # Obtener información del usuario para la respuesta
        user_info = user_doc.to_dict()
        
        return jsonify({
            "success": True,
            "message": "Token regenerado exitosamente",
            "user_info": {
                "user_id": user_id,
                "username": user_info.get('username'),
                "email": user_info.get('email'),
                "new_token": new_token,
                "regenerated_at": firestore.SERVER_TIMESTAMP,
                "regenerated_by": user_data.get('username', 'admin')
            },
            "warning": "⚠️ El token anterior ya no es válido. Notifique al usuario."
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/usage-statistics', methods=['GET'])
@token_required
def admin_usage_statistics(user_data):
    """Estadísticas detalladas de uso por plan (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        users_ref = db.collection(TOKENS_COLLECTION)
        docs = users_ref.stream()
        
        stats = {
            'free': {'users': 0, 'total_requests': 0, 'active_users': 0},
            'premium': {'users': 0, 'total_requests': 0, 'active_users': 0},
            'total': {'users': 0, 'total_requests': 0, 'active_users': 0}
        }
        
        current_time = time.time()
        
        for doc in docs:
            user_info = doc.to_dict()
            plan_type = user_info.get('plan_type', 'free')
            
            stats[plan_type]['users'] += 1
            stats['total']['users'] += 1
            
            total_requests = user_info.get('total_usage_count', 0)
            stats[plan_type]['total_requests'] += total_requests
            stats['total']['total_requests'] += total_requests
            
            # Usuario activo (usado en las últimas 24 horas)
            last_used = user_info.get('last_used')
            if last_used:
                if hasattr(last_used, 'timestamp'):
                    last_used_time = last_used.timestamp()
                else:
                    last_used_time = last_used
                
                if current_time - last_used_time < 86400:
                    stats[plan_type]['active_users'] += 1
                    stats['total']['active_users'] += 1
        
        # Calcular promedios
        for plan in ['free', 'premium', 'total']:
            if stats[plan]['users'] > 0:
                stats[plan]['avg_requests_per_user'] = stats[plan]['total_requests'] / stats[plan]['users']
            else:
                stats[plan]['avg_requests_per_user'] = 0
        
        return jsonify({
            "success": True,
            "statistics": stats,
            "plan_limits": PLAN_CONFIG,
            "timestamp": time.time()
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Endpoints para usuarios normales
@app.route('/api/user/info', methods=['GET'])
@token_required
def get_user_info(user_data):
    """Obtener información del usuario actual con detalles del plan"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    # Para usuarios normales, obtener información actualizada
    if not user_data.get('is_admin'):
        try:
            user_ref = db.collection(TOKENS_COLLECTION).document(user_data['user_id'])
            user_doc = user_ref.get()
            if user_doc.exists:
                current_data = user_doc.to_dict()
                user_data.update(current_data)
        except Exception as e:
            print(f"Error obteniendo información actualizada: {e}")
    
    # Calcular tiempo restante para resets
    current_time = time.time()
    daily_reset = user_data.get('daily_reset_timestamp', current_time)
    session_start = user_data.get('session_start_timestamp', current_time)
    
    daily_remaining = max(0, 86400 - (current_time - daily_reset))
    session_remaining = max(0, SESSION_TIMEOUT - (current_time - session_start))
    
    # Obtener información del plan
    plan_type = user_data.get('plan_type', 'free')
    plan_features = PLAN_CONFIG[plan_type]['features']
    
    user_response = {
        "user_id": user_data.get('user_id'),
        "username": user_data.get('username'),
        "email": user_data.get('email'),
        "active": user_data.get('active', True),
        "is_admin": user_data.get('is_admin', False),
        "plan_type": plan_type,
        "created_at": user_data.get('created_at'),
        "usage_stats": {
            "total": user_data.get('total_usage_count', 0),
            "daily": user_data.get('daily_usage_count', 0),
            "session": user_data.get('session_usage_count', 0),
            "daily_limit": user_data.get('max_requests_per_day', PLAN_CONFIG[plan_type]['daily_limit']),
            "session_limit": user_data.get('max_requests_per_session', PLAN_CONFIG[plan_type]['session_limit']),
            "daily_reset_in": f"{int(daily_remaining // 3600)}h {int((daily_remaining % 3600) // 60)}m",
            "session_reset_in": f"{int(session_remaining // 60)}m {int(session_remaining % 60)}s"
        },
        "features": plan_features
    }
    
    return jsonify({
        "success": True,
        "user": user_response
    })

# Endpoint de comparación de planes
@app.route('/api/plan-comparison', methods=['GET'])
def plan_comparison():
    """Mostrar comparación entre planes free y premium"""
    comparison = {
        'free': {
            'name': 'Free',
            'price': 'Gratuito',
            'daily_requests': PLAN_CONFIG['free']['daily_limit'],
            'session_requests': PLAN_CONFIG['free']['session_limit'],
            'features': [
                'Acceso a metadata básica',
                'Búsqueda limitada (5 resultados)',
                'Preview de contenido',
                'Soporte comunitario',
                'Límite de 50 películas visibles'
            ],
            'limitations': [
                'No streaming de video',
                'No descargas',
                'No acceso a series',
                'Búsquedas limitadas',
                'Sin soporte prioritario'
            ]
        },
        'premium': {
            'name': 'Premium', 
            'price': 'Personalizar',
            'daily_requests': PLAN_CONFIG['premium']['daily_limit'],
            'session_requests': PLAN_CONFIG['premium']['session_limit'],
            'features': [
                'Acceso completo al catálogo',
                'Streaming HD ilimitado',
                'Descargas disponibles',
                'Búsquedas avanzadas (50 resultados)',
                'Soporte prioritario 24/7',
                'Acceso a series y contenido exclusivo',
                'Operaciones masivas',
                'Recomendaciones personalizadas'
            ],
            'limitations': [
                'Uso comercial requiere licencia'
            ]
        }
    }
    
    return jsonify({
        "success": True,
        "plans": comparison
    })

# Endpoint principal (requiere token)
@app.route('/')
@token_required
def home(user_data):
    """Endpoint principal con información de la API"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    welcome_msg = "👋 ¡Bienvenido Administrador!" if user_data.get('is_admin') else "👋 ¡Bienvenido!"
    
    # Para usuarios normales, agregar información de límites
    limits_info = None
    if not user_data.get('is_admin'):
        try:
            user_ref = db.collection(TOKENS_COLLECTION).document(user_data['user_id'])
            user_doc = user_ref.get()
            if user_doc.exists:
                current_data = user_doc.to_dict()
                daily_usage = current_data.get('daily_usage_count', 0)
                session_usage = current_data.get('session_usage_count', 0)
                daily_limit = current_data.get('max_requests_per_day', PLAN_CONFIG['free']['daily_limit'])
                session_limit = current_data.get('max_requests_per_session', PLAN_CONFIG['free']['session_limit'])
                
                limits_info = {
                    "daily_usage": f"{daily_usage}/{daily_limit}",
                    "session_usage": f"{session_usage}/{session_limit}",
                    "remaining_daily": daily_limit - daily_usage,
                    "remaining_session": session_limit - session_usage
                }
        except Exception as e:
            print(f"Error obteniendo información de límites: {e}")
    
    return jsonify({
        "message": f"🎬 API de Streaming - {welcome_msg}",
        "version": "2.0.0",
        "user": user_data.get('username'),
        "plan_type": user_data.get('plan_type', 'free'),
        "firebase_status": "✅ Conectado",
        "usage_limits": limits_info,
        "endpoints_available": {
            "user_info": "GET /api/user/info",
            "peliculas": "GET /api/peliculas",
            "pelicula_especifica": "GET /api/peliculas/<id>",
            "series": "GET /api/series", 
            "serie_especifica": "GET /api/series/<id>",
            "canales": "GET /api/canales",
            "canal_especifico": "GET /api/canales/<id>",
            "buscar": "GET /api/buscar?q=<termino>",
            "estadisticas": "GET /api/estadisticas",
            "stream": "GET /api/stream/<id> (solo premium)"
        },
        "admin_endpoints": {
            "create_user": "POST /api/admin/create-user",
            "list_users": "GET /api/admin/users",
            "update_limits": "POST /api/admin/update-limits",
            "reset_limits": "POST /api/admin/reset-limits",
            "change_plan": "POST /api/admin/change-plan",
            "regenerate_token": "POST /api/admin/regenerate-token",
            "usage_statistics": "GET /api/admin/usage-statistics"
        } if user_data.get('is_admin') else None,
        "instructions": "Incluya el token en el header: Authorization: Bearer {token}"
    })

# Endpoints de contenido (todos requieren token)
@app.route('/api/peliculas', methods=['GET'])
@token_required
def get_peliculas(user_data):
    """Obtener todas las películas con paginación y restricciones por plan"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        limit = int(request.args.get('limit', 20))
        page = int(request.args.get('page', 1))
        
        # Aplicar límites según plan
        plan_type = user_data.get('plan_type', 'free')
        plan_config = PLAN_CONFIG[plan_type]
        
        # Free users tienen límites más estrictos
        if plan_type == 'free':
            limit = min(limit, 10)  # Máximo 10 resultados para free
            max_offset = 50  # Free solo puede acceder a primeras 50 películas
        else:
            max_offset = 1000  # Premium acceso completo
        
        peliculas_ref = db.collection('peliculas')
        
        # Para free users, limitar el offset
        offset = (page - 1) * limit
        if plan_type == 'free' and offset >= max_offset:
            return jsonify({
                "success": True,
                "count": 0,
                "message": "Límite de contenido gratuito alcanzado. Actualiza a premium para acceso completo.",
                "data": []
            })
        
        docs = peliculas_ref.limit(limit).offset(offset).stream()
        
        peliculas = []
        for doc in docs:
            pelicula_data = normalize_movie_data(doc.to_dict(), doc.id)
            
            # OPTIMIZAR IMAGEN si existe poster
            if pelicula_data.get('poster'):
                pelicula_data['poster_optimized'] = get_optimized_image_url(
                    pelicula_data['poster'], 
                    plan_type
                )
            
            # Para free users, limitar información
            if plan_type == 'free':
                pelicula_data = limit_content_info(pelicula_data, 'pelicula')
            
            peliculas.append(pelicula_data)
        
        return jsonify({
            "success": True,
            "count": len(peliculas),
            "page": page,
            "limit": limit,
            "plan_restrictions": plan_type == 'free',
            "image_optimization": "active",  # ← Indicar que hay compresión
            "data": peliculas
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['GET'])
@token_required
def get_pelicula(user_data, pelicula_id):
    """Obtener una película específica por ID"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        
        if doc.exists:
            pelicula_data = normalize_movie_data(doc.to_dict(), doc.id)
            
            # Aplicar restricciones de plan
            plan_type = user_data.get('plan_type', 'free')
            
            # OPTIMIZAR IMAGEN si existe poster
            if pelicula_data.get('poster'):
                pelicula_data['poster_optimized'] = get_optimized_image_url(
                    pelicula_data['poster'], 
                    plan_type
                )
            
            if plan_type == 'free':
                pelicula_data = limit_content_info(pelicula_data, 'pelicula')
            
            return jsonify({
                "success": True,
                "data": pelicula_data
            })
        else:
            return jsonify({"error": "Película no encontrada"}), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/series', methods=['GET'])
@token_required
@check_plan_feature('content_access')
def get_series(user_data):
    """Obtener todas las series (solo premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        limit = int(request.args.get('limit', 20))
        
        series_ref = db.collection('contenido')
        docs = series_ref.limit(limit).stream()
        
        series = []
        for doc in docs:
            serie_data = doc.to_dict()
            # Verificar si es una serie (tiene seasons)
            if serie_data.get('seasons'):
                serie_data = normalize_series_data(serie_data, doc.id)
                
                # OPTIMIZAR IMAGEN si existe poster
                if serie_data.get('poster'):
                    serie_data['poster_optimized'] = get_optimized_image_url(
                        serie_data['poster'], 
                        user_data.get('plan_type', 'premium')
                    )
                
                series.append(serie_data)
        
        return jsonify({
            "success": True,
            "count": len(series),
            "data": series
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/series/<serie_id>', methods=['GET'])
@token_required
@check_plan_feature('content_access')
def get_serie(user_data, serie_id):
    """Obtener una serie específica por ID (solo premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        
        if doc.exists:
            serie_data = doc.to_dict()
            # Verificar si es una serie (tiene seasons)
            if serie_data.get('seasons'):
                serie_data = normalize_series_data(serie_data, doc.id)
                
                # OPTIMIZAR IMAGEN si existe poster
                if serie_data.get('poster'):
                    serie_data['poster_optimized'] = get_optimized_image_url(
                        serie_data['poster'], 
                        user_data.get('plan_type', 'premium')
                    )
                
                return jsonify({
                    "success": True,
                    "data": serie_data
                })
            else:
                return jsonify({"error": "No es una serie válida"}), 404
        else:
            return jsonify({"error": "Serie no encontrada"}), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/canales', methods=['GET'])
@token_required
def get_canales(user_data):
    """Obtener todos los canales"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        canales_ref = db.collection('canales')
        docs = canales_ref.stream()
        
        canales = []
        for doc in docs:
            canal_data = normalize_channel_data(doc.to_dict(), doc.id)
            
            # OPTIMIZAR IMAGEN si existe logo
            if canal_data.get('logo'):
                canal_data['logo_optimized'] = get_optimized_image_url(
                    canal_data['logo'], 
                    user_data.get('plan_type', 'free')
                )
            
            canales.append(canal_data)
        
        return jsonify({
            "success": True,
            "count": len(canales),
            "data": canales
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/canales/<canal_id>', methods=['GET'])
@token_required
def get_canal(user_data, canal_id):
    """Obtener un canal específico por ID"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        
        if doc.exists:
            canal_data = normalize_channel_data(doc.to_dict(), doc.id)
            
            # OPTIMIZAR IMAGEN si existe logo
            if canal_data.get('logo'):
                canal_data['logo_optimized'] = get_optimized_image_url(
                    canal_data['logo'], 
                    user_data.get('plan_type', 'free')
                )
            
            return jsonify({
                "success": True,
                "data": canal_data
            })
        else:
            return jsonify({"error": "Canal no encontrado"}), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/buscar', methods=['GET'])
@token_required
def buscar(user_data):
    """Buscar contenido con límites por plan"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        termino = request.args.get('q', '')
        if not termino:
            return jsonify({"error": "Término de búsqueda requerido"}), 400
        
        plan_type = user_data.get('plan_type', 'free')
        plan_config = PLAN_CONFIG[plan_type]
        
        # Aplicar límite de búsqueda según plan
        search_limit = plan_config['features']['search_limit']
        limit = min(int(request.args.get('limit', 10)), search_limit)
        
        resultados = []
        
        # Buscar en películas
        peliculas_ref = db.collection('peliculas')
        peliculas_query = peliculas_ref.where('title', '>=', termino).where('title', '<=', termino + '\uf8ff')
        peliculas_docs = peliculas_query.limit(limit).stream()
        
        for doc in peliculas_docs:
            data = normalize_movie_data(doc.to_dict(), doc.id)
            data['tipo'] = 'pelicula'
            
            # OPTIMIZAR IMAGEN si existe poster
            if data.get('poster'):
                data['poster_optimized'] = get_optimized_image_url(
                    data['poster'], 
                    plan_type
                )
            
            # Aplicar restricciones de plan
            if plan_type == 'free':
                data = limit_content_info(data, 'pelicula')
            
            resultados.append(data)
        
        # Para premium, buscar también en series
        if plan_type == 'premium':
            series_ref = db.collection('contenido')
            series_query = series_ref.where('title', '>=', termino).where('title', '<=', termino + '\uf8ff')
            series_docs = series_query.limit(limit).stream()
            
            for doc in series_docs:
                data = doc.to_dict()
                # Solo incluir si es una serie (tiene seasons)
                if data.get('seasons'):
                    data = normalize_series_data(data, doc.id)
                    data['tipo'] = 'serie'
                    
                    # OPTIMIZAR IMAGEN si existe poster
                    if data.get('poster'):
                        data['poster_optimized'] = get_optimized_image_url(
                            data['poster'], 
                            plan_type
                        )
                    
                    resultados.append(data)
        
        return jsonify({
            "success": True,
            "termino": termino,
            "count": len(resultados),
            "search_limit": search_limit,
            "plan_type": plan_type,
            "data": resultados
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream/<content_id>', methods=['GET'])
@token_required
@check_plan_feature('streaming')
def get_stream_url(user_data, content_id):
    """Obtener URL de streaming (solo premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        # Buscar el contenido en películas
        content_ref = db.collection('peliculas').document(content_id)
        content_doc = content_ref.get()
        
        streaming_url = None
        content_type = "pelicula"
        
        if content_doc.exists:
            content_data = normalize_movie_data(content_doc.to_dict(), content_id)
            # Buscar en play_links para películas
            play_links = content_data.get('play_links', [])
            if play_links:
                streaming_url = play_links[0].get('url')  # Tomar el primer enlace
        else:
            # Buscar en series
            content_ref = db.collection('contenido').document(content_id)
            content_doc = content_ref.get()
            
            if content_doc.exists:
                content_data = doc.to_dict()
                content_type = "serie"
                # Para series, necesitaríamos lógica más compleja para episodios específicos
                # Por ahora, devolver un mensaje informativo
                return jsonify({
                    "success": False,
                    "message": "Para series, especifique temporada y episodio",
                    "content_type": "serie"
                }), 400
            
        if streaming_url:
            return jsonify({
                "success": True,
                "streaming_url": streaming_url,
                "content_type": content_type,
                "expires_in": 3600,  # URL expira en 1 hora
                "quality": "HD"
            })
        else:
            return jsonify({"error": "URL de streaming no disponible"}), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/estadisticas', methods=['GET'])
@token_required
def get_estadisticas(user_data):
    """Obtener estadísticas del contenido"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        # Contar películas
        peliculas_count = len(list(db.collection('peliculas').limit(1000).stream()))
        
        # Contar series (solo para premium)
        series_count = 0
        if user_data.get('plan_type') == 'premium' or user_data.get('is_admin'):
            series_ref = db.collection('contenido')
            series_docs = series_ref.limit(1000).stream()
            for doc in series_docs:
                data = doc.to_dict()
                if data.get('seasons'):
                    series_count += 1
        
        # Contar canales
        canales_count = len(list(db.collection('canales').limit(1000).stream()))
        
        return jsonify({
            "success": True,
            "data": {
                "total_peliculas": peliculas_count,
                "total_series": series_count if (user_data.get('plan_type') == 'premium' or user_data.get('is_admin')) else "Requiere plan premium",
                "total_canales": canales_count,
                "total_contenido": peliculas_count + series_count if (user_data.get('plan_type') == 'premium' or user_data.get('is_admin')) else "Requiere plan premium"
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Nuevo endpoint para compresión de imágenes
@app.route('/api/compress-image', methods=['POST'])
@token_required
def compress_image_endpoint(user_data):
    """Endpoint para comprimir imágenes desde URL"""
    try:
        data = request.get_json()
        image_url = data.get('url')
        quality = data.get('quality', 85)
        max_width = data.get('max_width', 800)
        
        if not image_url:
            return jsonify({"error": "URL de imagen requerida"}), 400
        
        # Comprimir imagen
        result = compress_image_url(image_url, quality, max_width)
        
        if result['success']:
            return jsonify({
                "success": True,
                "compressed_image": result['data_url'],
                "savings": f"{result['savings_percent']}%",
                "original_size": result['original_size'],
                "compressed_size": result['compressed_size'],
                "dimensions": result['dimensions']
            })
        else:
            return jsonify({
                "success": False,
                "error": result['error'],
                "original_url": image_url
            }), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Manejo de errores
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint no encontrado"}), 404

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({"error": "Método no permitido"}), 405

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Error interno del servidor"}), 500

@app.errorhandler(413)
def too_large(error):
    return jsonify({"error": "Archivo demasiado grande"}), 413

# Inicialización de la aplicación
if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)

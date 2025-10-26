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

# Variables globales para manejo de conexión Firebase
firebase_app = None
db = None
last_connection_test = 0
CONNECTION_TEST_INTERVAL = 300  # 5 minutos

# Inicializar Firebase
def initialize_firebase():
    """Inicialización robusta de Firebase con manejo de errores"""
    global firebase_app, db
    
    try:
        # Limpiar apps existentes si hay
        try:
            if firebase_app:
                firebase_admin.delete_app(firebase_app)
        except:
            pass
            
        print("🔄 Inicializando Firebase...")
        
        # Verificar variables críticas
        required_vars = ['FIREBASE_TYPE', 'FIREBASE_PROJECT_ID', 'FIREBASE_PRIVATE_KEY', 'FIREBASE_CLIENT_EMAIL']
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        
        if missing_vars:
            print(f"❌ Variables faltantes: {missing_vars}")
            return None
        
        # Configurar service account con formato correcto
        private_key = os.environ.get('FIREBASE_PRIVATE_KEY', '').replace('\\n', '\n')
        
        service_account_info = {
            "type": os.environ.get('FIREBASE_TYPE'),
            "project_id": os.environ.get('FIREBASE_PROJECT_ID'),
            "private_key_id": os.environ.get('FIREBASE_PRIVATE_KEY_ID'),
            "private_key": private_key,
            "client_email": os.environ.get('FIREBASE_CLIENT_EMAIL'),
            "client_id": os.environ.get('FIREBASE_CLIENT_ID'),
            "auth_uri": os.environ.get('FIREBASE_AUTH_URI'),
            "token_uri": os.environ.get('FIREBASE_TOKEN_URI'),
            "auth_provider_x509_cert_url": os.environ.get('FIREBASE_AUTH_PROVIDER_CERT_URL'),
            "client_x509_cert_url": os.environ.get('FIREBASE_CLIENT_CERT_URL')
        }
        
        # Inicializar Firebase
        cred = credentials.Certificate(service_account_info)
        firebase_app = firebase_admin.initialize_app(cred)
        db = firestore.client()
        
        # Test de conexión rápido
        test_ref = db.collection('api_users').limit(1)
        docs = list(test_ref.stream())
        print(f"✅ Firebase inicializado correctamente. Docs de prueba: {len(docs)}")
        
        return db
        
    except Exception as e:
        print(f"❌ Error crítico inicializando Firebase: {e}")
        import traceback
        traceback.print_exc()
        firebase_app = None
        db = None
        return None

def check_firebase_connection():
    """Verificar y mantener la conexión a Firebase"""
    global db, last_connection_test
    
    current_time = time.time()
    
    # Solo verificar cada 5 minutos para no sobrecargar
    if current_time - last_connection_test < CONNECTION_TEST_INTERVAL:
        return db is not None
    
    last_connection_test = current_time
    
    if not db:
        print("🔌 No hay conexión a Firebase, intentando reconectar...")
        return initialize_firebase() is not None
    
    try:
        # Test simple de conexión
        test_ref = db.collection('api_users').limit(1)
        list(test_ref.stream())
        print("✅ Conexión Firebase verificada")
        return True
    except Exception as e:
        print(f"❌ Conexión Firebase perdida: {e}")
        db = None
        firebase_app = None
        print("🔄 Intentando reconexión...")
        return initialize_firebase() is not None

# Inicializar Firebase al inicio
db = initialize_firebase()

# Colección para almacenar usuarios y tokens
TOKENS_COLLECTION = "api_users"

# Configuración de planes - ACTUALIZADA CON LÍMITES DE STREAMS
PLAN_CONFIG = {
    'free': {
        'daily_limit': 200,
        'session_limit': 10,
        'rate_limit_per_minute': 15,
        'concurrent_requests': 1,
        'daily_streams_limit': 10,
        'features': {
            'content_access': 'limited',
            'api_responses': 'basic',
            'search_limit': 5,
            'content_previews': True,
            'streaming': True,
            'download_links': False,
            'api_support': 'community',
            'request_priority': 'low',
            'bulk_operations': False,
            'advanced_filters': False,
            'content_recommendations': False,
            'content_creation': False,
            'content_editing': False
        }
    },
    'premium': {
        'daily_limit': 30000,
        'session_limit': 2000,
        'rate_limit_per_minute': 120,
        'concurrent_requests': 3,
        'daily_streams_limit': 0,
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
            'content_recommendations': True,
            'content_creation': True,
            'content_editing': True
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
            print(f"📧 [SIMULACIÓN] Email para {to_email}: {subject}")
            print(f"📧 [SIMULACIÓN] Mensaje: {message[:100]}...")
        except Exception as e:
            print(f"❌ Error enviando email a {to_email}: {e}")
    thread = threading.Thread(target=send_email)
    thread.daemon = True
    thread.start()

# FUNCIÓN MEJORADA PARA NOTIFICAR LÍMITES ALCANZADOS - ACTUALIZADA CON STREAMS
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
        
        if limit_type == 'daily_streams':
            subject = f"🚫 Límite Diario de Streams Alcanzado - API Streaming"
            message = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <h2 style="color: #e74c3c;">Hola {username},</h2>
                    <p>Has alcanzado tu límite diario de reproducciones en nuestra API de Streaming.</p>
                    <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin: 20px 0;">
                        <h3 style="color: #856404; margin-top: 0;">📊 Resumen de Uso:</h3>
                        <ul style="list-style: none; padding: 0;">
                            <li style="margin: 8px 0;"><strong>Plan Actual:</strong> {plan_type.upper()}</li>
                            <li style="margin: 8px 0;"><strong>Límite Diario de Streams:</strong> {limit} reproducciones</li>
                            <li style="margin: 8px 0;"><strong>Streams Hoy:</strong> {current_usage} reproducciones</li>
                            <li style="margin: 8px 0;"><strong>Se reinicia en:</strong> {reset_time}</li>
                        </ul>
                    </div>
                    <p>📺 <strong>¿Qué significa esto?</strong></p>
                    <ul>
                        <li>✅ Puedes seguir navegando por el catálogo</li>
                        <li>✅ Puedes buscar contenido</li>
                        <li>🚫 No puedes reproducir películas, series o canales hasta mañana</li>
                    </ul>
                    <p>💡 <strong>¿Necesitas más streams?</strong> Considera actualizar a nuestro plan PREMIUM para obtener:</p>
                    <ul>
                        <li>✅ Streaming ilimitado las 24/7</li>
                        <li>✅ Acceso completo a series y contenido exclusivo</li>
                        <li>✅ Streaming HD sin interrupciones</li>
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
        elif limit_type == 'daily':
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
        send_email_async(user_email, subject, message)
        print(f"✅ Notificación de {limit_type} enviada a {user_email}")
        if limit_type == 'daily_streams' and EMAIL_CONFIG.get('admin_email'):
            admin_subject = f"🔔 Usuario alcanzó límite diario de streams: {username}"
            admin_message = f"""
            <html>
            <body>
                <h2>Notificación de Admin - Límite de Streams</h2>
                <p>El usuario {username} ({user_email}) ha alcanzado su límite diario de streams.</p>
                <ul>
                    <li><strong>Plan:</strong> {plan_type}</li>
                    <li><strong>Streams Hoy:</strong> {current_usage}/{limit}</li>
                    <li><strong>Reset en:</strong> {reset_time}</li>
                </ul>
            </body>
            </html>
            """
            send_email_async(EMAIL_CONFIG['admin_email'], admin_subject, admin_message)
    except Exception as e:
        print(f"❌ Error en notificación de límite: {e}")

# FUNCIONES PARA NORMALIZAR DATOS DE LA BASE DE DATOS
def normalize_movie_data(movie_data, doc_id=None):
    if doc_id:
        movie_data['id'] = doc_id
    normalized = {
        'id': movie_data.get('id'),
        'title': movie_data.get('title', ''),
        'poster': movie_data.get('image_url', ''),
        'description': movie_data.get('sinopsis', ''),
        'year': movie_data.get('details', {}).get('year', '') if movie_data.get('details') else movie_data.get('year', ''),
        'genre': ', '.join(movie_data.get('details', {}).get('genres', [])) if movie_data.get('details') and movie_data.get('details', {}).get('genres') else movie_data.get('genre', ''),
        'rating': movie_data.get('details', {}).get('rating', '') if movie_data.get('details') else movie_data.get('rating', ''),
        'original_title': movie_data.get('original_title', ''),
        'actors': movie_data.get('details', {}).get('actors', []) if movie_data.get('details') else [],
        'duration': movie_data.get('details', {}).get('duration', '') if movie_data.get('details') else '',
        'director': movie_data.get('details', {}).get('director', '') if movie_data.get('details') else '',
        'play_links': movie_data.get('play_links', [])
    }
    return {k: v for k, v in normalized.items() if v not in [None, '', [], {}]}

def normalize_series_data(series_data, doc_id=None):
    if doc_id:
        series_data['id'] = doc_id
    normalized = {
        'id': series_data.get('id'),
        'title': series_data.get('title', ''),
        'poster': series_data.get('image_url', ''),
        'description': series_data.get('sinopsis', ''),
        'year': series_data.get('details', {}).get('year', '') if series_data.get('details') else series_data.get('year', ''),
        'genre': ', '.join(series_data.get('details', {}).get('genres', [])) if series_data.get('details') and series_data.get('details', {}).get('genres') else series_data.get('genre', ''),
        'rating': series_data.get('details', {}).get('rating', '') if series_data.get('details') else series_data.get('rating', ''),
        'total_seasons': series_data.get('details', {}).get('total_seasons', 0) if series_data.get('details') else 0,
        'status': series_data.get('details', {}).get('status', '') if series_data.get('details') else '',
        'seasons': normalize_seasons_data(series_data.get('seasons', {}))
    }
    return {k: v for k, v in normalized.items() if v not in [None, '', [], {}, 0]}

def normalize_seasons_data(seasons_dict):
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
    if doc_id:
        channel_data['id'] = doc_id
    normalized = {
        'id': channel_data.get('id'),
        'name': channel_data.get('name', ''),
        'logo': channel_data.get('image_url', ''),
        'status': channel_data.get('status', ''),
        'category': channel_data.get('category', ''),
        'country': channel_data.get('country', ''),
        'stream_options': channel_data.get('stream_options', [])
    }
    return {k: v for k, v in normalized.items() if v not in [None, '', [], {}]}

# FUNCIONES PARA VALIDACIÓN DE ESTRUCTURAS
def validate_movie_structure(data):
    """Valida que la estructura de película coincida con la documentación"""
    errors = []
    
    # Campos obligatorios
    if not data.get('title'):
        errors.append("El campo 'title' es obligatorio")
    if not data.get('image_url'):
        errors.append("El campo 'image_url' es obligatorio")
    
    # Validar estructura de details
    if 'details' in data:
        details = data['details']
        if not isinstance(details, dict):
            errors.append("El campo 'details' debe ser un objeto")
        else:
            if 'year' in details and not isinstance(details['year'], str):
                errors.append("El campo 'year' en details debe ser string")
            if 'genres' in details and not isinstance(details['genres'], list):
                errors.append("El campo 'genres' en details debe ser un array")
            if 'actors' in details and not isinstance(details['actors'], list):
                errors.append("El campo 'actors' en details debe ser un array")
    
    # Validar estructura de play_links
    if 'play_links' in data:
        play_links = data['play_links']
        if not isinstance(play_links, list):
            errors.append("El campo 'play_links' debe ser un array")
        else:
            for i, link in enumerate(play_links):
                if not isinstance(link, dict):
                    errors.append(f"play_links[{i}] debe ser un objeto")
                else:
                    if not link.get('server'):
                        errors.append(f"play_links[{i}] debe tener campo 'server'")
                    if not link.get('url'):
                        errors.append(f"play_links[{i}] debe tener campo 'url'")
    
    return errors

def validate_series_structure(data):
    """Valida que la estructura de serie coincida con la documentación"""
    errors = []
    
    # Campos obligatorios
    if not data.get('title'):
        errors.append("El campo 'title' es obligatorio")
    if not data.get('image_url'):
        errors.append("El campo 'image_url' es obligatorio")
    
    # Validar estructura de details
    if 'details' in data:
        details = data['details']
        if not isinstance(details, dict):
            errors.append("El campo 'details' debe ser un objeto")
        else:
            if 'year' in details and not isinstance(details['year'], str):
                errors.append("El campo 'year' en details debe ser string")
            if 'genres' en details and not isinstance(details['genres'], list):
                errors.append("El campo 'genres' en details debe ser un array")
            if 'total_seasons' in details and not isinstance(details['total_seasons'], int):
                errors.append("El campo 'total_seasons' en details debe ser número entero")
    
    # Validar estructura de seasons
    if 'seasons' in data:
        seasons = data['seasons']
        if not isinstance(seasons, dict):
            errors.append("El campo 'seasons' debe ser un objeto")
        else:
            for season_key, season_data in seasons.items():
                if not season_key.startswith('season-'):
                    errors.append(f"La clave de temporada '{season_key}' debe empezar con 'season-'")
                if not isinstance(season_data, dict):
                    errors.append(f"La temporada '{season_key}' debe ser un objeto")
                else:
                    # Validar episodios dentro de la temporada
                    if 'episodes' in season_data:
                        episodes = season_data['episodes']
                        if not isinstance(episodes, dict):
                            errors.append(f"El campo 'episodes' en {season_key} debe ser un objeto")
                        else:
                            for ep_key, ep_data in episodes.items():
                                if not ep_key.startswith('episode-'):
                                    errors.append(f"La clave de episodio '{ep_key}' debe empezar con 'episode-'")
    
    return errors

def validate_channel_structure(data):
    """Valida que la estructura de canal coincida con la documentación"""
    errors = []
    
    # Campos obligatorios
    if not data.get('name'):
        errors.append("El campo 'name' es obligatorio")
    if not data.get('image_url'):
        errors.append("El campo 'image_url' es obligatorio")
    
    # Validar estructura de stream_options
    if 'stream_options' in data:
        stream_options = data['stream_options']
        if not isinstance(stream_options, list):
            errors.append("El campo 'stream_options' debe ser un array")
        else:
            for i, option in enumerate(stream_options):
                if not isinstance(option, dict):
                    errors.append(f"stream_options[{i}] debe ser un objeto")
                else:
                    if not option.get('option_name'):
                        errors.append(f"stream_options[{i}] debe tener campo 'option_name'")
                    if not option.get('stream_url'):
                        errors.append(f"stream_options[{i}] debe tener campo 'stream_url'")
    
    return errors

# FUNCIÓN PARA NORMALIZAR ID
def normalize_id(title):
    """Normaliza un título para crear un ID válido"""
    import unicodedata
    import re
    
    # Convertir a minúsculas y normalizar
    normalized = title.lower()
    # Quitar acentos
    normalized = ''.join(
        c for c in unicodedata.normalize('NFD', normalized)
        if unicodedata.category(c) != 'Mn'
    )
    # Reemplazar espacios y caracteres especiales
    normalized = re.sub(r'[^a-z0-9\s-]', '', normalized)
    normalized = re.sub(r'[\s-]+', '-', normalized)
    normalized = normalized.strip('-')
    
    return normalized

# FUNCIÓN ACTUALIZADA: Mostrar opciones de streaming para usuarios free (INCLUYENDO ENLACES)
def limit_content_info(content_data, content_type):
    """Limitar información para usuarios free, pero MOSTRAR ENLACES DE STREAMING"""
    limited_data = {
        'id': content_data.get('id'),
        'title': content_data.get('title'),
        'year': content_data.get('year'),
        'genre': content_data.get('genre'),
        'rating': content_data.get('rating'),
        'poster': content_data.get('poster'),
        'description': content_data.get('description', '')[:100] + '...' if content_data.get('description') else ''
    }
    
    # ✅ MODIFICACIÓN: INCLUIR ENLACES DE STREAMING PARA USUARIOS FREE
    if content_type == 'pelicula':
        play_links = content_data.get('play_links', [])
        if play_links:
            limited_data['streaming_available'] = True
            limited_data['streaming_options_count'] = len(play_links)
            limited_data['streaming_servers'] = [link.get('server', 'Unknown') for link in play_links]
            # ✅ NUEVO: INCLUIR LOS ENLACES REALES
            limited_data['play_links'] = play_links
        else:
            limited_data['streaming_available'] = False
            
    elif content_type == 'serie':
        seasons = content_data.get('seasons', [])
        if seasons:
            limited_data['streaming_available'] = True
            limited_data['total_seasons'] = len(seasons)
            limited_data['total_episodes'] = sum(season.get('episode_count', 0) for season in seasons)
            # ✅ NUEVO: INCLUIR INFORMACIÓN DE TEMPORADAS CON ENLACES
            limited_data['seasons'] = []
            for season in seasons[:2]:  # Mostrar solo primeras 2 temporadas para free
                season_info = {
                    'season_number': season.get('season_number'),
                    'episode_count': season.get('episode_count'),
                    'episodes_available': len(season.get('episodes', []))
                }
                # Incluir episodios con enlaces (limitado)
                episodes_with_links = []
                for episode in season.get('episodes', [])[:3]:  # Máximo 3 episodios por temporada
                    if episode.get('play_links'):
                        episode_info = {
                            'episode_number': episode.get('episode_number'),
                            'title': episode.get('title'),
                            'play_links': episode.get('play_links', [])
                        }
                        episodes_with_links.append(episode_info)
                if episodes_with_links:
                    season_info['sample_episodes'] = episodes_with_links
                limited_data['seasons'].append(season_info)
        else:
            limited_data['streaming_available'] = False
            
    elif content_type == 'canal':
        stream_options = content_data.get('stream_options', [])
        if stream_options:
            limited_data['streaming_available'] = True
            limited_data['streaming_options_count'] = len(stream_options)
            limited_data['stream_servers'] = [option.get('option_name', 'Unknown') for option in stream_options]
            # ✅ NUEVO: INCLUIR LOS ENLACES REALES
            limited_data['stream_options'] = stream_options
        else:
            limited_data['streaming_available'] = False
    
    return limited_data

# DECORADOR ACTUALIZADO PARA VERIFICACIÓN DE TOKEN Y LÍMITES - AHORA CON STREAMS
def token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Verificar conexión Firebase
        if not check_firebase_connection():
            return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
        
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'Token requerido'}), 401
        
        # Extraer token si viene con "Bearer"
        if token.startswith('Bearer '):
            token = token[7:]
        
        try:
            # Buscar usuario por token
            users_ref = db.collection(TOKENS_COLLECTION)
            query = users_ref.where('tokens', 'array_contains', token).limit(1)
            users = list(query.stream())
            
            if not users:
                return jsonify({'error': 'Token inválido'}), 401
            
            user_doc = users[0]
            user_data = user_doc.to_dict()
            user_id = user_doc.id
            
            # Verificar si el token está expirado
            current_time = time.time()
            token_expired = False
            
            for t in user_data.get('tokens', []):
                if isinstance(t, dict) and t.get('token') == token:
                    if t.get('expires_at', 0) < current_time:
                        token_expired = True
                    break
            
            if token_expired:
                return jsonify({'error': 'Token expirado'}), 401
            
            # Verificar límites de uso
            plan_type = user_data.get('plan_type', 'free')
            plan_config = PLAN_CONFIG.get(plan_type, PLAN_CONFIG['free'])
            
            # Obtener fecha actual para límites diarios
            today = datetime.now().strftime('%Y-%m-%d')
            current_time_ts = time.time()
            
            # Inicializar contadores si no existen
            if 'usage_stats' not in user_data:
                user_data['usage_stats'] = {}
            
            if today not in user_data['usage_stats']:
                user_data['usage_stats'][today] = {
                    'daily_count': 0,
                    'daily_streams': 0,
                    'last_reset': current_time_ts
                }
            
            usage_today = user_data['usage_stats'][today]
            
            # Verificar límite diario de peticiones
            if usage_today['daily_count'] >= plan_config['daily_limit']:
                reset_time = datetime.fromtimestamp(usage_today['last_reset'] + 86400).strftime('%H:%M')
                notify_limit_reached(user_data, 'daily', usage_today['daily_count'], plan_config['daily_limit'], reset_time)
                return jsonify({
                    'error': f'Límite diario alcanzado ({usage_today["daily_count"]}/{plan_config["daily_limit"]}). Se reinicia a las {reset_time}',
                    'limit_type': 'daily',
                    'current_usage': usage_today['daily_count'],
                    'limit': plan_config['daily_limit'],
                    'reset_time': reset_time
                }), 429
            
            # Verificar límite de sesión (última hora)
            session_start = current_time_ts - SESSION_TIMEOUT
            session_count = 0
            
            for day, stats in user_data.get('usage_stats', {}).items():
                if 'requests' in stats:
                    for req_time in stats['requests']:
                        if req_time > session_start:
                            session_count += 1
            
            if session_count >= plan_config['session_limit']:
                reset_time = datetime.fromtimestamp(session_start + SESSION_TIMEOUT).strftime('%H:%M')
                notify_limit_reached(user_data, 'session', session_count, plan_config['session_limit'], reset_time)
                return jsonify({
                    'error': f'Límite de sesión alcanzado ({session_count}/{plan_config["session_limit"]}). Se reinicia a las {reset_time}',
                    'limit_type': 'session',
                    'current_usage': session_count,
                    'limit': plan_config['session_limit'],
                    'reset_time': reset_time
                }), 429
            
            # Rate limiting por minuto (por usuario)
            with request_lock:
                user_requests = user_request_times[user_id]
                current_time = time.time()
                one_minute_ago = current_time - 60
                
                # Filtrar requests del último minuto
                user_requests = [req_time for req_time in user_requests if req_time > one_minute_ago]
                user_request_times[user_id] = user_requests
                
                if len(user_requests) >= plan_config['rate_limit_per_minute']:
                    notify_limit_reached(user_data, 'rate_limit', len(user_requests), plan_config['rate_limit_per_minute'], '1 minuto')
                    return jsonify({
                        'error': f'Demasiadas peticiones. Límite: {plan_config["rate_limit_per_minute"]} por minuto',
                        'limit_type': 'rate_limit',
                        'current_usage': len(user_requests),
                        'limit': plan_config['rate_limit_per_minute'],
                        'reset_time': '1 minuto'
                    }), 429
                
                # Agregar request actual
                user_requests.append(current_time)
            
            # Rate limiting por IP
            client_ip = request.remote_addr
            with ip_lock:
                ip_requests = ip_request_times[client_ip]
                ip_requests = [req_time for req_time in ip_requests if req_time > one_minute_ago]
                ip_request_times[client_ip] = ip_requests
                
                if len(ip_requests) >= MAX_REQUESTS_PER_MINUTE_PER_IP:
                    return jsonify({
                        'error': f'Demasiadas peticiones desde tu IP. Límite: {MAX_REQUESTS_PER_MINUTE_PER_IP} por minuto'
                    }), 429
                
                ip_requests.append(current_time)
            
            # Incrementar contador diario
            usage_today['daily_count'] += 1
            
            # Inicializar array de requests si no existe
            if 'requests' not in usage_today:
                usage_today['requests'] = []
            usage_today['requests'].append(current_time_ts)
            
            # Actualizar en base de datos (de forma asíncrona para no bloquear)
            def update_usage():
                try:
                    users_ref.document(user_id).update({
                        'usage_stats': user_data['usage_stats'],
                        'last_used': current_time_ts
                    })
                except Exception as e:
                    print(f"Error actualizando uso: {e}")
            
            import threading
            thread = threading.Thread(target=update_usage)
            thread.daemon = True
            thread.start()
            
            # Agregar información del usuario al contexto de la request
            request.user_data = user_data
            request.user_id = user_id
            request.plan_type = plan_type
            request.plan_config = plan_config
            
            return f(*args, **kwargs)
            
        except Exception as e:
            print(f"Error en token_required: {e}")
            return jsonify({'error': 'Error interno del servidor'}), 500
    
    return decorated_function

# DECORADOR PARA ADMIN REQUERIDO
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'Token requerido'}), 401
        
        if token.startswith('Bearer '):
            token = token[7:]
        
        # Verificar si es un token de administrador
        if token not in ADMIN_TOKENS:
            return jsonify({'error': 'Se requieren privilegios de administrador'}), 403
        
        return f(*args, **kwargs)
    
    return decorated_function

# ENDPOINTS DE AUTENTICACIÓN Y GESTIÓN DE USUARIOS
@app.route('/api/register', methods=['POST'])
def register():
    """Registrar nuevo usuario"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        required_fields = ['username', 'email', 'plan_type']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Campo requerido faltante: {field}'}), 400
        
        username = data['username']
        email = data['email']
        plan_type = data['plan_type']
        
        # Validar plan type
        if plan_type not in PLAN_CONFIG:
            return jsonify({'error': f'Tipo de plan inválido. Opciones: {list(PLAN_CONFIG.keys())}'}), 400
        
        # Verificar si el usuario ya existe
        users_ref = db.collection(TOKENS_COLLECTION)
        
        # Verificar por email
        email_query = users_ref.where('email', '==', email).limit(1)
        email_users = list(email_query.stream())
        if email_users:
            return jsonify({'error': 'El email ya está registrado'}), 409
        
        # Verificar por username
        username_query = users_ref.where('username', '==', username).limit(1)
        username_users = list(username_query.stream())
        if username_users:
            return jsonify({'error': 'El nombre de usuario ya está registrado'}), 409
        
        # Generar token inicial
        initial_token = secrets.token_urlsafe(32)
        token_data = {
            'token': initial_token,
            'created_at': time.time(),
            'expires_at': time.time() + (30 * 24 * 60 * 60)  # 30 días
        }
        
        # Crear usuario
        user_data = {
            'username': username,
            'email': email,
            'plan_type': plan_type,
            'tokens': [token_data],
            'created_at': time.time(),
            'last_used': time.time(),
            'is_active': True,
            'usage_stats': {},
            'streaming_stats': {}
        }
        
        # Agregar usuario a la base de datos
        new_user_ref = users_ref.add(user_data)
        user_id = new_user_ref[1].id
        
        # Enviar email de bienvenida
        welcome_subject = f"🎉 ¡Bienvenido a la API Streaming, {username}!"
        welcome_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #2ecc71;">¡Bienvenido a nuestra API de Streaming!</h2>
                <p>Hola <strong>{username}</strong>,</p>
                <p>Tu registro ha sido exitoso. Aquí están los detalles de tu cuenta:</p>
                <div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #2c3e50; margin-top: 0;">📋 Detalles de tu cuenta:</h3>
                    <ul style="list-style: none; padding: 0;">
                        <li style="margin: 8px 0;"><strong>👤 Usuario:</strong> {username}</li>
                        <li style="margin: 8px 0;"><strong>📧 Email:</strong> {email}</li>
                        <li style="margin: 8px 0;"><strong>💎 Plan:</strong> {plan_type.upper()}</li>
                        <li style="margin: 8px 0;"><strong>🔑 Token:</strong> {initial_token}</li>
                    </ul>
                </div>
                <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #856404; margin-top: 0;">📊 Límites de tu plan {plan_type.upper()}:</h3>
                    <ul style="list-style: none; padding: 0;">
                        <li style="margin: 8px 0;"><strong>📅 Límite diario:</strong> {PLAN_CONFIG[plan_type]['daily_limit']} peticiones/día</li>
                        <li style="margin: 8px 0;"><strong>⏰ Límite por sesión:</strong> {PLAN_CONFIG[plan_type]['session_limit']} peticiones/hora</li>
                        <li style="margin: 8px 0;"><strong>🚀 Rate limit:</strong> {PLAN_CONFIG[plan_type]['rate_limit_per_minute']} peticiones/minuto</li>
                        <li style="margin: 8px 0;"><strong>📺 Streams diarios:</strong> {PLAN_CONFIG[plan_type]['daily_streams_limit'] if plan_type == 'free' else 'Ilimitados'}</li>
                    </ul>
                </div>
                <p>🔒 <strong>Guarda tu token de forma segura</strong>, ya que es tu llave de acceso a la API.</p>
                <p>📚 <strong>Documentación de la API:</strong> Consulta nuestra documentación para empezar a integrar.</p>
                <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee;">
                    Si tienes alguna pregunta o necesitas ayuda, no dudes en contactarnos.
                </p>
                <p style="color: #666; font-size: 14px;">
                    Saludos,<br>El equipo de API Streaming
                </p>
            </div>
        </body>
        </html>
        """
        send_email_async(email, welcome_subject, welcome_message)
        
        return jsonify({
            'message': 'Usuario registrado exitosamente',
            'user_id': user_id,
            'token': initial_token,
            'plan_type': plan_type,
            'limits': PLAN_CONFIG[plan_type]
        }), 201
        
    except Exception as e:
        print(f"Error en registro: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/login', methods=['POST'])
def login():
    """Iniciar sesión y obtener token"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Puede iniciar sesión con username o email
        identifier = data.get('username') or data.get('email')
        if not identifier:
            return jsonify({'error': 'Username o email requerido'}), 400
        
        users_ref = db.collection(TOKENS_COLLECTION)
        
        # Buscar por username o email
        username_query = users_ref.where('username', '==', identifier).limit(1)
        email_query = users_ref.where('email', '==', identifier).limit(1)
        
        users = list(username_query.stream()) or list(email_query.stream())
        
        if not users:
            return jsonify({'error': 'Usuario no encontrado'}), 404
        
        user_doc = users[0]
        user_data = user_doc.to_dict()
        user_id = user_doc.id
        
        # Verificar si el usuario está activo
        if not user_data.get('is_active', True):
            return jsonify({'error': 'Cuenta desactivada'}), 403
        
        # Generar nuevo token
        new_token = secrets.token_urlsafe(32)
        token_data = {
            'token': new_token,
            'created_at': time.time(),
            'expires_at': time.time() + (30 * 24 * 60 * 60)  # 30 días
        }
        
        # Actualizar tokens del usuario
        current_tokens = user_data.get('tokens', [])
        
        # Limitar a 5 tokens activos máximo
        if len(current_tokens) >= 5:
            # Mantener los 4 más recientes y agregar el nuevo
            current_tokens.sort(key=lambda x: x.get('created_at', 0), reverse=True)
            current_tokens = current_tokens[:4]
        
        current_tokens.append(token_data)
        
        # Actualizar en base de datos
        users_ref.document(user_id).update({
            'tokens': current_tokens,
            'last_login': time.time()
        })
        
        # Enviar notificación de login
        login_subject = "🔐 Nuevo inicio de sesión - API Streaming"
        login_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #3498db;">Nuevo inicio de sesión detectado</h2>
                <p>Hola <strong>{user_data.get('username')}</strong>,</p>
                <p>Se ha detectado un nuevo inicio de sesión en tu cuenta.</p>
                <div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #2c3e50; margin-top: 0;">📋 Detalles del acceso:</h3>
                    <ul style="list-style: none; padding: 0;">
                        <li style="margin: 8px 0;"><strong>👤 Usuario:</strong> {user_data.get('username')}</li>
                        <li style="margin: 8px 0;"><strong>🕐 Fecha y hora:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</li>
                        <li style="margin: 8px 0;"><strong>💎 Plan:</strong> {user_data.get('plan_type', 'free').upper()}</li>
                    </ul>
                </div>
                <p>🔒 <strong>Si no reconoces esta actividad</strong>, por favor contacta con soporte inmediatamente.</p>
                <p style="color: #666; font-size: 14px; margin-top: 30px;">
                    Saludos,<br>El equipo de API Streaming
                </p>
            </div>
        </body>
        </html>
        """
        send_email_async(user_data.get('email'), login_subject, login_message)
        
        return jsonify({
            'message': 'Login exitoso',
            'token': new_token,
            'user': {
                'user_id': user_id,
                'username': user_data.get('username'),
                'email': user_data.get('email'),
                'plan_type': user_data.get('plan_type')
            },
            'limits': PLAN_CONFIG.get(user_data.get('plan_type', 'free'), PLAN_CONFIG['free'])
        }), 200
        
    except Exception as e:
        print(f"Error en login: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/profile', methods=['GET'])
@token_required
def get_profile():
    """Obtener perfil del usuario actual"""
    user_data = request.user_data
    
    # Calcular uso del día actual
    today = datetime.now().strftime('%Y-%m-%d')
    usage_today = user_data.get('usage_stats', {}).get(today, {})
    daily_count = usage_today.get('daily_count', 0)
    daily_streams = usage_today.get('daily_streams', 0)
    
    plan_type = user_data.get('plan_type', 'free')
    plan_config = PLAN_CONFIG.get(plan_type, PLAN_CONFIG['free'])
    
    return jsonify({
        'user': {
            'user_id': request.user_id,
            'username': user_data.get('username'),
            'email': user_data.get('email'),
            'plan_type': plan_type,
            'created_at': user_data.get('created_at'),
            'last_used': user_data.get('last_used')
        },
        'usage': {
            'daily_requests': daily_count,
            'daily_requests_limit': plan_config['daily_limit'],
            'daily_streams': daily_streams,
            'daily_streams_limit': plan_config['daily_streams_limit'],
            'session_limit': plan_config['session_limit'],
            'rate_limit_per_minute': plan_config['rate_limit_per_minute']
        },
        'features': plan_config['features']
    }), 200

@app.route('/api/refresh-token', methods=['POST'])
@token_required
def refresh_token():
    """Generar un nuevo token y eliminar el actual"""
    try:
        current_token = request.headers.get('Authorization')
        if current_token.startswith('Bearer '):
            current_token = current_token[7:]
        
        users_ref = db.collection(TOKENS_COLLECTION)
        user_id = request.user_id
        user_data = request.user_data
        
        # Generar nuevo token
        new_token = secrets.token_urlsafe(32)
        token_data = {
            'token': new_token,
            'created_at': time.time(),
            'expires_at': time.time() + (30 * 24 * 60 * 60)  # 30 días
        }
        
        # Filtrar tokens para eliminar el actual y agregar el nuevo
        current_tokens = user_data.get('tokens', [])
        new_tokens = [t for t in current_tokens if not (isinstance(t, dict) and t.get('token') == current_token)]
        new_tokens.append(token_data)
        
        # Limitar a 5 tokens máximo
        if len(new_tokens) > 5:
            new_tokens.sort(key=lambda x: x.get('created_at', 0))
            new_tokens = new_tokens[-5:]
        
        # Actualizar en base de datos
        users_ref.document(user_id).update({
            'tokens': new_tokens,
            'last_used': time.time()
        })
        
        return jsonify({
            'message': 'Token actualizado exitosamente',
            'new_token': new_token
        }), 200
        
    except Exception as e:
        print(f"Error refrescando token: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

# ENDPOINTS ESPECÍFICOS PARA PELÍCULAS
@app.route('/api/peliculas', methods=['GET'])
@token_required
def get_peliculas():
    """Obtener lista de películas con paginación y filtros"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        # Parámetros de paginación y filtros
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        genre = request.args.get('genre')
        year = request.args.get('year')
        search = request.args.get('search')
        
        # Validar parámetros
        if page < 1:
            page = 1
        if limit < 1 or limit > 100:
            limit = 20
        
        # Calcular offset
        offset = (page - 1) * limit
        
        # Consulta base
        peliculas_ref = db.collection('peliculas')
        
        # Aplicar filtros
        if genre:
            peliculas_ref = peliculas_ref.where('details.genres', 'array_contains', genre)
        if year:
            peliculas_ref = peliculas_ref.where('details.year', '==', year)
        
        # Obtener total para paginación
        total_docs = len(list(peliculas_ref.stream()))
        
        # Aplicar paginación
        peliculas_ref = peliculas_ref.limit(limit).offset(offset)
        
        # Ejecutar consulta
        docs = list(peliculas_ref.stream())
        
        # Normalizar datos
        peliculas = []
        for doc in docs:
            movie_data = doc.to_dict()
            normalized = normalize_movie_data(movie_data, doc.id)
            
            # Aplicar límites según plan
            if request.plan_type == 'free':
                normalized = limit_content_info(normalized, 'pelicula')
            
            peliculas.append(normalized)
        
        # Búsqueda en memoria si se especificó (para casos donde Firestore no soporte búsqueda por texto)
        if search:
            search_lower = search.lower()
            peliculas = [p for p in peliculas if search_lower in p.get('title', '').lower() or 
                         search_lower in p.get('original_title', '').lower() or
                         search_lower in p.get('description', '').lower()]
        
        return jsonify({
            'data': peliculas,
            'pagination': {
                'page': page,
                'limit': limit,
                'total': total_docs,
                'pages': (total_docs + limit - 1) // limit
            },
            'filters': {
                'genre': genre,
                'year': year,
                'search': search
            }
        }), 200
        
    except Exception as e:
        print(f"Error obteniendo películas: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['GET'])
@token_required
def get_pelicula(pelicula_id):
    """Obtener una película específica por ID"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Película no encontrada'}), 404
        
        movie_data = doc.to_dict()
        normalized = normalize_movie_data(movie_data, doc.id)
        
        # Aplicar límites según plan
        if request.plan_type == 'free':
            normalized = limit_content_info(normalized, 'pelicula')
        
        return jsonify({'data': normalized}), 200
        
    except Exception as e:
        print(f"Error obteniendo película: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/peliculas', methods=['POST'])
@admin_required
def create_pelicula():
    """Crear nueva película (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Validar estructura
        errors = validate_movie_structure(data)
        if errors:
            return jsonify({'error': 'Estructura inválida', 'details': errors}), 400
        
        # Generar ID si no se proporciona
        if not data.get('id'):
            data['id'] = normalize_id(data['title'])
        
        # Verificar si ya existe
        existing_doc = db.collection('peliculas').document(data['id']).get()
        if existing_doc.exists:
            return jsonify({'error': 'Ya existe una película con este ID'}), 409
        
        # Agregar metadatos
        data['created_at'] = time.time()
        data['updated_at'] = time.time()
        data['created_by'] = 'admin'  # En un sistema real, usaría el ID del admin
        
        # Crear documento
        db.collection('peliculas').document(data['id']).set(data)
        
        return jsonify({
            'message': 'Película creada exitosamente',
            'id': data['id'],
            'data': normalize_movie_data(data, data['id'])
        }), 201
        
    except Exception as e:
        print(f"Error creando película: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['PUT'])
@admin_required
def update_pelicula(pelicula_id):
    """Actualizar película existente (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Verificar existencia
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Película no encontrada'}), 404
        
        # Validar estructura
        errors = validate_movie_structure(data)
        if errors:
            return jsonify({'error': 'Estructura inválida', 'details': errors}), 400
        
        # Actualizar metadatos
        data['updated_at'] = time.time()
        
        # Actualizar documento
        doc_ref.update(data)
        
        # Obtener datos actualizados
        updated_doc = doc_ref.get()
        updated_data = updated_doc.to_dict()
        
        return jsonify({
            'message': 'Película actualizada exitosamente',
            'data': normalize_movie_data(updated_data, pelicula_id)
        }), 200
        
    except Exception as e:
        print(f"Error actualizando película: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['DELETE'])
@admin_required
def delete_pelicula(pelicula_id):
    """Eliminar película (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Película no encontrada'}), 404
        
        # Eliminar documento
        doc_ref.delete()
        
        return jsonify({'message': 'Película eliminada exitosamente'}), 200
        
    except Exception as e:
        print(f"Error eliminando película: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

# ENDPOINTS ESPECÍFICOS PARA SERIES (contenido)
@app.route('/api/contenido', methods=['GET'])
@token_required
def get_contenido():
    """Obtener lista de series con paginación y filtros"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        # Parámetros de paginación y filtros
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        genre = request.args.get('genre')
        year = request.args.get('year')
        search = request.args.get('search')
        
        # Validar parámetros
        if page < 1:
            page = 1
        if limit < 1 or limit > 100:
            limit = 20
        
        # Calcular offset
        offset = (page - 1) * limit
        
        # Consulta base
        contenido_ref = db.collection('contenido')
        
        # Aplicar filtros
        if genre:
            contenido_ref = contenido_ref.where('details.genres', 'array_contains', genre)
        if year:
            contenido_ref = contenido_ref.where('details.year', '==', year)
        
        # Obtener total para paginación
        total_docs = len(list(contenido_ref.stream()))
        
        # Aplicar paginación
        contenido_ref = contenido_ref.limit(limit).offset(offset)
        
        # Ejecutar consulta
        docs = list(contenido_ref.stream())
        
        # Normalizar datos
        series_list = []
        for doc in docs:
            series_data = doc.to_dict()
            normalized = normalize_series_data(series_data, doc.id)
            
            # Aplicar límites según plan
            if request.plan_type == 'free':
                normalized = limit_content_info(normalized, 'serie')
            
            series_list.append(normalized)
        
        # Búsqueda en memoria si se especificó
        if search:
            search_lower = search.lower()
            series_list = [s for s in series_list if search_lower in s.get('title', '').lower() or 
                          search_lower in s.get('description', '').lower()]
        
        return jsonify({
            'data': series_list,
            'pagination': {
                'page': page,
                'limit': limit,
                'total': total_docs,
                'pages': (total_docs + limit - 1) // limit
            },
            'filters': {
                'genre': genre,
                'year': year,
                'search': search
            }
        }), 200
        
    except Exception as e:
        print(f"Error obteniendo series: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/contenido/<serie_id>', methods=['GET'])
@token_required
def get_serie(serie_id):
    """Obtener una serie específica por ID"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Serie no encontrada'}), 404
        
        series_data = doc.to_dict()
        normalized = normalize_series_data(series_data, doc.id)
        
        # Aplicar límites según plan
        if request.plan_type == 'free':
            normalized = limit_content_info(normalized, 'serie')
        
        return jsonify({'data': normalized}), 200
        
    except Exception as e:
        print(f"Error obteniendo serie: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/contenido', methods=['POST'])
@admin_required
def create_serie():
    """Crear nueva serie (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Validar estructura
        errors = validate_series_structure(data)
        if errors:
            return jsonify({'error': 'Estructura inválida', 'details': errors}), 400
        
        # Generar ID si no se proporciona
        if not data.get('id'):
            data['id'] = normalize_id(data['title'])
        
        # Verificar si ya existe
        existing_doc = db.collection('contenido').document(data['id']).get()
        if existing_doc.exists:
            return jsonify({'error': 'Ya existe una serie con este ID'}), 409
        
        # Agregar metadatos
        data['created_at'] = time.time()
        data['updated_at'] = time.time()
        data['created_by'] = 'admin'
        
        # Crear documento
        db.collection('contenido').document(data['id']).set(data)
        
        return jsonify({
            'message': 'Serie creada exitosamente',
            'id': data['id'],
            'data': normalize_series_data(data, data['id'])
        }), 201
        
    except Exception as e:
        print(f"Error creando serie: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/contenido/<serie_id>', methods=['PUT'])
@admin_required
def update_serie(serie_id):
    """Actualizar serie existente (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Verificar existencia
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Serie no encontrada'}), 404
        
        # Validar estructura
        errors = validate_series_structure(data)
        if errors:
            return jsonify({'error': 'Estructura inválida', 'details': errors}), 400
        
        # Actualizar metadatos
        data['updated_at'] = time.time()
        
        # Actualizar documento
        doc_ref.update(data)
        
        # Obtener datos actualizados
        updated_doc = doc_ref.get()
        updated_data = updated_doc.to_dict()
        
        return jsonify({
            'message': 'Serie actualizada exitosamente',
            'data': normalize_series_data(updated_data, serie_id)
        }), 200
        
    except Exception as e:
        print(f"Error actualizando serie: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/contenido/<serie_id>', methods=['DELETE'])
@admin_required
def delete_serie(serie_id):
    """Eliminar serie (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Serie no encontrada'}), 404
        
        # Eliminar documento
        doc_ref.delete()
        
        return jsonify({'message': 'Serie eliminada exitosamente'}), 200
        
    except Exception as e:
        print(f"Error eliminando serie: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

# ENDPOINTS ESPECÍFICOS PARA CANALES
@app.route('/api/canales', methods=['GET'])
@token_required
def get_canales():
    """Obtener lista de canales con paginación y filtros"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        # Parámetros de paginación y filtros
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        category = request.args.get('category')
        country = request.args.get('country')
        search = request.args.get('search')
        
        # Validar parámetros
        if page < 1:
            page = 1
        if limit < 1 or limit > 100:
            limit = 20
        
        # Calcular offset
        offset = (page - 1) * limit
        
        # Consulta base
        canales_ref = db.collection('canales')
        
        # Aplicar filtros
        if category:
            canales_ref = canales_ref.where('category', '==', category)
        if country:
            canales_ref = canales_ref.where('country', '==', country)
        
        # Obtener total para paginación
        total_docs = len(list(canales_ref.stream()))
        
        # Aplicar paginación
        canales_ref = canales_ref.limit(limit).offset(offset)
        
        # Ejecutar consulta
        docs = list(canales_ref.stream())
        
        # Normalizar datos
        canales = []
        for doc in docs:
            channel_data = doc.to_dict()
            normalized = normalize_channel_data(channel_data, doc.id)
            
            # Aplicar límites según plan
            if request.plan_type == 'free':
                normalized = limit_content_info(normalized, 'canal')
            
            canales.append(normalized)
        
        # Búsqueda en memoria si se especificó
        if search:
            search_lower = search.lower()
            canales = [c for c in canales if search_lower in c.get('name', '').lower() or 
                      search_lower in c.get('category', '').lower()]
        
        return jsonify({
            'data': canales,
            'pagination': {
                'page': page,
                'limit': limit,
                'total': total_docs,
                'pages': (total_docs + limit - 1) // limit
            },
            'filters': {
                'category': category,
                'country': country,
                'search': search
            }
        }), 200
        
    except Exception as e:
        print(f"Error obteniendo canales: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/canales/<canal_id>', methods=['GET'])
@token_required
def get_canal(canal_id):
    """Obtener un canal específico por ID"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Canal no encontrado'}), 404
        
        channel_data = doc.to_dict()
        normalized = normalize_channel_data(channel_data, doc.id)
        
        # Aplicar límites según plan
        if request.plan_type == 'free':
            normalized = limit_content_info(normalized, 'canal')
        
        return jsonify({'data': normalized}), 200
        
    except Exception as e:
        print(f"Error obteniendo canal: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/canales', methods=['POST'])
@admin_required
def create_canal():
    """Crear nuevo canal (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Validar estructura
        errors = validate_channel_structure(data)
        if errors:
            return jsonify({'error': 'Estructura inválida', 'details': errors}), 400
        
        # Generar ID si no se proporciona
        if not data.get('id'):
            data['id'] = normalize_id(data['name'])
        
        # Verificar si ya existe
        existing_doc = db.collection('canales').document(data['id']).get()
        if existing_doc.exists:
            return jsonify({'error': 'Ya existe un canal con este ID'}), 409
        
        # Agregar metadatos
        data['created_at'] = time.time()
        data['updated_at'] = time.time()
        data['created_by'] = 'admin'
        
        # Crear documento
        db.collection('canales').document(data['id']).set(data)
        
        return jsonify({
            'message': 'Canal creado exitosamente',
            'id': data['id'],
            'data': normalize_channel_data(data, data['id'])
        }), 201
        
    except Exception as e:
        print(f"Error creando canal: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/canales/<canal_id>', methods=['PUT'])
@admin_required
def update_canal(canal_id):
    """Actualizar canal existente (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Datos JSON requeridos'}), 400
        
        # Verificar existencia
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Canal no encontrado'}), 404
        
        # Validar estructura
        errors = validate_channel_structure(data)
        if errors:
            return jsonify({'error': 'Estructura inválida', 'details': errors}), 400
        
        # Actualizar metadatos
        data['updated_at'] = time.time()
        
        # Actualizar documento
        doc_ref.update(data)
        
        # Obtener datos actualizados
        updated_doc = doc_ref.get()
        updated_data = updated_doc.to_dict()
        
        return jsonify({
            'message': 'Canal actualizada exitosamente',
            'data': normalize_channel_data(updated_data, canal_id)
        }), 200
        
    except Exception as e:
        print(f"Error actualizando canal: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/canales/<canal_id>', methods=['DELETE'])
@admin_required
def delete_canal(canal_id):
    """Eliminar canal (solo admin)"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({'error': 'Canal no encontrado'}), 404
        
        # Eliminar documento
        doc_ref.delete()
        
        return jsonify({'message': 'Canal eliminado exitosamente'}), 200
        
    except Exception as e:
        print(f"Error eliminando canal: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

# FUNCIONES GENÉRICAS PARA ENDPOINTS AUTOMÁTICOS

def validate_permissions(user_data, collection_name, method):
    """Valida permisos para operaciones en colecciones"""
    plan_type = user_data.get('plan_type', 'free')
    
    # Admin tiene acceso completo
    if user_data.get('is_admin', False):
        return True
    
    # Verificar permisos según plan y colección
    if method in ['POST', 'PUT', 'DELETE']:
        # Solo admin puede crear, actualizar o eliminar
        return False
    
    # Verificar acceso de lectura según plan
    if plan_type == 'free':
        # Free tiene acceso limitado a ciertas colecciones
        allowed_collections = ['listas', 'trending']
        return collection_name in allowed_collections
    elif plan_type == 'premium':
        # Premium tiene acceso completo a todas las colecciones
        return True
    
    return False

def add_auto_metadata(document_data, user_data, operation):
    """Agrega metadatos automáticos a los documentos"""
    current_time = time.time()
    
    if operation == 'create':
        document_data['created_at'] = current_time
        document_data['created_by'] = user_data.get('username', 'unknown')
        document_data['updated_at'] = current_time
    elif operation == 'update':
        document_data['updated_at'] = current_time
        document_data['last_updated_by'] = user_data.get('username', 'unknown')
    
    return document_data

def normalize_generic_data(document_data, doc_id=None):
    """Normaliza datos genéricos para cualquier colección"""
    if doc_id:
        document_data['id'] = doc_id
    
    # Campos comunes que podrían existir en cualquier documento
    common_fields = {
        'id': document_data.get('id'),
        'title': document_data.get('title', document_data.get('name', '')),
        'description': document_data.get('description', document_data.get('sinopsis', '')),
        'image_url': document_data.get('image_url', document_data.get('poster', '')),
        'created_at': document_data.get('created_at'),
        'updated_at': document_data.get('updated_at')
    }
    
    # Filtrar campos vacíos
    normalized = {k: v for k, v in common_fields.items() if v not in [None, '', []]}
    
    # Incluir todos los campos adicionales del documento
    for key, value in document_data.items():
        if key not in normalized:
            normalized[key] = value
    
    return normalized

def create_generic_endpoints(collection_name):
    """Crea endpoints genéricos automáticos para una colección"""
    
    @app.route(f'/api/{collection_name}', methods=['GET'])
    @token_required
    def get_generic_collection():
        """Endpoint genérico GET para listar documentos"""
        if not check_firebase_connection():
            return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
        
        # Validar permisos
        if not validate_permissions(request.user_data, collection_name, 'GET'):
            return jsonify({'error': 'No tienes permisos para acceder a esta colección'}), 403
        
        try:
            # Parámetros de paginación
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 20))
            search = request.args.get('search', '')
            
            # Validar parámetros
            if page < 1:
                page = 1
            if limit < 1 or limit > 100:
                limit = 20
            
            offset = (page - 1) * limit
            
            # Consulta base
            collection_ref = db.collection(collection_name)
            
            # Obtener total para paginación
            total_docs = len(list(collection_ref.stream()))
            
            # Aplicar paginación
            collection_ref = collection_ref.limit(limit).offset(offset)
            
            # Ejecutar consulta
            docs = list(collection_ref.stream())
            
            # Normalizar datos
            items = []
            for doc in docs:
                item_data = doc.to_dict()
                normalized = normalize_generic_data(item_data, doc.id)
                items.append(normalized)
            
            # Búsqueda en memoria si se especificó
            if search:
                search_lower = search.lower()
                items = [item for item in items 
                        if any(search_lower in str(value).lower() 
                              for value in item.values() 
                              if isinstance(value, str))]
            
            return jsonify({
                'data': items,
                'pagination': {
                    'page': page,
                    'limit': limit,
                    'total': total_docs,
                    'pages': (total_docs + limit - 1) // limit
                },
                'collection': collection_name
            }), 200
            
        except Exception as e:
            print(f"Error obteniendo {collection_name}: {e}")
            return jsonify({'error': f'Error interno del servidor: {str(e)}'}), 500

    @app.route(f'/api/{collection_name}/<item_id>', methods=['GET'])
    @token_required
    def get_generic_item(item_id):
        """Endpoint genérico GET para un documento específico"""
        if not check_firebase_connection():
            return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
        
        # Validar permisos
        if not validate_permissions(request.user_data, collection_name, 'GET'):
            return jsonify({'error': 'No tienes permisos para acceder a esta colección'}), 403
        
        try:
            doc_ref = db.collection(collection_name).document(item_id)
            doc = doc_ref.get()
            
            if not doc.exists:
                return jsonify({'error': f'Documento no encontrado en {collection_name}'}), 404
            
            item_data = doc.to_dict()
            normalized = normalize_generic_data(item_data, doc.id)
            
            return jsonify({
                'data': normalized,
                'collection': collection_name
            }), 200
            
        except Exception as e:
            print(f"Error obteniendo {collection_name}/{item_id}: {e}")
            return jsonify({'error': 'Error interno del servidor'}), 500

    @app.route(f'/api/{collection_name}', methods=['POST'])
    @token_required
    def create_generic_item():
        """Endpoint genérico POST para crear documento"""
        if not check_firebase_connection():
            return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
        
        # Validar permisos
        if not validate_permissions(request.user_data, collection_name, 'POST'):
            return jsonify({'error': 'No tienes permisos para crear en esta colección'}), 403
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'Datos JSON requeridos'}), 400
            
            # Generar ID si no se proporciona
            if not data.get('id'):
                title = data.get('title') or data.get('name') or 'item'
                data['id'] = normalize_id(title)
            
            # Verificar si ya existe
            existing_doc = db.collection(collection_name).document(data['id']).get()
            if existing_doc.exists:
                return jsonify({'error': f'Ya existe un documento con este ID en {collection_name}'}), 409
            
            # Agregar metadatos automáticos
            data = add_auto_metadata(data, request.user_data, 'create')
            
            # Crear documento
            db.collection(collection_name).document(data['id']).set(data)
            
            return jsonify({
                'message': f'Documento creado exitosamente en {collection_name}',
                'id': data['id'],
                'data': normalize_generic_data(data, data['id'])
            }), 201
            
        except Exception as e:
            print(f"Error creando en {collection_name}: {e}")
            return jsonify({'error': 'Error interno del servidor'}), 500

    @app.route(f'/api/{collection_name}/<item_id>', methods=['PUT'])
    @token_required
    def update_generic_item(item_id):
        """Endpoint genérico PUT para actualizar documento"""
        if not check_firebase_connection():
            return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
        
        # Validar permisos
        if not validate_permissions(request.user_data, collection_name, 'PUT'):
            return jsonify({'error': 'No tienes permisos para actualizar en esta colección'}), 403
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'Datos JSON requeridos'}), 400
            
            # Verificar existencia
            doc_ref = db.collection(collection_name).document(item_id)
            doc = doc_ref.get()
            
            if not doc.exists:
                return jsonify({'error': f'Documento no encontrado en {collection_name}'}), 404
            
            # Agregar metadatos automáticos
            data = add_auto_metadata(data, request.user_data, 'update')
            
            # Actualizar documento
            doc_ref.update(data)
            
            # Obtener datos actualizados
            updated_doc = doc_ref.get()
            updated_data = updated_doc.to_dict()
            
            return jsonify({
                'message': f'Documento actualizado exitosamente en {collection_name}',
                'data': normalize_generic_data(updated_data, item_id)
            }), 200
            
        except Exception as e:
            print(f"Error actualizando {collection_name}/{item_id}: {e}")
            return jsonify({'error': 'Error interno del servidor'}), 500

    @app.route(f'/api/{collection_name}/<item_id>', methods=['DELETE'])
    @token_required
    def delete_generic_item(item_id):
        """Endpoint genérico DELETE para eliminar documento"""
        if not check_firebase_connection():
            return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
        
        # Validar permisos
        if not validate_permissions(request.user_data, collection_name, 'DELETE'):
            return jsonify({'error': 'No tienes permisos para eliminar en esta colección'}), 403
        
        try:
            doc_ref = db.collection(collection_name).document(item_id)
            doc = doc_ref.get()
            
            if not doc.exists:
                return jsonify({'error': f'Documento no encontrado en {collection_name}'}), 404
            
            # Eliminar documento
            doc_ref.delete()
            
            return jsonify({
                'message': f'Documento eliminado exitosamente de {collection_name}'
            }), 200
            
        except Exception as e:
            print(f"Error eliminando de {collection_name}: {e}")
            return jsonify({'error': 'Error interno del servidor'}), 500

    # Retornar los nombres de las funciones para referencia
    return {
        'get_collection': get_generic_collection,
        'get_item': get_generic_item,
        'create_item': create_generic_item,
        'update_item': update_generic_item,
        'delete_item': delete_generic_item
    }

# REGISTRAR ENDPOINTS GENÉRICOS PARA COLECCIONES FALTANTES
collections_to_register = ['listas', 'reports', 'sagas', 'trending']

# Diccionario para mantener referencia a los endpoints genéricos
generic_endpoints = {}

for collection in collections_to_register:
    print(f"🔄 Registrando endpoints genéricos para: {collection}")
    endpoints = create_generic_endpoints(collection)
    generic_endpoints[collection] = endpoints

print("✅ Endpoints genéricos registrados exitosamente")

# ENDPOINT DE HEALTH CHECK MEJORADO
@app.route('/api/health', methods=['GET'])
def health_check():
    """Endpoint de verificación de salud del servicio"""
    health_status = {
        'status': 'healthy',
        'timestamp': time.time(),
        'service': 'API Streaming',
        'version': '2.0.0',
        'firebase_connected': check_firebase_connection(),
        'collections_available': ['peliculas', 'contenido', 'canales'] + collections_to_register,
        'generic_endpoints_registered': list(generic_endpoints.keys()),
        'environment': 'production' if os.environ.get('FIREBASE_PROJECT_ID') else 'development'
    }
    
    # Verificar estado de Firebase más detallado
    if health_status['firebase_connected']:
        try:
            # Test de lectura
            test_ref = db.collection('api_users').limit(1)
            test_docs = list(test_ref.stream())
            health_status['firebase_read'] = True
            health_status['firebase_test_docs'] = len(test_docs)
        except Exception as e:
            health_status['firebase_read'] = False
            health_status['firebase_error'] = str(e)
    else:
        health_status['firebase_read'] = False
    
    status_code = 200 if health_status['firebase_connected'] else 503
    
    return jsonify(health_status), status_code

# ENDPOINT DE ESTADÍSTICAS (solo admin)
@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def admin_stats():
    """Estadísticas administrativas del sistema"""
    if not check_firebase_connection():
        return jsonify({'error': 'Servicio no disponible temporalmente. Intente nuevamente.'}), 503
    
    try:
        stats = {
            'total_users': 0,
            'users_by_plan': defaultdict(int),
            'active_today': 0,
            'total_requests_today': 0,
            'collection_counts': {},
            'system_health': {
                'firebase_connected': True,
                'memory_usage': {},
                'uptime': time.time() - (os.path.getctime(__file__) if os.path.exists(__file__) else time.time())
            }
        }
        
        # Estadísticas de usuarios
        users_ref = db.collection(TOKENS_COLLECTION)
        users = list(users_ref.stream())
        stats['total_users'] = len(users)
        
        today = datetime.now().strftime('%Y-%m-%d')
        for user in users:
            user_data = user.to_dict()
            plan_type = user_data.get('plan_type', 'free')
            stats['users_by_plan'][plan_type] += 1
            
            # Usuarios activos hoy
            last_used = user_data.get('last_used', 0)
            if time.time() - last_used < 86400:  # 24 horas
                stats['active_today'] += 1
            
            # Requests hoy
            usage_today = user_data.get('usage_stats', {}).get(today, {})
            stats['total_requests_today'] += usage_today.get('daily_count', 0)
        
        # Conteo de documentos por colección
        collections = ['peliculas', 'contenido', 'canales'] + collections_to_register
        for collection in collections:
            try:
                col_ref = db.collection(collection)
                docs = list(col_ref.stream())
                stats['collection_counts'][collection] = len(docs)
            except Exception as e:
                stats['collection_counts'][collection] = f'Error: {str(e)}'
        
        # Uso de memoria
        import psutil
        process = psutil.Process()
        stats['system_health']['memory_usage'] = {
            'rss_mb': process.memory_info().rss / 1024 / 1024,
            'percent': process.memory_percent()
        }
        
        return jsonify(stats), 200
        
    except Exception as e:
        print(f"Error obteniendo estadísticas: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

# MANEJADOR DE ERRORES GLOBAL
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint no encontrado'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Error interno del servidor'}), 500

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({'error': 'Método no permitido'}), 405

# INICIALIZACIÓN Y CONFIGURACIÓN FINAL
if __name__ == '__main__':
    print("🚀 Iniciando API Streaming...")
    print(f"📊 Colecciones registradas: {['peliculas', 'contenido', 'canales'] + collections_to_register}")
    print(f"🔧 Endpoints genéricos: {list(generic_endpoints.keys())}")
    print("✅ API lista para recibir requests")
    
    # Configuración para producción
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    app.run(host='0.0.0.0', port=port, debug=debug)

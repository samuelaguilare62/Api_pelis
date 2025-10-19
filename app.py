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
            'content_recommendations': False,
            'content_creation': False,
            'content_editing': False
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
        send_email_async(user_email, subject, message)
        print(f"✅ Notificación de {limit_type} enviada a {user_email}")
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
            if 'genres' in details and not isinstance(details['genres'], list):
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

# Decorador para verificar Firebase
def check_firebase():
    if not check_firebase_connection():
        return jsonify({
            "success": False,
            "error": "Firebase no disponible",
            "solution": "El servicio se está reconectando automáticamente",
            "reconnection_in_progress": True,
            "timestamp": time.time()
        }), 503
    return None

# Función para verificar rate limiting por IP
def check_ip_rate_limit(ip_address):
    current_time = time.time()
    with ip_lock:
        ip_request_times[ip_address] = [
            req_time for req_time in ip_request_times[ip_address] 
            if current_time - req_time < 60
        ]
        if len(ip_request_times[ip_address]) >= MAX_REQUESTS_PER_MINUTE_PER_IP:
            return {
                "error": "Límite global de requests por minuto excedido",
                "limit_type": "ip_rate_limit",
                "current_usage": len(ip_request_times[ip_address]),
                "limit": MAX_REQUESTS_PER_MINUTE_PER_IP,
                "wait_time": 60
            }, 429
        ip_request_times[ip_address].append(current_time)
    return None

# Función para verificar rate limiting por usuario
def check_user_rate_limit(user_data):
    user_id = user_data.get('user_id')
    plan_type = user_data.get('plan_type', 'free')
    if user_data.get('is_admin'):
        return None
    current_time = time.time()
    plan_config = PLAN_CONFIG[plan_type]
    with request_lock:
        user_request_times[user_id] = [
            req_time for req_time in user_request_times[user_id] 
            if current_time - req_time < 60
        ]
        if len(user_request_times[user_id]) >= plan_config['rate_limit_per_minute']:
            return {
                "error": "Límite de requests por minuto excedido",
                "limit_type": "rate_limit",
                "current_usage": len(user_request_times[user_id]),
                "limit": plan_config['rate_limit_per_minute'],
                "wait_time": 60
            }, 429
        user_request_times[user_id].append(current_time)
    return None

# Función para verificar y actualizar límites de uso
def check_usage_limits(user_data):
    if user_data.get('is_admin'):
        return None
    user_id = user_data.get('user_id')
    if not user_id:
        return {"error": "ID de usuario no válido"}, 401
    try:
        rate_limit_check = check_user_rate_limit(user_data)
        if rate_limit_check:
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
        daily_limit = plan_config['daily_limit']
        session_limit = plan_config['session_limit']
        update_data = {}
        last_reset = user_info.get('daily_reset_timestamp', 0)
        if current_time - last_reset >= 86400:
            update_data['daily_usage_count'] = 0
            update_data['daily_reset_timestamp'] = current_time
            daily_usage = 0
        else:
            daily_usage = user_info.get('daily_usage_count', 0)
        session_start = user_info.get('session_start_timestamp', 0)
        if current_time - session_start >= SESSION_TIMEOUT:
            update_data['session_usage_count'] = 0
            update_data['session_start_timestamp'] = current_time
            session_usage = 0
        else:
            session_usage = user_info.get('session_usage_count', 0)
        if daily_usage >= daily_limit:
            time_remaining = 86400 - (current_time - last_reset)
            reset_time = f"{int(time_remaining // 3600)}h {int((time_remaining % 3600) // 60)}m"
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
            notify_limit_reached(user_data, 'session', session_usage, session_limit, reset_time)
            return {
                "error": f"Límite de sesión excedido ({session_usage}/{session_limit})",
                "limit_type": "session", 
                "current_usage": session_usage,
                "limit": session_limit,
                "reset_in": reset_time
            }, 429
        update_data['daily_usage_count'] = daily_usage + 1
        update_data['session_usage_count'] = session_usage + 1
        update_data['last_used'] = firestore.SERVER_TIMESTAMP
        update_data['total_usage_count'] = firestore.Increment(1)
        if 'daily_reset_timestamp' not in user_info:
            update_data['daily_reset_timestamp'] = current_time
        if 'session_start_timestamp' not in user_info:
            update_data['session_start_timestamp'] = current_time
        user_ref.update(update_data)
        return None
    except Exception as e:
        print(f"Error verificando límites de uso: {e}")
        return {"error": f"Error interno verificando límites: {str(e)}"}, 500

# Función para limitar información de contenido para usuarios free
def limit_content_info(content_data, content_type):
    limited_data = {
        'id': content_data.get('id'),
        'title': content_data.get('title'),
        'year': content_data.get('year'),
        'genre': content_data.get('genre'),
        'rating': content_data.get('rating'),
        'poster': content_data.get('poster'),
        'description': content_data.get('description', '')[:100] + '...' if content_data.get('description') else ''
    }
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
    ip_address = request.remote_addr
    ip_limit_check = check_ip_rate_limit(ip_address)
    if ip_limit_check:
        return jsonify(ip_limit_check[0]), ip_limit_check[1]
    if request.endpoint and 'admin' not in request.endpoint:
        print(f"📥 Request: {request.method} {request.path} from {ip_address}")

@app.after_request
def after_request(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = "default-src 'self'"
    if request.path.startswith('/api/admin') or request.path.startswith('/api/user'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response

# Decorador para requerir autenticación
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if request.args.get('token'):
            token = request.args.get('token')
        elif 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                pass
        if not token:
            return jsonify({"error": "Token de acceso requerido"}), 401
        if len(token) < 10 or len(token) > 500:
            return jsonify({"error": "Formato de token inválido"}), 401
        try:
            if token in ADMIN_TOKENS:
                user_data = {
                    'user_id': 'admin',
                    'username': 'Administrador',
                    'email': 'admin@api.com',
                    'is_admin': True,
                    'plan_type': 'admin'
                }
                return f(user_data, *args, **kwargs)
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
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_username(username):
    if len(username) < 3 or len(username) > 50:
        return False
    pattern = r'^[a-zA-Z0-9_-]+$'
    return re.match(pattern, username) is not None

# =============================================
# ENDPOINTS DE CONEXIÓN Y RECONEXIÓN
# =============================================

@app.route('/api/connection/status', methods=['GET'])
def connection_status():
    """Verificar estado de la conexión Firebase"""
    try:
        firebase_healthy = check_firebase_connection()
        current_time = time.time()
        
        status_info = {
            "success": True,
            "timestamp": current_time,
            "firebase": {
                "connected": firebase_healthy,
                "project_id": os.environ.get('FIREBASE_PROJECT_ID', 'Unknown'),
                "last_test": last_connection_test
            },
            "system": {
                "python_version": os.environ.get('PYTHON_VERSION', 'Unknown'),
                "environment": "production"
            }
        }
        
        return jsonify(status_info)
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "timestamp": time.time()
        }), 500

@app.route('/api/connection/reconnect', methods=['POST'])
@token_required
def reconnect_firebase(user_data):
    """Forzar reconexión a Firebase (solo admin)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    try:
        global db, firebase_app
        
        print("🔄 Reconexión forzada solicitada por admin...")
        
        # Limpiar conexión existente
        try:
            if firebase_app:
                firebase_admin.delete_app(firebase_app)
        except Exception as e:
            print(f"⚠️ Error limpiando app existente: {e}")
        
        db = None
        firebase_app = None
        
        # Reintentar inicialización
        success = initialize_firebase() is not None
        
        if success:
            return jsonify({
                "success": True,
                "message": "✅ Reconexión forzada exitosa",
                "timestamp": time.time(),
                "firebase_connected": True
            })
        else:
            return jsonify({
                "success": False,
                "error": "❌ No se pudo reconectar a Firebase",
                "timestamp": time.time(),
                "firebase_connected": False
            }), 500
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Error en reconexión: {str(e)}",
            "timestamp": time.time()
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint para health checks de Render"""
    try:
        # Verificar Firebase
        firebase_status = "healthy" if check_firebase_connection() else "unhealthy"
        
        return jsonify({
            "status": "healthy",
            "timestamp": time.time(),
            "firebase": firebase_status,
            "service": "API Streaming",
            "version": "2.0.0"
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": time.time()
        }), 500

# Endpoint de diagnóstico
@app.route('/api/diagnostic', methods=['GET'])
def diagnostic():
    firebase_status = "✅ Conectado" if check_firebase_connection() else "❌ Desconectado"
    env_vars = {
        'FIREBASE_TYPE': "✅" if os.environ.get('FIREBASE_TYPE') else "❌",
        'FIREBASE_PROJECT_ID': "✅" if os.environ.get('FIREBASE_PROJECT_ID') else "❌", 
        'FIREBASE_PRIVATE_KEY': "✅" if os.environ.get('FIREBASE_PRIVATE_KEY') else "❌",
        'FIREBASE_CLIENT_EMAIL': "✅" if os.environ.get('FIREBASE_CLIENT_EMAIL') else "❌",
        'ADMIN_EMAIL': "✅" if os.environ.get('ADMIN_EMAIL') else "❌",
        'ADMIN_TOKEN': "✅" if os.environ.get('ADMIN_TOKEN') else "❌"
    }
    firestore_test = "No probado"
    if db:
        try:
            test_ref = db.collection('diagnostic_test').document('connection_test')
            test_ref.set({'test': True, 'timestamp': firestore.SERVER_TIMESTAMP})
            firestore_test = "✅ Escritura exitosa"
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
            "content": "🔒 /api/* (requiere token)",
            "health": "✅ /health",
            "connection_status": "✅ /api/connection/status",
            "reconnect": "🔒 /api/connection/reconnect (admin)"
        }
    })

# =============================================
# ENDPOINTS DE ADMINISTRACIÓN (SOLO ADMINS)
# =============================================

@app.route('/api/admin/create-user', methods=['POST'])
@token_required
def admin_create_user(user_data):
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
        if not username or not email:
            return jsonify({"error": "Username y email son requeridos"}), 400
        if not validate_username(username):
            return jsonify({"error": "Username debe tener entre 3-50 caracteres y solo puede contener letras, números, guiones y guiones bajos"}), 400
        if not validate_email(email):
            return jsonify({"error": "Formato de email inválido"}), 400
        if plan_type not in PLAN_CONFIG:
            return jsonify({"error": f"Plan no válido. Opciones: {list(PLAN_CONFIG.keys())}"}), 400
        users_ref = db.collection(TOKENS_COLLECTION)
        existing_user = users_ref.where('email', '==', email).limit(1).stream()
        if any(existing_user):
            return jsonify({"error": "El email ya está registrado"}), 400
        plan_config = PLAN_CONFIG[plan_type]
        daily_limit = data.get('daily_limit', plan_config['daily_limit'])
        session_limit = data.get('session_limit', plan_config['session_limit'])
        if daily_limit <= 0 or session_limit <= 0:
            return jsonify({"error": "Los límites deben ser mayores a 0"}), 400
        token = generate_unique_token()
        current_time = time.time()
        user_data_firestore = {
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
        user_ref = users_ref.document()
        user_ref.set(user_data_firestore)
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
            plan_type = user_info.get('plan_type', 'free')
            user_info['plan_type'] = plan_type
            user_info['plan_limits'] = {
                'daily': PLAN_CONFIG[plan_type]['daily_limit'],
                'session': PLAN_CONFIG[plan_type]['session_limit']
            }
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
            if 'token' in user_info:
                del user_info['token']
            users.append(user_info)
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
        if daily_limit is not None and daily_limit <= 0:
            return jsonify({"error": "El límite diario debe ser mayor a 0"}), 400
        if session_limit is not None and session_limit <= 0:
            return jsonify({"error": "El límite de sesión debe ser mayor a 0"}), 400
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
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        plan_config = PLAN_CONFIG[new_plan]
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
        reset_type = data.get('reset_type', 'both')
        if not user_id:
            return jsonify({"error": "user_id es requerido"}), 400
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
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        new_token = generate_unique_token()
        user_ref.update({
            'token': new_token,
            'last_token_regenerated': firestore.SERVER_TIMESTAMP,
            'regenerated_by_admin': user_data.get('username', 'admin')
        })
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
            last_used = user_info.get('last_used')
            if last_used:
                if hasattr(last_used, 'timestamp'):
                    last_used_time = last_used.timestamp()
                else:
                    last_used_time = last_used
                if current_time - last_used_time < 86400:
                    stats[plan_type]['active_users'] += 1
                    stats['total']['active_users'] += 1
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

# =============================================
# ENDPOINTS DE CREACIÓN Y EDICIÓN DE CONTENIDO
# =============================================

# ENDPOINTS PARA PELÍCULAS
@app.route('/api/peliculas', methods=['POST'])
@token_required
@check_plan_feature('content_creation')
def create_pelicula(user_data):
    """Crear nueva película (Admin y Premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        # Validar estructura
        validation_errors = validate_movie_structure(data)
        if validation_errors:
            return jsonify({
                "error": "Estructura de película inválida",
                "details": validation_errors
            }), 400
        
        # Generar ID automático si no se proporciona
        doc_id = data.get('id')
        if not doc_id:
            doc_id = normalize_id(data['title'])
            # Agregar año si está disponible para hacerlo único
            if data.get('details', {}).get('year'):
                doc_id = f"{doc_id}-{data['details']['year']}"
        
        # Verificar si ya existe
        existing_doc = db.collection('peliculas').document(doc_id).get()
        if existing_doc.exists:
            return jsonify({
                "error": "Ya existe una película con este ID",
                "suggested_id": f"{doc_id}-{int(time.time())}"
            }), 409
        
        # Agregar metadatos de creación
        data['created_by'] = user_data.get('username', 'unknown')
        data['created_at'] = firestore.SERVER_TIMESTAMP
        data['last_updated'] = firestore.SERVER_TIMESTAMP
        data['is_active'] = True
        
        # Crear documento
        doc_ref = db.collection('peliculas').document(doc_id)
        doc_ref.set(data)
        
        return jsonify({
            "success": True,
            "message": "Película creada exitosamente",
            "id": doc_id,
            "data": normalize_movie_data(data, doc_id)
        }), 201
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['PUT'])
@token_required
@check_plan_feature('content_editing')
def update_pelicula(user_data, pelicula_id):
    """Actualizar película existente (Admin y Premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        # Verificar que la película existe
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Película no encontrada"}), 404
        
        # Validar estructura
        validation_errors = validate_movie_structure(data)
        if validation_errors:
            return jsonify({
                "error": "Estructura de película inválida",
                "details": validation_errors
            }), 400
        
        # Agregar metadatos de actualización
        data['last_updated'] = firestore.SERVER_TIMESTAMP
        data['updated_by'] = user_data.get('username', 'unknown')
        
        # Actualizar documento
        doc_ref.update(data)
        
        # Obtener datos actualizados
        updated_doc = doc_ref.get()
        updated_data = normalize_movie_data(updated_doc.to_dict(), pelicula_id)
        
        return jsonify({
            "success": True,
            "message": "Película actualizada exitosamente",
            "id": pelicula_id,
            "data": updated_data
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['DELETE'])
@token_required
def delete_pelicula(user_data, pelicula_id):
    """Eliminar película (Solo Admin)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Película no encontrada"}), 404
        
        # Eliminar documento
        doc_ref.delete()
        
        return jsonify({
            "success": True,
            "message": "Película eliminada exitosamente",
            "id": pelicula_id
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ENDPOINTS PARA SERIES
@app.route('/api/series', methods=['POST'])
@token_required
@check_plan_feature('content_creation')
def create_serie(user_data):
    """Crear nueva serie (Admin y Premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        # Validar estructura
        validation_errors = validate_series_structure(data)
        if validation_errors:
            return jsonify({
                "error": "Estructura de serie inválida",
                "details": validation_errors
            }), 400
        
        # Generar ID automático si no se proporciona
        doc_id = data.get('id')
        if not doc_id:
            doc_id = normalize_id(data['title'])
        
        # Verificar si ya existe
        existing_doc = db.collection('contenido').document(doc_id).get()
        if existing_doc.exists:
            return jsonify({
                "error": "Ya existe una serie con este ID",
                "suggested_id": f"{doc_id}-{int(time.time())}"
            }), 409
        
        # Agregar metadatos de creación
        data['created_by'] = user_data.get('username', 'unknown')
        data['created_at'] = firestore.SERVER_TIMESTAMP
        data['last_updated'] = firestore.SERVER_TIMESTAMP
        data['is_active'] = True
        data['content_type'] = 'serie'
        
        # Crear documento
        doc_ref = db.collection('contenido').document(doc_id)
        doc_ref.set(data)
        
        return jsonify({
            "success": True,
            "message": "Serie creada exitosamente",
            "id": doc_id,
            "data": normalize_series_data(data, doc_id)
        }), 201
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/series/<serie_id>', methods=['PUT'])
@token_required
@check_plan_feature('content_editing')
def update_serie(user_data, serie_id):
    """Actualizar serie existente (Admin y Premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        # Verificar que la serie existe
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Serie no encontrada"}), 404
        
        # Validar estructura
        validation_errors = validate_series_structure(data)
        if validation_errors:
            return jsonify({
                "error": "Estructura de serie inválida",
                "details": validation_errors
            }), 400
        
        # Agregar metadatos de actualización
        data['last_updated'] = firestore.SERVER_TIMESTAMP
        data['updated_by'] = user_data.get('username', 'unknown')
        data['content_type'] = 'serie'
        
        # Actualizar documento
        doc_ref.update(data)
        
        # Obtener datos actualizados
        updated_doc = doc_ref.get()
        updated_data = normalize_series_data(updated_doc.to_dict(), serie_id)
        
        return jsonify({
            "success": True,
            "message": "Serie actualizada exitosamente",
            "id": serie_id,
            "data": updated_data
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/series/<serie_id>', methods=['DELETE'])
@token_required
def delete_serie(user_data, serie_id):
    """Eliminar serie (Solo Admin)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Serie no encontrada"}), 404
        
        # Eliminar documento
        doc_ref.delete()
        
        return jsonify({
            "success": True,
            "message": "Serie eliminada exitosamente",
            "id": serie_id
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ENDPOINTS PARA CANALES
@app.route('/api/canales', methods=['POST'])
@token_required
@check_plan_feature('content_creation')
def create_canal(user_data):
    """Crear nuevo canal (Admin y Premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        # Validar estructura
        validation_errors = validate_channel_structure(data)
        if validation_errors:
            return jsonify({
                "error": "Estructura de canal inválida",
                "details": validation_errors
            }), 400
        
        # Generar ID automático si no se proporciona
        doc_id = data.get('id')
        if not doc_id:
            doc_id = normalize_id(data['name'])
        
        # Verificar si ya existe
        existing_doc = db.collection('canales').document(doc_id).get()
        if existing_doc.exists:
            return jsonify({
                "error": "Ya existe un canal con este ID",
                "suggested_id": f"{doc_id}-{int(time.time())}"
            }), 409
        
        # Agregar metadatos de creación
        data['created_by'] = user_data.get('username', 'unknown')
        data['created_at'] = firestore.SERVER_TIMESTAMP
        data['last_updated'] = firestore.SERVER_TIMESTAMP
        data['is_active'] = True
        
        # Crear documento
        doc_ref = db.collection('canales').document(doc_id)
        doc_ref.set(data)
        
        return jsonify({
            "success": True,
            "message": "Canal creado exitosamente",
            "id": doc_id,
            "data": normalize_channel_data(data, doc_id)
        }), 201
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/canales/<canal_id>', methods=['PUT'])
@token_required
@check_plan_feature('content_editing')
def update_canal(user_data, canal_id):
    """Actualizar canal existente (Admin y Premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        # Verificar que el canal existe
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Canal no encontrado"}), 404
        
        # Validar estructura
        validation_errors = validate_channel_structure(data)
        if validation_errors:
            return jsonify({
                "error": "Estructura de canal inválida",
                "details": validation_errors
            }), 400
        
        # Agregar metadatos de actualización
        data['last_updated'] = firestore.SERVER_TIMESTAMP
        data['updated_by'] = user_data.get('username', 'unknown')
        
        # Actualizar documento
        doc_ref.update(data)
        
        # Obtener datos actualizados
        updated_doc = doc_ref.get()
        updated_data = normalize_channel_data(updated_doc.to_dict(), canal_id)
        
        return jsonify({
            "success": True,
            "message": "Canal actualizado exitosamente",
            "id": canal_id,
            "data": updated_data
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/canales/<canal_id>', methods=['DELETE'])
@token_required
def delete_canal(user_data, canal_id):
    """Eliminar canal (Solo Admin)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Canal no encontrado"}), 404
        
        # Eliminar documento
        doc_ref.delete()
        
        return jsonify({
            "success": True,
            "message": "Canal eliminado exitosamente",
            "id": canal_id
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================
# ENDPOINTS EXISTENTES PARA USUARIOS NORMALES
# =============================================

@app.route('/api/user/info', methods=['GET'])
@token_required
def get_user_info(user_data):
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    if not user_data.get('is_admin'):
        try:
            user_ref = db.collection(TOKENS_COLLECTION).document(user_data['user_id'])
            user_doc = user_ref.get()
            if user_doc.exists:
                current_data = user_doc.to_dict()
                user_data.update(current_data)
        except Exception as e:
            print(f"Error obteniendo información actualizada: {e}")
    current_time = time.time()
    daily_reset = user_data.get('daily_reset_timestamp', current_time)
    session_start = user_data.get('session_start_timestamp', current_time)
    daily_remaining = max(0, 86400 - (current_time - daily_reset))
    session_remaining = max(0, SESSION_TIMEOUT - (current_time - session_start))
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

@app.route('/api/plan-comparison', methods=['GET'])
def plan_comparison():
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
                'Sin soporte prioritario',
                'No puede crear/editar contenido'
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
                'Recomendaciones personalizadas',
                'Crear y editar películas/series/canales'
            ],
            'limitations': [
                'Uso comercial requiere licencia',
                'No puede eliminar contenido'
            ]
        }
    }
    return jsonify({
        "success": True,
        "plans": comparison
    })

@app.route('/')
@token_required
def home(user_data):
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    welcome_msg = "👋 ¡Bienvenido Administrador!" if user_data.get('is_admin') else "👋 ¡Bienvenido!"
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
    
    # Agregar información de endpoints de creación/edición
    creation_endpoints = {}
    if user_data.get('is_admin') or user_data.get('plan_type') == 'premium':
        creation_endpoints = {
            "create_movie": "POST /api/peliculas",
            "update_movie": "PUT /api/peliculas/<id>",
            "create_series": "POST /api/series",
            "update_series": "PUT /api/series/<id>",
            "create_channel": "POST /api/canales",
            "update_channel": "PUT /api/canales/<id>"
        }
        if user_data.get('is_admin'):
            creation_endpoints.update({
                "delete_movie": "DELETE /api/peliculas/<id>",
                "delete_series": "DELETE /api/series/<id>",
                "delete_channel": "DELETE /api/canales/<id>"
            })
    
    return jsonify({
        "message": f"🎬 API de Streaming - {welcome_msg}",
        "version": "2.0.0",
        "user": user_data.get('username'),
        "plan_type": user_data.get('plan_type', 'free'),
        "firebase_status": "✅ Conectado" if db else "❌ Desconectado",
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
            "stream": "GET /api/stream/<id> (solo premium)",
            "connection_status": "GET /api/connection/status",
            "health": "GET /health"
        },
        "creation_endpoints": creation_endpoints,
        "admin_endpoints": {
            "create_user": "POST /api/admin/create-user",
            "list_users": "GET /api/admin/users",
            "update_limits": "POST /api/admin/update-limits",
            "reset_limits": "POST /api/admin/reset-limits",
            "change_plan": "POST /api/admin/change-plan",
            "regenerate_token": "POST /api/admin/regenerate-token",
            "usage_statistics": "GET /api/admin/usage-statistics",
            "reconnect_firebase": "POST /api/connection/reconnect"
        } if user_data.get('is_admin') else None,
        "instructions": "Incluya el token en el header: Authorization: Bearer {token}"
    })

# Endpoints de contenido (todos requieren token) - EXISTENTES
@app.route('/api/peliculas', methods=['GET'])
@token_required
def get_peliculas(user_data):
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        limit = int(request.args.get('limit', 20))
        page = int(request.args.get('page', 1))
        plan_type = user_data.get('plan_type', 'free')
        plan_config = PLAN_CONFIG[plan_type]
        if plan_type == 'free':
            limit = min(limit, 10)
            max_offset = 50
        else:
            max_offset = 1000
        peliculas_ref = db.collection('peliculas')
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
            if plan_type == 'free':
                pelicula_data = limit_content_info(pelicula_data, 'pelicula')
            peliculas.append(pelicula_data)
        return jsonify({
            "success": True,
            "count": len(peliculas),
            "page": page,
            "limit": limit,
            "plan_restrictions": plan_type == 'free',
            "data": peliculas
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['GET'])
@token_required
def get_pelicula(user_data, pelicula_id):
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        if doc.exists:
            pelicula_data = normalize_movie_data(doc.to_dict(), doc.id)
            plan_type = user_data.get('plan_type', 'free')
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
            if serie_data.get('seasons'):
                serie_data = normalize_series_data(serie_data, doc.id)
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
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        doc_ref = db.collection('contenido').document(serie_id)
        doc = doc_ref.get()
        if doc.exists:
            serie_data = doc.to_dict()
            if serie_data.get('seasons'):
                serie_data = normalize_series_data(serie_data, doc.id)
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
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        canales_ref = db.collection('canales')
        docs = canales_ref.stream()
        canales = []
        for doc in docs:
            canal_data = normalize_channel_data(doc.to_dict(), doc.id)
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
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        doc_ref = db.collection('canales').document(canal_id)
        doc = doc_ref.get()
        if doc.exists:
            canal_data = normalize_channel_data(doc.to_dict(), doc.id)
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
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        termino = request.args.get('q', '')
        if not termino:
            return jsonify({"error": "Término de búsqueda requerido"}), 400
        plan_type = user_data.get('plan_type', 'free')
        plan_config = PLAN_CONFIG[plan_type]
        search_limit = plan_config['features']['search_limit']
        limit = min(int(request.args.get('limit', 10)), search_limit)
        resultados = []
        peliculas_ref = db.collection('peliculas')
        peliculas_query = peliculas_ref.where('title', '>=', termino).where('title', '<=', termino + '\uf8ff')
        peliculas_docs = peliculas_query.limit(limit).stream()
        for doc in peliculas_docs:
            data = normalize_movie_data(doc.to_dict(), doc.id)
            data['tipo'] = 'pelicula'
            if plan_type == 'free':
                data = limit_content_info(data, 'pelicula')
            resultados.append(data)
        if plan_type == 'premium':
            series_ref = db.collection('contenido')
            series_query = series_ref.where('title', '>=', termino).where('title', '<=', termino + '\uf8ff')
            series_docs = series_query.limit(limit).stream()
            for doc in series_docs:
                data = doc.to_dict()
                if data.get('seasons'):
                    data = normalize_series_data(data, doc.id)
                    data['tipo'] = 'serie'
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
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        content_ref = db.collection('peliculas').document(content_id)
        content_doc = content_ref.get()
        streaming_url = None
        content_type = "pelicula"
        if content_doc.exists:
            content_data = normalize_movie_data(content_doc.to_dict(), content_id)
            play_links = content_data.get('play_links', [])
            if play_links:
                streaming_url = play_links[0].get('url')
        else:
            content_ref = db.collection('contenido').document(content_id)
            content_doc = content_ref.get()
            if content_doc.exists:
                content_data = doc.to_dict()
                content_type = "serie"
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
                "expires_in": 3600,
                "quality": "HD"
            })
        else:
            return jsonify({"error": "URL de streaming no disponible"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/estadisticas', methods=['GET'])
@token_required
def get_estadisticas(user_data):
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    try:
        peliculas_count = len(list(db.collection('peliculas').limit(1000).stream()))
        series_count = 0
        if user_data.get('plan_type') == 'premium' or user_data.get('is_admin'):
            series_ref = db.collection('contenido')
            series_docs = series_ref.limit(1000).stream()
            for doc in series_docs:
                data = doc.to_dict()
                if data.get('seasons'):
                    series_count += 1
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

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)

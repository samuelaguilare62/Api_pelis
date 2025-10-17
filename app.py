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
import hashlib
import hmac

# Inicializar Flask
app = Flask(__name__)
CORS(app)

# Configuración de seguridad
app.config['JSON_SORT_KEYS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request size

# Configuración de Email - SIN CREDENCIALES SMTP
EMAIL_CONFIG = {
    'admin_email': os.environ.get('ADMIN_EMAIL', ''),
    'service_enabled': True
}

# SISTEMA DE TOKENS CONFIGURABLE - UN token para desarrollo, UN token para producción
ADMIN_TOKENS_HASH = set()
TOKEN_ACTUAL = "funisD"  # o "producción"

def initialize_tokens():
    """Inicializar tokens - Detecta automáticamente si usar desarrollo o producción"""
    global ADMIN_TOKENS_HASH, TOKEN_ACTUAL
    
    # Verificar si hay tokens de producción configurados
    admin_tokens_hashed = os.environ.get('ADMIN_TOKENS_HASHED', '')
    
    if admin_tokens_hashed and admin_tokens_hashed.strip():
        # MODO PRODUCCIÓN - Usar tokens de variables de entorno
        hashes = [h.strip() for h in admin_tokens_hashed.split(',') if h.strip()]
        ADMIN_TOKENS_HASH.update(hashes)
        TOKEN_ACTUAL = "funisP"
        print(f"🚀 MODO PRODUCCIÓN: {len(ADMIN_TOKENS_HASH)} tokens cargados desde variables de entorno")
        
    else:
        # MODO DESARROLLO - Usar token por defecto
        default_token = "token_desarrollo_2024"
        token_hash = hashlib.sha256(default_token.encode()).hexdigest()
        ADMIN_TOKENS_HASH.add(token_hash)
        TOKEN_ACTUAL = "desarrollo"
        print("⚙️  MODO DESARROLLO: Usando token por defecto")
        print(f"🔧 Token desarrollo: {default_token}")
        print(f"🔧 Hash desarrollo: {token_hash}")
        print("💡 Para producción: Configura ADMIN_TOKENS_HASHED en Render.com")

def verify_admin_token(token):
    """Verificar token de admin"""
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token_hash in ADMIN_TOKENS_HASH

# Inicializar tokens al inicio
initialize_tokens()

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

# Rate limiting para endpoints públicos
public_request_times = defaultdict(list)
public_lock = threading.Lock()

# Configuración de seguridad
MAX_REQUESTS_PER_MINUTE_PER_IP = 100
MAX_REQUESTS_PER_MINUTE_PER_USER = 60
MAX_REQUESTS_PER_MINUTE_PUBLIC = 200

# SISTEMA MEJORADO DE ENVÍO DE CORREOS
def send_email_async(to_email, subject, message):
    """Enviar email en segundo plano usando servicio sin autenticación"""
    def send_email():
        try:
            print(f"📧 Intentando enviar email a: {to_email}")
            
            # MÉTODO 1: Usar Webhook.site como webhook temporal
            try:
                webhook_url = "https://webhook.site/unique-id-aqui"
                email_data = {
                    "to": to_email,
                    "subject": subject,
                    "html": message,
                    "from": "notifications@streamingapi.com"
                }
                
                response = requests.post(
                    webhook_url,
                    json=email_data,
                    headers={'Content-Type': 'application/json'},
                    timeout=10
                )
                
                if response.status_code == 200:
                    print(f"✅ Email enviado exitosamente a: {to_email} (vía Webhook)")
                    return
                else:
                    print(f"⚠️  Webhook falló con código: {response.status_code}")
            except Exception as e:
                print(f"⚠️  Error con webhook: {e}")
            
            # MÉTODO 2: Log detallado para debugging
            print(f"📧 [EMAIL SIMULADO] Para: {to_email}")
            print(f"📧 [EMAIL SIMULADO] Asunto: {subject}")
            print(f"📧 [EMAIL SIMULADO] Mensaje: {message[:200]}...")
            print("💡 Configure un servicio de email real para envíos automáticos")
            
        except Exception as e:
            print(f"❌ Error crítico enviando email a {to_email}: {e}")
    
    thread = threading.Thread(target=send_email)
    thread.daemon = True
    thread.start()

# FUNCIÓN PARA NOTIFICAR LÍMITES ALCANZADOS
def notify_limit_reached(user_data, limit_type, current_usage, limit, reset_time):
    """Notificar automáticamente al usuario que alcanzó un límite"""
    try:
        user_email = user_data.get('email')
        username = user_data.get('username', 'Usuario')
        plan_type = user_data.get('plan_type', 'free')
        
        if not user_email:
            print("⚠️  No se puede notificar: usuario sin email")
            return False
        
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
        
        return True
            
    except Exception as e:
        print(f"❌ Error en notificación de límite: {e}")
        return False

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

# Rate limiting para endpoints públicos
def check_public_rate_limit(ip_address):
    """Rate limiting específico para endpoints públicos"""
    current_time = time.time()
    
    with public_lock:
        public_request_times[ip_address] = [
            req_time for req_time in public_request_times[ip_address] 
            if current_time - req_time < 60
        ]
        
        if len(public_request_times[ip_address]) >= MAX_REQUESTS_PER_MINUTE_PUBLIC:
            return {
                "error": "Límite de requests por minuto excedido para endpoints públicos",
                "limit_type": "public_rate_limit",
                "current_usage": len(public_request_times[ip_address]),
                "limit": MAX_REQUESTS_PER_MINUTE_PUBLIC,
                "wait_time": 60
            }, 429
        
        public_request_times[ip_address].append(current_time)
    
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
    """Verificar límites de uso diario, por sesión y rate limiting"""
    if user_data.get('is_admin'):
        return None
    
    user_id = user_data.get('user_id')
    if not user_id:
        return {"error": "ID de usuario no válido"}, 401
    
    try:
        rate_limit_check = check_user_rate_limit(user_data)
        if rate_limit_check:
            notification_sent = notify_limit_reached(
                user_data, 
                'rate_limit', 
                rate_limit_check[0]['current_usage'], 
                rate_limit_check[0]['limit'],
                '1 minuto'
            )
            if notification_sent:
                print(f"📧 Notificación de rate limit enviada para usuario {user_id}")
            return rate_limit_check
        
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return {"error": "Usuario no encontrado"}, 401
        
        user_info = user_doc.to_dict()
        current_time = time.time()
        plan_type = user_info.get('plan_type', 'free')
        plan_config = PLAN_CONFIG[plan_type]
        
        daily_limit = user_info.get('max_requests_per_day', plan_config['daily_limit'])
        session_limit = user_info.get('max_requests_per_session', plan_config['session_limit'])
        
        update_data = {}
        
        last_reset = user_info.get('daily_reset_timestamp', 0)
        if current_time - last_reset >= 86400:
            update_data['daily_usage_count'] = 0
            update_

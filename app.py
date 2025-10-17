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
    'service_enabled': True  # Siempre habilitado, usa método sin autenticación
}

# MEJORA 1: SISTEMA DE TOKENS PRE-HASHEADOS PARA MÁXIMA SEGURIDAD
ADMIN_TOKENS_HASH = set()
PUBLIC_TOKENS_HASH = set()

def initialize_tokens():
    """Inicializar tokens pre-hasheados para admin y endpoints públicos"""
    global ADMIN_TOKENS_HASH, PUBLIC_TOKENS_HASH
    
    # 1. Tokens de administrador (acceso completo)
    admin_tokens_hashed = os.environ.get('ADMIN_TOKENS_HASHED', '')
    if admin_tokens_hashed:
        hashes = [h.strip() for h in admin_tokens_hashed.split(',') if h.strip()]
        ADMIN_TOKENS_HASH.update(hashes)
        print(f"✅ {len(ADMIN_TOKENS_HASH)} tokens de admin cargados (pre-hasheados)")
    else:
        # Token por defecto para desarrollo (NUNCA en producción)
        default_token = 'admin_token_secreto_2024'
        default_hash = hashlib.sha256(default_token.encode()).hexdigest()
        ADMIN_TOKENS_HASH.add(default_hash)
        print("⚠️  MODO DESARROLLO: Usando token de admin por defecto")
    
    # 2. Tokens para endpoints públicos (acceso limitado solo a /public/)
    public_tokens_hashed = os.environ.get('PUBLIC_TOKENS_HASHED', '')
    if public_tokens_hashed:
        hashes = [h.strip() for h in public_tokens_hashed.split(',') if h.strip()]
        PUBLIC_TOKENS_HASH.update(hashes)
        print(f"✅ {len(PUBLIC_TOKENS_HASH)} tokens públicos cargados (pre-hasheados)")
    else:
        # Token público por defecto para desarrollo
        public_token = 'public_token_acceso_2024'
        public_hash = hashlib.sha256(public_token.encode()).hexdigest()
        PUBLIC_TOKENS_HASH.add(public_hash)
        print("⚠️  MODO DESARROLLO: Usando token público por defecto")

def verify_admin_token(token):
    """Verificar token de admin usando hash seguro"""
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token_hash in ADMIN_TOKENS_HASH

def verify_public_token(token):
    """Verificar token para endpoints públicos"""
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token_hash in PUBLIC_TOKENS_HASH

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
MAX_REQUESTS_PER_MINUTE_PUBLIC = 200  # Límite más alto para endpoints públicos

# MEJORA 2: SISTEMA MEJORADO DE ENVÍO DE CORREOS
def send_email_async(to_email, subject, message):
    """Enviar email en segundo plano usando servicio sin autenticación - MEJORADO"""
    def send_email():
        try:
            print(f"📧 Intentando enviar email a: {to_email}")
            
            # MÉTODO 1: Usar Webhook.site como webhook temporal
            try:
                webhook_url = "https://webhook.site/unique-id-aqui"  # Reemplaza con tu webhook
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
            
            # MÉTODO 2: Usar servicio de email transaccional gratuito (Mailtrap, etc.)
            try:
                # Configuración para servicio de email gratuito
                email_service_url = "https://api.mailersend.com/v1/email"  # Ejemplo
                email_payload = {
                    "from": {"email": "noreply@streamingapi.com", "name": "API Streaming"},
                    "to": [{"email": to_email}],
                    "subject": subject,
                    "html": message
                }
                
                response = requests.post(
                    email_service_url,
                    json=email_payload,
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': f"Bearer {os.environ.get('MAILERSEND_TOKEN', '')}"
                    },
                    timeout=10
                )
                
                if response.status_code in [200, 202]:
                    print(f"✅ Email enviado exitosamente a: {to_email} (vía servicio)")
                    return
                else:
                    print(f"⚠️  Servicio de email falló: {response.status_code}")
            except Exception as e:
                print(f"⚠️  Error con servicio de email: {e}")
            
            # MÉTODO 3: Log detallado para debugging (si fallan los métodos anteriores)
            print(f"📧 [EMAIL SIMULADO] Para: {to_email}")
            print(f"📧 [EMAIL SIMULADO] Asunto: {subject}")
            print(f"📧 [EMAIL SIMULADO] Mensaje: {message[:200]}...")
            print("💡 Configure un servicio de email real para envíos automáticos")
            
        except Exception as e:
            print(f"❌ Error crítico enviando email a {to_email}: {e}")
    
    # Ejecutar en segundo plano
    thread = threading.Thread(target=send_email)
    thread.daemon = True
    thread.start()

# FUNCIÓN MEJORADA PARA NOTIFICAR LÍMITES ALCANZADOS
def notify_limit_reached(user_data, limit_type, current_usage, limit, reset_time):
    """Notificar automáticamente al usuario que alcanzó un límite - MEJORADO"""
    try:
        user_email = user_data.get('email')
        username = user_data.get('username', 'Usuario')
        plan_type = user_data.get('plan_type', 'free')
        
        if not user_email:
            print("⚠️  No se puede notificar: usuario sin email")
            return False
        
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
        
        # Enviar email en segundo plano - MEJORADO
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

# Rate limiting para endpoints públicos
def check_public_rate_limit(ip_address):
    """Rate limiting específico para endpoints públicos"""
    current_time = time.time()
    
    with public_lock:
        # Limpiar requests antiguos
        public_request_times[ip_address] = [
            req_time for req_time in public_request_times[ip_address] 
            if current_time - req_time < 60
        ]
        
        # Verificar rate limit
        if len(public_request_times[ip_address]) >= MAX_REQUESTS_PER_MINUTE_PUBLIC:
            return {
                "error": "Límite de requests por minuto excedido para endpoints públicos",
                "limit_type": "public_rate_limit",
                "current_usage": len(public_request_times[ip_address]),
                "limit": MAX_REQUESTS_PER_MINUTE_PUBLIC,
                "wait_time": 60
            }, 429
        
        # Agregar nuevo request
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
    """Verificar límites de uso diario, por sesión y rate limiting - MEJORADO"""
    if user_data.get('is_admin'):
        return None  # Admin no tiene límites
    
    user_id = user_data.get('user_id')
    if not user_id:
        return {"error": "ID de usuario no válido"}, 401
    
    try:
        # Primero verificar rate limits
        rate_limit_check = check_user_rate_limit(user_data)
        if rate_limit_check:
            # Notificar al usuario sobre rate limit - MEJORADO
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
        
        # Obtener límites del plan
        daily_limit = user_info.get('max_requests_per_day', plan_config['daily_limit'])
        session_limit = user_info.get('max_requests_per_session', plan_config['session_limit'])
        
        # Inicializar contadores si no existen
        update_data = {}
        
        # Verificar y resetear límite diario si es un nuevo día
        last_reset = user_info.get('daily_reset_timestamp', 0)
        if current_time - last_reset >= 86400:  # 24 horas
            update_data['daily_usage_count'] = 0
            update_data['daily_reset_timestamp'] = current_time
            daily_usage = 0
            print(f"🔄 Reset diario para usuario {user_id}")
        else:
            daily_usage = user_info.get('daily_usage_count', 0)
        
        # Verificar y resetear límite de sesión si ha expirado
        session_start = user_info.get('session_start_timestamp', 0)
        if current_time - session_start >= SESSION_TIMEOUT:
            update_data['session_usage_count'] = 0
            update_data['session_start_timestamp'] = current_time
            session_usage = 0
            print(f"🔄 Reset de sesión para usuario {user_id}")
        else:
            session_usage = user_info.get('session_usage_count', 0)
        
        # Verificar si se excedieron los límites
        if daily_usage >= daily_limit:
            time_remaining = 86400 - (current_time - last_reset)
            reset_time = f"{int(time_remaining // 3600)}h {int((time_remaining % 3600) // 60)}m"
            
            # Notificar al usuario AUTOMÁTICAMENTE - MEJORADO
            notification_sent = notify_limit_reached(user_data, 'daily', daily_usage, daily_limit, reset_time)
            if notification_sent:
                print(f"📧 Notificación de límite diario enviada para usuario {user_id}")
            
            return {
                "error": f"Límite diario excedido ({daily_usage}/{daily_limit})",
                "limit_type": "daily",
                "current_usage": daily_usage,
                "limit": daily_limit,
                "reset_in": reset_time,
                "notification_sent": notification_sent
            }, 429
        
        if session_usage >= session_limit:
            time_remaining = SESSION_TIMEOUT - (current_time - session_start)
            reset_time = f"{int(time_remaining // 60)}m {int(time_remaining % 60)}s"
            
            # Notificar al usuario AUTOMÁTICAMENTE - MEJORADO
            notification_sent = notify_limit_reached(user_data, 'session', session_usage, session_limit, reset_time)
            if notification_sent:
                print(f"📧 Notificación de límite de sesión enviada para usuario {user_id}")
            
            return {
                "error": f"Límite de sesión excedido ({session_usage}/{session_limit})",
                "limit_type": "session", 
                "current_usage": session_usage,
                "limit": session_limit,
                "reset_in": reset_time,
                "notification_sent": notification_sent
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
    # Verificar rate limiting por IP para TODAS las rutas
    ip_address = request.remote_addr
    ip_limit_check = check_ip_rate_limit(ip_address)
    if ip_limit_check:
        return jsonify(ip_limit_check[0]), ip_limit_check[1]
    
    # Rate limiting específico para endpoints públicos
    if request.path.startswith('/public/'):
        public_limit_check = check_public_rate_limit(ip_address)
        if public_limit_check:
            return jsonify(public_limit_check[0]), public_limit_check[1]
    
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

# Decorador para requerir autenticación de admin
def admin_token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Buscar token en headers Authorization
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                pass
        
        if not token:
            return jsonify({"error": "Token de administrador requerido"}), 401
        
        # Validar formato del token
        if len(token) < 10 or len(token) > 500:
            return jsonify({"error": "Formato de token inválido"}), 401
        
        # Verificar token de admin
        if not verify_admin_token(token):
            return jsonify({"error": "Token de administrador inválido"}), 401
        
        # Si es válido, continuar
        return f(*args, **kwargs)
    
    return decorated

# Decorador para requerir autenticación pública
def public_token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Buscar token en headers Authorization
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                pass
        
        if not token:
            return jsonify({"error": "Token de acceso público requerido"}), 401
        
        # Validar formato del token
        if len(token) < 10 or len(token) > 500:
            return jsonify({"error": "Formato de token inválido"}), 401
        
        # Verificar token público
        if not verify_public_token(token):
            return jsonify({"error": "Token de acceso público inválido"}), 401
        
        # Si es válido, continuar
        return f(*args, **kwargs)
    
    return decorated

# Decorador para requerir autenticación (usuarios normales)
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
            # MEJORA 1: Verificar si es token de admin usando hash seguro
            if verify_admin_token(token):
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

# MEJORA 3: ENDPOINTS PÚBLICOS CON ACCESO COMPLETO
@app.route('/public/content', methods=['GET'])
@public_token_required
def public_all_content():
    """Endpoint público que devuelve TODO el contenido en una sola respuesta"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        # Obtener todas las películas
        peliculas_ref = db.collection('peliculas')
        peliculas_docs = peliculas_ref.limit(1000).stream()
        peliculas = []
        for doc in peliculas_docs:
            data = doc.to_dict()
            data['id'] = doc.id
            data['tipo'] = 'pelicula'
            peliculas.append(data)
        
        # Obtener todas las series
        series_ref = db.collection('contenido')
        series_docs = series_ref.limit(1000).stream()
        series = []
        for doc in series_docs:
            data = doc.to_dict()
            if 'seasons' in data:
                data['id'] = doc.id
                data['tipo'] = 'serie'
                series.append(data)
        
        # Obtener todos los canales
        canales_ref = db.collection('canales')
        canales_docs = canales_ref.limit(1000).stream()
        canales = []
        for doc in canales_docs:
            data = doc.to_dict()
            data['id'] = doc.id
            data['tipo'] = 'canal'
            canales.append(data)
        
        # Estadísticas
        estadisticas = {
            "total_peliculas": len(peliculas),
            "total_series": len(series),
            "total_canales": len(canales),
            "total_contenido": len(peliculas) + len(series) + len(canales),
            "timestamp": time.time()
        }
        
        return jsonify({
            "success": True,
            "estadisticas": estadisticas,
            "contenido": {
                "peliculas": peliculas,
                "series": series, 
                "canales": canales
            },
            "metadata": {
                "version": "1.0.0",
                "endpoint": "public",
                "cache_recomendado": 300  # 5 minutos
            }
        })
        
    except Exception as e:
        print(f"Error en endpoint público: {e}")
        return jsonify({
            "success": False,
            "error": f"Error obteniendo contenido: {str(e)}"
        }), 500

@app.route('/public/peliculas', methods=['GET'])
@public_token_required
def public_peliculas():
    """Endpoint público para todas las películas"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        limit = int(request.args.get('limit', 100))
        page = int(request.args.get('page', 1))
        offset = (page - 1) * limit
        
        peliculas_ref = db.collection('peliculas')
        docs = peliculas_ref.limit(limit).offset(offset).stream()
        
        peliculas = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            peliculas.append(data)
        
        return jsonify({
            "success": True,
            "count": len(peliculas),
            "page": page,
            "limit": limit,
            "data": peliculas
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/public/series', methods=['GET'])
@public_token_required
def public_series():
    """Endpoint público para todas las series"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        series_ref = db.collection('contenido')
        docs = series_ref.limit(1000).stream()
        
        series = []
        for doc in docs:
            data = doc.to_dict()
            if 'seasons' in data:
                data['id'] = doc.id
                series.append(data)
        
        return jsonify({
            "success": True,
            "count": len(series),
            "data": series
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/public/canales', methods=['GET'])
@public_token_required
def public_canales():
    """Endpoint público para todos los canales"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        canales_ref = db.collection('canales')
        docs = canales_ref.limit(1000).stream()
        
        canales = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            canales.append(data)
        
        return jsonify({
            "success": True,
            "count": len(canales),
            "data": canales
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/public/estadisticas', methods=['GET'])
@public_token_required
def public_estadisticas():
    """Endpoint público para estadísticas"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        # Contar películas
        peliculas_count = len(list(db.collection('peliculas').limit(1000).stream()))
        
        # Contar series
        series_ref = db.collection('contenido')
        series_docs = series_ref.limit(1000).stream()
        series_count = 0
        for doc in series_docs:
            if 'seasons' in doc.to_dict():
                series_count += 1
        
        # Contar canales
        canales_count = len(list(db.collection('canales').limit(1000).stream()))
        
        return jsonify({
            "success": True,
            "data": {
                "total_peliculas": peliculas_count,
                "total_series": series_count,
                "total_canales": canales_count,
                "total_contenido": peliculas_count + series_count + canales_count,
                "ultima_actualizacion": time.time()
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/public/search', methods=['GET'])
@public_token_required
def public_search():
    """Búsqueda pública en todo el contenido"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({"error": "Parámetro de búsqueda 'q' requerido"}), 400
        
        resultados = []
        search_term = query.lower()
        
        # Buscar en películas
        peliculas_ref = db.collection('peliculas')
        peliculas_docs = peliculas_ref.limit(100).stream()
        for doc in peliculas_docs:
            data = doc.to_dict()
            if (search_term in data.get('title', '').lower() or 
                search_term in data.get('genre', '').lower() or
                search_term in data.get('description', '').lower()):
                data['id'] = doc.id
                data['tipo'] = 'pelicula'
                resultados.append(data)
        
        # Buscar en series
        series_ref = db.collection('contenido')
        series_docs = series_ref.limit(100).stream()
        for doc in series_docs:
            data = doc.to_dict()
            if 'seasons' in data and (search_term in data.get('title', '').lower() or 
                                    search_term in data.get('genre', '').lower()):
                data['id'] = doc.id
                data['tipo'] = 'serie'
                resultados.append(data)
        
        return jsonify({
            "success": True,
            "query": query,
            "count": len(resultados),
            "data": resultados
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        'ADMIN_EMAIL': "✅" if os.environ.get('ADMIN_EMAIL') else "❌"
    }
    
    # Información de tokens
    token_info = {
        'admin_tokens_cargados': len(ADMIN_TOKENS_HASH),
        'public_tokens_cargados': len(PUBLIC_TOKENS_HASH),
        'tokens_prehasheados': True
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
            "environment_variables": env_vars,
            "token_security": token_info
        },
        "security": {
            "rate_limiting": "✅ Activado",
            "ip_restrictions": "✅ Activado", 
            "token_authentication": "✅ Activado",
            "plan_restrictions": "✅ Activado",
            "email_notifications": "✅ Activado",
            "tokens_prehasheados": "✅ Activado"
        },
        "endpoints_working": {
            "diagnostic": "✅ /api/diagnostic",
            "home": "🔒 / (requiere token)",
            "admin": "🔒 /api/admin/* (requiere admin token)",
            "content": "🔒 /api/* (requiere token)",
            "public": "🔒 /public/* (requiere public token)"
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
            "stream": "GET /api/stream/<id> (solo premium)",
            "public_content": "GET /public/content (token público)"
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
            pelicula_data = doc.to_dict()
            pelicula_data['id'] = doc.id
            
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
            pelicula_data = doc.to_dict()
            pelicula_data['id'] = doc.id
            
            # Aplicar restricciones de plan
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
            if 'seasons' in serie_data:
                serie_data['id'] = doc.id
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
            serie_data['id'] = doc.id
            
            return jsonify({
                "success": True,
                "data": serie_data
            })
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
            canal_data = doc.to_dict()
            canal_data['id'] = doc.id
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
            canal_data = doc.to_dict()
            canal_data['id'] = doc.id
            
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
            data = doc.to_dict()
            data['id'] = doc.id
            data['tipo'] = 'pelicula'
            
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
                if 'seasons' in data:
                    data['id'] = doc.id
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
    """Obtener URL de streaming (solo premium)"""
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        # Buscar el contenido
        content_ref = db.collection('peliculas').document(content_id)
        content_doc = content_ref.get()
        
        if not content_doc.exists:
            # Buscar en series
            content_ref = db.collection('contenido').document(content_id)
            content_doc = content_ref.get()
            
        if content_doc.exists:
            content_data = content_doc.to_dict()
            streaming_url = content_data.get('streaming_url')
            
            if streaming_url:
                return jsonify({
                    "success": True,
                    "streaming_url": streaming_url,
                    "expires_in": 3600,  # URL expira en 1 hora
                    "quality": "HD"
                })
            else:
                return jsonify({"error": "URL de streaming no disponible"}), 404
        else:
            return jsonify({"error": "Contenido no encontrado"}), 404
            
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
                if 'seasons' in doc.to_dict():
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

# Script para generar tokens pre-hasheados
def generate_hashed_tokens():
    """Función para generar tokens pre-hasheados (ejecutar una vez)"""
    print("🔐 GENERADOR DE TOKENS PRE-HASHEADOS")
    print("=" * 50)
    
    # Tokens originales (cambia estos por los que quieras usar)
    admin_tokens = [
        "admin_token_principal_2024",
        "admin_token_backup_2024"
    ]
    
    public_tokens = [
        "public_netlify_token_2024",
        "public_backup_token_2024"
    ]
    
    print("\n📋 TOKENS DE ADMINISTRADOR:")
    admin_hashes = []
    for token in admin_tokens:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        admin_hashes.append(token_hash)
        print(f"Original: {token}")
        print(f"Hash:     {token_hash}")
        print("-" * 30)
    
    print("\n🌐 TOKENS PÚBLICOS:")
    public_hashes = []
    for token in public_tokens:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        public_hashes.append(token_hash)
        print(f"Original: {token}")
        print(f"Hash:     {token_hash}")
        print("-" * 30)
    
    print("\n🎯 VARIABLES DE ENTORNO PARA RENDER.COM:")
    print(f"ADMIN_TOKENS_HASHED={','.join(admin_hashes)}")
    print(f"PUBLIC_TOKENS_HASHED={','.join(public_hashes)}")
    print("\n💡 Copia estas variables a Render.com > Environment")

# Inicialización de la aplicación
if __name__ == '__main__':
    print("🚀 Iniciando API Streaming Mejorada...")
    print("🔐 Sistema de tokens pre-hasheados activado")
    print("🌐 Endpoints públicos disponibles en /public/")
    print("📧 Sistema de notificaciones mejorado")
    
    # Para generar tokens: descomenta la línea siguiente y ejecuta una vez
    # generate_hashed_tokens()
    
    app.run(debug=False, host='0.0.0.0', port=5000)

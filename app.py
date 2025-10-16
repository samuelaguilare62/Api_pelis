from flask import Flask, jsonify, request
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import os
import secrets
from functools import wraps
from datetime import datetime, timedelta
import time

# Inicializar Flask
app = Flask(__name__)
CORS(app)

# Configuración de Firebase - MÉTODO CON ARCHIVO JSON
def initialize_firebase():
    try:
        # Método 1: Buscar archivo JSON en el repositorio
        if os.path.exists('service-account-key.json'):
            cred = credentials.Certificate('service-account-key.json')
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            print("✅ Firebase inicializado CON ARCHIVO JSON")
            return db
        
        # Método 2: Application Default (fallback)
        else:
            print("⚠️  Archivo JSON no encontrado, usando Application Default")
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {
                'projectId': 'phdt-b9b2c'
            })
            db = firestore.client()
            print("✅ Firebase inicializado con Application Default")
            return db
            
    except Exception as e:
        print(f"❌ Error crítico inicializando Firebase: {e}")
        print("📁 Archivos en directorio:", os.listdir('.'))
        return None

# Inicializar Firebase
db = initialize_firebase()

# Configuración de administrador
ADMIN_TOKENS = ["admin_token_secreto_2024"]

# Colección para almacenar usuarios y tokens
TOKENS_COLLECTION = "api_users"

# Límites por defecto para usuarios normales
DEFAULT_DAILY_LIMIT = 1000
DEFAULT_SESSION_LIMIT = 100
SESSION_TIMEOUT = 3600  # 1 hora en segundos

# Decorador para verificar Firebase
def check_firebase():
    if not db:
        return jsonify({
            "success": False,
            "error": "Firebase no inicializado",
            "solution": [
                "1. Agrega service-account-key.json al repositorio",
                "2. Verifica que el proyecto Firebase exista",
                "3. Revisa los permisos de Firestore"
            ]
        }), 500
    return None

# Función para verificar y actualizar límites de uso
def check_usage_limits(user_data):
    """Verificar límites de uso diario y por sesión"""
    if user_data.get('is_admin'):
        return None  # Admin no tiene límites
    
    user_id = user_data.get('user_id')
    if not user_id:
        return {"error": "ID de usuario no válido"}, 401
    
    try:
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return {"error": "Usuario no encontrado"}, 401
        
        user_info = user_doc.to_dict()
        current_time = time.time()
        
        # Obtener límites del usuario
        daily_limit = user_info.get('max_requests_per_day', DEFAULT_DAILY_LIMIT)
        session_limit = user_info.get('max_requests_per_session', DEFAULT_SESSION_LIMIT)
        
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
            return {
                "error": f"Límite diario excedido ({daily_usage}/{daily_limit}). Espere hasta mañana.",
                "limit_type": "daily",
                "current_usage": daily_usage,
                "limit": daily_limit
            }, 429
        
        if session_usage >= session_limit:
            return {
                "error": f"Límite de sesión excedido ({session_usage}/{session_limit}). Espere 1 hora.",
                "limit_type": "session", 
                "current_usage": session_usage,
                "limit": session_limit
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

# Decorador para requerir autenticación
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Verificar si el token está en el header
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                return jsonify({"error": "Formato de token inválido. Use: Bearer {token}"}), 401
        
        if not token:
            return jsonify({"error": "Token de acceso requerido"}), 401
        
        try:
            # Verificar si es token de admin
            if token in ADMIN_TOKENS:
                user_data = {
                    'user_id': 'admin',
                    'username': 'Administrador',
                    'email': 'admin@api.com',
                    'is_admin': True
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
            return jsonify({"error": f"Error al verificar token: {str(e)}"}), 500
    
    return decorated

# Generar token único
def generate_unique_token():
    return secrets.token_urlsafe(32)

# Endpoint de diagnóstico
@app.route('/api/diagnostic', methods=['GET'])
def diagnostic():
    """Endpoint de diagnóstico del sistema"""
    firebase_status = "✅ Conectado" if db else "❌ Desconectado"
    files = os.listdir('.')
    has_json = 'service-account-key.json' in files
    
    return jsonify({
        "success": True,
        "system": {
            "firebase_status": firebase_status,
            "project_id": "phdt-b9b2c",
            "files_in_directory": files,
            "has_service_account": has_json
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
    """Crear nuevo usuario (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    # Verificar Firebase
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Datos JSON requeridos"}), 400
        
        username = data.get('username')
        email = data.get('email')
        daily_limit = data.get('daily_limit', DEFAULT_DAILY_LIMIT)
        session_limit = data.get('session_limit', DEFAULT_SESSION_LIMIT)
        
        if not username or not email:
            return jsonify({"error": "Username y email son requeridos"}), 400
        
        # Verificar si el usuario ya existe
        users_ref = db.collection(TOKENS_COLLECTION)
        existing_user = users_ref.where('email', '==', email).limit(1).stream()
        
        if any(existing_user):
            return jsonify({"error": "El email ya está registrado"}), 400
        
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
            'created_at': firestore.SERVER_TIMESTAMP,
            'last_used': None,
            'total_usage_count': 0,
            'daily_usage_count': 0,
            'session_usage_count': 0,
            'daily_reset_timestamp': current_time,
            'session_start_timestamp': current_time,
            'max_requests_per_day': daily_limit,
            'max_requests_per_session': session_limit
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
                "limits": {
                    "daily": daily_limit,
                    "session": session_limit
                }
            },
            "instructions": {
                "usage": "Use el token en el header: Authorization: Bearer {token}",
                "endpoints": "Todos los endpoints requieren token",
                "limits": f"Límites: {daily_limit} requests por día, {session_limit} por sesión"
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users', methods=['GET'])
@token_required
def admin_get_users(user_data):
    """Obtener lista de usuarios (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    # Verificar Firebase
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
                'daily_limit': user_info.get('max_requests_per_day', DEFAULT_DAILY_LIMIT),
                'session_limit': user_info.get('max_requests_per_session', DEFAULT_SESSION_LIMIT)
            }
            
            # No mostrar el token por seguridad
            if 'token' in user_info:
                del user_info['token']
            users.append(user_info)
        
        return jsonify({
            "success": True,
            "count": len(users),
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
    
    # Verificar Firebase
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
        
        # Verificar si el usuario existe
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "Usuario no encontrado"}), 404
        
        update_data = {}
        if daily_limit is not None:
            if daily_limit <= 0:
                return jsonify({"error": "El límite diario debe ser mayor a 0"}), 400
            update_data['max_requests_per_day'] = daily_limit
        
        if session_limit is not None:
            if session_limit <= 0:
                return jsonify({"error": "El límite de sesión debe ser mayor a 0"}), 400
            update_data['max_requests_per_session'] = session_limit
        
        user_ref.update(update_data)
        
        return jsonify({
            "success": True,
            "message": "Límites actualizados exitosamente",
            "updated_limits": update_data
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/reset-limits', methods=['POST'])
@token_required
def admin_reset_limits(user_data):
    """Resetear contadores de uso de un usuario (solo administradores)"""
    if not user_data.get('is_admin'):
        return jsonify({"error": "Se requieren privilegios de administrador"}), 403
    
    # Verificar Firebase
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

# Endpoints para usuarios normales

@app.route('/api/user/info', methods=['GET'])
@token_required
def get_user_info(user_data):
    """Obtener información del usuario actual"""
    # Verificar Firebase
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    # Para usuarios normales, obtener información actualizada de límites
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
    
    user_response = {
        "user_id": user_data.get('user_id'),
        "username": user_data.get('username'),
        "email": user_data.get('email'),
        "active": user_data.get('active', True),
        "is_admin": user_data.get('is_admin', False),
        "created_at": user_data.get('created_at'),
        "usage_stats": {
            "total": user_data.get('total_usage_count', 0),
            "daily": user_data.get('daily_usage_count', 0),
            "session": user_data.get('session_usage_count', 0),
            "daily_limit": user_data.get('max_requests_per_day', DEFAULT_DAILY_LIMIT),
            "session_limit": user_data.get('max_requests_per_session', DEFAULT_SESSION_LIMIT),
            "daily_reset_in": f"{int(daily_remaining // 3600)}h {int((daily_remaining % 3600) // 60)}m",
            "session_reset_in": f"{int(session_remaining // 60)}m {int(session_remaining % 60)}s"
        }
    }
    
    return jsonify({
        "success": True,
        "user": user_response
    })

@app.route('/api/user/regenerate-token', methods=['POST'])
@token_required
def regenerate_token(user_data):
    """Generar un nuevo token para el usuario"""
    if user_data.get('is_admin'):
        return jsonify({"error": "Los administradores no pueden regenerar tokens"}), 400
    
    # Verificar Firebase
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        user_id = user_data['user_id']
        
        # Generar nuevo token
        new_token = generate_unique_token()
        
        # Actualizar en Firestore
        user_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        user_ref.update({
            'token': new_token,
            'last_updated': firestore.SERVER_TIMESTAMP
        })
        
        return jsonify({
            "success": True,
            "message": "Token regenerado exitosamente",
            "new_token": new_token,
            "warning": "⚠️ El token anterior ya no es válido. Actualice sus aplicaciones."
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Endpoint principal (requiere token)
@app.route('/')
@token_required
def home(user_data):
    """Endpoint principal con información de la API"""
    # Verificar Firebase
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
                daily_limit = current_data.get('max_requests_per_day', DEFAULT_DAILY_LIMIT)
                session_limit = current_data.get('max_requests_per_session', DEFAULT_SESSION_LIMIT)
                
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
        "version": "1.0.0",
        "user": user_data.get('username'),
        "firebase_status": "✅ Conectado",
        "usage_limits": limits_info,
        "endpoints_available": {
            "user_info": "GET /api/user/info",
            "regenerate_token": "POST /api/user/regenerate-token",
            "peliculas": "GET /api/peliculas",
            "pelicula_especifica": "GET /api/peliculas/<id>",
            "series": "GET /api/series", 
            "serie_especifica": "GET /api/series/<id>",
            "canales": "GET /api/canales",
            "canal_especifico": "GET /api/canales/<id>",
            "buscar": "GET /api/buscar?q=<termino>",
            "estadisticas": "GET /api/estadisticas"
        },
        "admin_endpoints": {
            "create_user": "POST /api/admin/create-user",
            "list_users": "GET /api/admin/users",
            "update_limits": "POST /api/admin/update-limits",
            "reset_limits": "POST /api/admin/reset-limits"
        } if user_data.get('is_admin') else None,
        "instructions": "Incluya el token en el header: Authorization: Bearer {token}"
    })

# Endpoints de contenido (todos requieren token)

@app.route('/api/peliculas', methods=['GET'])
@token_required
def get_peliculas(user_data):
    """Obtener todas las películas con paginación"""
    # Verificar Firebase
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        limit = int(request.args.get('limit', 20))
        page = int(request.args.get('page', 1))
        
        peliculas_ref = db.collection('peliculas')
        docs = peliculas_ref.limit(limit).stream()
        
        peliculas = []
        for doc in docs:
            pelicula_data = doc.to_dict()
            pelicula_data['id'] = doc.id
            peliculas.append(pelicula_data)
        
        return jsonify({
            "success": True,
            "count": len(peliculas),
            "page": page,
            "limit": limit,
            "data": peliculas
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/peliculas/<pelicula_id>', methods=['GET'])
@token_required
def get_pelicula(user_data, pelicula_id):
    """Obtener una película específica por ID"""
    # Verificar Firebase
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        doc_ref = db.collection('peliculas').document(pelicula_id)
        doc = doc_ref.get()
        
        if doc.exists:
            pelicula_data = doc.to_dict()
            pelicula_data['id'] = doc.id
            
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
def get_series(user_data):
    """Obtener todas las series"""
    # Verificar Firebase
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
def get_serie(user_data, serie_id):
    """Obtener una serie específica por ID"""
    # Verificar Firebase
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
    # Verificar Firebase
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
    # Verificar Firebase
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
    """Buscar contenido por término"""
    # Verificar Firebase
    firebase_check = check_firebase()
    if firebase_check:
        return firebase_check
    
    try:
        termino = request.args.get('q', '')
        if not termino:
            return jsonify({"error": "Término de búsqueda requerido"}), 400
        
        limit = int(request.args.get('limit', 10))
        
        resultados = []
        
        # Buscar en películas
        peliculas_ref = db.collection('peliculas')
        peliculas_query = peliculas_ref.where('title', '>=', termino).where('title', '<=', termino + '\uf8ff')
        peliculas_docs = peliculas_query.limit(limit).stream()
        
        for doc in peliculas_docs:
            data = doc.to_dict()
            data['id'] = doc.id
            data['tipo'] = 'pelicula'
            resultados.append(data)
        
        # Buscar en series
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
            "data": resultados
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/estadisticas', methods=['GET'])
@token_required
def get_estadisticas(user_data):
    """Obtener estadísticas del contenido"""
    # Verificar Firebase
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
                "total_contenido": peliculas_count + series_count
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Manejo de errores
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint no encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Error interno del servidor"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

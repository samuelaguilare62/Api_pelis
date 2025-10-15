from flask import Flask, jsonify, request
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import os

# Inicializar Flask
app = Flask(__name__)
CORS(app)

# Configuración de Firebase sin archivo JSON
try:
    # Usar las credenciales por defecto del Admin SDK
    # Solo necesitas la variable de entorno GOOGLE_APPLICATION_CREDENTIALS
    # O puedes inicializar sin parámetros si estás en Google Cloud
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {
        'projectId': 'phdt-b9b2c'
    })
    
    # Inicializar Firestore
    db = firestore.client()
    print("✅ Firebase inicializado correctamente")
    
except Exception as e:
    print(f"❌ Error inicializando Firebase: {e}")
    db = None

# Rutas de la API

@app.route('/')
def home():
    return jsonify({
        "message": "🎬 API de Streaming - Firestore",
        "version": "1.0.0",
        "endpoints": {
            "peliculas": "/api/peliculas",
            "pelicula_especifica": "/api/peliculas/<id>",
            "series": "/api/series", 
            "serie_especifica": "/api/series/<id>",
            "canales": "/api/canales",
            "canal_especifico": "/api/canales/<id>",
            "buscar": "/api/buscar?q=<termino>"
        }
    })

@app.route('/api/peliculas', methods=['GET'])
def get_peliculas():
    """Obtener todas las películas con paginación"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
    try:
        # Parámetros de paginación
        limit = int(request.args.get('limit', 20))
        page = int(request.args.get('page', 1))
        
        # Consulta a Firestore
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
def get_pelicula(pelicula_id):
    """Obtener una película específica por ID"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
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
def get_series():
    """Obtener todas las series"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
    try:
        limit = int(request.args.get('limit', 20))
        
        series_ref = db.collection('contenido')
        docs = series_ref.limit(limit).stream()
        
        series = []
        for doc in docs:
            serie_data = doc.to_dict()
            # Verificar si tiene estructura de serie (con seasons)
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
def get_serie(serie_id):
    """Obtener una serie específica por ID"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
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
def get_canales():
    """Obtener todos los canales"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
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
def get_canal(canal_id):
    """Obtener un canal específico por ID"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
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
def buscar():
    """Buscar contenido por término"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
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
            # Solo incluir si tiene estructura de serie
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
def get_estadisticas():
    """Obtener estadísticas del contenido"""
    if not db:
        return jsonify({"error": "Firebase no inicializado"}), 500
    
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
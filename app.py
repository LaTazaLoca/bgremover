"""
==============================================
BG Remover API v1.0
Quita el fondo de imágenes y exporta PNG transparente
Usa rembg (modelo U2NET) — mismo motor que remove.bg
==============================================
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from rembg import remove, new_session
from PIL import Image
import io
import os
import uuid
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Carpeta para guardar resultados
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Cargar modelo al iniciar (tarda la primera vez, después es rápido)
MODEL_NAME = os.environ.get("REMBG_MODEL", "u2net")
session = None

def get_session():
    global session
    if session is None:
        logger.info(f"🧠 Cargando modelo: {MODEL_NAME}...")
        session = new_session(MODEL_NAME)
        logger.info("✅ Modelo cargado")
    return session


@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response


@app.route("/")
def home():
    return jsonify({
        "name": "BG Remover API",
        "version": "1.0.0",
        "model": MODEL_NAME,
        "endpoints": {
            "POST /remove": "Quitar fondo (recibe imagen, devuelve PNG)",
            "POST /remove/batch": "Quitar fondo a múltiples imágenes",
            "GET /download/<id>": "Descargar imagen procesada",
            "GET /health": "Health check"
        }
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "model": MODEL_NAME})


@app.route("/remove", methods=["POST"])
def remove_bg():
    """
    Quita el fondo de una imagen.
    
    Recibe:
      - file: imagen (multipart form)
      - O base64 en JSON: {"image": "base64..."}
    
    Parámetros opcionales (query string o JSON):
      - model: u2net, u2netp, u2net_human_seg, silueta, isnet-general-use
      - alpha_matting: true/false (mejor calidad en bordes)
      - format: png (default) o webp
    
    Devuelve: imagen PNG sin fondo
    """
    start = time.time()

    # Obtener imagen
    image_bytes = None

    if "file" in request.files:
        file = request.files["file"]
        image_bytes = file.read()
        filename = file.filename or "image.png"
    elif request.is_json and "image" in request.get_json(silent=True, force=True):
        import base64
        data = request.get_json(force=True)
        image_bytes = base64.b64decode(data["image"])
        filename = data.get("filename", "image.png")
    else:
        return jsonify({"error": "Envía una imagen como 'file' o en base64"}), 400

    # Parámetros
    alpha_matting = request.args.get("alpha_matting", "false").lower() == "true"
    output_format = request.args.get("format", "png").lower()
    return_base64 = request.args.get("base64", "false").lower() == "true"

    try:
        # Procesar
        sess = get_session()
        result_bytes = remove(
            image_bytes,
            session=sess,
            alpha_matting=alpha_matting,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=10
        )

        # Convertir a PIL para control de formato
        result_img = Image.open(io.BytesIO(result_bytes)).convert("RGBA")

        # Guardar en outputs
        file_id = str(uuid.uuid4())[:8]
        ext = "webp" if output_format == "webp" else "png"
        out_filename = f"{file_id}.{ext}"
        out_path = os.path.join(OUTPUT_DIR, out_filename)

        if ext == "webp":
            result_img.save(out_path, "WEBP", quality=95)
        else:
            result_img.save(out_path, "PNG")

        elapsed = round(time.time() - start, 2)
        logger.info(f"✅ {filename} → {out_filename} ({elapsed}s)")

        # Devolver
        if return_base64:
            import base64
            buf = io.BytesIO()
            result_img.save(buf, "PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            return jsonify({
                "id": file_id,
                "filename": out_filename,
                "image": b64,
                "time": elapsed
            })
        else:
            buf = io.BytesIO()
            if ext == "webp":
                result_img.save(buf, "WEBP", quality=95)
                mimetype = "image/webp"
            else:
                result_img.save(buf, "PNG")
                mimetype = "image/png"
            buf.seek(0)
            return send_file(buf, mimetype=mimetype, 
                           download_name=out_filename,
                           as_attachment=False)

    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/remove/batch", methods=["POST"])
def remove_bg_batch():
    """Procesa múltiples imágenes de una vez."""
    if "files" not in request.files:
        return jsonify({"error": "Envía imágenes en el campo 'files'"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No se recibieron archivos"}), 400

    results = []
    sess = get_session()

    for file in files:
        start = time.time()
        try:
            image_bytes = file.read()
            result_bytes = remove(image_bytes, session=sess)
            
            result_img = Image.open(io.BytesIO(result_bytes)).convert("RGBA")
            
            file_id = str(uuid.uuid4())[:8]
            out_filename = f"{file_id}.png"
            out_path = os.path.join(OUTPUT_DIR, out_filename)
            result_img.save(out_path, "PNG")

            elapsed = round(time.time() - start, 2)
            results.append({
                "original": file.filename,
                "id": file_id,
                "filename": out_filename,
                "download": f"/download/{file_id}",
                "time": elapsed,
                "status": "ok"
            })
        except Exception as e:
            results.append({
                "original": file.filename,
                "status": "error",
                "error": str(e)
            })

    return jsonify({"processed": len(results), "results": results})


@app.route("/download/<file_id>")
def download(file_id):
    """Descarga una imagen procesada."""
    # Buscar archivo por ID
    for ext in ["png", "webp"]:
        path = os.path.join(OUTPUT_DIR, f"{file_id}.{ext}")
        if os.path.exists(path):
            return send_file(path, as_attachment=True)
    
    return jsonify({"error": "Archivo no encontrado"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"""
    ╔════════════════════════════════════╗
    ║  🖼️  BG Remover API v1.0          ║
    ║  Puerto: {port}                    ║
    ║  Modelo: {MODEL_NAME}              ║
    ╚════════════════════════════════════╝
    """)
    # Pre-cargar modelo
    #get_session()
    app.run(host="0.0.0.0", port=port, debug=False)

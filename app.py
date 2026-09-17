import os
import json
import requests
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from playwright.sync_api import sync_playwright
from supabase import create_client, Client
from openai import OpenAI

app = Flask(__name__)

# --- CREDENCIALES Y CONFIGURACIÓN ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

# Inicializar clientes
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
llm_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

ZONAS_OBJETIVO = [
    "https://inmuebles.mercadolibre.com.mx/casas/renta/estado-de-mexico/tepotzotlan/_PriceRange_0-7500",
    "https://inmuebles.mercadolibre.com.mx/departamentos/renta/estado-de-mexico/atizapan-de-zaragoza/_PriceRange_0-7500",
    "https://inmuebles.mercadolibre.com.mx/casas/renta/queretaro/san-juan-del-rio/_PriceRange_0-7500"
]

def enviar_telegram(mensaje):
    if not TOKEN or not CHAT_ID:
        print("Error: Faltan credenciales de Telegram")
        return
    url_tel = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    res = requests.post(url_tel, data={"chat_id": CHAT_ID, "text": mensaje})
    
    # Esto te mostrará el error exacto en los logs de Render si Telegram rechaza el mensaje
    if res.status_code != 200:
        print(f"Fallo al enviar a Telegram: {res.text}")

def url_ya_vista(url):
    try:
        data = supabase.table("propiedades_vistas").select("url").eq("url", url).execute()
        return len(data.data) > 0
    except Exception as e:
        print(f"Error consultando Supabase: {e}")
        return False

def marcar_como_vista(url):
    try:
        supabase.table("propiedades_vistas").insert({"url": url}).execute()
    except Exception as e:
        print(f"Error guardando en Supabase: {e}")

def evaluar_con_llm(descripcion):
    prompt = f"""
    Eres un analista inmobiliario. Lee la siguiente descripción de una propiedad en renta.
    Tu objetivo es determinar si es viable para un inquilino que NO tiene aval, NO tiene empleo fijo (pero tiene liquidez para pagar meses por adelantado), y NO puede pagar póliza jurídica. 
    Descarta inmediatamente si exige póliza jurídica, aval forzoso o menciona agencias inmobiliarias estrictas. 
    Premia si dice "trato directo" o es negociable.
    
    Descripción: "{descripcion}"
    
    Responde ÚNICAMENTE con un JSON válido con dos claves: "viable" (booleano) y "razon" (texto corto de 1 línea). No añadas formato markdown ni texto fuera del JSON.
    """
    
    try:
        # Utilizamos un modelo gratuito de alta capacidad
        response = llm_client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct:free",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        resultado_crudo = response.choices[0].message.content.strip()
        
        # Limpieza por si el modelo añade backticks a pesar de las instrucciones
        if resultado_crudo.startswith("```json"):
            resultado_crudo = resultado_crudo[7:-3]
            
        evaluacion = json.loads(resultado_crudo)
        return evaluacion.get("viable", False), evaluacion.get("razon", "Evaluación fallida")
    except Exception as e:
        print(f"Error en OpenRouter LLM: {e}")
        return False, "Error de API"

def buscar_propiedades():
    print("Iniciando patrullaje con Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for url in ZONAS_OBJETIVO:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Extraemos los enlaces de las tarjetas
                enlaces = page.eval_on_selector_all("a.ui-search-link", "elements => elements.map(e => e.href)")
                
                for link in enlaces:
                    link_limpio = link.split('#')[0]
                    
                    if url_ya_vista(link_limpio):
                        continue
                        
                    # Navegamos a la propiedad individual para leer la descripción completa
                    page.goto(link_limpio, wait_until="domcontentloaded", timeout=20000)
                    
                    # Extraer el texto de la descripción (selector específico de ML, debes adaptarlo para Lamudi/Inmuebles24)
                    descripcion = page.locator("p.ui-pdp-description__content").inner_text() if page.locator("p.ui-pdp-description__content").count() > 0 else "Sin descripción"
                    
                    es_viable, razon = evaluar_con_llm(descripcion)
                    
                    if es_viable:
                        zona_nombre = url.split('/')[-2].replace('-', ' ').title()
                        mensaje = f"✅ Propiedad Viable\n📍 {zona_nombre}\n🧠 Razón: {razon}\n🔗 {link_limpio}"
                        enviar_telegram(mensaje)
                    
                    marcar_como_vista(link_limpio)
                    
            except Exception as e:
                print(f"Error procesando zona {url}: {e}")
                
        browser.close()

# --- RUTAS FLASK ---
@app.route('/')
def home():
    return "Motor Cognitivo Inmobiliario Activo 🚀"

@app.route('/prueba')
def prueba_telegram():
    enviar_telegram("🤖 Ping de diagnóstico: Telegram conectado.")
    return "Mensaje enviado. Revisa los logs en Render si no llega."

# --- SCHEDULER ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(buscar_propiedades, 'interval', minutes=30)
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

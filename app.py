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
    "https://inmuebles.mercadolibre.com.mx/casas/renta/estado-de-mexico/tepotzotlan/_PriceRange_0-5500",
    "https://www.inmuebles24.com/departamentos-en-renta-en-atizapan-de-zaragoza-hasta-5500-pesos.html",
    "https://www.lamudi.com.mx/queretaro/san-juan-del-rio/departamento/for-rent/price:up-to-5500/",
    "https://inmuebles.mercadolibre.com.mx/casas/renta/estado-de-mexico/tepotzotlan/_PriceRange_0-5500",
    "https://inmuebles.mercadolibre.com.mx/departamentos/renta/estado-de-mexico/atizapan-de-zaragoza/_PriceRange_0-5500",
    "https://inmuebles.mercadolibre.com.mx/casas/renta/queretaro/san-juan-del-rio/_PriceRange_0-5500"
]

def obtener_selectores(url):
    """Devuelve la tupla (selector_enlaces, selector_descripcion) según el dominio."""
    if "mercadolibre.com" in url:
        return ("a.ui-search-link", "p.ui-pdp-description__content")
    
    elif "inmuebles24.com" in url:
        # Usamos atributos de datos (data-qa) porque I24 ofusca sus clases CSS
        return ("div[data-qa='posting'] a, a.go-to-posting", "div#longDescription, div[data-qa='posting-description']")
    
    elif "lamudi.com" in url:
        return ("a.js-listing-link, a.ListingCell-moreInfo-button", "div.ViewText-description, div#description")
    
    # Fallback genérico
    return ("a", "p")

def buscar_propiedades():
    print("Iniciando patrullaje multi-plataforma con Playwright...")
    with sync_playwright() as p:
        # Lanzamos Chromium. En Inmuebles24 a veces ayuda añadir un user_agent específico para no parecer un bot.
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        for url in ZONAS_OBJETIVO:
            try:
                # Obtenemos las reglas del DOM para esta página en específico
                selector_links, selector_desc = obtener_selectores(url)
                
                # Navegamos al listado
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # Extraemos los enlaces evaluando el selector dinámico
                enlaces = page.eval_on_selector_all(
                    selector_links, 
                    "elements => elements.map(e => e.href)"
                )
                
                for link in enlaces:
                    # Limpiamos anclas basura de la URL
                    link_limpio = link.split('#')[0].split('?')[0] 
                    
                    if url_ya_vista(link_limpio):
                        continue
                        
                    # Navegamos a la propiedad individual
                    page.goto(link_limpio, wait_until="domcontentloaded", timeout=20000)
                    
                    # Extraer texto de descripción
                    if page.locator(selector_desc).count() > 0:
                        # Unimos el texto si hay varios párrafos bajo el mismo selector
                        descripcion = "\n".join(page.locator(selector_desc).all_inner_texts())
                    else:
                        descripcion = "Sin descripción visible en el DOM."
                    
                    # Evaluamos con LLaMA 3 vía OpenRouter
                    es_viable, razon = evaluar_con_llm(descripcion)
                    
                    if es_viable:
                        zona_nombre = url.split('/')[-2].replace('-', ' ').title()
                        mensaje = f"✅ Propiedad Viable\n📍 {zona_nombre}\n🧠 {razon}\n🔗 {link_limpio}"
                        enviar_telegram(mensaje)
                    
                    # Registramos en Supabase
                    marcar_como_vista(link_limpio)
                    
            except Exception as e:
                print(f"Error procesando {url}: {e}")
                
        context.close()
        browser.close()

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
    Eres un auditor inmobiliario implacable. Analiza la siguiente descripción de una propiedad en renta y determina su viabilidad. 
    El inquilino potencial cuenta con liquidez para pagar 6 meses por adelantado, pero carece de aval y no aceptará trámites con póliza jurídica o inmobiliarias.

    REGLAS DE RECHAZO INMEDIATO (viable: false):
    - Exigencia explícita de: "póliza jurídica", "poliza", "aval", "fiador", "obligado solidario", "propiedad en garantía".
    - Jerga de agencias intermediarias: "inmobiliaria", "asesor", "comisión", "honorarios", "buro de crédito", "investigación".
    - Restricciones de estilo de vida: "solo señoritas", "estudiantes", "no visitas".

    REGLAS DE APROBACIÓN (viable: true):
    - Mención explícita de: "trato directo", "trato con dueño", "sin aval".
    - Textos neutrales que NO contengan NINGUNA de las palabras de rechazo absoluto (espacios donde el pago por adelantado sirve como llave de negociación).

    Devuelve ÚNICAMENTE un JSON válido con este formato exacto: {{"viable": true/false, "razon": "Justificación de 10 palabras máximo"}}. No incluyas markdown (```json), ni texto introductorio.

    Descripción a evaluar:
    "{descripcion}"
    """
    
    try:
        response = llm_client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct:free",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0 # Reducido a 0 para máxima consistencia y evitar alucinaciones
        )
        resultado_crudo = response.choices[0].message.content.strip()
        
        # Limpieza defensiva en caso de que el LLM ignore la instrucción de no usar markdown
        if resultado_crudo.startswith("```"):
            resultado_crudo = resultado_crudo.replace("```json", "").replace("```", "").strip()
            
        evaluacion = json.loads(resultado_crudo)
        return evaluacion.get("viable", False), evaluacion.get("razon", "Evaluación fallida")
        
    except json.JSONDecodeError:
        print(f"Error de parseo JSON. El LLM devolvió: {resultado_crudo}")
        return False, "Error de formato LLM"
    except Exception as e:
        print(f"Error en OpenRouter: {e}")
        return False, "Error de conexión"

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

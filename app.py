import os
import json
import threading
import requests
import asyncio
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from playwright.async_api import async_playwright
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
    "https://inmuebles.mercadolibre.com.mx/departamentos/renta/estado-de-mexico/atizapan-de-zaragoza/_PriceRange_0-5500",
    "https://inmuebles.mercadolibre.com.mx/casas/renta/queretaro/san-juan-del-rio/_PriceRange_0-5500"
]

def obtener_selectores(url):
    """Devuelve la tupla (selector_enlaces, selector_descripcion) según el dominio."""
    if "mercadolibre.com" in url:
        return ("a.ui-search-link", "p.ui-pdp-description__content")
    elif "inmuebles24.com" in url:
        return ("div[data-qa='posting'] a, a.go-to-posting", "div#longDescription, div[data-qa='posting-description']")
    elif "lamudi.com" in url:
        return ("a.js-listing-link, a.ListingCell-moreInfo-button", "div.ViewText-description, div#description")
    return ("a", "p")

def enviar_telegram(mensaje):
    if not TOKEN or not CHAT_ID:
        print("Error: Faltan credenciales de Telegram")
        return
    url_tel = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    res = requests.post(url_tel, data={"chat_id": CHAT_ID, "text": mensaje})
    if res.status_code != 200:
        print(f"Fallo al enviar mensaje a Telegram: {res.text}")

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

    Devuelve ÚNICAMENTE un JSON válido con este formato exacto: {{"viable": true/false, "razon": "Justificación de 10 palabras máximo"}}. No incluyas markdown, ni texto introductorio.

    Descripción a evaluar:
    "{descripcion}"
    """
    try:
        response = llm_client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct:free",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        resultado_crudo = response.choices[0].message.content.strip()
        if resultado_crudo.startswith("```"):
            resultado_crudo = resultado_crudo.replace("```json", "").replace("```", "").strip()
        evaluacion = json.loads(resultado_crudo)
        return evaluacion.get("viable", False), evaluacion.get("razon", "Evaluación fallida")
    except Exception as e:
        print(f"Error en OpenRouter: {e}")
        return False, "Error de API"

async def motor_scraping_asincrono():
    """Motor asíncrono aislado para evitar bloqueos en los hilos de fondo."""
    try:
        async with async_playwright() as p:
            print("Lanzando Chromium...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            for url in ZONAS_OBJETIVO:
                print(f"Escaneando URL: {url}")
                try:
                    selector_links, selector_desc = obtener_selectores(url)
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    
                    enlaces = await page.eval_on_selector_all(
                        selector_links, 
                        "elements => elements.map(e => e.href)"
                    )
                    print(f"Enlaces encontrados: {len(enlaces)}")
                    
                    for link in enlaces[:5]: # Lote de control
                        link_limpio = link.split('#')[0].split('?')[0]
                        if url_ya_vista(link_limpio):
                            continue
                            
                        print(f"Analizando propiedad nueva: {link_limpio}")
                        await page.goto(link_limpio, wait_until="domcontentloaded", timeout=20000)
                        
                        if await page.locator(selector_desc).count() > 0:
                            textos = await page.locator(selector_desc).all_inner_texts()
                            descripcion = "\n".join(textos)
                        else:
                            descripcion = "Sin descripción visible en el DOM."
                        
                        es_viable, razon = evaluar_con_llm(descripcion)
                        print(f"Resultado IA - Viable: {es_viable} | Razón: {razon}")
                        
                        if es_viable:
                            try:
                                zona_nombre = url.split('/')[-2].replace('-', ' ').title()
                            except:
                                zona_nombre = "Zona General"
                            mensaje = f"✅ Propiedad Viable\n📍 {zona_nombre}\n🧠 Razón: {razon}\n🔗 {link_limpio}"
                            enviar_telegram(mensaje)
                        
                        marcar_como_vista(link_limpio)
                except Exception as inner_e:
                    print(f"Error procesando la zona {url}: {inner_e}")
                    
            await context.close()
            await browser.close()
    except Exception as e:
        print(f"Error CRÍTICO en el navegador: {e}")

def buscar_propiedades():
    print("--- Iniciando patrullaje AUTOMÁTICO con Playwright ---")
    # Forzamos la ejecución asíncrona dentro de este hilo específico
    asyncio.run(motor_scraping_asincrono())
    print("--- PATRULLAJE FINALIZADO CON ÉXITO ---")

# --- RUTAS FLASK ---
@app.route('/')
def home():
    return "Motor Cognitivo Inmobiliario Activo 🚀"

@app.route('/prueba')
def prueba_telegram():
    enviar_telegram("🤖 Ping de diagnóstico: Telegram conectado correctamente.")
    threading.Thread(target=buscar_propiedades).start()
    return "PRUEBA LANZADA. Revisa los logs en Render."

# --- SCHEDULER ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(buscar_propiedades, 'interval', minutes=30)
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

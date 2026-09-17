import os
import requests
from bs4 import BeautifulSoup
import cloudscraper
import re
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- CONFIGURACIÓN ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PRECIO_MAXIMO = 7500 # Subimos un poco el margen de tolerancia, siempre se puede negociar a la baja.
ARCHIVO_VISTOS = "vistos.txt"

# Filtro burocrático ENDURECIDO
KEYWORDS_PROHIBIDAS = [
    "amueblado", "aval", "fiador", "fianza", "buró", "buro",
    "póliza", "poliza", "jurídica", "juridica", "obligado solidario", "obligado",
    "comisión", "comision", "estudiantes", "señoritas", "investigación", "investigacion"
]

def enviar_telegram(mensaje):
    if not TOKEN or not CHAT_ID:
        print("Faltan credenciales de Telegram")
        return
    url_tel = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url_tel, data={"chat_id": CHAT_ID, "text": mensaje})

def cargar_vistos():
    if not os.path.exists(ARCHIVO_VISTOS):
        return set()
    with open(ARCHIVO_VISTOS, "r") as f:
        return set(f.read().splitlines())

def guardar_visto(link):
    with open(ARCHIVO_VISTOS, "a") as f:
        f.write(link + "\n")

def cumple_criterios_texto(texto):
    texto_limpio = texto.lower()
    for palabra in KEYWORDS_PROHIBIDAS:
        if palabra in texto_limpio:
            return False
    return True

def limpiar_precio(texto_precio):
    try:
        numeros = re.sub(r'[^\d]', '', texto_precio)
        return int(numeros) if numeros else 0
    except:
        return 999999

def buscar_casas():
    print("Ejecutando escaneo táctico de propiedades...")
    vistos = cargar_vistos()
    scraper = cloudscraper.create_scraper()
    
    # URLs de MercadoLibre (incluyendo Tepotzotlán, Atizapán y Cuautitlán)
    urls_ml = [
        "https://inmuebles.mercadolibre.com.mx/departamentos/renta/estado-de-mexico/cuautitlan-izcalli/_PriceRange_0-7500",
        "https://inmuebles.mercadolibre.com.mx/departamentos/renta/estado-de-mexico/atizapan-de-zaragoza/_PriceRange_0-7500",
        "https://inmuebles.mercadolibre.com.mx/departamentos/renta/estado-de-mexico/tepotzotlan/_PriceRange_0-7500", # Nueva zona prioritaria
        "https://inmuebles.mercadolibre.com.mx/casas/renta/estado-de-mexico/tepotzotlan/_PriceRange_0-7500" # Casas también en Tepo
    ]
    
    for url in urls_ml:
        try:
            res = scraper.get(url)
            soup = BeautifulSoup(res.text, "html.parser")
            
            tarjetas = soup.find_all("li", class_="ui-search-layout__item")
            
            for tarjeta in tarjetas:
                link_tag = tarjeta.find("a", class_="ui-search-link")
                if not link_tag: continue
                
                link = link_tag['href'].split('#')[0]
                
                if link in vistos: continue
                
                texto_anuncio = tarjeta.text
                
                if cumple_criterios_texto(texto_anuncio):
                    zona_nombre = url.split('/')[-2].replace('-', ' ').title()
                    mensaje = f"🎯 Trato Directo Potencial (Max $7.5k)\n📍 {zona_nombre}\n🔗 {link}"
                    enviar_telegram(mensaje)
                    
                    guardar_visto(link)
                    vistos.add(link)
                    
        except Exception as e:
            print(f"Error raspando ML en {url}: {e}")

# --- RUTAS FLASK ---
@app.route('/')
def home():
    return "Scraper Inmobiliario Táctico Activo 🚀"

@app.route('/prueba')
def prueba_telegram():
    enviar_telegram("🤖 Ping de prueba: El scraper está alerta buscando rentas sin burocracia.")
    return "¡Mensaje de prueba enviado!"

# --- SCHEDULER ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(buscar_casas, 'interval', minutes=30)
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

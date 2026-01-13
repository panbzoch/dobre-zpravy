import feedparser
import urllib.parse
from groq import Groq
import time
import os
import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from jinja2 import Environment, FileSystemLoader
from zoneinfo import ZoneInfo

# --- KONFIGURACE ---
# Tohle funguje univerzálně: doma to bere z .env, na GitHubu ze Secrets
API_KEY = os.getenv("GROQ_API_KEY")
DB_FILE = "database.json"
OUTPUT_FILE = "index.html"
TEMPLATE_FILE = "template.html"
DELAY_SECONDS = 2.0 

client = Groq(api_key=API_KEY)

# --- PRÁCE S DATABÁZÍ ---

def load_database():
    """Načte existující články ze souboru, aby se nepřemazávaly."""
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Reset tagu 'is_new' pro staré články - při novém běhu už nejsou nové
            for article in data:
                article['is_new'] = False 
            return data
    except Exception as e:
        print(f"⚠️ Chyba při načítání DB: {e}, zakládám novou.")
        return []

def save_database(data):
    """Uloží aktuální stav do JSON"""
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_existing_links(data):
    """Vrátí set všech URL, které už máme, pro rychlou kontrolu duplicit"""
    return {item['link'] for item in data}

def parse_rss_date(entry):
    """Vytáhne datum z RSS nebo použije aktuální"""
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        # Převedeme struct_time na datetime
        return datetime(*entry.published_parsed[:6]).isoformat()
    elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6]).isoformat()
    else:
        # Fallback na teď
        return datetime.now().isoformat()

def format_date_display(iso_date):
    """Převede ISO string na hezký český formát"""
    try:
        dt = datetime.fromisoformat(iso_date)
        return dt.strftime("%d. %m. %Y %H:%M")
    except:
        return iso_date

# --- VRÁTNÝ (Python Filtr) ---
def is_worth_checking(title):
    title_lower = title.lower()
    stop_words = [
        "vražda", "zabil", "zemřel", "úmrtí", "nehoda", "tragédie", "požár", 
        "soud", "vězení", "policie", "krimi", "zloděj", "podvod", 
        "babiš", "fiala", "okamura", "pavel", "sněmovna", "vláda", "volby",
        "válka", "rusko", "ukrajina", "izrael", "gaza", "útok", "zbraně",
        "recenze", "komentář", "glosa", "sport", "hokej", "fotbal", "liga"
    ]
    for word in stop_words:
        if word in title_lower:
            return False
    return True

# --- AI ANALÝZA ---
def analyze_article_with_groq(title, description, link):
    system_prompt = """
    Jsi editor seriózního pozitivního webu. 
    Analyzuj zprávu na základě titulku a perexu.
    
    KRITÉRIA:
    1. Hledáme POUZE: Vědecké objevy, Technologické inovace, Byznysové úspěchy, Medicínské průlomy, Dokončené projekty, Pomoc lidem.
    2. IGNORUJ: Politiku, Krimi, Nehody, Bulvár, Sportovní výsledky.
    
    POKUD ZPRÁVA NENÍ POZITIVNÍ:
    Odpověz pouze slovem: SKIP
    
    POKUD JE POZITIVNÍ:
    Odpověz v tomto formátu:
    KATEGORIE: [Vyber jednu: Věda / Technologie / Medicína / Byznys / Společnost]
    TITULEK: [Přeformuluj na úderný titulek, max 8 slov]
    SHRNUTÍ: [Napiš kvalitní shrnutí na 30-50 slov.]
    """

    clean_desc = description.replace("<br>", " ").replace("<p>", "")[:600]

    user_content = f"TITULEK: '{title}'\nPEREX: '{clean_desc}'\nODKAZ: {link}"

    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            model="llama-3.3-70b-versatile", 
            temperature=0.1, 
        )

        text = chat_completion.choices[0].message.content.strip()
        if "SKIP" in text:
            return None
        return text

    except Exception as e:
        print(f"⚠️ Chyba Groq: {e}")
        if "429" in str(e):
            time.sleep(30)
        return None

def parse_ai_result(text, original_link, timestamp):
    try:
        category = text.split("KATEGORIE:")[1].split("TITULEK:")[0].strip()
        title = text.split("TITULEK:")[1].split("SHRNUTÍ:")[0].strip()
        summary = text.split("SHRNUTÍ:")[1].strip()
        title = title.replace('"', '').replace("'", "")
        
        # Sjednotíme kategorie pro filtrování (kdyby AI vymýšlela)
        valid_cats = ["Věda", "Technologie", "Medicína", "Byznys", "Společnost"]
        if category not in valid_cats:
            category = "Společnost" # Default

        return {
            "category": category,
            "title": title,
            "summary": summary,
            "link": original_link,
            "timestamp": timestamp, # Ukládáme ISO formát pro třídění
            "timestamp_display": format_date_display(timestamp), # Pro zobrazení
            "is_new": True # Právě přidáno
        }
    except Exception as e:
        return None

def generate_html_from_template(articles):
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template(TEMPLATE_FILE)
    
    # Získáme aktuální čas v Praze
    cz_time = datetime.now(ZoneInfo("Europe/Prague")) # <--- ZMĚNA
    
    output_html = template.render(
        articles=articles,
        last_update=cz_time.strftime("%d. %m. %Y %H:%M") # <--- ZMĚNA (použijeme cz_time)
    )
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(output_html)
    print(f"\n✅ Web vygenerován: {os.path.abspath(OUTPUT_FILE)}")

# --- HLAVNÍ LOGIKA ---

base_query = '(site:e15.cz OR site:ceskenoviny.cz OR site:vtm.zive.cz OR site:irozhlas.cz OR site:cc.cz OR site:forbes.cz) AND (úspěch OR investice OR "nová továrna" OR vynález OR startup OR vědci OR lék) -krimi -soud'
encoded_query = urllib.parse.quote(base_query)
rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=cs&gl=CZ&ceid=CZ:cs"

# 1. Načtení databáze
print("📂 Načítám lokální databázi...")
db_articles = load_database()
existing_links = get_existing_links(db_articles)
print(f"   Máme v paměti {len(db_articles)} starších článků.")

print("📡 Stahuji RSS feed...")
feed = feedparser.parse(rss_url)
print(f"   Ve feedu je {len(feed.entries)} článků ke kontrole.")
print("-" * 50)

processed_count = 0
new_articles_count = 0

try:
    for entry in feed.entries:
        if new_articles_count >= 10: # Max 10 nových na jeden běh
            print("🎉 Máme dost nových zpráv.")
            break
        
        if processed_count >= 60: 
            print("🏁 Prošli jsme dostatek položek z feedu.")
            break

        link = entry.link
        original_title = entry.title
        
        # KONTROLA DUPLICIT
        if link in existing_links:
            # print(f"   (Známé) {original_title[:30]}...") # Odkomentuj pro debug
            processed_count += 1
            continue # Přeskakujeme, už to máme

        # PYTHON FILTR
        if not is_worth_checking(original_title):
            processed_count += 1
            continue 

        processed_count += 1
        
        # GROQ ANALÝZA (pouze pokud prošlo všemi filtry)
        if processed_count > 1:
            time.sleep(DELAY_SECONDS)

        print(f"[{processed_count}] Groq analyzuje NOVÉ: {original_title[:40]}...")
        
        description = getattr(entry, 'summary', '') or getattr(entry, 'description', '') 
        timestamp = parse_rss_date(entry) # Získání data

        result = analyze_article_with_groq(original_title, description, link)
        
        if result:
            print(f"   ✅ BINGO! Přidávám do DB.")
            parsed = parse_ai_result(result, link, timestamp)
            if parsed:
                db_articles.append(parsed)
                new_articles_count += 1
        else:
            print(f"   ❌ Odpad.")

except KeyboardInterrupt:
    print("\n🛑 Přerušeno uživatelem.")

finally:
    # 2. TŘÍDĚNÍ (SORTING)
    # Třídíme takto: 
    #   1. Kritérium: is_new (True je větší než False, takže True bude nahoře)
    #   2. Kritérium: timestamp (nejnovější datum nahoře)
    print("🔄 Třídím články (Nové > Staré)...")
    db_articles.sort(key=lambda x: (x.get('is_new', False), x.get('timestamp', '')), reverse=True)

    # 3. ULOŽENÍ
    save_database(db_articles)

    # 4. GENEROWÁNÍ HTML

    generate_html_from_template(db_articles)

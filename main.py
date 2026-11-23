import feedparser
import urllib.parse
from openai import OpenAI
import time
import os
import json
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from jinja2 import Environment, FileSystemLoader

# --- KONFIGURACE ---
# Tohle funguje univerzálně: doma to bere z .env, na GitHubu ze Secrets
API_KEY = os.getenv("OPENROUTER_API_KEY")
DB_FILE = "database.json"
OUTPUT_FILE = "index.html"
TEMPLATE_FILE = "template.html"
DELAY_SECONDS = 3.5  # free modely na OpenRouteru: max 20 požadavků/min

# Pouze :free modely => vždy zdarma. OpenRouter dovolí max 3 modely v poli "models"
# (při nedostupnosti prvního automaticky zkusí další).
OPENROUTER_MODELS = [m.strip() for m in os.getenv(
    "OPENROUTER_MODELS",
    "baidu/ernie-5.5-flash-preview:free,google/gemma-4-31b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free",
).split(",") if m.strip()][:3]
MAX_ATTEMPTS = 3          # pokusy na jeden článek
FATAL_HTTP_CODES = ("401", "402", "403")  # špatný klíč / kredit / zákaz -> run musí selhat
MAX_AI_FAILURES = 5       # kolik článků smí skončit chybou AI, než run označíme za neúspěšný

# Retence: články starší než RETENTION_DAYS mažeme, ale vždy necháme aspoň MIN_KEEP nejnovějších
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "120"))
MIN_KEEP = int(os.getenv("MIN_KEEP", "30"))

ai_failures = 0

client = OpenAI(api_key=API_KEY, base_url="https://openrouter.ai/api/v1")

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
def analyze_article_with_ai(title, description, link):
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

    DALŠÍ PRAVIDLA:
    - Odpovídej výhradně česky.
    - Nevymýšlej jména, čísla ani fakta, která nejsou v titulku nebo perexu.
    """

    clean_desc = description.replace("<br>", " ").replace("<p>", "")[:600]

    user_content = f"TITULEK: '{title}'\nPEREX: '{clean_desc}'\nODKAZ: {link}"

    global ai_failures
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(MAX_ATTEMPTS):
        try:
            chat_completion = client.chat.completions.create(
                messages=messages,
                model=OPENROUTER_MODELS[0],
                extra_body={"models": OPENROUTER_MODELS},  # fallback jen mezi :free modely
                temperature=0.1,
            )
            text = (chat_completion.choices[0].message.content or "").strip()
            used_model = getattr(chat_completion, "model", "?")

            if text.upper().startswith("SKIP"):
                return None
            if all(k in text for k in ("KATEGORIE:", "TITULEK:", "SHRNUTÍ:")):
                print(f"   (model: {used_model})")
                return text
            print(f"   ⚠️ Neplatná/prázdná odpověď od {used_model}: {text[:60]!r}")

        except Exception as e:
            msg = str(e)
            print(f"   ⚠️ Chyba AI: {msg[:200]}")
            if any(f"Error code: {c}" in msg for c in FATAL_HTTP_CODES):
                raise SystemExit(f"❌ Fatální chyba OpenRouteru (klíč/kredit/přístup): {msg}")
            # 404 (model zmizel), 429 (limit) i 5xx řešíme opakováním; zbylé modely v seznamu
            # OpenRouter zkouší sám.

        if attempt < MAX_ATTEMPTS - 1:
            wait = 10 * 2 ** attempt  # 10 s, 20 s
            print(f"   ⏳ Zkusím znovu za {wait} s...")
            time.sleep(wait)

    ai_failures += 1
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

def prune_old_articles(articles):
    """Smaže články starší než RETENTION_DAYS, vždy ale ponechá MIN_KEEP nejnovějších."""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    by_date = sorted(articles, key=lambda x: x.get("timestamp", ""), reverse=True)
    keep_links = {a["link"] for a in by_date[:MIN_KEEP]}
    kept = [a for a in articles if a.get("timestamp", "") >= cutoff or a["link"] in keep_links]
    removed = len(articles) - len(kept)
    if removed:
        print(f"🧹 Retence: mažu {removed} článků starších než {RETENTION_DAYS} dní (ponechávám {len(kept)}).")
    return kept

def generate_html_from_template(articles):
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template(TEMPLATE_FILE)
    
    output_html = template.render(
        articles=articles,
        last_update=datetime.now().strftime("%d. %m. %Y %H:%M")
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
        
        # AI ANALÝZA (pouze pokud prošlo všemi filtry)
        if processed_count > 1:
            time.sleep(DELAY_SECONDS)

        print(f"[{processed_count}] AI analyzuje NOVÉ: {original_title[:40]}...")
        
        description = getattr(entry, 'summary', '') or getattr(entry, 'description', '') 
        timestamp = parse_rss_date(entry) # Získání data

        result = analyze_article_with_ai(original_title, description, link)
        
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
    if new_articles_count > 0:
        # Retenci spouštíme jen po úspěšném běhu, aby se web nikdy nevyprázdnil.
        db_articles = prune_old_articles(db_articles)

        print("🔄 Třídím články (Nové > Staré)...")
        db_articles.sort(key=lambda x: (x.get('is_new', False), x.get('timestamp', '')), reverse=True)

        # 3. ULOŽENÍ
        save_database(db_articles)

        # 4. GENEROVÁNÍ HTML
        generate_html_from_template(db_articles)
    else:
        print("ℹ️ Žádné nové články – database.json ani index.html neměním.")

    print(f"📊 Nové články: {new_articles_count}, selhání AI: {ai_failures}")
    if ai_failures >= MAX_AI_FAILURES and ai_failures >= new_articles_count:
        raise SystemExit(f"❌ AI selhala u {ai_failures} článků – run označuji jako neúspěšný.")
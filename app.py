import os, sqlite3, json, csv, logging, unicodedata
from datetime import datetime
from flask import Flask, request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "recepcionista.db")
CSV = os.path.join(BASE, "leads.csv")
PUBLIC_URL_FILE = os.path.join(BASE, "public_url.txt")
NEGOCIO = "Climas y Plomería del Norte"
logging.basicConfig(handlers=[logging.FileHandler(os.path.join(BASE, "server.log")), logging.StreamHandler()], level=logging.INFO,
                    format="%(asctime)s %(message)s")
app = Flask(__name__)
validator = RequestValidator(os.environ.get("TWILIO_AUTH_TOKEN", ""))

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS sessions(phone TEXT PRIMARY KEY, state TEXT, data TEXT, updated TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, phone TEXT,
      nombre TEXT, servicio TEXT, problema TEXT, colonia TEXT, horario TEXT, recordatorio INTEGER, estado TEXT)""")
    return c

def norm(s):
    s = unicodedata.normalize("NFD", s.lower().strip())
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")

def mask(p): return p[:-4].replace(p[-8:-4], "****") + p[-4:] if len(p) > 8 else p

SERVICIOS = {"1": "Minisplit / clima", "2": "Refrigeración", "3": "Plomería"}
def servicio(t):
    t = norm(t)
    if t in SERVICIOS: return SERVICIOS[t]
    if any(k in t for k in ["mini", "clima", "aire", "split", "a/c", "enfri", "calor", "evapor"]): return SERVICIOS["1"]
    if any(k in t for k in ["refri", "congel", "camara", "hielo"]): return SERVICIOS["2"]
    if any(k in t for k in ["plom", "fuga", "agua", "tuber", "bano", "drenaje", "boiler", "calentador", "llave", "taza", "wc"]): return SERVICIOS["3"]
    return None

SI = ["si", "sip", "claro", "ok", "va", "dale", "correcto", "sale", "esta bien", "perfecto", "de acuerdo", "afirmativo", "simon"]
NO = ["no", "nel", "nop", "para nada", "no gracias"]
def yesno(t):
    t = norm(t).strip(" .!¡")
    if any(t == w or t.startswith(w + " ") or t.startswith(w + ",") for w in NO): return False
    if any(t == w or t.startswith(w + " ") or t.startswith(w + ",") for w in SI): return True
    return None

MENU = "1️⃣ Minisplit / clima\n2️⃣ Refrigeración\n3️⃣ Plomería"

def resumen(d):
    return (f"📋 *Resumen de tu cita*\n• Nombre: {d['nombre']}\n• Servicio: {d['servicio']}\n• Problema: {d['problema']}\n"
            f"• Colonia: {d['colonia']}\n• Día y hora: {d['horario']}")

def guardar(phone, d):
    now = datetime.now().isoformat(timespec="seconds")
    c = db()
    cur = c.execute("INSERT INTO leads(created,phone,nombre,servicio,problema,colonia,horario,recordatorio,estado) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, phone, d["nombre"], d["servicio"], d["problema"], d["colonia"], d["horario"], int(d.get("recordatorio", False)), "pendiente_confirmar_tecnico"))
    c.commit(); lid = cur.lastrowid; c.close()
    nuevo = not os.path.exists(CSV)
    with open(CSV, "a", newline="") as f:
        w = csv.writer(f)
        if nuevo: w.writerow(["id", "fecha", "telefono", "nombre", "servicio", "problema", "colonia", "horario", "recordatorio_mantenimiento"])
        w.writerow([lid, now, phone, d["nombre"], d["servicio"], d["problema"], d["colonia"], d["horario"], "sí" if d.get("recordatorio") else "no"])
    logging.info("NUEVO LEAD para el técnico #%s %s | %s | %s | %s | %s | recordatorio=%s", lid, mask(phone),
                 d["servicio"], d["problema"], d["colonia"], d["horario"], d.get("recordatorio"))
    return lid

def responder(phone, body):
    c = db()
    row = c.execute("SELECT state,data FROM sessions WHERE phone=?", (phone,)).fetchone()
    state, d = (row["state"], json.loads(row["data"])) if row else ("nuevo", {})
    t = body.strip(); nt = norm(t)
    if nt in ("reiniciar", "reset", "menu", "inicio", "empezar") or (state == "fin" and nt):
        if state == "fin" and nt in ("gracias", "muchas gracias", "ok", "va"):
            out = "¡A ti! 🙌 Si necesitas algo más, solo escríbenos.";  c.close(); return out
        state, d = "nuevo", {}

    if nt.startswith("join "):
        out = f"¡Listo! Ya estás conectado con {NEGOCIO}. Escríbenos *hola* para agendar un servicio. 😊"; c.close(); return out
    if state == "nuevo":
        out = (f"¡Hola! 👋 Gracias por escribir a *{NEGOCIO}*. Soy Sofi, la recepcionista virtual, y con gusto te ayudo a agendar tu servicio.\n\n"
               "¿Me compartes tu nombre, por favor?"); state = "nombre"
    elif state == "nombre":
        d["nombre"] = t[:60].title()
        out = f"Mucho gusto, {d['nombre'].split()[0]}. ¿Qué servicio necesitas?\n{MENU}"; state = "servicio"
    elif state == "servicio":
        s = servicio(t)
        if not s: out = f"Perdón, no te entendí bien 😅. Responde con el número:\n{MENU}"
        else:
            d["servicio"] = s
            ej = {"Minisplit / clima": "no enfría, gotea, hace ruido, no prende o necesita limpieza",
                  "Refrigeración": "no enfría, hace hielo de más, hace ruido",
                  "Plomería": "fuga, baño tapado, no hay presión, calentador/boiler"}[s]
            out = f"Perfecto, {s.lower()}. Cuéntame, ¿qué problema tiene? (por ejemplo: {ej}). Si sabes la marca o capacidad del equipo, también ayuda."
            state = "problema"
    elif state == "problema":
        d["problema"] = t[:300]
        out = "Entendido, gracias por el detalle. 📍 ¿En qué colonia de Hermosillo estás?"; state = "colonia"
    elif state == "colonia":
        d["colonia"] = t[:80]
        out = "¡Va! ¿Qué día y horario te acomoda para la visita? (por ejemplo: *mañana en la mañana* o *jueves 4 pm*)"; state = "horario"
    elif state == "horario":
        d["horario"] = t[:80]
        out = resumen(d) + "\n\n¿Está todo correcto? Responde *sí* para confirmar o *no* para corregir."; state = "confirmar"
    elif state == "confirmar":
        y = yesno(t)
        if y is True:
            out = ("¡Excelente! ✅ Ya le pasé tu solicitud al técnico y te confirmamos la hora exacta en breve.\n\n"
                   "Una pregunta más: aquí en Hermosillo los climas sufren mucho en verano 🔥. ¿Quieres que te mandemos un *recordatorio de mantenimiento* antes del calor (marzo-abril)? Responde *sí* o *no*.")
            state = "recordatorio"
        elif y is False:
            out = "Sin problema, empecemos de nuevo con los datos. ¿Qué servicio necesitas?\n" + MENU; state = "servicio"
        else: out = "Responde *sí* para confirmar la cita o *no* para corregir los datos, por favor."
    elif state == "recordatorio":
        y = yesno(t)
        if y is None: out = "¿Te mandamos el recordatorio de mantenimiento antes del verano? Responde *sí* o *no*."
        else:
            d["recordatorio"] = y
            lid = guardar(phone, d)
            out = (("¡Anotado! 📅 Te avisaremos antes del verano. " if y else "Va, no te mandaremos recordatorio. ")
                   + f"Tu folio es *#{lid}*. ¡Gracias por confiar en {NEGOCIO}! 🙌 Si necesitas otra cosa, escribe *hola*.")
            state, d = "fin", {}
    else:
        out = "¡Hola de nuevo! Escribe *hola* para agendar un servicio."; state = "nuevo"
    c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)", (phone, state, json.dumps(d, ensure_ascii=False), datetime.now().isoformat()))
    c.commit(); c.close()
    return out

@app.post("/whatsapp")
def whatsapp():
    base = (os.environ.get("PUBLIC_URL") or (open(PUBLIC_URL_FILE).read().strip() if os.path.exists(PUBLIC_URL_FILE) else request.url_root)).rstrip("/")
    url = base + request.full_path.rstrip("?")
    if os.environ.get("SKIP_SIGNATURE") != "1" and not validator.validate(url, request.form.to_dict(), request.headers.get("X-Twilio-Signature", "")):
        logging.warning("Firma inválida para %s", url); return Response("forbidden", 403)
    phone, body = request.form.get("From", ""), request.form.get("Body", "")
    logging.info("IN %s: %s", mask(phone), body)
    out = responder(phone, body)
    logging.info("OUT %s: %s", mask(phone), out.replace("\n", " | "))
    r = MessagingResponse(); r.message(out)
    return Response(str(r), mimetype="application/xml")

@app.get("/health")
def health(): return "ok"

db().close()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5055)))

#!/usr/bin/env python3
import os, re, json, time, glob, hmac, sqlite3, secrets, threading, subprocess, sys, requests
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_file, send_from_directory, abort

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

BUF, OUT, DB = (os.path.join(BASE, x) for x in ("buffer", "replays", "replay.db"))
CONFIG_PATH = os.path.join(BASE, "config.json")

CFG = json.load(open(CONFIG_PATH, encoding="utf-8"))
PRE, POST = CFG.get("pre", 20), CFG.get("post", 10)
SEG, KEEP, DEBOUNCE = 2, 70, 15
KEEP_DAYS = CFG.get("keep_days", 30)

os.makedirs(BUF, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
app = Flask(__name__, static_folder=None)

DEF_CFG = {"a": "RE", "b": "PLAY", "accent": "#39ff14"}
DEF_TXT = {"navHome": "Início", "navCourts": "Quadras", "navSet": "Ajustes", "tHome": "Painel",
           "tCourts": "Quadras", "sTotal": "Replays salvos", "sToday": "Salvos hoje",
           "sLast": "Último replay", "sCourts": "Quadras ativas", "navReplays": "Replays", "tReplays": "Todos os replays", "sSize": "Espaço usado"}

def q(sql, args=()):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args); rows = cur.fetchall(); c.commit(); return rows, cur.lastrowid
    finally:
        c.close()

def kget(k):
    r, _ = q("SELECT v FROM kv WHERE k=?", (k,)); return json.loads(r[0]["v"]) if r else None
def kset(k, v): q("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

def init():
    q("CREATE TABLE IF NOT EXISTS courts(id TEXT PRIMARY KEY, name TEXT, type TEXT, pos INTEGER)")
    q("CREATE TABLE IF NOT EXISTS replays(n INTEGER PRIMARY KEY AUTOINCREMENT, court TEXT, ts INTEGER, label TEXT DEFAULT '')")
    q("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    if not q("SELECT 1 FROM courts")[0]:
        for i, (cid, n, t) in enumerate([("society", "Quadra Society", "society"), ("volei", "Quadra Vôlei", "volei"), ("campo", "Campo", "campo")]):
            q("INSERT INTO courts VALUES(?,?,?,?)", (cid, n, t, i))
    if kget("cfg") is None: kset("cfg", DEF_CFG)
    if kget("txt") is None: kset("txt", DEF_TXT)

def rp(r):
    i = f"RPL-{r['n']:04d}"
    return {"id": i, "court": r["court"], "ts": r["ts"], "label": r["label"], "video": f"/api/replays/{i}/video"}
def rnum(rid):
    if not re.fullmatch(r"RPL-\d{4,}", rid): abort(404)
    return int(rid[4:])
def rfile(n): return os.path.join(OUT, f"RPL-{n:04d}.mp4")

def auth(need):
    def deco(f): return f
    return deco

def seg_time(p): return datetime.strptime(os.path.basename(p)[:-3], "%Y%m%d-%H%M%S").timestamp()

def record(cid, rtsp):
    d = os.path.join(BUF, cid)
    os.makedirs(d, exist_ok=True)
    while True:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
            "-use_wallclock_as_timestamps", "1", "-i", rtsp, "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "setpts=PTS-STARTPTS", "-af", "asetpts=PTS-STARTPTS", "-c:v", "libx264",
            "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-r", "15",
            "-g", "30", "-sc_threshold", "0", "-force_key_frames", f"expr:gte(t,n_forced*{SEG})",
            "-c:a", "aac", "-b:a", "128k", "-ar", "16000", "-ac", "1", "-f", "segment",
            "-segment_time", str(SEG), "-segment_format", "mpegts", "-strftime", "1",
            "-reset_timestamps", "1", "-avoid_negative_ts", "make_zero",
            os.path.join(d, "%Y%m%d-%H%M%S.ts")
        ])
        print(f"[{cid}] câmera desconectada, tentando de novo em 5s")
        time.sleep(5)

def cleaner():
    while True:
        for f in glob.glob(os.path.join(BUF, "*", "*.ts")):
            try:
                if time.time() - os.path.getmtime(f) > KEEP: os.remove(f)
            except OSError: pass
        if KEEP_DAYS:
            lim = int((time.time() - KEEP_DAYS * 86400) * 1000)
            for r in q("SELECT n FROM replays WHERE ts < ?", (lim,))[0]:
                try: os.remove(rfile(r["n"]))
                except OSError: pass
            q("DELETE FROM replays WHERE ts < ?", (lim,))
        time.sleep(10)

def salvar(cid, t):
    time.sleep(POST + SEG + 1)
    ini, fim = t - PRE, t + POST
    segs = [s for s in sorted(glob.glob(os.path.join(BUF, cid, "*.ts"))) if seg_time(s) + SEG >= ini and seg_time(s) <= fim]
    if not segs: print(f"[{cid}] sem vídeo no buffer, replay não salvo"); return
    lst = os.path.join(BUF, cid, f"{int(t)}.txt"); tmp = os.path.join(OUT, f"tmp-{cid}-{int(t)}.mp4")
    open(lst, "w").write("".join(f"file '{os.path.abspath(s)}'\n" for s in segs))
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", lst,
                        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", tmp])
    os.remove(lst)
    if r.returncode or not os.path.exists(tmp):
        print(f"[{cid}] falha ao montar o replay")
        if os.path.exists(tmp): os.remove(tmp)
        return
    _, n = q("INSERT INTO replays(court, ts) VALUES(?,?)", (cid, int(t * 1000)))
    os.replace(tmp, rfile(n)); print(f"[{cid}] replay RPL-{n:04d} salvo")

last_click = {}
def trigger(cid):
    if not q("SELECT 1 FROM courts WHERE id=?", (cid,))[0]: return "quadra inexistente", 404
    now = time.time()
    if now - last_click.get(cid, 0) < DEBOUNCE: return "ignorado (clique repetido)", 200
    last_click[cid] = now
    threading.Thread(target=salvar, args=(cid, now), daemon=True).start()
    return "ok", 200

@app.route("/click", methods=["GET", "POST"])
def click():
    key = request.args.get("key") or request.headers.get("X-Key", "")
    if not hmac.compare_digest(key, CFG["device_key"]): abort(401)
    return trigger(request.args.get("court", ""))

@app.post("/api/courts/<cid>/click")
@auth("staff")
def click_test(cid): return trigger(cid)

@app.get("/api/state")
@auth("staff")
def state():
    courts = [dict(id=r["id"], name=r["name"], type=r["type"]) for r in q("SELECT * FROM courts ORDER BY pos")[0]]
    reps = [rp(r) for r in q("SELECT * FROM replays ORDER BY ts DESC")[0]]
    mb = round(sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT)) / 1048576, 1)
    return jsonify(courts=courts, replays=reps, cfg=kget("cfg"), txt={**DEF_TXT, **kget("txt")}, mb=mb, keep_days=KEEP_DAYS)

@app.get("/api/replays/<rid>/video")
@auth("staff")
def video(rid):
    f = rfile(rnum(rid))
    if not os.path.exists(f): abort(404)
    return send_file(f, mimetype="video/mp4", conditional=True)

@app.delete("/api/replays/<rid>")
@auth("staff")
def del_replay(rid):
    n = rnum(rid); q("DELETE FROM replays WHERE n=?", (n,))
    if os.path.exists(rfile(n)): os.remove(rfile(n))
    return jsonify(ok=True)

@app.patch("/api/replays/<rid>")
@auth("editor")
def rename_replay(rid):
    q("UPDATE replays SET label=? WHERE n=?", (str((request.get_json(silent=True) or {}).get("label", ""))[:40], rnum(rid)))
    return jsonify(ok=True)

def court_body():
    b = request.get_json(silent=True) or {}; name = str(b.get("name", "")).strip()[:30]
    if not name or b.get("type") not in ("society", "volei", "campo"): abort(400)
    return name, b["type"]

@app.post("/api/courts")
@auth("editor")
def new_court():
    name, t = court_body()
    nums = [int(r["id"][1:]) for r in q("SELECT id FROM courts")[0] if re.fullmatch(r"q\d+", r["id"])]
    cid = f"q{max(nums + [3]) + 1}"
    q("INSERT INTO courts VALUES(?,?,?,(SELECT COALESCE(MAX(pos),0)+1 FROM courts))", (cid, name, t))
    return jsonify(id=cid, name=name, type=t)

@app.patch("/api/courts/<cid>")
@auth("editor")
def edit_court(cid):
    name, t = court_body(); q("UPDATE courts SET name=?, type=? WHERE id=?", (name, t, cid)); return jsonify(ok=True)

@app.delete("/api/courts/<cid>")
@auth("editor")
def del_court(cid):
    if len(q("SELECT id FROM courts")[0]) < 2: return jsonify(error="Mantenha ao menos uma quadra"), 400
    for r in q("SELECT n FROM replays WHERE court=?", (cid,))[0]:
        if os.path.exists(rfile(r["n"])): os.remove(rfile(r["n"]))
    q("DELETE FROM replays WHERE court=?", (cid,)); q("DELETE FROM courts WHERE id=?", (cid,)); return jsonify(ok=True)

@app.put("/api/settings")
@auth("editor")
def settings():
    b = request.get_json(silent=True) or {}; cfg, txt = kget("cfg"), kget("txt")
    for k in ("a", "b"):
        if str(b.get("cfg", {}).get(k, "")).strip(): cfg[k] = str(b["cfg"][k]).strip()[:12]
    if re.fullmatch(r"#[0-9a-fA-F]{6}", str(b.get("cfg", {}).get("accent", ""))): cfg["accent"] = b["cfg"]["accent"]
    for k in DEF_TXT:
        if str(b.get("txt", {}).get(k, "")).strip(): txt[k] = str(b["txt"][k]).strip()[:30]
    kset("cfg", cfg); kset("txt", txt); return jsonify(cfg=cfg, txt=txt)

@app.get("/")
def index(): return send_from_directory(os.path.join(BASE, "static"), "index.html")

PWA = {"manifest.json": "application/manifest+json", "sw.js": "text/javascript",
       "icon-192.png": "image/png", "icon-512.png": "image/png"}
@app.get("/<name>")
def pwa(name):
    if name not in PWA: abort(404)
    return send_from_directory(os.path.join(BASE, "static"), name, mimetype=PWA[name])

def check_license_loop():
    url = CFG.get("license_url", "").strip("/")
    key = CFG.get("license_key", "").strip()
    if not url or not key:
        print("[AVISO] Licença não configurada no config.json")
        return
    while True:
        try:
            r = requests.post(f"{url}/api/check", json={"key": key}, timeout=10)
            data = r.json()
            if data.get("active"):
                print(f"[LICENÇA] ✅ OK - Arena: {data.get('name')}")
            else:
                print(f"[LICENÇA] ❌ BLOQUEADA! {data.get('message')}")
        except Exception as e:
            print(f"[LICENÇA] Erro de conexão: {e}")
        time.sleep(3600)

if __name__ == "__main__":
    init()
    threading.Thread(target=check_license_loop, daemon=True).start()
    for cid, cam in CFG.get("cameras", {}).items():
        threading.Thread(target=record, args=(cid, cam["rtsp"]), daemon=True).start()
    threading.Thread(target=cleaner, daemon=True).start()
    app.run(host="0.0.0.0", port=CFG.get("port", 5000), threaded=True)

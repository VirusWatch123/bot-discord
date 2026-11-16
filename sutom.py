import os, json, sqlite3, secrets, time, asyncio, unicodedata, datetime as dt
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from aiohttp import web

OWNER_ID = 1258814145969782845
BASE = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "http://localhost:8080").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))
TZ, MAX = ZoneInfo("Europe/Paris"), 6
EM = ["🟦", "🟡", "🟥"]

db = sqlite3.connect("sutom.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS admins(guild INT, user INT, PRIMARY KEY(guild,user));
CREATE TABLE IF NOT EXISTS words(guild INT, day TEXT, word TEXT, PRIMARY KEY(guild,day));
CREATE TABLE IF NOT EXISTS games(guild INT, user INT, day TEXT, guesses TEXT, done INT, won INT, PRIMARY KEY(guild,user,day));
CREATE TABLE IF NOT EXISTS sessions(tok TEXT PRIMARY KEY, guild INT, user INT, channel INT, name TEXT, avatar TEXT, gname TEXT, exp REAL);
""")

def norm(s):
    s = unicodedata.normalize("NFD", s).upper()
    return "".join(c for c in s if "A" <= c <= "Z")

# Dictionnaire facultatif : un mot par ligne dans mots.txt (sans ça, tout mot de la bonne longueur est accepté)
WORDS = set()
if os.path.exists("mots.txt"):
    WORDS = {norm(l) for l in open("mots.txt", encoding="utf-8") if l.strip()}

def today():
    return dt.datetime.now(TZ).date().isoformat()

def current_word(g):
    return db.execute("SELECT day,word FROM words WHERE guild=? AND day<=? ORDER BY day DESC LIMIT 1", (g, today())).fetchone()

def is_admin(g, u):
    return u == OWNER_ID or db.execute("SELECT 1 FROM admins WHERE guild=? AND user=?", (g, u)).fetchone() is not None

def score(g, w):
    r, left = [0] * len(w), list(w)
    for i, c in enumerate(g):
        if c == w[i]: r[i] = 2; left[i] = None
    for i, c in enumerate(g):
        if r[i] == 0 and c in left: r[i] = 1; left[left.index(c)] = None
    return r

# ───────────────────────────── Intégration ─────────────────────────────
bot = None
_admin = None

async def guard(i):
    if (_admin or is_admin)(i.guild_id, i.user.id): return True
    await i.response.send_message("Commande réservée aux admins du bot.", ephemeral=True)
    return False

@app_commands.command(name="sutom-mot", description="Définir le mot du jour (admins)")
@app_commands.describe(mot="Le mot à deviner (5 à 12 lettres)", date="JJ/MM/AAAA (par défaut : aujourd'hui)")
@app_commands.guild_only()
async def setmot(i: discord.Interaction, mot: str, date: str = None):
    if not await guard(i): return
    w = norm(mot)
    if not 5 <= len(w) <= 12:
        return await i.response.send_message("Le mot doit faire entre 5 et 12 lettres.", ephemeral=True)
    try:
        d = dt.datetime.strptime(date, "%d/%m/%Y").date().isoformat() if date else today()
    except ValueError:
        return await i.response.send_message("Date invalide, format JJ/MM/AAAA.", ephemeral=True)
    db.execute("INSERT OR REPLACE INTO words VALUES(?,?,?)", (i.guild_id, d, w)); db.commit()
    await i.response.send_message(f"✅ Mot enregistré pour le {d} : ||{w}||", ephemeral=True)

@app_commands.command(name="sutom", description="Jouer au SUTOM du jour")
@app_commands.guild_only()
async def sutom(i: discord.Interaction):
    if not current_word(i.guild_id):
        return await i.response.send_message("Aucun mot du jour pour l'instant.", ephemeral=True)
    tok = secrets.token_urlsafe(16)
    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (tok, i.guild_id, i.user.id, i.channel_id,
               i.user.display_name, i.user.display_avatar.url, i.guild.name, time.time() + 43200)); db.commit()
    v = discord.ui.View(); v.add_item(discord.ui.Button(label="Ouvrir le SUTOM", emoji="🟥", url=f"{BASE}/play/{tok}"))
    await i.response.send_message("Ta grille est prête. Ce lien est personnel, ne le partage pas.", view=v, ephemeral=True)

@app_commands.command(name="sutom-classement", description="Classement du serveur")
@app_commands.guild_only()
async def top(i: discord.Interaction):
    rows = db.execute("SELECT user,SUM(won),COUNT(*) FROM games WHERE guild=? AND done=1 GROUP BY user "
                      "ORDER BY 2 DESC,3 ASC LIMIT 10", (i.guild_id,)).fetchall()
    txt = "\n".join(f"**{n}.** <@{u}> — {w} victoire(s) sur {c} partie(s)" for n, (u, w, c) in enumerate(rows, 1)) or "Personne n'a encore joué."
    await i.response.send_message(embed=discord.Embed(title="🏆 Classement SUTOM", description=txt, color=0xE5162D),
                                  allowed_mentions=discord.AllowedMentions.none())

@app_commands.command(name="sutom-add", description="Donner l'accès aux commandes admin du SUTOM")
@app_commands.guild_only()
async def add(i: discord.Interaction, membre: discord.Member):
    if not await guard(i): return
    db.execute("INSERT OR IGNORE INTO admins VALUES(?,?)", (i.guild_id, membre.id)); db.commit()
    await i.response.send_message(f"✅ {membre.mention} a maintenant accès aux commandes admin.", ephemeral=True)

@app_commands.command(name="sutom-remove", description="Retirer l'accès aux commandes admin")
@app_commands.guild_only()
async def remove(i: discord.Interaction, membre: discord.Member):
    if not await guard(i): return
    db.execute("DELETE FROM admins WHERE guild=? AND user=?", (i.guild_id, membre.id)); db.commit()
    await i.response.send_message(f"✅ Accès retiré à {membre.mention}.", ephemeral=True)

# ───────────────────────────── Serveur web ─────────────────────────────
def sess(t):
    return db.execute("SELECT guild,user,channel,name,avatar,gname FROM sessions WHERE tok=? AND exp>?", (t, time.time())).fetchone()

def bad(msg, code=400):
    return web.json_response({"error": msg}, status=code)

def load(s):
    day, w = current_word(s[0])
    g = db.execute("SELECT guesses,done,won FROM games WHERE guild=? AND user=? AND day=?", (s[0], s[1], day)).fetchone()
    return day, w, (json.loads(g[0]) if g else []), bool(g and g[1]), bool(g and g[2])

async def ping(r): return web.Response(text="ok")
async def page(r): return web.Response(text=PAGE, content_type="text/html")

async def state(r):
    s = sess(r.match_info["t"])
    if not s or not current_word(s[0]): return bad("Lien expiré. Relance /sutom sur Discord.", 404)
    day, w, gs, done, won = load(s)
    return web.json_response({"length": len(w), "first": w[0], "max": MAX, "done": done, "won": won,
        "answer": w if done else None, "name": s[3], "avatar": s[4], "gname": s[5],
        "guesses": [{"w": x, "r": score(x, w)} for x in gs]})

async def guess(r):
    s = sess(r.match_info["t"])
    if not s or not current_word(s[0]): return bad("Lien expiré. Relance /sutom sur Discord.", 404)
    day, w, gs, done, _ = load(s)
    g = norm((await r.json()).get("word", ""))
    if done: return bad("Partie terminée")
    if len(g) != len(w): return bad(f"Le mot doit faire {len(w)} lettres")
    if g[0] != w[0]: return bad(f"Le mot doit commencer par {w[0]}")
    if WORDS and g not in WORDS: return bad("Ce mot n'est pas dans le dictionnaire")
    gs.append(g); won = g == w; done = won or len(gs) >= MAX
    db.execute("INSERT OR REPLACE INTO games VALUES(?,?,?,?,?,?)", (s[0], s[1], day, json.dumps(gs), int(done), int(won))); db.commit()
    if done: asyncio.create_task(announce(s, gs, w, won))
    return web.json_response({"guess": {"w": g, "r": score(g, w)}, "done": done, "won": won, "answer": w if done else None})

async def announce(s, gs, w, won):  # grille d'emojis sans les lettres, pas de spoil
    ch = bot.get_channel(s[2])
    if not ch: return
    grid = "\n".join("".join(EM[v] for v in score(x, w)) for x in gs)
    t = f"**{s[3]}** a trouvé le mot du jour en **{len(gs)}/{MAX}** 🎉" if won else f"**{s[3]}** n'a pas trouvé le mot du jour 😵"
    try: await ch.send(f"{t}\n{grid}")
    except Exception: pass

PAGE = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SUTOM</title><link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@500;700;800&display=swap" rel="stylesheet">
<style>
:root{--c:#e5162d;--p:#ffc01e;--t:#1a4bb0}*{box-sizing:border-box;margin:0}
body{min-height:100vh;background:radial-gradient(120% 80% at 50% -10%,#1a4bb0,#0a2463 45%,#06143a);color:#fff;font-family:'Bricolage Grotesque',system-ui,sans-serif;overflow-x:hidden}
#fx{position:fixed;inset:0;pointer-events:none}
main{position:relative;max-width:540px;margin:0 auto;padding:20px 16px 28px;display:flex;flex-direction:column;align-items:center;gap:18px}
header{width:100%;display:flex;justify-content:space-between;align-items:center;gap:10px}
.brand{display:flex;gap:5px}.brand .t{--s:34px}
.who{display:flex;align-items:center;gap:10px;font-weight:700;padding:5px 14px 5px 5px;border-radius:99px;background:rgba(255,255,255,.08);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.14);min-width:0}
.who img{width:30px;height:30px;border-radius:50%}.who span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#sub{opacity:.7;font-size:14px;text-align:center}
#grid{display:grid;gap:6px;perspective:800px}.row{display:grid;gap:6px}
.t{width:var(--s);height:var(--s);display:grid;place-items:center;font-weight:800;font-size:calc(var(--s)*.52);background:rgba(255,255,255,.06);border:2px solid rgba(160,190,255,.25);border-radius:6px;position:relative;z-index:0;transition:border-color .15s}
.t.f{border-color:#fff}.t.h{color:rgba(255,255,255,.35)}
.t.c{background:var(--c);border-color:#ff6b7d;box-shadow:0 0 18px rgba(229,22,45,.45)}
.t.p{border-color:transparent;color:#2a1c00}.t.p::before{content:'';position:absolute;inset:1px;border-radius:50%;background:var(--p);z-index:-1;box-shadow:0 0 16px rgba(255,192,30,.5)}
.t.a{background:var(--t);border-color:#3d6fd8}
.flip{animation:flip .6s ease both}@keyframes flip{50%{transform:rotateX(90deg)}}
.shake{animation:sh .45s}@keyframes sh{20%,60%{transform:translateX(-7px)}40%,80%{transform:translateX(7px)}}
#kb{display:flex;flex-direction:column;gap:6px;width:100%}.kr{display:flex;gap:5px;justify-content:center}
.k{flex:1;max-width:44px;height:50px;border:0;border-radius:8px;background:rgba(255,255,255,.14);color:#fff;font-weight:700;font-size:15px;font-family:inherit;cursor:pointer;transition:transform .08s,background .25s}
.k.w{max-width:66px;font-size:12px}.k:active{transform:scale(.92)}.k:focus-visible{outline:2px solid #fff}
.k.c{background:var(--c)}.k.p{background:var(--p);color:#2a1c00}.k.a{background:rgba(0,0,0,.35);color:rgba(255,255,255,.4)}
#toast{position:fixed;top:22px;left:50%;transform:translate(-50%,-90px);background:#fff;color:#0a2463;padding:10px 18px;border-radius:99px;font-weight:700;transition:transform .35s cubic-bezier(.3,1.5,.5,1);z-index:9}
#toast.on{transform:translate(-50%,0)}
#modal{position:fixed;inset:0;display:grid;place-items:center;background:rgba(3,10,35,.7);backdrop-filter:blur(6px);z-index:8;padding:20px}#modal[hidden]{display:none}
.card{background:linear-gradient(160deg,#16419c,#0a2463);border:1px solid rgba(255,255,255,.2);border-radius:22px;padding:28px;text-align:center;max-width:380px;width:100%;animation:pop .5s cubic-bezier(.3,1.4,.5,1)}
@keyframes pop{from{transform:scale(.7);opacity:0}}
.card h2{font-size:32px}.ans{font-size:34px;font-weight:800;letter-spacing:.12em;margin:10px 0}.card pre{font-family:inherit;font-size:20px;line-height:1.3;margin:12px 0}
.btn{padding:12px 22px;border:0;border-radius:99px;font-weight:700;font-size:15px;font-family:inherit;background:var(--p);color:#2a1c00;cursor:pointer;margin:4px}.btn.g{background:rgba(255,255,255,.14);color:#fff}
.err{margin-top:40px;font-size:18px;text-align:center}
@media(prefers-reduced-motion:reduce){*{animation-duration:.01s!important}}
</style></head><body><canvas id="fx"></canvas>
<main id="app"><header><div class="brand" id="brand"></div><div class="who"><img id="av" alt=""><span id="nm"></span></div></header>
<p id="sub"></p><div id="grid"></div><div id="kb"></div></main><div id="toast"></div>
<div id="modal" hidden><div class="card"><h2 id="mt"></h2><div class="ans" id="ma"></div><pre id="mp"></pre>
<button class="btn" id="cp">Copier ma grille</button><button class="btn g" id="cl">Fermer</button></div></div>
<script>
const tok=location.pathname.split('/').pop(),$=s=>document.querySelector(s),rows=[];let S,typed='',busy=false,tt;
const api=async(p,o)=>{const r=await fetch('/api/'+p+'/'+tok,o);return[r.ok,await r.json()]};
// fond : lettres qui dérivent + confettis (carrés rouges, ronds jaunes)
const cv=$('#fx'),cx=cv.getContext('2d');let W,H,L=[],P=[];
const rs=()=>{W=cv.width=innerWidth;H=cv.height=innerHeight};rs();addEventListener('resize',rs);
for(let i=0;i<34;i++)L.push({x:Math.random()*1600,y:Math.random()*1600,s:24+Math.random()*70,v:.08+Math.random()*.3,c:'SUTOMABCDEFGHIJKLMNOPQRSTUVWXYZ'[i]});
function loop(){cx.clearRect(0,0,W,H);cx.fillStyle='rgba(130,170,255,.07)';
L.forEach(l=>{l.y-=l.v;if(l.y<-120){l.y=H+120;l.x=Math.random()*W}cx.font=`800 ${l.s}px 'Bricolage Grotesque',sans-serif`;cx.fillText(l.c,l.x%W,l.y)});
P=P.filter(p=>p.y<H+30);P.forEach(p=>{p.x+=p.vx;p.y+=p.vy;p.vy+=.2;p.a+=p.va;cx.save();cx.translate(p.x,p.y);cx.rotate(p.a);cx.fillStyle=p.c;
if(p.r){cx.beginPath();cx.arc(0,0,p.z/2,0,7);cx.fill()}else cx.fillRect(-p.z/2,-p.z/2,p.z,p.z);cx.restore()});requestAnimationFrame(loop)}
if(!matchMedia('(prefers-reduced-motion:reduce)').matches)loop();
const confetti=()=>{for(let i=0;i<150;i++)P.push({x:W/2,y:H*.55,vx:(Math.random()-.5)*15,vy:-Math.random()*17-4,a:0,va:(Math.random()-.5)*.4,z:8+Math.random()*10,c:['#e5162d','#ffc01e','#fff','#4f8dff'][i%4],r:i%3==0})};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('on'),2000)}
function build(){const g=$('#grid'),n=S.length,sz=Math.min(58,(Math.min(innerWidth,540)-40)/n-6);g.innerHTML='';g.style.setProperty('--s',sz+'px');
for(let r=0;r<S.max;r++){const d=document.createElement('div');d.className='row';d.style.gridTemplateColumns=`repeat(${n},var(--s))`;const ts=[];
for(let i=0;i<n;i++){const t=document.createElement('div');t.className='t';d.append(t);ts.push(t)}g.append(d);rows.push(ts)}}
function hints(){const h=Array(S.length).fill('');h[0]=S.first;S.guesses.forEach(x=>x.r.forEach((v,i)=>{if(v==2)h[i]=x.w[i]}));return h}
function paint(){const cur=S.guesses.length,h=hints();rows.forEach((ts,r)=>ts.forEach((t,i)=>{
if(r<cur){t.textContent=S.guesses[r].w[i];t.className='t '+'apc'[S.guesses[r].r[i]]}
else if(r==cur&&!S.done){const ch=typed[i];t.textContent=ch||h[i]||'·';t.className='t'+(ch?' f':' h')}
else{t.textContent='';t.className='t'}}));$('#sub').textContent=S.done?'Partie terminée':`Essai ${Math.min(cur+1,S.max)} sur ${S.max}`}
function ukeys(){const st={},rk={a:0,p:1,c:2};S.guesses.forEach(x=>[...x.w].forEach((ch,i)=>{const c='apc'[x.r[i]];if(!(ch in st)||rk[c]>rk[st[ch]])st[ch]=c}));
document.querySelectorAll('.k').forEach(b=>{const c=st[b.dataset.k];if(c)b.className='k'+(b.classList.contains('w')?' w':'')+' '+c})}
function keys(){['AZERTYUIOP','QSDFGHJKLM',['ENTER',...'WXCVBN','⌫']].forEach(r=>{const d=document.createElement('div');d.className='kr';
[...r].forEach(k=>{const b=document.createElement('button');b.className='k'+(k.length>1||k=='⌫'?' w':'');b.textContent=k=='ENTER'?'Entrée':k;b.dataset.k=k;b.onclick=()=>press(k);d.append(b)});$('#kb').append(d)})}
function press(k){if(!S||S.done||busy)return;if(k=='ENTER')return submit();
if(k=='⌫'){if(typed.length>1)typed=typed.slice(0,-1);return paint()}
if(/^[A-Z]$/.test(k)&&typed.length<S.length){typed+=k;paint()}}
addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey)return;let k=e.key=='Backspace'?'⌫':e.key=='Enter'?'ENTER':e.key.length==1?e.key.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toUpperCase():'';if(k)press(k)});
function shake(m){toast(m);const r=$('#grid').children[S.guesses.length];r.classList.add('shake');setTimeout(()=>r.classList.remove('shake'),450)}
async function submit(){if(typed.length<S.length)return shake('Mot trop court');busy=true;
const[ok,d]=await api('guess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({word:typed})});
if(!ok){busy=false;return shake(d.error)}
const r=S.guesses.length,n=S.length;rows[r].forEach((t,i)=>{t.textContent=d.guess.w[i];t.style.animationDelay=i*.2+'s';t.className='t flip';setTimeout(()=>t.className='t flip '+'apc'[d.guess.r[i]],i*200+300)});
setTimeout(()=>{S.guesses.push(d.guess);S.done=d.done;S.won=d.won;S.answer=d.answer;typed=S.first;busy=false;paint();ukeys();if(d.done)end(d.won)},n*200+700)}
function end(fx){const g=S.guesses.map(x=>x.r.map(v=>EMJ[v]).join('')).join('\n');
$('#mt').textContent=S.won?'Bravo !':'Raté…';$('#ma').textContent=S.answer;$('#mp').textContent=g;$('#modal').hidden=false;
$('#cp').onclick=()=>{navigator.clipboard.writeText(`SUTOM ${S.gname} ${S.won?S.guesses.length:'X'}/${S.max}\n${g}`);$('#cp').textContent='Copié !'};
if(fx&&S.won)confetti()}
const EMJ=['🟦','🟡','🟥'];$('#cl').onclick=()=>$('#modal').hidden=true;
(async()=>{[...'SUTOM'].forEach((c,i)=>{const t=document.createElement('div');t.className='t flip';t.textContent=c;t.style.animationDelay=i*.15+'s';
setTimeout(()=>t.className='t flip '+(i==0?'c':i==1?'p':'a'),i*150+300);$('#brand').append(t)});
const[ok,d]=await api('state');if(!ok){$('#app').innerHTML='<p class="err">'+d.error+'</p>';return}
S=d;typed=S.first;$('#av').src=S.avatar;$('#nm').textContent=S.name;build();keys();paint();ukeys();if(S.done)end(false)})();
</script></body></html>'''

async def setup(client, tree=None, admin_check=None):
    """À appeler UNE seule fois, avant tree.sync(). admin_check(guild_id, user_id) -> bool est facultatif."""
    global bot, _admin
    bot, _admin = client, admin_check
    for c in (setmot, sutom, top, add, remove):
        (tree or client.tree).add_command(c)
    app = web.Application()
    app.add_routes([web.get("/", ping), web.get("/ping", ping), web.get("/play/{t}", page),
                    web.get("/api/state/{t}", state), web.post("/api/guess/{t}", guess)])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()   # sert aussi de keep alive (/ping)

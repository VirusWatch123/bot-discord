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
CREATE TABLE IF NOT EXISTS extra(guild INT, word TEXT, PRIMARY KEY(guild,word));
CREATE TABLE IF NOT EXISTS sessions(tok TEXT PRIMARY KEY, guild INT, user INT, channel INT, name TEXT, avatar TEXT, gname TEXT, exp REAL);
""")

def norm(s):
    s = unicodedata.normalize("NFD", s.replace("œ", "oe").replace("Œ", "OE").replace("æ", "ae")).upper()
    return "".join(c for c in s if "A" <= c <= "Z")

# Dictionnaire facultatif : un mot par ligne dans mots.txt (sans ça, tout mot de la bonne longueur est accepté)
import re
WORDS = set()
LETTERS = re.compile(r"[a-zàâäçéèêëîïôöùûüÿœæ]+")
CACHE = "mots_fr.txt"
SOURCES = [  # liste de ~336 000 mots français (formes conjuguées et accordées comprises)
    "https://cdn.jsdelivr.net/npm/an-array-of-french-words@2/index.json",
    "https://raw.githubusercontent.com/words/an-array-of-french-words/main/index.json",
    "https://raw.githubusercontent.com/words/an-array-of-french-words/master/index.json",
]
for f in ("mots.txt", CACHE):  # un mot par ligne, tu peux aussi poser ton propre mots.txt
    if os.path.exists(f):
        WORDS |= {norm(l) for l in open(f, encoding="utf-8") if l.strip()}


GAMES = """
minecraft creeper enderman ender dragon zombie squelette araignee sorciere villageois pillard golem nether redstone obsidienne
diamant emeraude netherite lingot pioche epee hache pelle armure bouclier fleche piston creuser miner craft enclume fournaise
coffre torche lave bedrock herobrine steve alex warden wither blaze ghast piglin hoglin shulker phantom axolotl lama panda renard
abeille ocelot cochon vache mouton poulet loup cheval tortue dauphin calmar elytres trident boussole potion enchanter
zelda link ganon ganondorf hyrule triforce navi epona goron zora gerudo kokiri sheikah moblin bokoblin lizalfos octorok mipha urbosa
revali daruk impa midona tingle ocarina korogu hylien rubis boomerang tektite sidon kass paraglider
fnaf freddy fazbear bonnie chica foxy springtrap marionnette mangle withered nightmare pizzeria animatronique vanny glitchtrap
funtime ennard fredbear lefty schmidt balloon bidybab
fortnite victoire planeur tilted retail pleasant tempete emote skin passe jonesy peely fishstick raven drift midas meowscles
cuddle lynx loot butin tomate pioche glisse reboot
roblox robux avatar obby brookhaven bloxburg jailbreak piggy tycoon simulateur noob builderman studio
pokemon pikachu salameche carapuce bulbizarre evoli ronflex dracaufeu mewtwo pokeball pokedex dresseur miaouss rattata arcanin
mentali noctali aquali voltali pyroli phyllali givrali nymphali lucario tortank florizarre magicarpe leviator rondoudou togepi
psykokwak pichu raichu ectoplasma metamorph
mario luigi peach bowser yoshi toad wario waluigi goomba koopa champignon daisy rosalina kart odyssey
sonic tails knuckles eggman robotnik
undertale sans papyrus toriel asgore frisk chara terraria stardew pelican isabelle fallout nuka halo spartan glados freeman skyrim
geralt sorceleur witcher hollow knight genshin paimon primogemmes overwatch tracer genji reinhardt valorant brawler shelly colt
brawl clash royale warzone trevor franklin michael
gamer streamer respawn spawn lag speedrun farm build rush camper headshot sniper boss quete niveau manette console pixel joueur
imposteur equipage crewmate cafeteria reacteur sabotage
""".split()
GAMES_SET = {norm(w) for w in GAMES}
if os.path.exists("jeux.txt"):  # ajoute ici tes propres mots (un par ligne)
    GAMES_SET |= {norm(l) for l in open("jeux.txt", encoding="utf-8") if l.strip()}

def extra_ok(guild, g):
    return g in GAMES_SET or db.execute("SELECT 1 FROM extra WHERE guild=? AND word=?", (guild, g)).fetchone() is not None


_GH = "https://raw.githubusercontent.com/killerboyyy777/mc-og-name-hunter/main/data/word_lists/"
GAME_SOURCES = [  # listes publiques : objets/mobs Minecraft, termes de gamer, personnages de franchises, Pokémon...
    _GH + "minecraft_items.txt", _GH + "minecraft_ecosystem.txt", _GH + "gaming_terms.txt",
    _GH + "franchise_characters.txt",
    "https://raw.githubusercontent.com/cervoise/pentest-scripts/master/password-cracking/wordlists/pokemon-list-en.txt",
]
GCACHE = "jeux_web.txt"

def _tokens(text):
    out = set()
    for line in text.splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#"): continue
        parts = [p for p in re.split(r"[_\s\-.]+", line) if LETTERS.fullmatch(p)]
        out |= {norm(p) for p in parts if 3 <= len(p) <= 14}
        j = "".join(parts)
        if len(parts) > 1 and 3 <= len(j) <= 14: out.add(norm(j))
    return out

async def load_games():
    """Télécharge les listes de mots de jeux au démarrage (puis cache)."""
    if os.path.exists(GCACHE):
        GAMES_SET.update(l.strip() for l in open(GCACHE, encoding="utf-8") if l.strip()); return
    import aiohttp
    found = set()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as ses:
        for url in GAME_SOURCES:
            try:
                async with ses.get(url) as r:
                    if r.status != 200:
                        print("[sutom] liste de jeux introuvable", r.status, url); continue
                    found |= _tokens(await r.text())
            except Exception as e:
                print("[sutom] liste de jeux indisponible", url, e)
    if found:
        GAMES_SET.update(found)
        open(GCACHE, "w", encoding="utf-8").write("\n".join(sorted(found)))
    print(f"[sutom] mots de jeux téléchargés : {len(found)}")

async def load_words():
    """Télécharge le dictionnaire au démarrage (une seule fois, puis cache). Sans lui, tout mot est accepté."""
    if WORDS: return
    import aiohttp
    for url in SOURCES:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as ses:
                async with ses.get(url) as r:
                    if r.status != 200: continue
                    data = json.loads(await r.text())
            ws = {norm(w) for w in data if isinstance(w, str) and LETTERS.fullmatch(w) and 3 <= len(w) <= 14}
            if len(ws) < 1000: continue
            WORDS.update(ws)
            open(CACHE, "w", encoding="utf-8").write("\n".join(sorted(ws)))
            print(f"[sutom] dictionnaire chargé : {len(ws)} mots")
            return
        except Exception as e:
            print("[sutom] dictionnaire indisponible via", url, e)
    print("[sutom] AUCUN dictionnaire chargé : tous les mots sont acceptés")

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

@app_commands.command(name="sutom-mots", description="Ajouter des mots acceptés : jeux, personnages... (admins)")
@app_commands.describe(mots="Mots séparés par des espaces ou des virgules")
@app_commands.guild_only()
async def addmots(i: discord.Interaction, mots: str):
    if not await guard(i): return
    ws = {norm(w) for w in re.split(r"[\s,;]+", mots) if 3 <= len(norm(w)) <= 14}
    db.executemany("INSERT OR IGNORE INTO extra VALUES(?,?)", [(i.guild_id, w) for w in ws]); db.commit()
    await i.response.send_message(f"✅ {len(ws)} mot(s) ajouté(s) à la liste acceptée.", ephemeral=True)

@app_commands.command(name="sutom-profil", description="Voir les statistiques SUTOM d'un joueur")
@app_commands.describe(membre="Le joueur (toi par défaut)")
@app_commands.guild_only()
async def profil(i: discord.Interaction, membre: discord.Member = None):
    m = membre or i.user
    rows = db.execute("SELECT g.day,g.guesses,g.won,w.word FROM games g JOIN words w ON w.guild=g.guild AND w.day=g.day "
                      "WHERE g.guild=? AND g.user=? AND g.done=1 ORDER BY g.day", (i.guild_id, m.id)).fetchall()
    if not rows:
        return await i.response.send_message(f"{m.display_name} n'a pas encore terminé de partie.", ephemeral=True)
    played, dist, red, yel, blue, tries, cur, best, won_tries = len(rows), [0] * MAX, 0, 0, 0, 0, 0, 0, []
    for day, gj, won, w in rows:
        gs = json.loads(gj); tries += len(gs)
        for x in gs:
            for v in score(x, w):
                if v == 2: red += 1
                elif v == 1: yel += 1
                else: blue += 1
        if won:
            dist[len(gs) - 1] += 1; won_tries.append(len(gs)); cur += 1; best = max(best, cur)
        else:
            cur = 0
    wins = len(won_tries)
    ranking = [u for u, _ in db.execute("SELECT user,SUM(won) w FROM games WHERE guild=? AND done=1 GROUP BY user ORDER BY w DESC", (i.guild_id,))]
    top = max(dist) or 1
    bars = "\n".join(f"{n}  {'█' * (max(1, round(10 * c / top)) if c else 0):<10} {c}" for n, c in enumerate(dist, 1))
    e = discord.Embed(title=f"Profil SUTOM de {m.display_name}", color=0xE5162D)
    e.set_thumbnail(url=m.display_avatar.url)
    e.add_field(name="📊 Parties", value=f"**{played}** jouées\n**{wins}** gagnées ({round(100 * wins / played)} %)\n**{played - wins}** ratées")
    e.add_field(name="🎯 Moyenne", value=(f"**{sum(won_tries) / wins:.2f}** essais\npar mot trouvé\nmeilleur : **{min(won_tries)}** essai(s)" if wins else "Aucun mot trouvé"))
    e.add_field(name="🔥 Séries", value=f"actuelle : **{cur}**\nrecord : **{best}**\nrang : **#{ranking.index(m.id) + 1}**")
    e.add_field(name="🟥 Bien placées", value=f"**{red}** lettres")
    e.add_field(name="🟡 Mal placées", value=f"**{yel}** lettres")
    e.add_field(name="🟦 Absentes", value=f"**{blue}** lettres")
    e.add_field(name="⌨️ Mots proposés", value=f"**{tries}** au total", inline=False)
    e.add_field(name="Répartition des victoires (essais)", value=f"```{bars}```", inline=False)
    await i.response.send_message(embed=e)

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
    if WORDS and g not in WORDS and g != w and not extra_ok(s[0], g): return bad("Ce mot n'est pas dans le dictionnaire")
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
body{min-height:100vh;background:#050d2b;color:#fff;font-family:'Bricolage Grotesque',system-ui,sans-serif;overflow-x:hidden}
body::before{content:'';position:fixed;inset:-20%;background:radial-gradient(40% 35% at 20% 15%,rgba(229,22,45,.28),transparent 70%),radial-gradient(45% 40% at 80% 25%,rgba(38,99,235,.55),transparent 70%),radial-gradient(50% 45% at 50% 100%,rgba(255,192,30,.16),transparent 70%);animation:drift 22s ease-in-out infinite alternate;pointer-events:none}
@keyframes drift{to{transform:translate3d(3%,2%,0) scale(1.08)}}
body::after{content:'';position:fixed;inset:0;background:radial-gradient(rgba(160,190,255,.22) 1px,transparent 1.5px) 0 0/26px 26px;-webkit-mask:radial-gradient(70% 60% at 50% 40%,#000,transparent);mask:radial-gradient(70% 60% at 50% 40%,#000,transparent);pointer-events:none}
#fx{position:fixed;inset:0;pointer-events:none}
main{position:relative;z-index:1;max-width:580px;margin:0 auto;padding:22px 14px 30px;display:flex;flex-direction:column;gap:18px}
header{display:flex;justify-content:space-between;align-items:center;gap:10px}
.brand{display:flex;gap:6px;filter:drop-shadow(0 6px 18px rgba(0,0,0,.4))}.brand .t{--s:42px}
.who{display:flex;align-items:center;gap:10px;padding:6px 16px 6px 6px;border-radius:99px;background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.05));border:1px solid rgba(255,255,255,.16);backdrop-filter:blur(10px);min-width:0;box-shadow:0 8px 24px rgba(0,0,0,.3)}
.who img{width:36px;height:36px;border-radius:50%;box-shadow:0 0 0 2px var(--p)}
.who div{display:flex;flex-direction:column;min-width:0;line-height:1.15}.who span{font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.who small{opacity:.6;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.panel{display:flex;flex-direction:column;align-items:center;gap:18px;padding:22px 16px 20px;border-radius:30px;background:linear-gradient(160deg,rgba(255,255,255,.13),rgba(255,255,255,.04) 40%,rgba(8,20,70,.55));border:1px solid rgba(255,255,255,.16);box-shadow:0 30px 80px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.22);backdrop-filter:blur(14px)}
.bar{display:flex;flex-direction:column;align-items:center;gap:8px}
.pips{display:flex;gap:7px}.pip{width:26px;height:6px;border-radius:9px;background:rgba(255,255,255,.14);transition:.4s}.pip.u{background:var(--p);box-shadow:0 0 10px var(--p)}.pip.c{background:#fff}
#sub{opacity:.75;font-size:14px;font-weight:500}
#grid{display:grid;gap:7px;perspective:900px}.row{display:grid;gap:7px}
.t{width:var(--s);height:var(--s);display:grid;place-items:center;font-weight:800;font-size:calc(var(--s)*.5);background:linear-gradient(180deg,rgba(255,255,255,.1),rgba(255,255,255,.03));border:1px solid rgba(160,190,255,.28);border-radius:9px;position:relative;z-index:0;box-shadow:inset 0 1px 0 rgba(255,255,255,.14),0 3px 8px rgba(0,0,0,.25);transition:border-color .15s}
.t.f{border-color:#fff;background:rgba(255,255,255,.14)}.t.h{color:rgba(255,255,255,.3)}
.pop{animation:pop2 .14s ease-out}@keyframes pop2{50%{transform:scale(1.14)}}
.t.c{background:linear-gradient(180deg,#ff4a60,#c8061d);border-color:#ff8a98;box-shadow:inset 0 2px 0 rgba(255,255,255,.35),inset 0 -3px 0 rgba(0,0,0,.2),0 0 22px rgba(229,22,45,.5)}
.t.p{color:#2a1c00;border-color:transparent}
.t.p::before{content:'';position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle at 35% 28%,#ffe58a,var(--p) 55%,#e39a00);z-index:-1;box-shadow:0 0 20px rgba(255,192,30,.6),inset 0 -3px 0 rgba(0,0,0,.15)}
.t.a{background:linear-gradient(180deg,#2a62d0,#173f9a);border-color:#5b8cf0;box-shadow:inset 0 2px 0 rgba(255,255,255,.2),inset 0 -3px 0 rgba(0,0,0,.2)}
.flip{animation:flip .6s ease both}@keyframes flip{50%{transform:rotateX(90deg)}}
.shake{animation:sh .45s}@keyframes sh{20%,60%{transform:translateX(-7px)}40%,80%{transform:translateX(7px)}}
#kb{display:flex;flex-direction:column;gap:7px;width:100%}.kr{display:flex;gap:6px;justify-content:center}
.k{flex:1;max-width:46px;height:52px;border:0;border-radius:10px;background:linear-gradient(180deg,rgba(255,255,255,.24),rgba(255,255,255,.12));color:#fff;font-weight:800;font-size:15px;font-family:inherit;cursor:pointer;box-shadow:0 3px 0 rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.25);transition:transform .08s,box-shadow .08s,background .25s}
.k.w{max-width:70px;font-size:12px}.k:hover{filter:brightness(1.15)}.k:active{transform:translateY(3px);box-shadow:none}.k:focus-visible{outline:2px solid #fff}
.k.c{background:linear-gradient(180deg,#ff4a60,#c8061d)}.k.p{background:linear-gradient(180deg,#ffd45c,#e8a400);color:#2a1c00}.k.a{background:rgba(0,0,0,.4);color:rgba(255,255,255,.35)}
@media(max-width:480px){.k{height:46px}.brand .t{--s:34px}.panel{padding:18px 8px}}
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
<main id="app"><header><div class="brand" id="brand"></div><div class="who"><img id="av" alt=""><div><span id="nm"></span><small id="gn"></small></div></div></header>
<section class="panel"><div class="bar"><div class="pips"><i class="pip"></i><i class="pip"></i><i class="pip"></i><i class="pip"></i><i class="pip"></i><i class="pip"></i></div><p id="sub"></p></div><div id="grid"></div><div id="kb"></div></section></main><div id="toast"></div>
<div id="modal" hidden><div class="card"><h2 id="mt"></h2><div class="ans" id="ma"></div><pre id="mp"></pre>
<button class="btn" id="cp">Copier ma grille</button><button class="btn g" id="cl">Fermer</button></div></div>
<script>
const tok=location.pathname.split('/').pop(),$=s=>document.querySelector(s),rows=[];let S,typed='',busy=false,tt;
const api=async(p,o)=>{const r=await fetch('/api/'+p+'/'+tok,o);return[r.ok,await r.json()]};
// fond : lettres qui dérivent + confettis (carrés rouges, ronds jaunes)
const cv=$('#fx'),cx=cv.getContext('2d');let W,H,L=[],P=[];
const rs=()=>{W=cv.width=innerWidth;H=cv.height=innerHeight};rs();addEventListener('resize',rs);
for(let i=0;i<34;i++)L.push({x:Math.random()*1600,y:Math.random()*1600,s:18+Math.random()*40,v:.08+Math.random()*.3,c:'SUTOMABCDEFGHIJKLMNOPQRSTUVWXYZ'[i%31]});
function loop(){cx.clearRect(0,0,W,H);cx.fillStyle='rgba(130,170,255,.07)';
L.forEach(l=>{l.y-=l.v;if(l.y<-120){l.y=H+120;l.x=Math.random()*W}cx.font=`800 ${l.s}px 'Bricolage Grotesque',sans-serif`;cx.fillText(l.c,l.x%W,l.y)});
P=P.filter(p=>p.y<H+30);P.forEach(p=>{p.x+=p.vx;p.y+=p.vy;p.vy+=.2;p.a+=p.va;cx.save();cx.translate(p.x,p.y);cx.rotate(p.a);cx.fillStyle=p.c;
if(p.r){cx.beginPath();cx.arc(0,0,p.z/2,0,7);cx.fill()}else cx.fillRect(-p.z/2,-p.z/2,p.z,p.z);cx.restore()});requestAnimationFrame(loop)}
if(!matchMedia('(prefers-reduced-motion:reduce)').matches)loop();
const confetti=()=>{for(let i=0;i<150;i++)P.push({x:W/2,y:H*.55,vx:(Math.random()-.5)*15,vy:-Math.random()*17-4,a:0,va:(Math.random()-.5)*.4,z:8+Math.random()*10,c:['#e5162d','#ffc01e','#fff','#4f8dff'][i%4],r:i%3==0})};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('on'),2000)}
function build(){const g=$('#grid'),n=S.length,sz=Math.min(62,(Math.min(innerWidth,580)-84)/n-7);g.innerHTML='';g.style.setProperty('--s',sz+'px');
for(let r=0;r<S.max;r++){const d=document.createElement('div');d.className='row';d.style.gridTemplateColumns=`repeat(${n},var(--s))`;const ts=[];
for(let i=0;i<n;i++){const t=document.createElement('div');t.className='t';d.append(t);ts.push(t)}g.append(d);rows.push(ts)}}
function hints(){const h=Array(S.length).fill('');h[0]=S.first;S.guesses.forEach(x=>x.r.forEach((v,i)=>{if(v==2)h[i]=x.w[i]}));return h}
function paint(){const cur=S.guesses.length,h=hints();rows.forEach((ts,r)=>ts.forEach((t,i)=>{
if(r<cur){t.textContent=S.guesses[r].w[i];t.className='t '+'apc'[S.guesses[r].r[i]]}
else if(r==cur&&!S.done){const ch=typed[i];t.textContent=ch||h[i]||'·';t.className='t'+(ch?' f':' h')}
else{t.textContent='';t.className='t'}}));document.querySelectorAll('.pip').forEach((p,i)=>p.className='pip'+(i<cur?' u':i==cur&&!S.done?' c':''));$('#sub').textContent=S.done?'Partie terminée':`Essai ${Math.min(cur+1,S.max)} sur ${S.max}`}
function ukeys(){const st={},rk={a:0,p:1,c:2};S.guesses.forEach(x=>[...x.w].forEach((ch,i)=>{const c='apc'[x.r[i]];if(!(ch in st)||rk[c]>rk[st[ch]])st[ch]=c}));
document.querySelectorAll('.k').forEach(b=>{const c=st[b.dataset.k];if(c)b.className='k'+(b.classList.contains('w')?' w':'')+' '+c})}
function keys(){['AZERTYUIOP','QSDFGHJKLM',['ENTER',...'WXCVBN','⌫']].forEach(r=>{const d=document.createElement('div');d.className='kr';
[...r].forEach(k=>{const b=document.createElement('button');b.className='k'+(k.length>1||k=='⌫'?' w':'');b.textContent=k=='ENTER'?'Entrée':k;b.dataset.k=k;b.onclick=()=>press(k);d.append(b)});$('#kb').append(d)})}
function press(k){if(!S||S.done||busy)return;if(k=='ENTER')return submit();
if(k=='⌫'){if(typed.length>1)typed=typed.slice(0,-1);return paint()}
if(/^[A-Z]$/.test(k)&&typed.length<S.length){typed+=k;paint();rows[S.guesses.length][typed.length-1].classList.add('pop')}}
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
S=d;typed=S.first;$('#av').src=S.avatar;$('#nm').textContent=S.name;$('#gn').textContent=S.gname;build();keys();paint();ukeys();if(S.done)end(false)})();
</script></body></html>'''

async def setup(client, tree=None, admin_check=None):
    """À appeler UNE seule fois, avant tree.sync(). admin_check(guild_id, user_id) -> bool est facultatif."""
    global bot, _admin, _t, _t2
    bot, _admin = client, admin_check
    _t = asyncio.create_task(load_words())
    _t2 = asyncio.create_task(load_games())
    for c in (setmot, sutom, top, add, remove, addmots, profil):
        (tree or client.tree).add_command(c)
    app = web.Application()
    app.add_routes([web.get("/", ping), web.get("/ping", ping), web.get("/play/{t}", page),
                    web.get("/api/state/{t}", state), web.post("/api/guess/{t}", guess)])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()   # sert aussi de keep alive (/ping)

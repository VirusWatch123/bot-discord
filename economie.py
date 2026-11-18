"""
economie.py : système d'économie et de boutique (cog discord.py 2.x + SQLite)

Chargement dans ton bot :  await bot.load_extension("economie")
"""
import os
import random
import sqlite3
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# ───────────────────────── Réglages ─────────────────────────
DB_PATH = os.getenv("ECO_DB_PATH", "economie.db")   # fichier séparé de la base du quiz
CURRENCY = "🪙"
START_WALLET = 100
DAILY_BASE, DAILY_STEP, DAILY_CD = 250, 25, 24 * 3600   # bonus de série : +25 / jour (max 30 jours)
WORK_CD = 3600
ROB_CD = 2 * 3600
ROB_MIN = 200            # minimum en portefeuille (voleur et victime)
ROB_SUCCESS = 0.40
MSG_CD, MSG_MIN, MSG_MAX = 60, 1, 5   # gains passifs en discutant
MAX_BET = 100_000
SELL_RATIO = 0.5         # revente à 50 % du prix
BONUS_CAP = 300          # bonus de gains maximum cumulé (en %)
INTEREST_PCT, INTEREST_MAX = 1, 2000  # intérêts de la banque, versés avec /daily

JOBS = [  # (nombre de /work requis, métier, gain min, gain max)
    (0, "Stagiaire", 30, 70),
    (5, "Livreur", 50, 110),
    (15, "Serveur", 90, 170),
    (35, "Développeur", 150, 280),
    (70, "Ingénieur", 240, 420),
    (120, "Directeur", 380, 700),
]
WORK_TEXTS = [
    "Tu as terminé une grosse mission",
    "Un client est ravi de ton travail",
    "Tu as fait des heures supplémentaires",
    "Ta journée a été très productive",
    "Ton patron t'a glissé une prime",
]
RARITIES = {
    "commun": ("⚪", 0x95A5A6),
    "rare": ("🔵", 0x3498DB),
    "epique": ("🟣", 0x9B59B6),
    "legendaire": ("🟡", 0xF1C40F),
}
KINDS = {"collection": "Collection", "role": "Rôle", "boost": "Boost de gains", "coffre": "Coffre"}

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 12, 7, 4, 2]
SLOT_TRIPLE = {"🍒": 3, "🍋": 4, "🍇": 6, "🔔": 10, "⭐": 15, "💎": 30, "7️⃣": 60}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    guild_id INTEGER, user_id INTEGER,
    wallet INTEGER NOT NULL DEFAULT 0, bank INTEGER NOT NULL DEFAULT 0,
    last_daily REAL NOT NULL DEFAULT 0, last_work REAL NOT NULL DEFAULT 0,
    last_rob REAL NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
    work_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id));
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT -1,
    category TEXT NOT NULL DEFAULT 'Général', rarity TEXT NOT NULL DEFAULT 'commun',
    kind TEXT NOT NULL DEFAULT 'collection', value INTEGER NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 0, role_id INTEGER NOT NULL DEFAULT 0,
    max_per_user INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, name COLLATE NOCASE));
CREATE TABLE IF NOT EXISTS inventory(
    guild_id INTEGER, user_id INTEGER, item_id INTEGER, qty INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item_id));
CREATE TABLE IF NOT EXISTS boosts(
    guild_id INTEGER, user_id INTEGER, bonus INTEGER, expires REAL);
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
    delta INTEGER, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS config(
    guild_id INTEGER PRIMARY KEY, log_channel INTEGER NOT NULL DEFAULT 0);
"""


# ───────────────────────── Utilitaires ─────────────────────────
def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + f" {CURRENCY}"


def parse_amount(text: str, available: int) -> Optional[int]:
    t = text.strip().lower().replace(" ", "")
    if t in ("all", "tout", "max"):
        return available
    if t in ("half", "moitie", "moitié"):
        return available // 2
    if t.endswith("%") and t[:-1].isdigit():
        return available * min(int(t[:-1]), 100) // 100
    if t.isdigit():
        return int(t)
    return None


def job_for(work_count: int):
    return [j for j in JOBS if work_count >= j[0]][-1]


def rarity_emoji(r: str) -> str:
    return RARITIES.get(r, RARITIES["commun"])[0]


def item_field(r):
    if r["stock"] == -1:
        stock = "illimité"
    elif r["stock"] == 0:
        stock = "**épuisé**"
    else:
        stock = f"{r['stock']} en stock"
    lines = []
    if r["description"]:
        lines.append(r["description"])
    lines.append(f"💵 {fmt(r['price'])} · 📦 {stock} · {KINDS.get(r['kind'], r['kind'])}")
    if r["kind"] == "boost":
        lines.append(f"⚡ +{r['value']} % de gains pendant {r['duration'] // 60} min")
    elif r["kind"] == "coffre":
        lines.append(f"🎁 Récompense jusqu'à {fmt(r['value'])}")
    elif r["kind"] == "role":
        lines.append(f"🏷️ Donne le rôle <@&{r['role_id']}>")
    if r["max_per_user"]:
        lines.append(f"🔒 Limite : {r['max_per_user']} par membre")
    return f"{rarity_emoji(r['rarity'])} {r['name']}", "\n".join(lines)


# ───────────────────────── Base de données ─────────────────────────
class Store:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # Les méthodes ci-dessous ne commit pas : chaque commande fait un seul commit à la fin,
    # sans await entre la lecture et l'écriture (donc opération atomique).
    def user(self, g, u):
        self.db.execute(
            "INSERT OR IGNORE INTO users(guild_id,user_id,wallet) VALUES(?,?,?)", (g, u, START_WALLET))
        return self.db.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (g, u)).fetchone()

    def add(self, g, u, wallet=0, bank=0, reason=""):
        self.db.execute(
            "UPDATE users SET wallet=wallet+?, bank=bank+? WHERE guild_id=? AND user_id=?",
            (wallet, bank, g, u))
        if reason and wallet:
            self.db.execute(
                "INSERT INTO transactions(guild_id,user_id,delta,reason,ts) VALUES(?,?,?,?,?)",
                (g, u, wallet, reason, time.time()))

    def bonus(self, g, u) -> int:
        rows = self.db.execute(
            "SELECT bonus FROM boosts WHERE guild_id=? AND user_id IN (?,0) AND expires>?",
            (g, u, time.time())).fetchall()
        return min(sum(r[0] for r in rows), BONUS_CAP)

    def boosted(self, g, u, amount):
        b = self.bonus(g, u)
        return amount + amount * b // 100, b

    def find_item(self, g, name):
        return self.db.execute(
            "SELECT * FROM items WHERE guild_id=? AND name=? COLLATE NOCASE", (g, name.strip())).fetchone()

    def qty(self, g, u, item_id) -> int:
        r = self.db.execute(
            "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?", (g, u, item_id)).fetchone()
        return r[0] if r else 0

    def give_item(self, g, u, item_id, qty):
        self.db.execute(
            "INSERT INTO inventory(guild_id,user_id,item_id,qty) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id,item_id) DO UPDATE SET qty=qty+excluded.qty",
            (g, u, item_id, qty))

    def take_item(self, g, u, item_id, qty) -> bool:
        cur = self.db.execute(
            "UPDATE inventory SET qty=qty-? WHERE guild_id=? AND user_id=? AND item_id=? AND qty>=?",
            (qty, g, u, item_id, qty))
        if cur.rowcount:
            self.db.execute("DELETE FROM inventory WHERE qty<=0")
        return cur.rowcount > 0


# ───────────────────────── Boutique (interface) ─────────────────────────
class ShopView(discord.ui.View):
    PER_PAGE = 5

    def __init__(self, cog, origin: discord.Interaction, category: Optional[str] = None):
        super().__init__(timeout=180)
        self.cog, self.origin, self.category, self.page = cog, origin, category, 0
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.origin.user.id:
            await interaction.response.send_message("Ouvre ta propre boutique avec `/shop` 😉", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    def items(self):
        q, a = "SELECT * FROM items WHERE guild_id=?", [self.origin.guild_id]
        if self.category:
            q += " AND category=?"
            a.append(self.category)
        return self.cog.store.db.execute(q + " ORDER BY price, name", a).fetchall()

    def total_pages(self, n):
        return max(1, -(-n // self.PER_PAGE))

    def chunk(self):
        items = self.items()
        self.page = max(0, min(self.page, self.total_pages(len(items)) - 1))
        return items, items[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]

    def rebuild(self):
        self.clear_items()
        items, chunk = self.chunk()
        pages = self.total_pages(len(items))
        cats = [r[0] for r in self.cog.store.db.execute(
            "SELECT DISTINCT category FROM items WHERE guild_id=? ORDER BY category LIMIT 24",
            (self.origin.guild_id,))]
        opts = [discord.SelectOption(label="Toutes les catégories", value="__all__", default=self.category is None)]
        opts += [discord.SelectOption(label=c[:100], value=c, default=(c == self.category)) for c in cats]
        cat = discord.ui.Select(placeholder="Filtrer par catégorie", options=opts, row=0)
        cat.callback = self.on_category
        self.add_item(cat)
        if chunk:
            buy = discord.ui.Select(
                placeholder="Acheter un objet (×1)", row=1,
                options=[discord.SelectOption(
                    label=r["name"][:100], value=str(r["id"]), description=fmt(r["price"])[:100],
                    emoji=rarity_emoji(r["rarity"])) for r in chunk])
            buy.callback = self.on_buy
            self.add_item(buy)
        prev = discord.ui.Button(emoji="◀️", label="Précédent", row=2, disabled=self.page == 0,
                                 style=discord.ButtonStyle.secondary)
        nxt = discord.ui.Button(emoji="▶️", label="Suivant", row=2, disabled=self.page >= pages - 1,
                                style=discord.ButtonStyle.secondary)
        prev.callback, nxt.callback = self.on_prev, self.on_next
        self.add_item(prev)
        self.add_item(nxt)

    def embed(self):
        items, chunk = self.chunk()
        e = discord.Embed(title="🛍️ Boutique" + (f" · {self.category}" if self.category else ""),
                          color=0x5865F2)
        if not chunk:
            e.description = "Rien à vendre pour le moment."
        for r in chunk:
            name, value = item_field(r)
            e.add_field(name=name, value=value, inline=False)
        wallet = self.cog.store.user(self.origin.guild_id, self.origin.user.id)["wallet"]
        e.set_footer(text=f"Page {self.page + 1}/{self.total_pages(len(items))} • Portefeuille : {fmt(wallet)}")
        return e

    async def refresh(self, interaction):
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_category(self, interaction: discord.Interaction):
        v = interaction.data["values"][0]
        self.category = None if v == "__all__" else v
        self.page = 0
        await self.refresh(interaction)

    async def on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        await self.refresh(interaction)

    async def on_next(self, interaction: discord.Interaction):
        self.page += 1
        await self.refresh(interaction)

    async def on_buy(self, interaction: discord.Interaction):
        ok, msg = self.cog.purchase(interaction.guild_id, interaction.user.id,
                                    int(interaction.data["values"][0]), 1)
        await interaction.response.send_message(msg, ephemeral=True)
        self.rebuild()
        try:
            await self.origin.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass


# ───────────────────────── Cog ─────────────────────────
class Economie(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(DB_PATH)
        self._msg_cache = {}

    async def cog_load(self):
        self.store.db.execute("DELETE FROM boosts WHERE expires<=?", (time.time(),))
        self.store.db.commit()

    # ---- outils ----
    async def is_admin(self, interaction: discord.Interaction) -> bool:
        # Remplace cette vérification par ton système (propriétaire + personne qui a invité le bot).
        ok = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not ok:
            await interaction.response.send_message(
                "⛔ Cette commande est réservée aux administrateurs.", ephemeral=True)
        return ok

    async def log_event(self, guild: discord.Guild, text: str):
        r = self.store.db.execute("SELECT log_channel FROM config WHERE guild_id=?", (guild.id,)).fetchone()
        ch = guild.get_channel(r[0]) if r and r[0] else None
        if ch:
            try:
                await ch.send(embed=discord.Embed(title="📒 Journal de l'économie", description=text,
                                                  color=0x2C3E50, timestamp=discord.utils.utcnow()))
            except discord.HTTPException:
                pass

    def purchase(self, g, u, item_id, qty):
        s = self.store
        it = s.db.execute("SELECT * FROM items WHERE id=? AND guild_id=?", (item_id, g)).fetchone()
        if not it:
            return False, "❌ Cet objet n'existe plus."
        if it["stock"] == 0:
            return False, f"❌ **{it['name']}** est épuisé."
        if it["stock"] != -1 and it["stock"] < qty:
            return False, f"❌ Il ne reste que {it['stock']} exemplaire(s)."
        if it["max_per_user"] and s.qty(g, u, item_id) + qty > it["max_per_user"]:
            return False, f"❌ Tu ne peux pas en posséder plus de {it['max_per_user']}."
        cost = it["price"] * qty
        wallet = s.user(g, u)["wallet"]
        if wallet < cost:
            return False, f"❌ Il te manque {fmt(cost - wallet)}."
        s.add(g, u, wallet=-cost, reason=f"Achat {qty}× {it['name']}")
        if it["stock"] != -1:
            s.db.execute("UPDATE items SET stock=stock-? WHERE id=?", (qty, item_id))
        s.give_item(g, u, item_id, qty)
        s.db.commit()
        return True, f"✅ Tu as acheté {qty}× **{it['name']}** pour {fmt(cost)}."

    # ---- gains passifs ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        key, now = (message.guild.id, message.author.id), time.time()
        if now - self._msg_cache.get(key, 0) < MSG_CD:
            return
        self._msg_cache[key] = now
        g, u = key
        self.store.user(g, u)
        gain, _ = self.store.boosted(g, u, random.randint(MSG_MIN, MSG_MAX))
        self.store.add(g, u, wallet=gain)
        self.store.db.commit()

    # ================= ÉCONOMIE =================
    @app_commands.command(name="balance", description="Voir ton solde (ou celui d'un membre)")
    @app_commands.guild_only()
    async def balance(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        membre = membre or interaction.user
        g = interaction.guild_id
        r = self.store.user(g, membre.id)
        total = r["wallet"] + r["bank"]
        rank = self.store.db.execute(
            "SELECT COUNT(*)+1 FROM users WHERE guild_id=? AND wallet+bank>?", (g, total)).fetchone()[0]
        bonus = self.store.bonus(g, membre.id)
        self.store.db.commit()
        e = discord.Embed(title=f"💰 Solde de {membre.display_name}", color=0xF1C40F)
        e.add_field(name="Portefeuille", value=fmt(r["wallet"]))
        e.add_field(name="Banque", value=fmt(r["bank"]))
        e.add_field(name="Total", value=fmt(total))
        e.add_field(name="Classement", value=f"#{rank}")
        e.add_field(name="Métier", value=job_for(r["work_count"])[1])
        e.add_field(name="Série /daily", value=f"{r['streak']} jour(s)")
        if bonus:
            e.add_field(name="⚡ Bonus de gains actif", value=f"+{bonus} %", inline=False)
        e.set_thumbnail(url=membre.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="daily", description="Récupérer ta récompense quotidienne")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        g, u, now = interaction.guild_id, interaction.user.id, time.time()
        r = self.store.user(g, u)
        ready = r["last_daily"] + DAILY_CD
        if ready > now:
            return await interaction.response.send_message(
                f"⏳ Ta récompense sera prête <t:{int(ready)}:R>.", ephemeral=True)
        streak = r["streak"] + 1 if r["last_daily"] and now - r["last_daily"] <= 2 * DAILY_CD else 1
        base = DAILY_BASE + DAILY_STEP * min(streak - 1, 30)
        gain, bonus = self.store.boosted(g, u, base)
        interest = min(r["bank"] * INTEREST_PCT // 100, INTEREST_MAX)
        self.store.add(g, u, wallet=gain, bank=interest, reason="Récompense quotidienne")
        self.store.db.execute("UPDATE users SET last_daily=?, streak=? WHERE guild_id=? AND user_id=?",
                              (now, streak, g, u))
        self.store.db.commit()
        e = discord.Embed(title="🎁 Récompense quotidienne", color=0x2ECC71,
                          description=f"Tu reçois **{fmt(gain)}**" + (f" (bonus +{bonus} %)" if bonus else ""))
        e.add_field(name="Série", value=f"🔥 {streak} jour(s) d'affilée")
        if interest:
            e.add_field(name="Intérêts bancaires", value=fmt(interest))
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="work", description="Travailler pour gagner de l'argent (1 h de délai)")
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction):
        g, u, now = interaction.guild_id, interaction.user.id, time.time()
        r = self.store.user(g, u)
        ready = r["last_work"] + WORK_CD
        if ready > now:
            return await interaction.response.send_message(
                f"⏳ Tu es fatigué ! Retour au travail <t:{int(ready)}:R>.", ephemeral=True)
        count = r["work_count"] + 1
        old, job = job_for(r["work_count"]), job_for(count)
        gain, bonus = self.store.boosted(g, u, random.randint(job[2], job[3]))
        self.store.add(g, u, wallet=gain, reason=f"Travail ({job[1]})")
        self.store.db.execute("UPDATE users SET last_work=?, work_count=? WHERE guild_id=? AND user_id=?",
                              (now, count, g, u))
        self.store.db.commit()
        txt = f"{random.choice(WORK_TEXTS)} en tant que **{job[1]}** : **+{fmt(gain)}**"
        if bonus:
            txt += f" (bonus +{bonus} %)"
        if job != old:
            txt += f"\n🎉 Promotion ! Tu es maintenant **{job[1]}**."
        await interaction.response.send_message(embed=discord.Embed(description=txt, color=0x3498DB))

    @app_commands.command(name="deposit", description="Mettre de l'argent à la banque (à l'abri des vols)")
    @app_commands.describe(montant="Un nombre, « tout », « moitié » ou un pourcentage (ex. 25%)")
    @app_commands.guild_only()
    async def deposit(self, interaction: discord.Interaction, montant: str):
        g, u = interaction.guild_id, interaction.user.id
        wallet = self.store.user(g, u)["wallet"]
        amount = parse_amount(montant, wallet)
        if not amount or amount < 1 or amount > wallet:
            return await interaction.response.send_message("❌ Montant invalide.", ephemeral=True)
        self.store.add(g, u, wallet=-amount, bank=amount)
        self.store.db.commit()
        await interaction.response.send_message(f"🏦 Tu as déposé **{fmt(amount)}** à la banque.")

    @app_commands.command(name="withdraw", description="Retirer de l'argent de la banque")
    @app_commands.describe(montant="Un nombre, « tout », « moitié » ou un pourcentage (ex. 25%)")
    @app_commands.guild_only()
    async def withdraw(self, interaction: discord.Interaction, montant: str):
        g, u = interaction.guild_id, interaction.user.id
        bank = self.store.user(g, u)["bank"]
        amount = parse_amount(montant, bank)
        if not amount or amount < 1 or amount > bank:
            return await interaction.response.send_message("❌ Montant invalide.", ephemeral=True)
        self.store.add(g, u, wallet=amount, bank=-amount)
        self.store.db.commit()
        await interaction.response.send_message(f"🏦 Tu as retiré **{fmt(amount)}** de la banque.")

    @app_commands.command(name="pay", description="Envoyer de l'argent à un membre")
    @app_commands.guild_only()
    async def pay(self, interaction: discord.Interaction, membre: discord.Member,
                  montant: app_commands.Range[int, 1, 10_000_000]):
        g, u = interaction.guild_id, interaction.user.id
        if membre.bot or membre.id == u:
            return await interaction.response.send_message("❌ Destinataire invalide.", ephemeral=True)
        wallet = self.store.user(g, u)["wallet"]
        self.store.user(g, membre.id)
        if wallet < montant:
            return await interaction.response.send_message("❌ Fonds insuffisants dans ton portefeuille.", ephemeral=True)
        self.store.add(g, u, wallet=-montant, reason=f"Envoi à {membre.id}")
        self.store.add(g, membre.id, wallet=montant, reason=f"Reçu de {u}")
        self.store.db.commit()
        await interaction.response.send_message(f"💸 {interaction.user.mention} a envoyé **{fmt(montant)}** à {membre.mention}.")

    @app_commands.command(name="rob", description="Tenter de voler le portefeuille d'un membre")
    @app_commands.guild_only()
    async def rob(self, interaction: discord.Interaction, membre: discord.Member):
        g, u, now = interaction.guild_id, interaction.user.id, time.time()
        if membre.bot or membre.id == u:
            return await interaction.response.send_message("❌ Cible invalide.", ephemeral=True)
        thief, victim = self.store.user(g, u), self.store.user(g, membre.id)
        ready = thief["last_rob"] + ROB_CD
        if ready > now:
            return await interaction.response.send_message(f"⏳ La police te surveille… réessaie <t:{int(ready)}:R>.", ephemeral=True)
        if victim["wallet"] < ROB_MIN:
            return await interaction.response.send_message("❌ Cette personne n'a presque rien dans son portefeuille (la banque est à l'abri).", ephemeral=True)
        if thief["wallet"] < ROB_MIN:
            return await interaction.response.send_message(f"❌ Il te faut au moins {fmt(ROB_MIN)} en portefeuille pour tenter un vol.", ephemeral=True)
        self.store.db.execute("UPDATE users SET last_rob=? WHERE guild_id=? AND user_id=?", (now, g, u))
        if random.random() < ROB_SUCCESS:
            lo = max(1, victim["wallet"] // 10)
            amount = random.randint(lo, max(lo, victim["wallet"] * 30 // 100))
            self.store.add(g, membre.id, wallet=-amount, reason=f"Volé par {u}")
            self.store.add(g, u, wallet=amount, reason=f"Vol sur {membre.id}")
            msg = f"🦹 {interaction.user.mention} a volé **{fmt(amount)}** à {membre.mention} !"
            color = 0xE67E22
        else:
            lo = max(1, thief["wallet"] // 10)
            fine = random.randint(lo, max(lo, thief["wallet"] // 4))
            self.store.add(g, u, wallet=-fine, reason=f"Amende (vol raté sur {membre.id})")
            self.store.add(g, membre.id, wallet=fine, reason=f"Indemnité (vol raté de {u})")
            msg = f"🚓 {interaction.user.mention} s'est fait attraper ! Amende de **{fmt(fine)}** versée à {membre.mention}."
            color = 0xE74C3C
        self.store.db.commit()
        await interaction.response.send_message(embed=discord.Embed(description=msg, color=color))

    @app_commands.command(name="coinflip", description="Pile ou face : double ta mise ou perds-la")
    @app_commands.describe(mise="Un nombre, « tout », « moitié » ou un pourcentage")
    @app_commands.choices(choix=[app_commands.Choice(name="Pile", value="pile"),
                                 app_commands.Choice(name="Face", value="face")])
    @app_commands.guild_only()
    async def coinflip(self, interaction: discord.Interaction, choix: str, mise: str):
        g, u = interaction.guild_id, interaction.user.id
        wallet = self.store.user(g, u)["wallet"]
        bet = parse_amount(mise, min(wallet, MAX_BET))
        if not bet or bet < 1 or bet > wallet or bet > MAX_BET:
            return await interaction.response.send_message(
                f"❌ Mise invalide (maximum {fmt(MAX_BET)}).", ephemeral=True)
        result = random.choice(["pile", "face"])
        win = result == choix
        self.store.add(g, u, wallet=bet if win else -bet, reason="Pile ou face")
        self.store.db.commit()
        txt = f"La pièce tombe sur **{result}** : " + (f"tu gagnes **{fmt(bet)}** 🎉" if win else f"tu perds **{fmt(bet)}** 😢")
        await interaction.response.send_message(embed=discord.Embed(
            description=txt, color=0x2ECC71 if win else 0xE74C3C))

    @app_commands.command(name="slots", description="Machine à sous")
    @app_commands.describe(mise="Un nombre, « tout », « moitié » ou un pourcentage")
    @app_commands.guild_only()
    async def slots(self, interaction: discord.Interaction, mise: str):
        g, u = interaction.guild_id, interaction.user.id
        wallet = self.store.user(g, u)["wallet"]
        bet = parse_amount(mise, min(wallet, MAX_BET))
        if not bet or bet < 1 or bet > wallet or bet > MAX_BET:
            return await interaction.response.send_message(
                f"❌ Mise invalide (maximum {fmt(MAX_BET)}).", ephemeral=True)
        reels = random.choices(SLOT_SYMBOLS, SLOT_WEIGHTS, k=3)
        if len(set(reels)) == 1:
            mult, label = SLOT_TRIPLE[reels[0]], "JACKPOT !"
        elif len(set(reels)) == 2:
            mult, label = 1.5, "Deux identiques !"
        else:
            mult, label = 0, "Perdu…"
        payout = int(bet * mult)
        self.store.add(g, u, wallet=payout - bet, reason="Machine à sous")
        self.store.db.commit()
        net = payout - bet
        e = discord.Embed(title="🎰 Machine à sous", color=0x2ECC71 if net > 0 else 0xE74C3C,
                          description=f"**{'  |  '.join(reels)}**\n\n{label}\n" +
                          (f"Gain net : **+{fmt(net)}**" if net > 0 else f"Perte : **{fmt(-net)}**" if net < 0 else "Mise remboursée."))
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="baltop", description="Classement des membres les plus riches")
    @app_commands.guild_only()
    async def baltop(self, interaction: discord.Interaction):
        rows = self.store.db.execute(
            "SELECT user_id, wallet+bank AS total FROM users WHERE guild_id=? ORDER BY total DESC LIMIT 10",
            (interaction.guild_id,)).fetchall()
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{r['user_id']}> : {fmt(r['total'])}"
                 for i, r in enumerate(rows)]
        await interaction.response.send_message(embed=discord.Embed(
            title="🏆 Les plus riches", description="\n".join(lines) or "Personne pour l'instant.", color=0xF1C40F))

    # ================= BOUTIQUE =================
    @app_commands.command(name="shop", description="Ouvrir la boutique")
    @app_commands.describe(categorie="Filtrer sur une catégorie")
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction, categorie: Optional[str] = None):
        view = ShopView(self, interaction, categorie)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="buy", description="Acheter un objet de la boutique")
    @app_commands.guild_only()
    async def buy(self, interaction: discord.Interaction, objet: str,
                  quantite: app_commands.Range[int, 1, 100] = 1):
        it = self.store.find_item(interaction.guild_id, objet)
        if not it:
            return await interaction.response.send_message("❌ Objet introuvable.", ephemeral=True)
        ok, msg = self.purchase(interaction.guild_id, interaction.user.id, it["id"], quantite)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @app_commands.command(name="inventory", description="Voir ton inventaire (ou celui d'un membre)")
    @app_commands.guild_only()
    async def inventory(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        membre = membre or interaction.user
        rows = self.store.db.execute(
            "SELECT i.*, inv.qty FROM inventory inv JOIN items i ON i.id=inv.item_id "
            "WHERE inv.guild_id=? AND inv.user_id=? ORDER BY i.name", (interaction.guild_id, membre.id)).fetchall()
        lines = [f"{rarity_emoji(r['rarity'])} **{r['name']}** ×{r['qty']} · {KINDS.get(r['kind'], r['kind'])}"
                 for r in rows]
        await interaction.response.send_message(embed=discord.Embed(
            title=f"🎒 Inventaire de {membre.display_name}",
            description="\n".join(lines)[:4000] or "Vide pour l'instant.", color=0x9B59B6))

    @app_commands.command(name="use", description="Utiliser un objet de ton inventaire")
    @app_commands.guild_only()
    async def use(self, interaction: discord.Interaction, objet: str):
        g, u = interaction.guild_id, interaction.user.id
        it = self.store.find_item(g, objet)
        if not it or self.store.qty(g, u, it["id"]) < 1:
            return await interaction.response.send_message("❌ Tu n'as pas cet objet.", ephemeral=True)
        kind = it["kind"]
        if kind == "collection":
            return await interaction.response.send_message(
                f"🖼️ **{it['name']}** est un objet de collection, il n'a pas d'effet.", ephemeral=True)
        if kind == "role":
            role = interaction.guild.get_role(it["role_id"])
            if not role:
                return await interaction.response.send_message("❌ Le rôle associé n'existe plus.", ephemeral=True)
            if role in interaction.user.roles:
                return await interaction.response.send_message("❌ Tu as déjà ce rôle.", ephemeral=True)
            self.store.take_item(g, u, it["id"], 1)
            self.store.db.commit()
            try:
                await interaction.user.add_roles(role, reason=f"Objet de boutique : {it['name']}")
            except discord.HTTPException:
                self.store.give_item(g, u, it["id"], 1)   # remboursement de l'objet
                self.store.db.commit()
                return await interaction.response.send_message(
                    "❌ Je ne peux pas donner ce rôle (vérifie mes permissions et la position de mon rôle). Objet conservé.",
                    ephemeral=True)
            return await interaction.response.send_message(f"🏷️ Tu as reçu le rôle {role.mention} !")
        if not self.store.take_item(g, u, it["id"], 1):
            return await interaction.response.send_message("❌ Tu n'as pas cet objet.", ephemeral=True)
        if kind == "boost":
            self.store.db.execute("INSERT INTO boosts(guild_id,user_id,bonus,expires) VALUES(?,?,?,?)",
                                  (g, u, it["value"], time.time() + it["duration"]))
            self.store.db.commit()
            return await interaction.response.send_message(
                f"⚡ **{it['name']}** activé : +{it['value']} % de gains pendant {it['duration'] // 60} min !")
        if kind == "coffre":
            reward = random.randint(max(1, it["value"] // 4), max(1, it["value"]))
            self.store.add(g, u, wallet=reward, reason=f"Coffre {it['name']}")
            self.store.db.commit()
            return await interaction.response.send_message(embed=discord.Embed(
                title=f"🎁 {it['name']} ouvert !", description=f"Tu trouves **{fmt(reward)}** dedans.", color=0xF1C40F))

    @app_commands.command(name="sell", description="Revendre un objet (50 % de son prix)")
    @app_commands.guild_only()
    async def sell(self, interaction: discord.Interaction, objet: str,
                   quantite: app_commands.Range[int, 1, 100] = 1):
        g, u = interaction.guild_id, interaction.user.id
        it = self.store.find_item(g, objet)
        if not it or not self.store.take_item(g, u, it["id"], quantite):
            return await interaction.response.send_message("❌ Tu n'en as pas assez.", ephemeral=True)
        total = int(it["price"] * SELL_RATIO) * quantite
        self.store.user(g, u)
        self.store.add(g, u, wallet=total, reason=f"Vente {quantite}× {it['name']}")
        self.store.db.commit()
        await interaction.response.send_message(f"💱 Tu as vendu {quantite}× **{it['name']}** pour **{fmt(total)}**.")

    # ================= ADMINISTRATION =================
    eco = app_commands.Group(name="eco", description="Administration de l'économie", guild_only=True,
                             default_permissions=discord.Permissions(administrator=True))
    item = app_commands.Group(name="item", description="Gérer les objets de la boutique", guild_only=True,
                              default_permissions=discord.Permissions(administrator=True))

    @eco.command(name="give", description="Donner de l'argent à un membre")
    async def eco_give(self, interaction: discord.Interaction, membre: discord.Member,
                       montant: app_commands.Range[int, 1, 1_000_000_000]):
        if not await self.is_admin(interaction):
            return
        g = interaction.guild_id
        self.store.user(g, membre.id)
        self.store.add(g, membre.id, wallet=montant, reason=f"Don admin ({interaction.user.id})")
        self.store.db.commit()
        await interaction.response.send_message(f"✅ {fmt(montant)} ajoutés à {membre.mention}.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a donné **{fmt(montant)}** à {membre.mention}.")

    @eco.command(name="take", description="Retirer de l'argent du portefeuille d'un membre")
    async def eco_take(self, interaction: discord.Interaction, membre: discord.Member,
                       montant: app_commands.Range[int, 1, 1_000_000_000]):
        if not await self.is_admin(interaction):
            return
        g = interaction.guild_id
        taken = min(montant, self.store.user(g, membre.id)["wallet"])
        self.store.add(g, membre.id, wallet=-taken, reason=f"Retrait admin ({interaction.user.id})")
        self.store.db.commit()
        await interaction.response.send_message(f"✅ {fmt(taken)} retirés à {membre.mention}.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a retiré **{fmt(taken)}** à {membre.mention}.")

    @eco.command(name="reset", description="Réinitialiser totalement l'économie d'un membre")
    async def eco_reset(self, interaction: discord.Interaction, membre: discord.Member):
        if not await self.is_admin(interaction):
            return
        g = interaction.guild_id
        for t in ("users", "inventory", "boosts"):
            self.store.db.execute(f"DELETE FROM {t} WHERE guild_id=? AND user_id=?", (g, membre.id))
        self.store.db.commit()
        await interaction.response.send_message(f"♻️ Économie de {membre.mention} réinitialisée.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a réinitialisé l'économie de {membre.mention}.")

    @eco.command(name="event", description="Lancer un événement : bonus de gains pour tout le serveur")
    @app_commands.describe(bonus="Bonus en %", duree="Durée en minutes")
    async def eco_event(self, interaction: discord.Interaction, bonus: app_commands.Range[int, 1, 300],
                        duree: app_commands.Range[int, 1, 10080]):
        if not await self.is_admin(interaction):
            return
        end = int(time.time() + duree * 60)
        self.store.db.execute("INSERT INTO boosts(guild_id,user_id,bonus,expires) VALUES(?,0,?,?)",
                              (interaction.guild_id, bonus, end))
        self.store.db.commit()
        await interaction.response.send_message(embed=discord.Embed(
            title="🎉 Événement économique !", color=0xE91E63,
            description=f"**+{bonus} %** de gains pour tout le monde jusqu'à <t:{end}:t> (<t:{end}:R>)."))

    @eco.command(name="event_stop", description="Arrêter les événements de bonus en cours")
    async def eco_event_stop(self, interaction: discord.Interaction):
        if not await self.is_admin(interaction):
            return
        self.store.db.execute("DELETE FROM boosts WHERE guild_id=? AND user_id=0", (interaction.guild_id,))
        self.store.db.commit()
        await interaction.response.send_message("🛑 Événements de bonus arrêtés.", ephemeral=True)

    @eco.command(name="logchannel", description="Choisir le salon du journal de l'économie")
    async def eco_logchannel(self, interaction: discord.Interaction, salon: discord.TextChannel):
        if not await self.is_admin(interaction):
            return
        self.store.db.execute(
            "INSERT INTO config(guild_id,log_channel) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel=excluded.log_channel", (interaction.guild_id, salon.id))
        self.store.db.commit()
        await interaction.response.send_message(f"✅ Journal envoyé dans {salon.mention}.", ephemeral=True)

    @item.command(name="add", description="Ajouter un objet à la boutique")
    @app_commands.describe(
        nom="Nom de l'objet", prix="Prix", genre="Type d'objet", description="Description",
        categorie="Catégorie de la boutique", rarete="Rareté", stock="Stock (-1 = illimité)",
        valeur="Boost : bonus en % · Coffre : récompense maximale", duree="Boost : durée en minutes",
        role="Rôle donné (type Rôle)", limite="Maximum détenu par membre (0 = aucune limite)")
    @app_commands.choices(
        genre=[app_commands.Choice(name=v, value=k) for k, v in KINDS.items()],
        rarete=[app_commands.Choice(name="Commun", value="commun"), app_commands.Choice(name="Rare", value="rare"),
                app_commands.Choice(name="Épique", value="epique"), app_commands.Choice(name="Légendaire", value="legendaire")])
    async def item_add(self, interaction: discord.Interaction, nom: app_commands.Range[str, 1, 80],
                       prix: app_commands.Range[int, 1, 1_000_000_000], genre: str = "collection",
                       description: str = "", categorie: app_commands.Range[str, 1, 50] = "Général",
                       rarete: str = "commun", stock: app_commands.Range[int, -1, 1_000_000] = -1,
                       valeur: app_commands.Range[int, 0, 1_000_000_000] = 0,
                       duree: app_commands.Range[int, 0, 10080] = 0, role: Optional[discord.Role] = None,
                       limite: app_commands.Range[int, 0, 1000] = 0):
        if not await self.is_admin(interaction):
            return
        problem = None
        if genre == "role" and not role:
            problem = "Un objet de type Rôle demande l'option `role`."
        elif genre == "boost" and (valeur < 1 or duree < 1):
            problem = "Un boost demande `valeur` (bonus en %) et `duree` (minutes)."
        elif genre == "coffre" and valeur < 1:
            problem = "Un coffre demande `valeur` (récompense maximale)."
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        try:
            self.store.db.execute(
                "INSERT INTO items(guild_id,name,description,price,stock,category,rarity,kind,value,duration,role_id,max_per_user) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (interaction.guild_id, nom.strip(), description[:200], prix, stock, categorie.strip(), rarete,
                 genre, valeur, duree * 60, role.id if role else 0, limite))
            self.store.db.commit()
        except sqlite3.IntegrityError:
            self.store.db.rollback()
            return await interaction.response.send_message("❌ Un objet porte déjà ce nom.", ephemeral=True)
        await interaction.response.send_message(f"✅ **{nom}** ajouté à la boutique.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a ajouté **{nom}** ({fmt(prix)}).")

    @item.command(name="edit", description="Modifier un objet de la boutique")
    async def item_edit(self, interaction: discord.Interaction, nom: str,
                        prix: Optional[app_commands.Range[int, 1, 1_000_000_000]] = None,
                        stock: Optional[app_commands.Range[int, -1, 1_000_000]] = None,
                        description: Optional[str] = None, categorie: Optional[app_commands.Range[str, 1, 50]] = None,
                        nouveau_nom: Optional[app_commands.Range[str, 1, 80]] = None):
        if not await self.is_admin(interaction):
            return
        it = self.store.find_item(interaction.guild_id, nom)
        if not it:
            return await interaction.response.send_message("❌ Objet introuvable.", ephemeral=True)
        changes = {"price": prix, "stock": stock, "description": description[:200] if description is not None else None,
                   "category": categorie, "name": nouveau_nom}
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return await interaction.response.send_message("❌ Rien à modifier.", ephemeral=True)
        try:
            self.store.db.execute(
                f"UPDATE items SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?", (*changes.values(), it["id"]))
            self.store.db.commit()
        except sqlite3.IntegrityError:
            self.store.db.rollback()
            return await interaction.response.send_message("❌ Un objet porte déjà ce nom.", ephemeral=True)
        await interaction.response.send_message(f"✅ **{it['name']}** modifié.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a modifié **{it['name']}**.")

    @item.command(name="remove", description="Retirer un objet de la boutique (et des inventaires)")
    async def item_remove(self, interaction: discord.Interaction, nom: str):
        if not await self.is_admin(interaction):
            return
        it = self.store.find_item(interaction.guild_id, nom)
        if not it:
            return await interaction.response.send_message("❌ Objet introuvable.", ephemeral=True)
        self.store.db.execute("DELETE FROM inventory WHERE item_id=?", (it["id"],))
        self.store.db.execute("DELETE FROM items WHERE id=?", (it["id"],))
        self.store.db.commit()
        await interaction.response.send_message(f"🗑️ **{it['name']}** supprimé.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a supprimé **{it['name']}**.")

    # ================= AUTOCOMPLÉTION =================
    @buy.autocomplete("objet")
    @item_edit.autocomplete("nom")
    @item_remove.autocomplete("nom")
    async def ac_shop(self, interaction: discord.Interaction, current: str):
        rows = self.store.db.execute(
            "SELECT name FROM items WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
            (interaction.guild_id, f"%{current}%")).fetchall()
        return [app_commands.Choice(name=r["name"][:100], value=r["name"]) for r in rows]

    @use.autocomplete("objet")
    @sell.autocomplete("objet")
    async def ac_owned(self, interaction: discord.Interaction, current: str):
        rows = self.store.db.execute(
            "SELECT i.name FROM inventory inv JOIN items i ON i.id=inv.item_id "
            "WHERE inv.guild_id=? AND inv.user_id=? AND i.name LIKE ? ORDER BY i.name LIMIT 25",
            (interaction.guild_id, interaction.user.id, f"%{current}%")).fetchall()
        return [app_commands.Choice(name=r["name"][:100], value=r["name"]) for r in rows]

    @shop.autocomplete("categorie")
    async def ac_categories(self, interaction: discord.Interaction, current: str):
        rows = self.store.db.execute(
            "SELECT DISTINCT category FROM items WHERE guild_id=? AND category LIKE ? ORDER BY category LIMIT 25",
            (interaction.guild_id, f"%{current}%")).fetchall()
        return [app_commands.Choice(name=r[0][:100], value=r[0]) for r in rows]


async def setup(bot: commands.Bot):
    await bot.add_cog(Economie(bot))

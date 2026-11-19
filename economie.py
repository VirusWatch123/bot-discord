"""
economie.py : économie et boutique (cog discord.py 2.x + SQLite) avec panneau de configuration.

Chargement dans ton bot :  await bot.load_extension("economie")

Administration (réservée à ECO_OWNER_ID, au propriétaire défini dans BOT.py et à la personne qui a invité le bot) :
    /ecoconfig   panneau complet : réglages, boutique, membres, événements, journal
    /addmoney    ajouter de l'argent à un membre
    /removemoney retirer de l'argent à un membre
"""
import os
import random
import sqlite3
import time
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── Réglages fixes ─────────────────────────
DB_PATH = os.getenv("ECO_DB_PATH", "economie.db")   # fichier séparé de la base du quiz
ECO_OWNER_ID = 1572724368029646999                  # toi : accès à l'administration partout

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
    "commun": ("⚪", "Commun"),
    "rare": ("🔵", "Rare"),
    "epique": ("🟣", "Épique"),
    "legendaire": ("🟡", "Légendaire"),
}
KINDS = {"collection": "Collection", "role": "Rôle", "boost": "Boost de gains", "coffre": "Coffre"}

ROB_STEAL = (10, 30)   # % du portefeuille de la victime volés en cas de réussite
ROB_FINE = (10, 25)    # % du portefeuille du voleur payés en cas d'échec
COINFLIP_MAX_OPEN = 3  # paris coinflip ouverts en même temps par membre
OTHER_SIDE = {"pile": "face", "face": "pile"}

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 12, 7, 4, 2]
SLOT_TRIPLE = {"🍒": 3, "🍋": 4, "🍇": 6, "🔔": 10, "⭐": 15, "💎": 30, "7️⃣": 60}

BIG = 10 ** 9
# Réglages modifiables depuis le panneau : clé -> (section, libellé, type, défaut, min, max)
# (pour le type « text », min / max = longueur du texte)
SETTINGS = {
    "currency": ("general", "Nom / symbole de la monnaie", "text", "rubis", 1, 40),
    "start_wallet": ("general", "Solde de départ des nouveaux membres", "int", 100, 0, BIG),
    "shop_open": ("general", "Boutique ouverte", "bool", 1, 0, 1),
    "sell_ratio": ("general", "Prix de revente (% du prix d'achat)", "int", 50, 0, 100),
    "bonus_cap": ("general", "Bonus de gains maximum cumulé (%)", "int", 300, 0, 1000),
    "daily_base": ("gains", "Récompense /daily de base", "int", 250, 0, BIG),
    "daily_step": ("gains", "Bonus de série /daily (par jour d'affilée)", "int", 25, 0, BIG),
    "daily_cd": ("gains", "Délai du /daily (heures)", "int", 24, 1, 720),
    "work_cd": ("gains", "Délai du /work (minutes)", "int", 60, 1, 1440),
    "work_mult": ("gains", "Multiplicateur des salaires /work (%)", "int", 100, 10, 1000),
    "interest_pct": ("gains", "Intérêts bancaires avec /daily (%)", "int", 1, 0, 100),
    "interest_max": ("gains", "Plafond des intérêts par /daily", "int", 2000, 0, BIG),
    "msg_enabled": ("gains", "Gains en discutant", "bool", 1, 0, 1),
    "msg_min": ("gains", "Gain minimum par message", "int", 1, 0, 1_000_000),
    "msg_max": ("gains", "Gain maximum par message", "int", 5, 0, 1_000_000),
    "msg_cd": ("gains", "Délai entre deux gains de message (s)", "int", 60, 5, 3600),
    "games_enabled": ("games", "Jeux d'argent (coinflip, machine à sous)", "bool", 1, 0, 1),
    "max_bet": ("games", "Mise maximale de la machine à sous", "int", 100_000, 1, BIG),
    "coinflip_max": ("games", "Mise maximale du coinflip", "int", 5000, 1, BIG),
    "coinflip_ttl": ("games", "Durée de vie d'un pari coinflip (minutes)", "int", 30, 1, 1440),
    "rob_enabled": ("games", "Vol entre membres (/rob)", "bool", 1, 0, 1),
    "rob_chance": ("games", "Chances de réussite du vol (%)", "int", 40, 1, 95),
    "rob_cd": ("games", "Délai du /rob (minutes)", "int", 120, 1, 1440),
    "rob_min": ("games", "Minimum en portefeuille pour voler / être volé", "int", 200, 1, BIG),
    "log_channel": ("hidden", "Salon du journal", "int", 0, 0, 2 ** 63 - 1),
}
SECTIONS = {
    "general": ("⚙️", "Général"),
    "gains": ("💰", "Gains"),
    "games": ("🎲", "Jeux & vols"),
}

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
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (guild_id, key));
CREATE TABLE IF NOT EXISTS coinflips(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
    amount INTEGER NOT NULL, side TEXT NOT NULL, created REAL NOT NULL);
"""


# ───────────────────────── Utilitaires ─────────────────────────
def num(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


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


def parse_int(text: str) -> Optional[int]:
    try:
        return int(text.strip().replace(" ", "").replace("_", ""))
    except ValueError:
        return None


def job_for(work_count: int):
    return [j for j in JOBS if work_count >= j[0]][-1]


def rarity_emoji(r: str) -> str:
    return RARITIES.get(r, RARITIES["commun"])[0]


def stock_text(stock: int) -> str:
    if stock == -1:
        return "illimité"
    return "épuisé" if stock == 0 else f"{stock} en stock"


def item_field(r, money: Callable[[int], str]):
    lines = []
    if r["description"]:
        lines.append(r["description"])
    lines.append(f"💵 {money(r['price'])} · 📦 {stock_text(r['stock'])} · {KINDS.get(r['kind'], r['kind'])}")
    if r["kind"] == "boost":
        lines.append(f"⚡ +{r['value']} % de gains pendant {r['duration'] // 60} min")
    elif r["kind"] == "coffre":
        lines.append(f"🎁 Récompense jusqu'à {money(r['value'])}")
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
        self._cfg = {}

    # ---- réglages ----
    def cfg(self, g, key):
        spec = SETTINGS[key]
        cache = self._cfg.get(g)
        if cache is None:
            cache = {r["key"]: r["value"] for r in
                     self.db.execute("SELECT key, value FROM settings WHERE guild_id=?", (g,))}
            self._cfg[g] = cache
        raw = cache.get(key)
        if raw is None:
            return spec[3]
        if spec[2] == "text":
            return raw
        try:
            return int(raw)
        except ValueError:
            return spec[3]

    def set_cfg(self, g, key, value):
        self.db.execute(
            "INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (g, key, str(value)))
        self.db.commit()
        self._cfg.pop(g, None)

    def money(self, g, n) -> str:
        return f"{num(n)} {self.cfg(g, 'currency')}"

    # Les méthodes ci-dessous ne commit pas : chaque commande fait un seul commit à la fin,
    # sans await entre la lecture et l'écriture (donc opération atomique).
    def user(self, g, u):
        self.db.execute(
            "INSERT OR IGNORE INTO users(guild_id,user_id,wallet) VALUES(?,?,?)",
            (g, u, self.cfg(g, "start_wallet")))
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
        return min(sum(r[0] for r in rows), self.cfg(g, "bonus_cap"))

    def boosted(self, g, u, amount):
        b = self.bonus(g, u)
        return amount + amount * b // 100, b

    def events(self, g):
        return self.db.execute(
            "SELECT bonus, expires FROM boosts WHERE guild_id=? AND user_id=0 AND expires>? ORDER BY expires",
            (g, time.time())).fetchall()

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


# ───────────────────────── Boutique des membres (/shop) ─────────────────────────
class ShopView(discord.ui.View):
    PER_PAGE = 5

    def __init__(self, cog, origin: discord.Interaction, category: Optional[str] = None):
        super().__init__(timeout=180)
        self.cog, self.origin, self.category, self.page = cog, origin, category, 0
        self.g = origin.guild_id
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
        q, a = "SELECT * FROM items WHERE guild_id=?", [self.g]
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
            "SELECT DISTINCT category FROM items WHERE guild_id=? ORDER BY category LIMIT 24", (self.g,))]
        opts = [discord.SelectOption(label="Toutes les catégories", value="__all__", default=self.category is None)]
        opts += [discord.SelectOption(label=c[:100], value=c, default=(c == self.category)) for c in cats]
        cat = discord.ui.Select(placeholder="Filtrer par catégorie", options=opts, row=0)
        cat.callback = self.on_category
        self.add_item(cat)
        if chunk:
            buy = discord.ui.Select(
                placeholder="Acheter un objet (×1)", row=1,
                options=[discord.SelectOption(
                    label=r["name"][:100], value=str(r["id"]),
                    description=self.cog.m(self.g, r["price"])[:100],
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
            name, value = item_field(r, lambda n: self.cog.m(self.g, n))
            e.add_field(name=name, value=value, inline=False)
        wallet = self.cog.store.user(self.g, self.origin.user.id)["wallet"]
        e.set_footer(text=f"Page {self.page + 1}/{self.total_pages(len(items))} • "
                          f"Portefeuille : {self.cog.m(self.g, wallet)}")
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
        if not self.cog.store.cfg(self.g, "shop_open"):
            return await interaction.response.send_message("🔒 La boutique est fermée.", ephemeral=True)
        ok, msg = self.cog.purchase(self.g, interaction.user.id, int(interaction.data["values"][0]), 1)
        await interaction.response.send_message(msg, ephemeral=True)
        self.rebuild()
        try:
            await self.origin.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass


# ───────────────────────── Panneau de configuration ─────────────────────────
class AdminView(discord.ui.View):
    """Base des écrans du panneau : réservée à la personne qui l'a ouvert (et toujours autorisée)."""

    def __init__(self, cog, origin: discord.Interaction, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.cog, self.origin = cog, origin
        self.g = origin.guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.origin.user.id or not self.cog.can_admin(interaction.user.id, interaction.guild_id):
            await interaction.response.send_message("⛔ Ce panneau n'est pas à toi.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    async def go(self, interaction: discord.Interaction, view: "AdminView"):
        self.stop()  # annule le délai de cet écran pour qu'il n'écrase pas le suivant
        await interaction.response.edit_message(embed=view.embed(), view=view)

    def embed(self) -> discord.Embed:  # pragma: no cover
        raise NotImplementedError


class ErrorModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        import logging
        logging.getLogger("economie").exception("Erreur dans une fenêtre du panneau", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)


# ---------- Écran principal ----------
class MainPanel(AdminView):
    def embed(self):
        s, g, m = self.cog.store, self.g, self.cog.m
        n_items = s.db.execute("SELECT COUNT(*) FROM items WHERE guild_id=?", (g,)).fetchone()[0]
        members, total = s.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(wallet+bank),0) FROM users WHERE guild_id=?", (g,)).fetchone()
        log_id = s.cfg(g, "log_channel")
        events = s.events(g)
        yes = lambda k: "✅" if s.cfg(g, k) else "❌"
        e = discord.Embed(
            title="⚙️ Panneau de configuration de l'économie",
            description="Choisis une section avec les boutons ci-dessous.", color=0x5865F2)
        e.add_field(name="Monnaie", value=f"{s.cfg(g, 'currency')} · départ {m(g, s.cfg(g, 'start_wallet'))}")
        e.add_field(name="Boutique", value=f"{'🔓 ouverte' if s.cfg(g, 'shop_open') else '🔒 fermée'} · {n_items} objet(s)")
        e.add_field(name="Membres", value=f"{members} · {m(g, total)} en circulation")
        e.add_field(name="Gains en discutant", value=yes("msg_enabled"))
        e.add_field(name="Jeux d'argent", value=yes("games_enabled"))
        e.add_field(name="Vol", value=yes("rob_enabled"))
        e.add_field(name="Journal", value=f"<#{log_id}>" if log_id else "❌ aucun")
        e.add_field(name="Événement", value=(f"🎉 +{events[0]['bonus']} % jusqu'à <t:{int(events[0]['expires'])}:R>"
                                              if events else "aucun"))
        return e

    @discord.ui.button(label="Général", emoji="⚙️", style=discord.ButtonStyle.primary, row=0)
    async def b_general(self, interaction, button):
        await self.go(interaction, SettingsPanel(self.cog, self.origin, "general"))

    @discord.ui.button(label="Gains", emoji="💰", style=discord.ButtonStyle.primary, row=0)
    async def b_gains(self, interaction, button):
        await self.go(interaction, SettingsPanel(self.cog, self.origin, "gains"))

    @discord.ui.button(label="Jeux & vols", emoji="🎲", style=discord.ButtonStyle.primary, row=0)
    async def b_games(self, interaction, button):
        await self.go(interaction, SettingsPanel(self.cog, self.origin, "games"))

    @discord.ui.button(label="Boutique", emoji="🛍️", style=discord.ButtonStyle.success, row=1)
    async def b_shop(self, interaction, button):
        await self.go(interaction, ShopPanel(self.cog, self.origin))

    @discord.ui.button(label="Membres", emoji="👥", style=discord.ButtonStyle.success, row=1)
    async def b_members(self, interaction, button):
        await self.go(interaction, MembersPanel(self.cog, self.origin))

    @discord.ui.button(label="Événements", emoji="🎉", style=discord.ButtonStyle.success, row=1)
    async def b_events(self, interaction, button):
        await self.go(interaction, EventsPanel(self.cog, self.origin))

    @discord.ui.button(label="Journal", emoji="📒", style=discord.ButtonStyle.secondary, row=2)
    async def b_log(self, interaction, button):
        await self.go(interaction, LogPanel(self.cog, self.origin))

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def b_refresh(self, interaction, button):
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Fermer", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
    async def b_close(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Panneau fermé.", embed=None, view=None)


# ---------- Réglages (Général / Gains / Jeux) ----------
def show_setting(store: Store, g, key) -> str:
    spec, v = SETTINGS[key], store.cfg(g, key)
    if spec[2] == "bool":
        return "✅ Activé" if v else "❌ Désactivé"
    return str(v) if spec[2] == "text" else num(v)


class SettingModal(ErrorModal):
    def __init__(self, panel: "SettingsPanel", key: str):
        spec = SETTINGS[key]
        super().__init__(title=spec[1][:45])
        self.panel, self.key = panel, key
        self.field = discord.ui.TextInput(
            label="Nouvelle valeur", default=str(panel.cog.store.cfg(panel.g, key)),
            max_length=spec[5] if spec[2] == "text" else 20)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        spec, g, store = SETTINGS[self.key], self.panel.g, self.panel.cog.store
        raw = self.field.value.strip()
        if spec[2] == "text":
            if not spec[4] <= len(raw) <= spec[5]:
                return await interaction.response.send_message("⚠️ Valeur invalide.", ephemeral=True)
            value = raw
        else:
            value = parse_int(raw)
            if value is None or not spec[4] <= value <= spec[5]:
                return await interaction.response.send_message(
                    f"⚠️ Entre un nombre entre **{num(spec[4])}** et **{num(spec[5])}**.", ephemeral=True)
            if self.key == "msg_min" and value > store.cfg(g, "msg_max"):
                return await interaction.response.send_message("⚠️ Le minimum doit être ≤ au maximum.", ephemeral=True)
            if self.key == "msg_max" and value < store.cfg(g, "msg_min"):
                return await interaction.response.send_message("⚠️ Le maximum doit être ≥ au minimum.", ephemeral=True)
        store.set_cfg(g, self.key, value)
        await self.panel.cog.log_event(interaction.guild, f"{interaction.user.mention} a réglé **{spec[1]}** sur `{value}`.")
        await self.panel.go(interaction, SettingsPanel(self.panel.cog, self.panel.origin, self.panel.section))


class SettingsPanel(AdminView):
    def __init__(self, cog, origin, section: str):
        super().__init__(cog, origin)
        self.section = section
        self.keys = [k for k, v in SETTINGS.items() if v[0] == section]
        opts = [discord.SelectOption(
            label=SETTINGS[k][1][:100], value=k,
            description=(("Clic = activer / désactiver · " if SETTINGS[k][2] == "bool" else "") +
                         f"Actuel : {show_setting(cog.store, self.g, k)}")[:100])
            for k in self.keys]
        sel = discord.ui.Select(placeholder="Choisis un réglage à modifier…", options=opts, row=0)
        sel.callback = self.on_pick
        self.add_item(sel)

    def embed(self):
        icon, name = SECTIONS[self.section]
        lines = [f"**{SETTINGS[k][1]}** : {show_setting(self.cog.store, self.g, k)}" for k in self.keys]
        e = discord.Embed(title=f"{icon} Réglages : {name}", description="\n".join(lines), color=0x5865F2)
        e.set_footer(text="Choisis un réglage dans le menu pour le modifier (les interrupteurs changent tout de suite).")
        return e

    async def on_pick(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        if SETTINGS[key][2] == "bool":
            new = 0 if self.cog.store.cfg(self.g, key) else 1
            self.cog.store.set_cfg(self.g, key, new)
            await self.cog.log_event(interaction.guild, f"{interaction.user.mention} a réglé **{SETTINGS[key][1]}** sur `{'oui' if new else 'non'}`.")
            return await self.go(interaction, SettingsPanel(self.cog, self.origin, self.section))
        await interaction.response.send_modal(SettingModal(self, key))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))


# ---------- Confirmation générique ----------
class ConfirmView(AdminView):
    def __init__(self, cog, origin, text: str, action: Callable[[], str], back: Callable[[], AdminView]):
        super().__init__(cog, origin, timeout=120)
        self.text, self.action, self.back = text, action, back

    def embed(self):
        return discord.Embed(title="⚠️ Confirmation", description=self.text, color=0xE74C3C)

    @discord.ui.button(label="Confirmer", emoji="✅", style=discord.ButtonStyle.danger)
    async def yes(self, interaction, button):
        result = self.action()
        await self.go(interaction, self.back())
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await self.go(interaction, self.back())


# ---------- Gestion de la boutique ----------
def new_draft() -> dict:
    return dict(name="", description="", price=100, stock=-1, category="Général", rarity="commun",
                kind="collection", value=0, duration=0, role_id=0, max_per_user=0)


class ItemInfoModal(ErrorModal):
    def __init__(self, draft: dict, on_done: Callable):
        super().__init__(title="Informations de l'objet")
        self.on_done = on_done
        self.f_name = discord.ui.TextInput(label="Nom", default=draft["name"] or None, max_length=80)
        self.f_price = discord.ui.TextInput(label="Prix", default=str(draft["price"]), max_length=12)
        self.f_desc = discord.ui.TextInput(
            label="Description (facultatif)", style=discord.TextStyle.paragraph,
            default=draft["description"] or None, required=False, max_length=200)
        self.f_cat = discord.ui.TextInput(label="Catégorie", default=draft["category"], max_length=50)
        self.f_stock = discord.ui.TextInput(label="Stock (-1 = illimité)", default=str(draft["stock"]), max_length=9)
        for f in (self.f_name, self.f_price, self.f_desc, self.f_cat, self.f_stock):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        price, stock = parse_int(self.f_price.value), parse_int(self.f_stock.value)
        if price is None or price < 1 or price > BIG:
            return await interaction.response.send_message("⚠️ Prix invalide (nombre ≥ 1).", ephemeral=True)
        if stock is None or stock < -1 or stock > 1_000_000:
            return await interaction.response.send_message("⚠️ Stock invalide (-1 = illimité).", ephemeral=True)
        await self.on_done(interaction, dict(
            name=self.f_name.value.strip(), price=price, description=self.f_desc.value.strip(),
            category=self.f_cat.value.strip() or "Général", stock=stock))


class ItemDetailModal(ErrorModal):
    def __init__(self, editor: "ItemEditorView"):
        super().__init__(title="Détails de l'objet")
        self.editor = editor
        d = editor.draft
        self.value_in = self.dur_in = None
        if d["kind"] in ("boost", "coffre"):
            self.value_in = discord.ui.TextInput(
                label="Bonus en %" if d["kind"] == "boost" else "Récompense maximale",
                default=str(d["value"]), max_length=12)
            self.add_item(self.value_in)
        if d["kind"] == "boost":
            self.dur_in = discord.ui.TextInput(label="Durée du boost (minutes)", default=str(d["duration"] // 60),
                                               max_length=6)
            self.add_item(self.dur_in)
        self.limit_in = discord.ui.TextInput(label="Limite par membre (0 = aucune)", default=str(d["max_per_user"]),
                                             max_length=6)
        self.add_item(self.limit_in)

    async def on_submit(self, interaction: discord.Interaction):
        d = self.editor.draft
        limit = parse_int(self.limit_in.value)
        value = parse_int(self.value_in.value) if self.value_in else d["value"]
        minutes = parse_int(self.dur_in.value) if self.dur_in else d["duration"] // 60
        if limit is None or limit < 0 or value is None or value < 0 or minutes is None or minutes < 0:
            return await interaction.response.send_message("⚠️ Entre des nombres positifs.", ephemeral=True)
        d.update(max_per_user=limit, value=value, duration=minutes * 60)
        await self.editor.refresh(interaction)


class StockModal(ErrorModal):
    def __init__(self, panel: "ShopPanel", item):
        super().__init__(title=f"Stock : {item['name']}"[:45])
        self.panel, self.item = panel, item
        self.field = discord.ui.TextInput(label="Nouveau stock, +N ou -1", default=str(item["stock"]),
                                          placeholder="50 · +10 (ajouter) · -1 (illimité)", max_length=9)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.field.value.strip()
        add = raw.startswith("+")
        n = parse_int(raw[1:] if add else raw)
        if n is None or n < (0 if add else -1) or n > 1_000_000:
            return await interaction.response.send_message("⚠️ Valeur invalide.", ephemeral=True)
        cur = self.item["stock"]
        new = cur + n if add and cur != -1 else n
        self.panel.cog.store.db.execute("UPDATE items SET stock=? WHERE id=?", (new, self.item["id"]))
        self.panel.cog.store.db.commit()
        await self.panel.go(interaction, ShopPanel(self.panel.cog, self.panel.origin, self.panel.page, self.item["id"]))
        await interaction.followup.send(f"📦 Stock de **{self.item['name']}** : {stock_text(new)}.", ephemeral=True)


class ShopPanel(AdminView):
    PER = 10

    def __init__(self, cog, origin, page: int = 0, selected: Optional[int] = None):
        super().__init__(cog, origin)
        self.page, self.selected, self.pick = page, selected, None
        self._render()

    def items(self):
        return self.cog.store.db.execute(
            "SELECT * FROM items WHERE guild_id=? ORDER BY category, name", (self.g,)).fetchall()

    def _chunk(self):
        items = self.items()
        pages = max(1, -(-len(items) // self.PER))
        self.page = max(0, min(self.page, pages - 1))
        return items, items[self.page * self.PER:(self.page + 1) * self.PER], pages

    def _render(self):
        items, chunk, pages = self._chunk()
        if self.pick is not None:
            self.remove_item(self.pick)
            self.pick = None
        if chunk:
            self.pick = discord.ui.Select(
                placeholder="Choisis un objet à modifier…", row=0,
                options=[discord.SelectOption(
                    label=r["name"][:100], value=str(r["id"]), emoji=rarity_emoji(r["rarity"]),
                    description=f"{self.cog.m(self.g, r['price'])} · {r['category']}"[:100],
                    default=r["id"] == self.selected) for r in chunk])
            self.pick.callback = self.on_pick
            self.add_item(self.pick)
        has = self.selected is not None and any(r["id"] == self.selected for r in items)
        if not has:
            self.selected = None
        self.edit_btn.disabled = self.stock_btn.disabled = self.del_btn.disabled = not has
        self.prev_btn.disabled, self.next_btn.disabled = self.page == 0, self.page >= pages - 1
        opened = bool(self.cog.store.cfg(self.g, "shop_open"))
        self.toggle_btn.label = "Boutique : ouverte" if opened else "Boutique : fermée"
        self.toggle_btn.emoji = "🔓" if opened else "🔒"
        self.toggle_btn.style = discord.ButtonStyle.success if opened else discord.ButtonStyle.danger

    def embed(self):
        items, chunk, pages = self._chunk()
        m = lambda n: self.cog.m(self.g, n)
        lines = [f"{rarity_emoji(r['rarity'])} **{r['name']}** · {m(r['price'])} · {stock_text(r['stock'])} · "
                 f"{r['category']} · {KINDS.get(r['kind'], r['kind'])}" for r in chunk]
        e = discord.Embed(title="🛍️ Gestion de la boutique", color=0x2ECC71,
                          description="\n".join(lines) or "Aucun objet pour l'instant. Clique sur **➕ Ajouter**.")
        sel = next((r for r in items if r["id"] == self.selected), None)
        if sel:
            name, value = item_field(sel, m)
            e.add_field(name=f"Sélection : {name}", value=value, inline=False)
        e.set_footer(text=f"Page {self.page + 1}/{pages} · {len(items)} objet(s)")
        return e

    async def on_pick(self, interaction: discord.Interaction):
        self.selected = int(interaction.data["values"][0])
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def _selected_row(self):
        return self.cog.store.db.execute(
            "SELECT * FROM items WHERE id=? AND guild_id=?", (self.selected, self.g)).fetchone()

    @discord.ui.button(label="Ajouter", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_btn(self, interaction, button):
        draft = new_draft()

        async def done(inter, info):
            draft.update(info)
            await self.go(inter, ItemEditorView(self.cog, self.origin, draft, None))
        await interaction.response.send_modal(ItemInfoModal(draft, done))

    @discord.ui.button(label="Modifier", emoji="✏️", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def edit_btn(self, interaction, button):
        r = self._selected_row()
        if not r:
            return await interaction.response.send_message("❌ Cet objet n'existe plus.", ephemeral=True)
        draft = {k: r[k] for k in new_draft()}
        await self.go(interaction, ItemEditorView(self.cog, self.origin, draft, r["id"]))

    @discord.ui.button(label="Stock", emoji="📦", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def stock_btn(self, interaction, button):
        r = self._selected_row()
        if not r:
            return await interaction.response.send_message("❌ Cet objet n'existe plus.", ephemeral=True)
        await interaction.response.send_modal(StockModal(self, r))

    @discord.ui.button(label="Supprimer", emoji="🗑️", style=discord.ButtonStyle.danger, row=1, disabled=True)
    async def del_btn(self, interaction, button):
        r = self._selected_row()
        if not r:
            return await interaction.response.send_message("❌ Cet objet n'existe plus.", ephemeral=True)

        def action():
            db = self.cog.store.db
            db.execute("DELETE FROM inventory WHERE item_id=?", (r["id"],))
            db.execute("DELETE FROM items WHERE id=?", (r["id"],))
            db.commit()
            return f"🗑️ **{r['name']}** supprimé (et retiré des inventaires)."
        await self.go(interaction, ConfirmView(
            self.cog, self.origin,
            f"Supprimer **{r['name']}** de la boutique ? Il sera aussi retiré de tous les inventaires.",
            action, lambda: ShopPanel(self.cog, self.origin, self.page)))

    @discord.ui.button(label="Boutique", style=discord.ButtonStyle.success, row=1)
    async def toggle_btn(self, interaction, button):
        new = 0 if self.cog.store.cfg(self.g, "shop_open") else 1
        self.cog.store.set_cfg(self.g, "shop_open", new)
        await self.cog.log_event(interaction.guild, f"{interaction.user.mention} a {'ouvert' if new else 'fermé'} la boutique.")
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Précédent", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
    async def prev_btn(self, interaction, button):
        self.page -= 1
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Suivant", emoji="▶️", style=discord.ButtonStyle.secondary, row=2)
    async def next_btn(self, interaction, button):
        self.page += 1
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))


class ItemEditorView(AdminView):
    """Création / modification d'un objet : type, rareté, rôle, détails, puis enregistrement."""

    def __init__(self, cog, origin, draft: dict, item_id: Optional[int]):
        super().__init__(cog, origin)
        self.draft, self.item_id = draft, item_id
        self.role_sel = None
        self._render()

    def _render(self):
        d = self.draft
        self.clear_items()
        kind = discord.ui.Select(placeholder="Type d'objet", row=0, options=[
            discord.SelectOption(label=v, value=k, default=k == d["kind"]) for k, v in KINDS.items()])
        kind.callback = self.on_kind
        rarity = discord.ui.Select(placeholder="Rareté", row=1, options=[
            discord.SelectOption(label=v[1], value=k, emoji=v[0], default=k == d["rarity"])
            for k, v in RARITIES.items()])
        rarity.callback = self.on_rarity
        self.add_item(kind)
        self.add_item(rarity)
        if d["kind"] == "role":
            role = discord.ui.RoleSelect(placeholder="Rôle donné quand on utilise l'objet",
                                         min_values=1, max_values=1, row=2)

            async def on_role(interaction: discord.Interaction):
                self.draft["role_id"] = role.values[0].id
                await self.refresh(interaction)
            role.callback = on_role
            self.add_item(role)
        for b in (self.info_btn, self.detail_btn, self.save_btn, self.cancel_btn):
            self.add_item(b)

    def problem(self) -> Optional[str]:
        d = self.draft
        if not d["name"]:
            return "Donne un nom à l'objet (bouton **Infos**)."
        if d["kind"] == "role" and not d["role_id"]:
            return "Choisis le rôle à donner."
        if d["kind"] == "boost" and (d["value"] < 1 or d["duration"] < 1):
            return "Un boost demande un bonus en % et une durée (bouton **Détails**)."
        if d["kind"] == "coffre" and d["value"] < 1:
            return "Un coffre demande une récompense maximale (bouton **Détails**)."
        return None

    def embed(self):
        d, m = self.draft, lambda n: self.cog.m(self.g, n)
        shown = dict(d, name=d["name"] or "(sans nom)")
        name, value = item_field(shown, m)
        e = discord.Embed(
            title="🛠️ " + ("Modifier l'objet" if self.item_id else "Nouvel objet"),
            color=0x3498DB, description=f"**{name}**\n{value}")
        e.add_field(name="Catégorie", value=d["category"])
        e.add_field(name="Rareté", value=RARITIES[d["rarity"]][1])
        e.add_field(name="Type", value=KINDS[d["kind"]])
        problem = self.problem()
        e.set_footer(text=("⚠️ " + problem) if problem else "Tout est prêt : clique sur Enregistrer.")
        return e

    async def refresh(self, interaction: discord.Interaction):
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_kind(self, interaction: discord.Interaction):
        self.draft["kind"] = interaction.data["values"][0]
        await self.refresh(interaction)

    async def on_rarity(self, interaction: discord.Interaction):
        self.draft["rarity"] = interaction.data["values"][0]
        await self.refresh(interaction)

    @discord.ui.button(label="Infos", emoji="📝", style=discord.ButtonStyle.secondary, row=3)
    async def info_btn(self, interaction, button):
        async def done(inter, info):
            self.draft.update(info)
            await self.refresh(inter)
        await interaction.response.send_modal(ItemInfoModal(self.draft, done))

    @discord.ui.button(label="Détails", emoji="🔧", style=discord.ButtonStyle.secondary, row=3)
    async def detail_btn(self, interaction, button):
        await interaction.response.send_modal(ItemDetailModal(self))

    @discord.ui.button(label="Enregistrer", emoji="💾", style=discord.ButtonStyle.success, row=3)
    async def save_btn(self, interaction, button):
        problem = self.problem()
        if problem:
            return await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
        d, db = self.draft, self.cog.store.db
        try:
            if self.item_id:
                db.execute(
                    "UPDATE items SET name=?, description=?, price=?, stock=?, category=?, rarity=?, kind=?, "
                    "value=?, duration=?, role_id=?, max_per_user=? WHERE id=? AND guild_id=?",
                    (d["name"], d["description"], d["price"], d["stock"], d["category"], d["rarity"], d["kind"],
                     d["value"], d["duration"], d["role_id"], d["max_per_user"], self.item_id, self.g))
            else:
                cur = db.execute(
                    "INSERT INTO items(guild_id,name,description,price,stock,category,rarity,kind,value,duration,"
                    "role_id,max_per_user) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.g, d["name"], d["description"], d["price"], d["stock"], d["category"], d["rarity"],
                     d["kind"], d["value"], d["duration"], d["role_id"], d["max_per_user"]))
                self.item_id = cur.lastrowid
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return await interaction.response.send_message("⚠️ Un objet porte déjà ce nom.", ephemeral=True)
        await self.cog.log_event(interaction.guild, f"{interaction.user.mention} a enregistré l'objet **{d['name']}**.")
        await self.go(interaction, ShopPanel(self.cog, self.origin, selected=self.item_id))
        await interaction.followup.send(f"✅ **{d['name']}** enregistré.", ephemeral=True)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, row=3)
    async def cancel_btn(self, interaction, button):
        await self.go(interaction, ShopPanel(self.cog, self.origin, selected=self.item_id))


# ---------- Membres ----------
class MoneyModal(ErrorModal):
    def __init__(self, panel: "MembersPanel", sign: int):
        super().__init__(title="Ajouter de l'argent" if sign > 0 else "Retirer de l'argent")
        self.panel, self.sign = panel, sign
        self.field = discord.ui.TextInput(label="Montant", max_length=14)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        amount = parse_int(self.field.value)
        if amount is None or amount < 1 or amount > 10 ** 12:
            return await interaction.response.send_message("⚠️ Montant invalide.", ephemeral=True)
        p = self.panel
        applied = p.cog.adjust(p.g, p.member_id, amount * self.sign, p.where)
        await p.cog.log_event(interaction.guild, f"{interaction.user.mention} a {'ajouté' if self.sign > 0 else 'retiré'} "
                                                 f"**{p.cog.m(p.g, abs(applied))}** à <@{p.member_id}> ({'portefeuille' if p.where == 'wallet' else 'banque'}).")
        await p.go(interaction, MembersPanel(p.cog, p.origin, p.member_id, p.where))


class MembersPanel(AdminView):
    def __init__(self, cog, origin, member_id: Optional[int] = None, where: str = "wallet"):
        super().__init__(cog, origin)
        self.member_id, self.where = member_id, where
        pick = discord.ui.UserSelect(placeholder="Choisis un membre…", min_values=1, max_values=1, row=0)

        async def on_pick(interaction: discord.Interaction):
            user = pick.values[0]
            if user.bot:
                return await interaction.response.send_message("⚠️ Choisis un membre, pas un bot.", ephemeral=True)
            self.member_id = user.id
            await self.go(interaction, MembersPanel(self.cog, self.origin, user.id, self.where))
        pick.callback = on_pick
        self.add_item(pick)
        for b in (self.add_btn, self.remove_btn, self.where_btn, self.reset_btn, self.back_btn):
            b.disabled = member_id is None and b is not self.back_btn
        self.where_btn.label = "Cible : " + ("portefeuille" if where == "wallet" else "banque")

    def embed(self):
        e = discord.Embed(title="👥 Gestion des membres", color=0x2ECC71,
                          description="Choisis un membre, puis ajoute ou retire de l'argent, ou réinitialise son économie.")
        if self.member_id:
            r = self.cog.store.user(self.g, self.member_id)
            self.cog.store.db.commit()
            n_inv = self.cog.store.db.execute(
                "SELECT COALESCE(SUM(qty),0) FROM inventory WHERE guild_id=? AND user_id=?",
                (self.g, self.member_id)).fetchone()[0]
            e.add_field(name="Membre", value=f"<@{self.member_id}>")
            e.add_field(name="Portefeuille", value=self.cog.m(self.g, r["wallet"]))
            e.add_field(name="Banque", value=self.cog.m(self.g, r["bank"]))
            e.add_field(name="Objets", value=str(n_inv))
        return e

    @discord.ui.button(label="Ajouter", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_btn(self, interaction, button):
        await interaction.response.send_modal(MoneyModal(self, +1))

    @discord.ui.button(label="Retirer", emoji="➖", style=discord.ButtonStyle.danger, row=1)
    async def remove_btn(self, interaction, button):
        await interaction.response.send_modal(MoneyModal(self, -1))

    @discord.ui.button(label="Cible", style=discord.ButtonStyle.secondary, row=1)
    async def where_btn(self, interaction, button):
        await self.go(interaction, MembersPanel(self.cog, self.origin, self.member_id,
                                                "bank" if self.where == "wallet" else "wallet"))

    @discord.ui.button(label="Réinitialiser", emoji="♻️", style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, interaction, button):
        uid = self.member_id

        def action():
            db = self.cog.store.db
            for t in ("users", "inventory", "boosts"):
                db.execute(f"DELETE FROM {t} WHERE guild_id=? AND user_id=?", (self.g, uid))
            db.commit()
            return f"♻️ Économie de <@{uid}> réinitialisée."
        await self.go(interaction, ConfirmView(
            self.cog, self.origin, f"Réinitialiser toute l'économie de <@{uid}> (argent, inventaire, boosts) ?",
            action, lambda: MembersPanel(self.cog, self.origin, uid, self.where)))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))


# ---------- Événements ----------
class EventModal(ErrorModal):
    def __init__(self, panel: "EventsPanel"):
        super().__init__(title="Lancer un événement")
        self.panel = panel
        self.f_bonus = discord.ui.TextInput(label="Bonus de gains (%)", default="50", max_length=4)
        self.f_dur = discord.ui.TextInput(label="Durée (minutes)", default="60", max_length=6)
        self.add_item(self.f_bonus)
        self.add_item(self.f_dur)

    async def on_submit(self, interaction: discord.Interaction):
        bonus, minutes = parse_int(self.f_bonus.value), parse_int(self.f_dur.value)
        if bonus is None or not 1 <= bonus <= 300 or minutes is None or not 1 <= minutes <= 10080:
            return await interaction.response.send_message(
                "⚠️ Bonus entre 1 et 300 %, durée entre 1 et 10 080 minutes.", ephemeral=True)
        p = self.panel
        end = int(time.time() + minutes * 60)
        p.cog.store.db.execute("INSERT INTO boosts(guild_id,user_id,bonus,expires) VALUES(?,0,?,?)", (p.g, bonus, end))
        p.cog.store.db.commit()
        await p.go(interaction, EventsPanel(p.cog, p.origin))
        try:
            await interaction.channel.send(embed=discord.Embed(
                title="🎉 Événement économique !", color=0xE91E63,
                description=f"**+{bonus} %** de gains pour tout le monde jusqu'à <t:{end}:t> (<t:{end}:R>)."))
        except (discord.HTTPException, AttributeError):
            pass


class EventsPanel(AdminView):
    def embed(self):
        events = self.cog.store.events(self.g)
        lines = [f"🎉 **+{e['bonus']} %** jusqu'à <t:{int(e['expires'])}:f> (<t:{int(e['expires'])}:R>)" for e in events]
        return discord.Embed(
            title="🎉 Événements", color=0xE91E63,
            description="\n".join(lines) or "Aucun événement en cours.\nUn événement donne un bonus de gains à tout le serveur.")

    @discord.ui.button(label="Lancer un événement", emoji="🚀", style=discord.ButtonStyle.success)
    async def launch(self, interaction, button):
        await interaction.response.send_modal(EventModal(self))

    @discord.ui.button(label="Tout arrêter", emoji="🛑", style=discord.ButtonStyle.danger)
    async def stop_all(self, interaction, button):
        self.cog.store.db.execute("DELETE FROM boosts WHERE guild_id=? AND user_id=0", (self.g,))
        self.cog.store.db.commit()
        await self.go(interaction, EventsPanel(self.cog, self.origin))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))


# ---------- Journal ----------
class LogPanel(AdminView):
    def __init__(self, cog, origin):
        super().__init__(cog, origin)
        sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text],
                                       placeholder="Choisis le salon du journal…", min_values=1, max_values=1, row=0)

        async def on_pick(interaction: discord.Interaction):
            self.cog.store.set_cfg(self.g, "log_channel", sel.values[0].id)
            await self.go(interaction, LogPanel(self.cog, self.origin))
        sel.callback = on_pick
        self.add_item(sel)

    def embed(self):
        log_id = self.cog.store.cfg(self.g, "log_channel")
        return discord.Embed(
            title="📒 Journal de l'économie", color=0x2C3E50,
            description=("Les actions d'administration (réglages, boutique, argent donné ou retiré) sont écrites dans "
                         f"{'<#' + str(log_id) + '>' if log_id else 'aucun salon pour l’instant'}."))

    @discord.ui.button(label="Désactiver le journal", emoji="🚫", style=discord.ButtonStyle.danger, row=1)
    async def off(self, interaction, button):
        self.cog.store.set_cfg(self.g, "log_channel", 0)
        await self.go(interaction, LogPanel(self.cog, self.origin))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))


# ---------- Coinflip entre membres ----------
class CoinflipModal(ErrorModal):
    def __init__(self, lobby: "CoinflipView", side: str):
        super().__init__(title=f"Coinflip : tu prends {side}"[:45])
        self.lobby, self.side = lobby, side
        maxi = lobby.cog.store.cfg(lobby.g, "coinflip_max")
        self.f_amount = discord.ui.TextInput(
            label="Ta mise (nombre, tout ou moitié)", placeholder=f"Maximum : {num(maxi)}", max_length=14)
        self.add_item(self.f_amount)

    async def on_submit(self, interaction: discord.Interaction):
        lobby = self.lobby
        res, err = lobby.cog.create_coinflip(lobby.g, interaction.user.id, self.side, self.f_amount.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        lobby.rebuild()
        await interaction.response.edit_message(embed=lobby.embed(), view=lobby)
        try:
            await interaction.channel.send(embed=lobby.cog.coinflip_created_embed(lobby.g, interaction.user, res),
                                           allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError):
            pass


class CoinflipView(discord.ui.View):
    """Liste des paris coinflip ouverts : on en relève un, ou on crée le sien."""

    def __init__(self, cog, origin: discord.Interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, origin
        self.g, self.uid = origin.guild_id, origin.user.id
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.origin.user.id:
            await interaction.response.send_message("Tape `/coinflip` pour ouvrir ta propre liste.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    def rebuild(self):
        self.cog.refund_expired_coinflips(self.g)
        self.clear_items()
        rows = self.cog.open_coinflips(self.g)
        others = [r for r in rows if r["creator_id"] != self.uid][:25]
        guild = self.origin.guild
        if others:
            opts = []
            for r in others:
                member = guild.get_member(r["creator_id"]) if guild else None
                opts.append(discord.SelectOption(
                    label=f"{self.cog.m(self.g, r['amount'])} · {member.display_name if member else 'un membre'}"[:100],
                    value=str(r["id"]),
                    description=f"Tu auras {OTHER_SIDE[r['side']]} (il a pris {r['side']})"[:100]))
            sel = discord.ui.Select(placeholder="Relever un pari…", options=opts, row=0)
            sel.callback = self.on_join
            self.add_item(sel)
        self.b_cancel.disabled = not any(r["creator_id"] == self.uid for r in rows)
        for b in (self.b_pile, self.b_face, self.b_cancel, self.b_refresh):
            self.add_item(b)

    def embed(self):
        g, m = self.g, self.cog.m
        rows = self.cog.open_coinflips(g)
        ttl = self.cog.store.cfg(g, "coinflip_ttl") * 60
        e = discord.Embed(title="🪙 Coinflips disponibles", color=0xF1C40F)
        if rows:
            lines = [f"**#{r['id']}** · <@{r['creator_id']}> met **{m(g, r['amount'])}** sur **{r['side']}** · "
                     f"expire <t:{int(r['created'] + ttl)}:R>" + (" · *(le tien)*" if r["creator_id"] == self.uid else "")
                     for r in rows[:15]]
            e.description = "\n".join(lines)
        else:
            e.description = "Aucun pari disponible pour le moment.\nCrée le tien avec les boutons ci-dessous !"
        e.add_field(
            name="Comment ça marche ?",
            value=("Un joueur met une mise en jeu et choisit **pile** ou **face**. Un autre membre relève le pari avec la "
                   "même mise et prend l'autre côté. La pièce est lancée : **le gagnant empoche les deux mises, "
                   "le perdant perd la sienne**."), inline=False)
        wallet = self.cog.store.user(g, self.uid)["wallet"]
        e.set_footer(text=f"Mise max : {m(g, self.cog.store.cfg(g, 'coinflip_max'))} · Ton portefeuille : {m(g, wallet)}")
        return e

    async def on_join(self, interaction: discord.Interaction):
        res, err = self.cog.join_coinflip(self.g, interaction.user.id, int(interaction.data["values"][0]))
        self.rebuild()
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            try:
                await self.origin.edit_original_response(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)
        e = self.cog.coinflip_result_embed(self.g, res)
        mentions = discord.AllowedMentions(users=[discord.Object(id=res["creator"]), discord.Object(id=res["joiner"])])
        try:
            await interaction.channel.send(content=f"<@{res['creator']}> <@{res['joiner']}>", embed=e,
                                           allowed_mentions=mentions)
        except (discord.HTTPException, AttributeError):
            await interaction.followup.send(embed=e, allowed_mentions=mentions)

    @discord.ui.button(label="Créer : pile", emoji="🪙", style=discord.ButtonStyle.success, row=1)
    async def b_pile(self, interaction, button):
        await interaction.response.send_modal(CoinflipModal(self, "pile"))

    @discord.ui.button(label="Créer : face", emoji="🪙", style=discord.ButtonStyle.success, row=1)
    async def b_face(self, interaction, button):
        await interaction.response.send_modal(CoinflipModal(self, "face"))

    @discord.ui.button(label="Annuler mes paris", emoji="↩️", style=discord.ButtonStyle.danger, row=1)
    async def b_cancel(self, interaction, button):
        n, total = self.cog.cancel_coinflips(self.g, interaction.user.id)
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)
        await interaction.followup.send(f"↩️ {n} pari(s) annulé(s) : {self.cog.m(self.g, total)} rendus.", ephemeral=True)

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def b_refresh(self, interaction, button):
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)


# ───────────────────────── Cog ─────────────────────────
class Economie(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(DB_PATH)
        self._msg_cache = {}

    async def cog_load(self):
        self.store.db.execute("DELETE FROM boosts WHERE expires<=?", (time.time(),))
        self.store.db.commit()
        self.refund_expired_coinflips()
        self.coinflip_cleanup.start()

    async def cog_unload(self):
        self.coinflip_cleanup.cancel()

    @tasks.loop(seconds=60)
    async def coinflip_cleanup(self):
        self.refund_expired_coinflips()

    @coinflip_cleanup.before_loop
    async def _before_coinflip_cleanup(self):
        await self.bot.wait_until_ready()

    # ---- outils ----
    def m(self, g, n) -> str:
        return self.store.money(g, n)

    def can_admin(self, user_id: int, guild_id: Optional[int]) -> bool:
        """Toi (ECO_OWNER_ID), le propriétaire défini dans BOT.py, ou la personne qui a invité le bot ici."""
        if user_id == ECO_OWNER_ID:
            return True
        quiz_db = getattr(self.bot, "db", None)
        if quiz_db is None:
            return False
        manage = getattr(quiz_db, "can_manage_access", None)   # OWNER_ID de BOT.py ou inviteur du bot
        if manage is not None:
            return bool(manage(user_id, guild_id))
        is_admin = getattr(quiz_db, "is_guild_admin", None)
        return bool(guild_id and is_admin is not None and is_admin(guild_id, user_id))

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if not self.can_admin(interaction.user.id, interaction.guild_id):
            await interaction.response.send_message(
                "⛔ Cette commande est réservée à la personne qui a invité le bot et au propriétaire du bot.\n"
                f"Ton ID Discord : `{interaction.user.id}`", ephemeral=True)
            return False
        return True

    async def log_event(self, guild: Optional[discord.Guild], text: str):
        if guild is None:
            return
        ch_id = self.store.cfg(guild.id, "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            try:
                await ch.send(embed=discord.Embed(title="📒 Journal de l'économie", description=text, color=0x2C3E50,
                                                  timestamp=discord.utils.utcnow()))
            except discord.HTTPException:
                pass

    def adjust(self, g, uid, amount: int, where: str) -> int:
        """Ajoute (ou retire si négatif, jamais sous zéro) de l'argent ; retourne la variation appliquée."""
        s = self.store
        row = s.user(g, uid)
        if amount < 0:
            amount = -min(-amount, row[where])
        if where == "wallet":
            s.add(g, uid, wallet=amount, reason="Ajustement admin")
        else:
            s.add(g, uid, bank=amount)
        s.db.commit()
        return amount

    def purchase(self, g, u, item_id, qty):
        s = self.store
        if not s.cfg(g, "shop_open"):
            return False, "🔒 La boutique est fermée."
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
            return False, f"❌ Il te manque {self.m(g, cost - wallet)}."
        s.add(g, u, wallet=-cost, reason=f"Achat {qty}× {it['name']}")
        if it["stock"] != -1:
            s.db.execute("UPDATE items SET stock=stock-? WHERE id=?", (qty, item_id))
        s.give_item(g, u, item_id, qty)
        s.db.commit()
        return True, f"✅ Tu as acheté {qty}× **{it['name']}** pour {self.m(g, cost)}."

    # ---- gains passifs ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        g, u = message.guild.id, message.author.id
        if not self.store.cfg(g, "msg_enabled"):
            return
        now = time.time()
        if now - self._msg_cache.get((g, u), 0) < self.store.cfg(g, "msg_cd"):
            return
        self._msg_cache[(g, u)] = now
        lo = self.store.cfg(g, "msg_min")
        hi = max(lo, self.store.cfg(g, "msg_max"))
        self.store.user(g, u)
        gain, _ = self.store.boosted(g, u, random.randint(lo, hi))
        self.store.add(g, u, wallet=gain)
        self.store.db.commit()

    # ================= ADMINISTRATION =================
    @app_commands.command(name="ecoconfig", description="Panneau de configuration de l'économie et de la boutique")
    @app_commands.guild_only()
    async def ecoconfig(self, interaction: discord.Interaction):
        if not await self.require_admin(interaction):
            return
        view = MainPanel(self, interaction)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="addmoney", description="Ajouter de l'argent à un membre")
    @app_commands.describe(membre="Le membre", montant="Montant à ajouter", destination="Portefeuille (par défaut) ou banque")
    @app_commands.choices(destination=[app_commands.Choice(name="Portefeuille", value="wallet"),
                                       app_commands.Choice(name="Banque", value="bank")])
    @app_commands.guild_only()
    async def addmoney(self, interaction: discord.Interaction, membre: discord.Member,
                       montant: app_commands.Range[int, 1, 10 ** 12], destination: str = "wallet"):
        if not await self.require_admin(interaction):
            return
        if membre.bot:
            return await interaction.response.send_message("❌ Choisis un membre, pas un bot.", ephemeral=True)
        g = interaction.guild_id
        applied = self.adjust(g, membre.id, montant, destination)
        where = "portefeuille" if destination == "wallet" else "banque"
        await interaction.response.send_message(
            f"✅ {self.m(g, applied)} ajoutés au {where} de {membre.mention}.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a ajouté **{self.m(g, applied)}** au {where} de {membre.mention}.")

    @app_commands.command(name="removemoney", description="Retirer de l'argent à un membre")
    @app_commands.describe(membre="Le membre", montant="Montant à retirer", destination="Portefeuille (par défaut) ou banque")
    @app_commands.choices(destination=[app_commands.Choice(name="Portefeuille", value="wallet"),
                                       app_commands.Choice(name="Banque", value="bank")])
    @app_commands.guild_only()
    async def removemoney(self, interaction: discord.Interaction, membre: discord.Member,
                          montant: app_commands.Range[int, 1, 10 ** 12], destination: str = "wallet"):
        if not await self.require_admin(interaction):
            return
        if membre.bot:
            return await interaction.response.send_message("❌ Choisis un membre, pas un bot.", ephemeral=True)
        g = interaction.guild_id
        applied = -self.adjust(g, membre.id, -montant, destination)
        where = "portefeuille" if destination == "wallet" else "banque"
        await interaction.response.send_message(
            f"✅ {self.m(g, applied)} retirés du {where} de {membre.mention}.", ephemeral=True)
        await self.log_event(interaction.guild, f"{interaction.user.mention} a retiré **{self.m(g, applied)}** du {where} de {membre.mention}.")

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
        e.add_field(name="Portefeuille", value=self.m(g, r["wallet"]))
        e.add_field(name="Banque", value=self.m(g, r["bank"]))
        e.add_field(name="Total", value=self.m(g, total))
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
        g, u, now, cfg = interaction.guild_id, interaction.user.id, time.time(), self.store.cfg
        cd = cfg(g, "daily_cd") * 3600
        r = self.store.user(g, u)
        ready = r["last_daily"] + cd
        if ready > now:
            return await interaction.response.send_message(
                f"⏳ Ta récompense sera prête <t:{int(ready)}:R>.", ephemeral=True)
        streak = r["streak"] + 1 if r["last_daily"] and now - r["last_daily"] <= 2 * cd else 1
        base = cfg(g, "daily_base") + cfg(g, "daily_step") * min(streak - 1, 30)
        gain, bonus = self.store.boosted(g, u, base)
        interest = min(r["bank"] * cfg(g, "interest_pct") // 100, cfg(g, "interest_max"))
        self.store.add(g, u, wallet=gain, bank=interest, reason="Récompense quotidienne")
        self.store.db.execute("UPDATE users SET last_daily=?, streak=? WHERE guild_id=? AND user_id=?",
                              (now, streak, g, u))
        self.store.db.commit()
        e = discord.Embed(title="🎁 Récompense quotidienne", color=0x2ECC71,
                          description=f"Tu reçois **{self.m(g, gain)}**" + (f" (bonus +{bonus} %)" if bonus else ""))
        e.add_field(name="Série", value=f"🔥 {streak} jour(s) d'affilée")
        if interest:
            e.add_field(name="Intérêts bancaires", value=self.m(g, interest))
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="work", description="Travailler pour gagner de l'argent")
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction):
        g, u, now, cfg = interaction.guild_id, interaction.user.id, time.time(), self.store.cfg
        r = self.store.user(g, u)
        ready = r["last_work"] + cfg(g, "work_cd") * 60
        if ready > now:
            return await interaction.response.send_message(
                f"⏳ Tu es fatigué ! Retour au travail <t:{int(ready)}:R>.", ephemeral=True)
        count = r["work_count"] + 1
        old, job = job_for(r["work_count"]), job_for(count)
        base = random.randint(job[2], job[3]) * cfg(g, "work_mult") // 100
        gain, bonus = self.store.boosted(g, u, base)
        self.store.add(g, u, wallet=gain, reason=f"Travail ({job[1]})")
        self.store.db.execute("UPDATE users SET last_work=?, work_count=? WHERE guild_id=? AND user_id=?",
                              (now, count, g, u))
        self.store.db.commit()
        txt = f"{random.choice(WORK_TEXTS)} en tant que **{job[1]}** : **+{self.m(g, gain)}**"
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
        await interaction.response.send_message(f"🏦 Tu as déposé **{self.m(g, amount)}** à la banque.")

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
        await interaction.response.send_message(f"🏦 Tu as retiré **{self.m(g, amount)}** de la banque.")

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
        await interaction.response.send_message(
            f"💸 {interaction.user.mention} a envoyé **{self.m(g, montant)}** à {membre.mention}.")

    def rob_rules_embed(self, g) -> discord.Embed:
        cfg = self.store.cfg
        p = cfg(g, "rob_chance") / 100
        steal, fine = sum(ROB_STEAL) / 2, sum(ROB_FINE) / 2          # moyennes (tirage uniforme)
        ev = lambda victim, thief: p * victim * steal / 100 - (1 - p) * thief * fine / 100
        ratio = ((1 - p) * fine) / (p * steal)
        cd = cfg(g, "rob_cd")
        cd_txt = (f"{cd // 60} h" + (f" {cd % 60} min" if cd % 60 else "")) if cd >= 60 else f"{cd} min"
        e = discord.Embed(
            title="🦹 Le vol : règles et probabilités", color=0xE67E22,
            description="**But** : piquer une partie de l'argent **du portefeuille** d'un autre membre. "
                        "Plus ta cible est riche, plus le gain potentiel est gros… mais un échec te coûte cher.")
        e.add_field(name="🎲 Chances", value=f"**{p * 100:.0f} %** de réussite · **{(1 - p) * 100:.0f} %** d'échec", inline=False)
        e.add_field(name="✅ Si ça réussit",
                    value=f"Tu voles **{ROB_STEAL[0]} à {ROB_STEAL[1]} %** du portefeuille de ta cible (≈ {steal:g} % en moyenne).",
                    inline=False)
        e.add_field(name="🚓 Si tu te fais attraper",
                    value=f"Tu paies une amende de **{ROB_FINE[0]} à {ROB_FINE[1]} %** de **ton** portefeuille, "
                          f"versée à ta cible (≈ {fine:g} % en moyenne).", inline=False)
        e.add_field(name="🛡️ Se protéger",
                    value="La banque est intouchable : seul le portefeuille peut être volé. "
                          "Utilise `/deposit` pour mettre ton argent à l'abri.", inline=False)
        e.add_field(name="⏳ Conditions",
                    value=f"Un vol toutes les **{cd_txt}** · toi et ta cible devez avoir au moins "
                          f"**{self.m(g, cfg(g, 'rob_min'))}** en portefeuille.", inline=False)
        e.add_field(name="📊 Est-ce rentable ?",
                    value=(f"En moyenne, un vol ne rapporte que si ta cible a plus de **{ratio:.1f}×** ton portefeuille.\n"
                           f"• Cible {self.m(g, 2000)} / toi {self.m(g, 1000)} : **{round(ev(2000, 1000)):+d}** par essai en moyenne\n"
                           f"• Cible {self.m(g, 1000)} / toi {self.m(g, 1000)} : **{round(ev(1000, 1000)):+d}** par essai en moyenne"),
                    inline=False)
        e.set_footer(text="Tente ta chance avec /rob @membre")
        return e

    @app_commands.command(name="rob", description="Voler le portefeuille d'un membre (sans cible : règles et probabilités)")
    @app_commands.describe(membre="La cible du vol (laisse vide pour voir les règles et les probabilités)")
    @app_commands.guild_only()
    async def rob(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        g, u, now, cfg = interaction.guild_id, interaction.user.id, time.time(), self.store.cfg
        if membre is None:
            return await interaction.response.send_message(embed=self.rob_rules_embed(g), ephemeral=True)
        if not cfg(g, "rob_enabled"):
            return await interaction.response.send_message("🚫 Le vol est désactivé sur ce serveur.", ephemeral=True)
        if membre.bot or membre.id == u:
            return await interaction.response.send_message("❌ Cible invalide.", ephemeral=True)
        thief, victim = self.store.user(g, u), self.store.user(g, membre.id)
        minimum = cfg(g, "rob_min")
        ready = thief["last_rob"] + cfg(g, "rob_cd") * 60
        if ready > now:
            return await interaction.response.send_message(
                f"⏳ La police te surveille… réessaie <t:{int(ready)}:R>.", ephemeral=True)
        if victim["wallet"] < minimum:
            return await interaction.response.send_message(
                "❌ Cette personne n'a presque rien dans son portefeuille (la banque est à l'abri).", ephemeral=True)
        if thief["wallet"] < minimum:
            return await interaction.response.send_message(
                f"❌ Il te faut au moins {self.m(g, minimum)} en portefeuille pour tenter un vol.", ephemeral=True)
        self.store.db.execute("UPDATE users SET last_rob=? WHERE guild_id=? AND user_id=?", (now, g, u))
        chance = cfg(g, "rob_chance")
        if random.random() < chance / 100:
            lo = max(1, victim["wallet"] * ROB_STEAL[0] // 100)
            amount = random.randint(lo, max(lo, victim["wallet"] * ROB_STEAL[1] // 100))
            self.store.add(g, membre.id, wallet=-amount, reason=f"Volé par {u}")
            self.store.add(g, u, wallet=amount, reason=f"Vol sur {membre.id}")
            msg = f"🦹 {interaction.user.mention} a volé **{self.m(g, amount)}** à {membre.mention} !"
            color = 0xE67E22
        else:
            lo = max(1, thief["wallet"] * ROB_FINE[0] // 100)
            fine = random.randint(lo, max(lo, thief["wallet"] * ROB_FINE[1] // 100))
            self.store.add(g, u, wallet=-fine, reason=f"Amende (vol raté sur {membre.id})")
            self.store.add(g, membre.id, wallet=fine, reason=f"Indemnité (vol raté de {u})")
            msg = (f"🚓 {interaction.user.mention} s'est fait attraper ! "
                   f"Amende de **{self.m(g, fine)}** versée à {membre.mention}.")
            color = 0xE74C3C
        self.store.db.commit()
        e = discord.Embed(description=msg, color=color)
        e.set_footer(text=f"Chances de réussite : {chance} % · /rob sans cible = règles détaillées")
        await interaction.response.send_message(embed=e)

    def _bet(self, g, u, raw: str):
        """Valide une mise ; retourne (mise, message d'erreur)."""
        cfg = self.store.cfg
        if not cfg(g, "games_enabled"):
            return None, "🚫 Les jeux d'argent sont désactivés sur ce serveur."
        wallet = self.store.user(g, u)["wallet"]
        maxi = cfg(g, "max_bet")
        bet = parse_amount(raw, min(wallet, maxi))
        if not bet or bet < 1 or bet > wallet or bet > maxi:
            return None, f"❌ Mise invalide (maximum {self.m(g, maxi)})."
        return bet, None

    # ---- coinflip entre membres ----
    def open_coinflips(self, g):
        return self.store.db.execute("SELECT * FROM coinflips WHERE guild_id=? ORDER BY created", (g,)).fetchall()

    def refund_expired_coinflips(self, g: Optional[int] = None) -> int:
        """Rend leur mise aux créateurs des paris trop anciens ; retourne le nombre de paris remboursés."""
        s, now, n = self.store, time.time(), 0
        rows = (s.db.execute("SELECT * FROM coinflips").fetchall() if g is None else
                s.db.execute("SELECT * FROM coinflips WHERE guild_id=?", (g,)).fetchall())
        for r in rows:
            if now - r["created"] > s.cfg(r["guild_id"], "coinflip_ttl") * 60:
                s.user(r["guild_id"], r["creator_id"])
                s.add(r["guild_id"], r["creator_id"], wallet=r["amount"], reason="Coinflip expiré (mise rendue)")
                s.db.execute("DELETE FROM coinflips WHERE id=?", (r["id"],))
                n += 1
        if n:
            s.db.commit()
        return n

    def create_coinflip(self, g, u, side: str, raw_amount: str):
        """Crée un pari : la mise est bloquée tout de suite. Retourne (pari, None) ou (None, erreur)."""
        s, cfg = self.store, self.store.cfg
        if not cfg(g, "games_enabled"):
            return None, "🚫 Les jeux d'argent sont désactivés sur ce serveur."
        self.refund_expired_coinflips(g)
        wallet = s.user(g, u)["wallet"]
        maxi = cfg(g, "coinflip_max")
        amount = parse_amount(raw_amount, min(wallet, maxi))
        if not amount or amount < 1:
            return None, "❌ Mise invalide."
        if amount > maxi:
            return None, f"❌ La mise maximale du coinflip est de {self.m(g, maxi)}."
        if amount > wallet:
            return None, f"❌ Il te manque {self.m(g, amount - wallet)} dans ton portefeuille."
        mine = s.db.execute("SELECT COUNT(*) FROM coinflips WHERE guild_id=? AND creator_id=?", (g, u)).fetchone()[0]
        if mine >= COINFLIP_MAX_OPEN:
            return None, f"❌ Tu as déjà {COINFLIP_MAX_OPEN} paris ouverts : attends qu'ils soient relevés ou annule-en un."
        s.add(g, u, wallet=-amount, reason="Coinflip créé (mise bloquée)")
        cur = s.db.execute("INSERT INTO coinflips(guild_id,creator_id,amount,side,created) VALUES(?,?,?,?,?)",
                           (g, u, amount, side, time.time()))
        s.db.commit()
        return dict(id=cur.lastrowid, amount=amount, side=side), None

    def join_coinflip(self, g, u, cf_id: int):
        """Relève un pari : lance la pièce, le gagnant prend les deux mises. Retourne (résultat, None) ou (None, erreur)."""
        s = self.store
        if not s.cfg(g, "games_enabled"):
            return None, "🚫 Les jeux d'argent sont désactivés sur ce serveur."
        self.refund_expired_coinflips(g)
        cf = s.db.execute("SELECT * FROM coinflips WHERE id=? AND guild_id=?", (cf_id, g)).fetchone()
        if not cf:
            return None, "❌ Ce pari n'est plus disponible (déjà relevé, annulé ou expiré)."
        if cf["creator_id"] == u:
            return None, "❌ Tu ne peux pas relever ton propre pari."
        wallet = s.user(g, u)["wallet"]
        if wallet < cf["amount"]:
            return None, f"❌ Il te manque {self.m(g, cf['amount'] - wallet)} pour relever ce pari."
        s.db.execute("DELETE FROM coinflips WHERE id=?", (cf_id,))
        result = random.choice(["pile", "face"])
        creator_wins = result == cf["side"]
        winner, loser = (cf["creator_id"], u) if creator_wins else (u, cf["creator_id"])
        s.add(g, u, wallet=-cf["amount"], reason="Coinflip : mise")
        s.user(g, cf["creator_id"])
        s.add(g, winner, wallet=cf["amount"] * 2, reason="Coinflip gagné")
        s.db.commit()
        return dict(result=result, winner=winner, loser=loser, amount=cf["amount"], creator=cf["creator_id"],
                    creator_side=cf["side"], joiner=u), None

    def cancel_coinflips(self, g, u):
        s = self.store
        rows = s.db.execute("SELECT * FROM coinflips WHERE guild_id=? AND creator_id=?", (g, u)).fetchall()
        total = sum(r["amount"] for r in rows)
        if rows:
            s.user(g, u)
            s.add(g, u, wallet=total, reason="Coinflip annulé (mise rendue)")
            s.db.execute("DELETE FROM coinflips WHERE guild_id=? AND creator_id=?", (g, u))
            s.db.commit()
        return len(rows), total

    def coinflip_created_embed(self, g, user, res) -> discord.Embed:
        return discord.Embed(
            title="🪙 Nouveau coinflip", color=0xF1C40F,
            description=(f"{user.mention} met **{self.m(g, res['amount'])}** en jeu et prend **{res['side']}**.\n"
                         f"Tape `/coinflip` pour relever le pari : tu auras **{OTHER_SIDE[res['side']]}**. "
                         "Le gagnant empoche les deux mises."))

    def coinflip_result_embed(self, g, r) -> discord.Embed:
        return discord.Embed(
            title="🪙 Coinflip", color=0xF1C40F,
            description=(f"La pièce tombe sur **{r['result']}** !\n"
                         f"🏆 <@{r['winner']}> remporte **{self.m(g, r['amount'] * 2)}** "
                         f"(mise de {self.m(g, r['amount'])} chacun).\n"
                         f"💀 <@{r['loser']}> perd sa mise."))

    @app_commands.command(name="coinflip", description="Parier pile ou face contre un autre membre (le gagnant prend les deux mises)")
    @app_commands.describe(choix="Ton côté de la pièce (pour créer un pari directement)",
                           mise="Ta mise (pour créer un pari directement)")
    @app_commands.choices(choix=[app_commands.Choice(name="Pile", value="pile"),
                                 app_commands.Choice(name="Face", value="face")])
    @app_commands.guild_only()
    async def coinflip(self, interaction: discord.Interaction, choix: Optional[str] = None, mise: Optional[str] = None):
        g, u = interaction.guild_id, interaction.user.id
        if not self.store.cfg(g, "games_enabled"):
            return await interaction.response.send_message("🚫 Les jeux d'argent sont désactivés sur ce serveur.", ephemeral=True)
        if (choix is None) != (mise is None):
            return await interaction.response.send_message(
                "❌ Pour créer un pari directement, indique à la fois `choix` et `mise`. "
                "Sans option, `/coinflip` affiche les paris disponibles.", ephemeral=True)
        if choix and mise:
            res, err = self.create_coinflip(g, u, choix, mise)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            return await interaction.response.send_message(
                embed=self.coinflip_created_embed(g, interaction.user, res),
                allowed_mentions=discord.AllowedMentions.none())
        view = CoinflipView(self, interaction)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="slots", description="Machine à sous")
    @app_commands.describe(mise="Un nombre, « tout », « moitié » ou un pourcentage")
    @app_commands.guild_only()
    async def slots(self, interaction: discord.Interaction, mise: str):
        g, u = interaction.guild_id, interaction.user.id
        bet, err = self._bet(g, u, mise)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        reels = random.choices(SLOT_SYMBOLS, SLOT_WEIGHTS, k=3)
        if len(set(reels)) == 1:
            mult, label = SLOT_TRIPLE[reels[0]], "JACKPOT !"
        elif len(set(reels)) == 2:
            mult, label = 1.5, "Deux identiques !"
        else:
            mult, label = 0, "Perdu…"
        payout = int(bet * mult)
        net = payout - bet
        self.store.add(g, u, wallet=net, reason="Machine à sous")
        self.store.db.commit()
        result = (f"Gain net : **+{self.m(g, net)}**" if net > 0
                  else f"Perte : **{self.m(g, -net)}**" if net < 0 else "Mise remboursée.")
        await interaction.response.send_message(embed=discord.Embed(
            title="🎰 Machine à sous", color=0x2ECC71 if net > 0 else 0xE74C3C,
            description=f"**{'  |  '.join(reels)}**\n\n{label}\n{result}"))

    @app_commands.command(name="baltop", description="Classement des membres les plus riches")
    @app_commands.guild_only()
    async def baltop(self, interaction: discord.Interaction):
        g = interaction.guild_id
        rows = self.store.db.execute(
            "SELECT user_id, wallet+bank AS total FROM users WHERE guild_id=? ORDER BY total DESC LIMIT 10",
            (g,)).fetchall()
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{r['user_id']}> : {self.m(g, r['total'])}"
                 for i, r in enumerate(rows)]
        await interaction.response.send_message(embed=discord.Embed(
            title="🏆 Les plus riches", description="\n".join(lines) or "Personne pour l'instant.", color=0xF1C40F))

    # ================= BOUTIQUE =================
    @app_commands.command(name="shop", description="Ouvrir la boutique")
    @app_commands.describe(categorie="Filtrer sur une catégorie")
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction, categorie: Optional[str] = None):
        if not self.store.cfg(interaction.guild_id, "shop_open"):
            return await interaction.response.send_message("🔒 La boutique est fermée pour le moment.", ephemeral=True)
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
                    "❌ Je ne peux pas donner ce rôle (vérifie mes permissions et la position de mon rôle). "
                    "Objet conservé.", ephemeral=True)
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
                title=f"🎁 {it['name']} ouvert !", description=f"Tu trouves **{self.m(g, reward)}** dedans.",
                color=0xF1C40F))

    @app_commands.command(name="sell", description="Revendre un objet")
    @app_commands.guild_only()
    async def sell(self, interaction: discord.Interaction, objet: str,
                   quantite: app_commands.Range[int, 1, 100] = 1):
        g, u = interaction.guild_id, interaction.user.id
        it = self.store.find_item(g, objet)
        if not it or not self.store.take_item(g, u, it["id"], quantite):
            return await interaction.response.send_message("❌ Tu n'en as pas assez.", ephemeral=True)
        total = it["price"] * self.store.cfg(g, "sell_ratio") // 100 * quantite
        self.store.user(g, u)
        self.store.add(g, u, wallet=total, reason=f"Vente {quantite}× {it['name']}")
        self.store.db.commit()
        await interaction.response.send_message(
            f"💱 Tu as vendu {quantite}× **{it['name']}** pour **{self.m(g, total)}**.")

    # ================= AUTOCOMPLÉTION =================
    @buy.autocomplete("objet")
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

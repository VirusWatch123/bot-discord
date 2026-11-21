"""
economie.py : économie (cog discord.py 2.x + SQLite) avec panneau de configuration. La boutique est désactivée pour l'instant.

Chargement dans ton bot :  await bot.load_extension("economie")

Une seule commande dans la liste de Discord : /eco. Elle ouvre un menu (boutons et fenêtres) avec toutes les
actions : récompense quotidienne, banque, envoi d'argent, classement, coinflip, machine à sous, vol, aide.
L'administration (réglages, membres, événements, journal) est un bouton du menu, réservé à ECO_OWNER_ID,
au propriétaire défini dans BOT.py et à la personne qui a invité le bot.
"""
import logging
import os
import random
import re
import sqlite3
import time
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── Réglages fixes ─────────────────────────
DB_PATH = os.getenv("ECO_DB_PATH", "economie.db")   # fichier séparé de la base du quiz
ECO_OWNER_ID = 1572724368029646999                  # toi : accès à l'administration partout

ROB_STEAL = (10, 30)   # % du portefeuille de la victime volés en cas de réussite
ROB_FINE = (10, 25)    # % du portefeuille du voleur payés en cas d'échec
COINFLIP_MAX_OPEN = 3  # paris coinflip ouverts en même temps par membre
OTHER_SIDE = {"pile": "face", "face": "pile"}

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 12, 7, 4, 2]
SLOT_PAIR = 1.5   # multiplicateur quand exactement deux symboles sont identiques
SLOT_TRIPLE = {"🍒": 3, "🍋": 4, "🍇": 6, "🔔": 10, "⭐": 15, "💎": 30, "7️⃣": 60}

BIG = 10 ** 9
_re_thousands = re.compile(r"-?\d{1,3}([,.]\d{3})+")
_re_sep = re.compile(r"[,.]")
_re_decimal = re.compile(r"\d+[.,]\d{1,3}")
_re_int = re.compile(r"-?\d+")
# Réglages modifiables depuis le panneau : clé -> (section, libellé, type, défaut, min, max)
# (pour le type « text », min / max = longueur du texte)
SETTINGS = {
    "currency": ("general", "Nom / symbole de la monnaie", "text", "rubis", 1, 40,
                 "Ce qui s'affiche après chaque montant. Ex. « rubis » donne « 250 rubis » (un emoji marche aussi)."),
    "start_wallet": ("general", "Solde de départ des nouveaux membres", "int", 100, 0, BIG,
                     "Argent donné à un membre la toute première fois qu'il utilise l'économie."),
    "bonus_cap": ("general", "Bonus de gains maximum cumulé (%)", "int", 300, 0, 1000,
                  "Les événements de bonus s'additionnent : ce plafond limite le total. 300 = gains ×4 au maximum."),
    "daily_base": ("gains", "Récompense quotidienne de base", "int", 250, 0, BIG,
                   "Montant reçu avec la récompense quotidienne le premier jour de la série."),
    "daily_step": ("gains", "Bonus de série quotidienne (par jour d'affilée)", "int", 25, 0, BIG,
                   "Montant ajouté pour chaque jour d'affilée (30 jours max). Ex. base 250 et bonus 25 : 275 le 2e jour."),
    "daily_cd": ("gains", "Délai de la récompense quotidienne (heures)", "int", 24, 1, 720,
                 "Temps à attendre entre deux récompenses quotidiennes. Si un membre attend plus du double, sa série repart de zéro."),
    "msg_enabled": ("gains", "Gains en discutant", "bool", 1, 0, 1,
                    "Les membres gagnent un peu d'argent en écrivant des messages dans le serveur."),
    "msg_min": ("gains", "Gain minimum par message", "int", 1, 0, 1_000_000,
                "Plus petit gain possible pour un message qui rapporte (tirage au hasard entre minimum et maximum)."),
    "msg_max": ("gains", "Gain maximum par message", "int", 5, 0, 1_000_000,
                "Plus gros gain possible pour un message qui rapporte."),
    "msg_cd": ("gains", "Délai entre deux gains de message (s)", "int", 60, 5, 3600,
               "Anti-spam : un membre ne gagne qu'une fois par ce délai. 60 = 1 gain par minute au maximum."),
    "games_enabled": ("games", "Jeux d'argent (coinflip, machine à sous)", "bool", 1, 0, 1,
                      "Active ou coupe le coinflip et la machine à sous."),
    "max_bet": ("games", "Mise maximale de la machine à sous", "int", 100_000, 1, BIG,
                "Plus grosse mise acceptée à la machine à sous."),
    "coinflip_max": ("games", "Mise maximale du coinflip", "int", 5000, 1, BIG,
                     "Plus grosse mise acceptée pour un pari coinflip entre membres."),
    "coinflip_ttl": ("games", "Durée de vie d'un pari coinflip (minutes)", "int", 2880, 1, 20160,
                     "Au bout de ce temps, un pari que personne n'a relevé expire et la mise est rendue. 2880 = 2 jours."),
    "rob_enabled": ("games", "Vol entre membres", "bool", 1, 0, 1,
                    "Active ou coupe le vol (voler le portefeuille d'un autre membre). Les règles sont affichées dans le menu Voler."),
    "rob_chance": ("games", "Chances de réussite du vol (%)", "int", 40, 1, 95,
                   "Probabilité qu'un vol réussisse. 40 = 4 chances sur 10. Plus c'est bas, plus voler est risqué."),
    "rob_cd": ("games", "Délai entre deux vols (minutes)", "int", 120, 1, 1440,
               "Temps à attendre entre deux vols."),
    "rob_min": ("games", "Minimum en portefeuille pour voler / être volé", "int", 200, 1, BIG,
                "Il faut au moins ce montant dans son portefeuille pour voler, et la cible aussi pour être volée."),
    "log_channel": ("hidden", "Salon du journal", "int", 0, 0, 2 ** 63 - 1, ""),
}
SECTIONS = {
    "general": ("⚙️", "Général", "Les règles de base : nom de la monnaie, argent de départ, plafond de bonus."),
    "gains": ("💰", "Gains", "Combien les membres gagnent : récompense quotidienne et gains en discutant."),
    "games": ("🎲", "Jeux & vols", "Coinflip, machine à sous et vol : mises maximales, chances et délais."),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    guild_id INTEGER, user_id INTEGER,
    wallet INTEGER NOT NULL DEFAULT 0, bank INTEGER NOT NULL DEFAULT 0,
    last_daily REAL NOT NULL DEFAULT 0, last_work REAL NOT NULL DEFAULT 0,
    last_rob REAL NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
    work_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id));
-- Boutique désactivée pour l'instant : les tables items / inventory / purchases sont conservées telles quelles.
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
CREATE TABLE IF NOT EXISTS purchases(
    guild_id INTEGER, user_id INTEGER, item_id INTEGER, qty INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item_id));
CREATE TABLE IF NOT EXISTS coinflips(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
    amount INTEGER NOT NULL, side TEXT NOT NULL, created REAL NOT NULL,
    channel_id INTEGER NOT NULL DEFAULT 0, message_id INTEGER NOT NULL DEFAULT 0);
"""


# ───────────────────────── Utilitaires ─────────────────────────
def num(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def parse_int(text: str) -> Optional[int]:
    """Lit un nombre entier : 5000, 5 000, 5,000, 5.000, 5k, 1,5k, 2m… (None si invalide)."""
    t = text.strip().lower()
    for ch in (" ", "\u202f", "\u00a0", "_", "'", "’"):
        t = t.replace(ch, "")
    mult = 1
    if t and t[-1] in "km":
        mult = 1000 if t[-1] == "k" else 1_000_000
        t = t[:-1]
    if _re_thousands.fullmatch(t):
        t = _re_sep.sub("", t)                                   # 5,000 ou 5.000 ou 1,500,000
    elif mult > 1 and _re_decimal.fullmatch(t):
        return int(round(float(t.replace(",", ".")) * mult))      # 1,5k = 1500
    if not _re_int.fullmatch(t):
        return None
    return int(t) * mult


def parse_amount(text: str, available: int) -> Optional[int]:
    t = text.strip().lower().replace(" ", "")
    if t in ("all", "tout", "max"):
        return available
    if t in ("half", "moitie", "moitié"):
        return available // 2
    if t.endswith("%") and t[:-1].isdigit():
        return available * min(int(t[:-1]), 100) // 100
    return parse_int(text)


def dur_text(minutes: int) -> str:
    days, rest = divmod(int(minutes), 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} jour{'s' if days > 1 else ''}")
    if hours:
        parts.append(f"{hours} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


def fr(x: float, nd: int = 1) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def slots_stats():
    """Probabilités exactes de la machine à sous (3 rouleaux indépendants, tirage pondéré)."""
    total = sum(SLOT_WEIGHTS)
    p = [w / total for w in SLOT_WEIGHTS]
    triples = [(sym, pi ** 3, SLOT_TRIPLE[sym]) for sym, pi in zip(SLOT_SYMBOLS, p)]
    p_pair = sum(3 * pi ** 2 * (1 - pi) for pi in p)
    p_none = 1 - sum(t[1] for t in triples) - p_pair
    rtp = sum(pt * mult for _, pt, mult in triples) + p_pair * SLOT_PAIR
    return triples, p_pair, p_none, rtp


def slots_odds_text() -> str:
    triples, p_pair, p_none, rtp = slots_stats()
    lines = [f"{sym * 3} → **×{mult}** · 1 chance sur {num(round(1 / pt))}" for sym, pt, mult in triples]
    lines.append(f"Deux symboles identiques → **×{fr(SLOT_PAIR)}** · environ {round(p_pair * 100)} parties sur 100")
    lines.append(f"Aucun symbole identique → mise perdue · environ {round(p_none * 100)} parties sur 100")
    lines.append(f"**Retour moyen : {fr(rtp * 100)} %** : sur 1 000 misés, tu récupères en moyenne {round(rtp * 1000)}.")
    return "\n".join(lines)


# ───────────────────────── Base de données ─────────────────────────
class Store:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(coinflips)")}
        for name in ("channel_id", "message_id"):       # anciennes bases : colonnes ajoutées après coup
            if name not in cols:
                self.db.execute(f"ALTER TABLE coinflips ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
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
        logging.getLogger("economie").exception("Erreur dans une fenêtre du panneau", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)


def help_embed() -> discord.Embed:
    e = discord.Embed(title="❓ Aide : à quoi sert chaque section ?", color=0x5865F2)
    e.add_field(name="⚙️ Général", value="Les règles de base : nom de la monnaie, argent de départ, plafond de bonus.", inline=False)
    e.add_field(name="💰 Gains", value="Combien les membres gagnent : récompense quotidienne et petits gains en discutant.", inline=False)
    e.add_field(name="🎲 Jeux & vols", value="Les jeux d'argent : mises maximales, durée des paris coinflip, chances du vol. Cette section affiche aussi les gains et probabilités de la machine à sous.", inline=False)
    e.add_field(name="👥 Membres", value="Donner ou retirer de l'argent à un membre précis, ou remettre son économie à zéro.", inline=False)
    e.add_field(name="🎉 Événements", value="Lancer un bonus de gains temporaire pour tout le serveur (ex. +50 % pendant 1 heure).", inline=False)
    e.add_field(name="📒 Journal", value="Le salon où le bot note toutes les actions d'administration (qui a changé quoi).", inline=False)
    e.set_footer(text="Dans chaque section, chaque réglage est expliqué juste en dessous. Les nombres peuvent s'écrire 5000, 5,000 ou 5k.")
    return e


# ---------- Écran principal ----------
class MainPanel(AdminView):
    def embed(self):
        s, g, m = self.cog.store, self.g, self.cog.m
        members, total = s.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(wallet+bank),0) FROM users WHERE guild_id=?", (g,)).fetchone()
        log_id = s.cfg(g, "log_channel")
        events = s.events(g)
        yes = lambda k: "✅" if s.cfg(g, k) else "❌"
        e = discord.Embed(
            title="⚙️ Panneau de configuration de l'économie",
            description="Choisis une section avec les boutons ci-dessous.", color=0x5865F2)
        e.add_field(name="Monnaie", value=f"{s.cfg(g, 'currency')} · départ {m(g, s.cfg(g, 'start_wallet'))}")
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

    @discord.ui.button(label="Aide", emoji="❓", style=discord.ButtonStyle.secondary, row=2)
    async def b_help(self, interaction, button):
        await interaction.response.send_message(embed=help_embed(), ephemeral=True)

    @discord.ui.button(label="Menu /eco", emoji="🏠", style=discord.ButtonStyle.secondary, row=2)
    async def b_home(self, interaction, button):
        await self.go(interaction, EcoHub(self.cog, self.origin))

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
            label="Nouvelle valeur" if spec[2] == "text" else "Nouvelle valeur (5000, 5,000, 5k…)",
            default=str(panel.cog.store.cfg(panel.g, key)),
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
                    f"⚠️ Entre un nombre entre **{num(spec[4])}** et **{num(spec[5])}** "
                    "(tu peux écrire 5000, 5,000 ou 5k).", ephemeral=True)
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
            description=f"Actuel : {show_setting(cog.store, self.g, k)} · {SETTINGS[k][6]}"[:100])
            for k in self.keys]
        sel = discord.ui.Select(placeholder="Choisis un réglage à modifier…", options=opts, row=0)
        sel.callback = self.on_pick
        self.add_item(sel)

    def embed(self):
        icon, name, intro = SECTIONS[self.section]
        lines = [f"**{SETTINGS[k][1]}** : {show_setting(self.cog.store, self.g, k)}\n↳ *{SETTINGS[k][6]}*"
                 for k in self.keys]
        e = discord.Embed(title=f"{icon} Réglages : {name}", color=0x5865F2,
                          description=f"{intro}\n\n" + "\n\n".join(lines))
        if self.section == "games":
            chance = self.cog.store.cfg(self.g, "rob_chance")
            e.add_field(name="🎰 Machine à sous : gains et probabilités (fixes)", value=slots_odds_text(), inline=False)
            e.add_field(name="🪙 Coinflip",
                        value="Pari entre deux membres, 50 % / 50 %, sans commission : le gagnant empoche les deux mises.",
                        inline=False)
            e.add_field(name="🦹 Vol",
                        value=(f"Réussite : **{chance} %** → on vole {ROB_STEAL[0]} à {ROB_STEAL[1]} % du portefeuille de la cible. "
                               f"Échec : amende de {ROB_FINE[0]} à {ROB_FINE[1]} % du portefeuille du voleur. "
                               "Ces deux fourchettes sont fixes. Détails dans le menu Voler."), inline=False)
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
            e.add_field(name="Membre", value=f"<@{self.member_id}>")
            e.add_field(name="Portefeuille", value=self.cog.m(self.g, r["wallet"]))
            e.add_field(name="Banque", value=self.cog.m(self.g, r["bank"]))
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
            description=("Les actions d'administration (réglages, argent donné ou retiré) sont écrites dans "
                         f"{'<#' + str(log_id) + '>' if log_id else 'aucun salon pour l’instant'}."))

    @discord.ui.button(label="Désactiver le journal", emoji="🚫", style=discord.ButtonStyle.danger, row=1)
    async def off(self, interaction, button):
        self.cog.store.set_cfg(self.g, "log_channel", 0)
        await self.go(interaction, LogPanel(self.cog, self.origin))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))


# ---------- Menu /eco (pour tous les membres) ----------
class EcoView(discord.ui.View):
    """Base des écrans du menu /eco : réservé à la personne qui l'a ouvert."""

    def __init__(self, cog, origin: discord.Interaction, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.cog, self.origin = cog, origin
        self.g, self.uid = origin.guild_id, origin.user.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.origin.user.id:
            await interaction.response.send_message("Ce menu n'est pas à toi : tape `/eco` pour ouvrir le tien.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    async def go(self, interaction: discord.Interaction, view: "EcoView"):
        self.stop()  # annule le délai de cet écran pour qu'il n'écrase pas le suivant
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def home(self, interaction: discord.Interaction, notice: Optional[str] = None):
        await self.go(interaction, EcoHub(self.cog, self.origin, notice))

    def embed(self) -> discord.Embed:  # pragma: no cover
        raise NotImplementedError


class AmountModal(ErrorModal):
    """Fenêtre de saisie d'un montant (5000, 5,000, 5k, moitié, tout…)."""

    def __init__(self, title: str, placeholder: str, on_value: Callable):
        super().__init__(title=title[:45])
        self.on_value = on_value
        self.f_amount = discord.ui.TextInput(label="Montant (5000, 5k, moitié, tout…)",
                                             placeholder=placeholder[:100], max_length=20)
        self.add_item(self.f_amount)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_value(interaction, self.f_amount.value)


class EcoHub(EcoView):
    """Écran d'accueil du menu /eco : ton solde et toutes les actions."""

    def __init__(self, cog, origin, notice: Optional[str] = None):
        super().__init__(cog, origin)
        self.notice = notice
        if not cog.can_admin(self.uid, self.g):
            self.remove_item(self.b_admin)

    def embed(self):
        s, g, m = self.cog.store, self.g, self.cog.m
        r = s.user(g, self.uid)
        total = r["wallet"] + r["bank"]
        rank = s.db.execute("SELECT COUNT(*)+1 FROM users WHERE guild_id=? AND wallet+bank>?", (g, total)).fetchone()[0]
        s.db.commit()
        e = discord.Embed(title=f"💼 Économie : {self.origin.user.display_name}", color=0xF1C40F,
                          description=self.notice or "Choisis une action avec les boutons ci-dessous.")
        e.add_field(name="Portefeuille", value=m(g, r["wallet"]))
        e.add_field(name="Banque", value=m(g, r["bank"]))
        e.add_field(name="Total", value=f"{m(g, total)} · #{rank}")
        events = s.events(g)
        if events:
            e.add_field(name="🎉 Événement en cours",
                        value=f"+{events[0]['bonus']} % de gains jusqu'à <t:{int(events[0]['expires'])}:R>", inline=False)
        e.set_footer(text=f"La banque est à l'abri des vols · Série quotidienne : {r['streak']} jour(s)")
        return e

    @discord.ui.button(label="Récompense quotidienne", emoji="🎁", style=discord.ButtonStyle.success, row=0)
    async def b_daily(self, interaction, button):
        await self.home(interaction, self.cog.claim_daily(self.g, self.uid))

    @discord.ui.button(label="Classement", emoji="🏆", style=discord.ButtonStyle.secondary, row=0)
    async def b_rank(self, interaction, button):
        await self.go(interaction, TextScreen(self.cog, self.origin, self.cog.leaderboard_embed(self.g)))

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def b_refresh(self, interaction, button):
        await self.home(interaction)

    @discord.ui.button(label="Déposer", emoji="🏦", style=discord.ButtonStyle.primary, row=1)
    async def b_deposit(self, interaction, button):
        async def done(inter, raw):
            await self.home(inter, self.cog.bank_move(self.g, self.uid, raw, True))
        wallet = self.cog.store.user(self.g, self.uid)["wallet"]
        await interaction.response.send_modal(AmountModal("Déposer à la banque", f"Portefeuille : {num(wallet)}", done))

    @discord.ui.button(label="Retirer", emoji="🏧", style=discord.ButtonStyle.primary, row=1)
    async def b_withdraw(self, interaction, button):
        async def done(inter, raw):
            await self.home(inter, self.cog.bank_move(self.g, self.uid, raw, False))
        bank = self.cog.store.user(self.g, self.uid)["bank"]
        await interaction.response.send_modal(AmountModal("Retirer de la banque", f"Banque : {num(bank)}", done))

    @discord.ui.button(label="Envoyer", emoji="💸", style=discord.ButtonStyle.primary, row=1)
    async def b_pay(self, interaction, button):
        await self.go(interaction, PayView(self.cog, self.origin))

    @discord.ui.button(label="Coinflip", emoji="🪙", style=discord.ButtonStyle.success, row=2)
    async def b_coinflip(self, interaction, button):
        await self.go(interaction, CoinflipMenu(self.cog, self.origin))

    @discord.ui.button(label="Machine à sous", emoji="🎰", style=discord.ButtonStyle.success, row=2)
    async def b_slots(self, interaction, button):
        await self.go(interaction, SlotsView(self.cog, self.origin))

    @discord.ui.button(label="Voler", emoji="🦹", style=discord.ButtonStyle.danger, row=2)
    async def b_rob(self, interaction, button):
        await self.go(interaction, RobView(self.cog, self.origin))

    @discord.ui.button(label="Aide", emoji="❓", style=discord.ButtonStyle.secondary, row=3)
    async def b_help(self, interaction, button):
        await self.go(interaction, TextScreen(self.cog, self.origin, self.cog.player_help_embed(self.g)))

    @discord.ui.button(label="Administration", emoji="⚙️", style=discord.ButtonStyle.secondary, row=3)
    async def b_admin(self, interaction, button):
        await self.go(interaction, MainPanel(self.cog, self.origin))

    @discord.ui.button(label="Fermer", emoji="✖️", style=discord.ButtonStyle.danger, row=3)
    async def b_close(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Menu fermé. Tape `/eco` pour le rouvrir.", embed=None, view=None)


class TextScreen(EcoView):
    """Écran simple : un embed et un bouton Retour (classement, aide)."""

    def __init__(self, cog, origin, embed: discord.Embed):
        super().__init__(cog, origin)
        self._embed = embed

    def embed(self):
        return self._embed

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await self.home(interaction)


class PayView(EcoView):
    def __init__(self, cog, origin):
        super().__init__(cog, origin)
        self.target = None
        pick = discord.ui.UserSelect(placeholder="À qui envoyer de l'argent ?", min_values=1, max_values=1, row=0)

        async def on_pick(interaction: discord.Interaction):
            user = pick.values[0]
            if user.bot or user.id == self.uid:
                return await interaction.response.send_message(
                    "⚠️ Choisis un autre membre (pas toi, pas un bot).", ephemeral=True)
            self.target = user
            self.send_btn.disabled = False
            await interaction.response.edit_message(embed=self.embed(), view=self)
        pick.callback = on_pick
        self.add_item(pick)

    def embed(self):
        wallet = self.cog.store.user(self.g, self.uid)["wallet"]
        return discord.Embed(
            title="💸 Envoyer de l'argent", color=0x3498DB,
            description=(f"Destinataire : {self.target.mention}\nClique sur **Choisir le montant**."
                         if self.target else "Choisis un destinataire dans le menu, puis clique sur **Choisir le montant**.")
                        + f"\n\nTon portefeuille : {self.cog.m(self.g, wallet)}")

    @discord.ui.button(label="Choisir le montant", emoji="💸", style=discord.ButtonStyle.success, row=1, disabled=True)
    async def send_btn(self, interaction, button):
        target = self.target

        async def done(inter, raw):
            ok, text, public = self.cog.do_pay(self.g, self.uid, target.id, raw)
            if not ok:
                return await inter.response.send_message(text, ephemeral=True)
            await self.home(inter, text)
            try:
                await inter.channel.send(
                    content=public, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=target.id)]))
            except (discord.HTTPException, AttributeError):
                pass
        await interaction.response.send_modal(AmountModal("Envoyer de l'argent", "Ex. 500, 1,000, 1k, moitié…", done))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.home(interaction)


class CoinflipMenu(EcoView):
    def __init__(self, cog, origin, notice: Optional[str] = None):
        super().__init__(cog, origin)
        self.notice = notice

    def embed(self):
        return self.cog.coinflip_menu_embed(self.g, self.uid, self.notice)

    async def _create(self, interaction: discord.Interaction, side: str):
        cog = self.cog
        maxi = cog.store.cfg(self.g, "coinflip_max")

        async def done(inter, raw):
            res, err = cog.create_coinflip(self.g, self.uid, side, raw)
            if err:
                return await inter.response.send_message(err, ephemeral=True)
            try:
                msg = await inter.channel.send(
                    embed=cog.coinflip_open_embed(self.g, inter.user, res), view=CoinflipButtons(cog),
                    allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, AttributeError):
                cog.cancel_coinflip(self.g, self.uid, res["id"])
                return await inter.response.send_message(
                    "❌ Je n'ai pas pu poster le pari dans ce salon (il me faut la permission d'envoyer des messages "
                    "et des embeds). Ta mise t'a été rendue.", ephemeral=True)
            cog.store.db.execute("UPDATE coinflips SET channel_id=?, message_id=? WHERE id=?",
                                 (msg.channel.id, msg.id, res["id"]))
            cog.store.db.commit()
            await self.go(inter, CoinflipMenu(cog, self.origin, f"✅ Ton pari est en ligne : {msg.jump_url}"))
        await interaction.response.send_modal(
            AmountModal(f"Coinflip : tu prends {side}", f"Maximum : {num(maxi)}", done))

    @discord.ui.button(label="Créer : pile", emoji="🪙", style=discord.ButtonStyle.success, row=0)
    async def b_pile(self, interaction, button):
        await self._create(interaction, "pile")

    @discord.ui.button(label="Créer : face", emoji="🪙", style=discord.ButtonStyle.success, row=0)
    async def b_face(self, interaction, button):
        await self._create(interaction, "face")

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def b_refresh(self, interaction, button):
        await self.go(interaction, CoinflipMenu(self.cog, self.origin))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.home(interaction)


class SlotsView(EcoView):
    def __init__(self, cog, origin, last: Optional[dict] = None):
        super().__init__(cog, origin)
        self.last = last
        self.again_btn.disabled = last is None
        if last:
            self.again_btn.label = f"Rejouer {num(last['bet'])}"[:80]

    def embed(self):
        g, last = self.g, self.last
        color = 0x9B59B6 if not last else 0x2ECC71 if last["net"] > 0 else 0xE74C3C if last["net"] < 0 else 0x95A5A6
        e = discord.Embed(
            title="🎰 Machine à sous", color=color,
            description=last["text"] if last else
            "Trois symboles sont tirés au hasard. **Tu gagnes dès que deux symboles sont identiques.**")
        e.add_field(name="Gains et probabilités", value=slots_odds_text(), inline=False)
        wallet = self.cog.store.user(g, self.uid)["wallet"]
        e.set_footer(text=f"Mise max : {self.cog.m(g, self.cog.store.cfg(g, 'max_bet'))} · "
                          f"Portefeuille : {self.cog.m(g, wallet)}")
        return e

    async def _play(self, interaction: discord.Interaction, raw: str):
        res, err = self.cog.play_slots(self.g, self.uid, raw)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await self.go(interaction, SlotsView(self.cog, self.origin, res))

    @discord.ui.button(label="Miser…", emoji="🎰", style=discord.ButtonStyle.success, row=0)
    async def bet_btn(self, interaction, button):
        maxi = self.cog.store.cfg(self.g, "max_bet")

        async def done(inter, raw):
            await self._play(inter, raw)
        await interaction.response.send_modal(AmountModal("Machine à sous", f"Maximum : {num(maxi)}", done))

    @discord.ui.button(label="Rejouer", emoji="🔁", style=discord.ButtonStyle.primary, row=0, disabled=True)
    async def again_btn(self, interaction, button):
        await self._play(interaction, str(self.last["bet"]))

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.home(interaction)


class RobView(EcoView):
    def __init__(self, cog, origin):
        super().__init__(cog, origin)
        self.target = None
        pick = discord.ui.UserSelect(placeholder="Qui veux-tu voler ?", min_values=1, max_values=1, row=0)

        async def on_pick(interaction: discord.Interaction):
            user = pick.values[0]
            if user.bot or user.id == self.uid:
                return await interaction.response.send_message(
                    "⚠️ Choisis un autre membre (pas toi, pas un bot).", ephemeral=True)
            self.target = user
            self.rob_btn.disabled = False
            await interaction.response.edit_message(embed=self.embed(), view=self)
        pick.callback = on_pick
        self.add_item(pick)

    def embed(self):
        e = self.cog.rob_rules_embed(self.g)
        if self.target:
            e.add_field(name="🎯 Cible", value=f"{self.target.mention} : clique sur **Tenter le vol**.", inline=False)
        return e

    @discord.ui.button(label="Tenter le vol", emoji="🦹", style=discord.ButtonStyle.danger, row=1, disabled=True)
    async def rob_btn(self, interaction, button):
        target = self.target
        ok, text, public = self.cog.do_rob(self.g, self.uid, target.id)
        if not ok:
            return await interaction.response.send_message(text, ephemeral=True)
        await self.home(interaction, text)
        try:
            await interaction.channel.send(embed=public, allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=target.id)]))
        except (discord.HTTPException, AttributeError):
            pass

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await self.home(interaction)


# ---------- Coinflip entre membres ----------
class CoinflipButtons(discord.ui.View):
    """Boutons sous chaque pari coinflip. Permanents : ils marchent encore après un redémarrage du bot."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Participer", emoji="🪙", style=discord.ButtonStyle.success, custom_id="eco:cf:join")
    async def join(self, interaction, button):
        await self.cog.coinflip_click(interaction, "join")

    @discord.ui.button(label="Annuler (créateur)", emoji="↩️", style=discord.ButtonStyle.secondary,
                       custom_id="eco:cf:cancel")
    async def cancel(self, interaction, button):
        await self.cog.coinflip_click(interaction, "cancel")


# ───────────────────────── Cog ─────────────────────────
class Economie(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(DB_PATH)
        self._msg_cache = {}
        self._expired = []   # paris expirés dont le message reste à mettre à jour

    async def cog_load(self):
        self.store.db.execute("DELETE FROM boosts WHERE expires<=?", (time.time(),))
        self.store.db.commit()
        self.refund_expired_coinflips()
        self.bot.add_view(CoinflipButtons(self))   # boutons des paris : actifs même après un redémarrage
        self.coinflip_cleanup.start()

    async def cog_unload(self):
        self.coinflip_cleanup.cancel()

    @tasks.loop(seconds=60)
    async def coinflip_cleanup(self):
        try:
            self.refund_expired_coinflips()
            await self.update_closed_coinflip_messages()
        except Exception:
            logging.getLogger("economie").exception("Erreur dans le nettoyage des coinflips")

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

    # ---- la commande /eco : ouvre le menu ----
    @app_commands.command(name="eco", description="Ouvre le menu de l'économie : argent, jeux, classement…")
    @app_commands.guild_only()
    async def eco(self, interaction: discord.Interaction):
        view = EcoHub(self, interaction)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    # ---- actions de l'économie (appelées par les boutons du menu) ----
    def claim_daily(self, g, u) -> str:
        now, cfg = time.time(), self.store.cfg
        cd = cfg(g, "daily_cd") * 3600
        r = self.store.user(g, u)
        ready = r["last_daily"] + cd
        if ready > now:
            return f"⏳ Ta récompense quotidienne sera prête <t:{int(ready)}:R>."
        streak = r["streak"] + 1 if r["last_daily"] and now - r["last_daily"] <= 2 * cd else 1
        base = cfg(g, "daily_base") + cfg(g, "daily_step") * min(streak - 1, 30)
        gain, bonus = self.store.boosted(g, u, base)
        self.store.add(g, u, wallet=gain, reason="Récompense quotidienne")
        self.store.db.execute("UPDATE users SET last_daily=?, streak=? WHERE guild_id=? AND user_id=?",
                              (now, streak, g, u))
        self.store.db.commit()
        return (f"🎁 Récompense quotidienne : **+{self.m(g, gain)}**" + (f" (bonus +{bonus} %)" if bonus else "")
                + f" · 🔥 série de {streak} jour(s) d'affilée")

    def bank_move(self, g, u, raw: str, deposit: bool) -> str:
        row = self.store.user(g, u)
        available = row["wallet"] if deposit else row["bank"]
        amount = parse_amount(raw, available)
        if not amount or amount < 1 or amount > available:
            return f"❌ Montant invalide : tu as {self.m(g, available)} {'dans ton portefeuille' if deposit else 'à la banque'}."
        if deposit:
            self.store.add(g, u, wallet=-amount, bank=amount)
        else:
            self.store.add(g, u, wallet=amount, bank=-amount)
        self.store.db.commit()
        return (f"🏦 Tu as déposé **{self.m(g, amount)}** à la banque." if deposit
                else f"🏧 Tu as retiré **{self.m(g, amount)}** de la banque.")

    def do_pay(self, g, u, target_id: int, raw: str):
        """Envoie de l'argent. Retourne (ok, message pour l'expéditeur, message public ou None)."""
        wallet = self.store.user(g, u)["wallet"]
        self.store.user(g, target_id)
        amount = parse_amount(raw, wallet)
        if not amount or amount < 1:
            return False, "❌ Montant invalide.", None
        if wallet < amount:
            return False, f"❌ Fonds insuffisants : tu as {self.m(g, wallet)} dans ton portefeuille.", None
        self.store.add(g, u, wallet=-amount, reason=f"Envoi à {target_id}")
        self.store.add(g, target_id, wallet=amount, reason=f"Reçu de {u}")
        self.store.db.commit()
        return (True, f"💸 Tu as envoyé **{self.m(g, amount)}** à <@{target_id}>.",
                f"💸 <@{u}> a envoyé **{self.m(g, amount)}** à <@{target_id}>.")

    def leaderboard_embed(self, g) -> discord.Embed:
        rows = self.store.db.execute(
            "SELECT user_id, wallet+bank AS total FROM users WHERE guild_id=? ORDER BY total DESC LIMIT 10",
            (g,)).fetchall()
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{r['user_id']}> : {self.m(g, r['total'])}"
                 for i, r in enumerate(rows)]
        return discord.Embed(title="🏆 Les plus riches", description="\n".join(lines) or "Personne pour l'instant.",
                             color=0xF1C40F)

    def player_help_embed(self, g) -> discord.Embed:
        c = self.store.cfg
        gains = (f"🎁 **Récompense quotidienne** : une fois toutes les {c(g, 'daily_cd')} h. "
                 "Plus tu l'enchaînes, plus elle grossit.")
        if c(g, "msg_enabled"):
            gains += f"\n💬 Écrire dans le serveur rapporte un petit gain (au plus une fois toutes les {c(g, 'msg_cd')} s)."
        e = discord.Embed(
            title="📖 Aide de l'économie", color=0x5865F2,
            description=f"La monnaie du serveur : **{c(g, 'currency')}**. Tout se fait avec les boutons du menu `/eco`.")
        e.add_field(name="💰 Gagner de l'argent", value=gains, inline=False)
        e.add_field(name="🏦 Ton argent", value=(
            "• **Déposer** / **Retirer** : mettre de l'argent à l'abri à la banque (elle ne peut pas être volée) ou le reprendre\n"
            "• **Envoyer** : donner de l'argent à un membre\n"
            "• **Classement** : les 10 plus riches"), inline=False)
        e.add_field(name="🎲 Jeux", value=(
            f"• **Coinflip** : pari pile ou face contre un membre (mise max {self.m(g, c(g, 'coinflip_max'))})\n"
            "• **Machine à sous** : gains et probabilités affichés dans l'écran\n"
            "• **Voler** : piquer le portefeuille d'un membre (règles et probabilités affichées dans l'écran)"), inline=False)
        e.set_footer(text="Les montants acceptent 5000, 5,000, 5k, « moitié » ou « tout ».")
        return e

    def rob_rules_embed(self, g) -> discord.Embed:
        cfg = self.store.cfg
        p = cfg(g, "rob_chance") / 100
        steal, fine = sum(ROB_STEAL) / 2, sum(ROB_FINE) / 2          # moyennes (tirage uniforme)
        ev = lambda victim, thief: p * victim * steal / 100 - (1 - p) * thief * fine / 100
        ratio = ((1 - p) * fine) / (p * steal)
        cd = cfg(g, "rob_cd")
        cd_txt = dur_text(cd)
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
                          "Utilise le bouton **Déposer** du menu pour mettre ton argent à l'abri.", inline=False)
        e.add_field(name="⏳ Conditions",
                    value=f"Un vol toutes les **{cd_txt}** · toi et ta cible devez avoir au moins "
                          f"**{self.m(g, cfg(g, 'rob_min'))}** en portefeuille.", inline=False)
        e.add_field(name="📊 Est-ce rentable ?",
                    value=(f"En moyenne, un vol ne rapporte que si ta cible a plus de **{ratio:.1f}×** ton portefeuille.\n"
                           f"• Cible {self.m(g, 2000)} / toi {self.m(g, 1000)} : **{round(ev(2000, 1000)):+d}** par essai en moyenne\n"
                           f"• Cible {self.m(g, 1000)} / toi {self.m(g, 1000)} : **{round(ev(1000, 1000)):+d}** par essai en moyenne"),
                    inline=False)
        e.set_footer(text="Choisis une cible, puis clique sur « Tenter le vol ».")
        return e

    def do_rob(self, g, u, target_id: int):
        """Tente un vol. Retourne (ok, texte pour le voleur, embed public ou None)."""
        cfg, now = self.store.cfg, time.time()
        if not cfg(g, "rob_enabled"):
            return False, "🚫 Le vol est désactivé sur ce serveur.", None
        if target_id == u:
            return False, "❌ Cible invalide.", None
        thief, victim = self.store.user(g, u), self.store.user(g, target_id)
        minimum = cfg(g, "rob_min")
        ready = thief["last_rob"] + cfg(g, "rob_cd") * 60
        if ready > now:
            return False, f"⏳ La police te surveille… réessaie <t:{int(ready)}:R>.", None
        if victim["wallet"] < minimum:
            return False, "❌ Cette personne n'a presque rien dans son portefeuille (la banque est à l'abri).", None
        if thief["wallet"] < minimum:
            return False, f"❌ Il te faut au moins {self.m(g, minimum)} en portefeuille pour tenter un vol.", None
        self.store.db.execute("UPDATE users SET last_rob=? WHERE guild_id=? AND user_id=?", (now, g, u))
        chance = cfg(g, "rob_chance")
        if random.random() < chance / 100:
            lo = max(1, victim["wallet"] * ROB_STEAL[0] // 100)
            amount = random.randint(lo, max(lo, victim["wallet"] * ROB_STEAL[1] // 100))
            self.store.add(g, target_id, wallet=-amount, reason=f"Volé par {u}")
            self.store.add(g, u, wallet=amount, reason=f"Vol sur {target_id}")
            public = f"🦹 <@{u}> a volé **{self.m(g, amount)}** à <@{target_id}> !"
            text, color = f"🦹 Vol réussi : tu as volé **{self.m(g, amount)}** !", 0xE67E22
        else:
            lo = max(1, thief["wallet"] * ROB_FINE[0] // 100)
            fine = random.randint(lo, max(lo, thief["wallet"] * ROB_FINE[1] // 100))
            self.store.add(g, u, wallet=-fine, reason=f"Amende (vol raté sur {target_id})")
            self.store.add(g, target_id, wallet=fine, reason=f"Indemnité (vol raté de {u})")
            public = f"🚓 <@{u}> s'est fait attraper ! Amende de **{self.m(g, fine)}** versée à <@{target_id}>."
            text, color = f"🚓 Vol raté : tu paies une amende de **{self.m(g, fine)}**.", 0xE74C3C
        self.store.db.commit()
        e = discord.Embed(description=public, color=color)
        e.set_footer(text=f"Chances de réussite : {chance} %")
        return True, text, e

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
        """Rend leur mise aux créateurs des paris expirés (ou jamais publiés). Les messages sont mis à jour par la boucle."""
        s, now, n = self.store, time.time(), 0
        rows = (s.db.execute("SELECT * FROM coinflips").fetchall() if g is None else
                s.db.execute("SELECT * FROM coinflips WHERE guild_id=?", (g,)).fetchall())
        for r in rows:
            expired = now - r["created"] > s.cfg(r["guild_id"], "coinflip_ttl") * 60
            never_posted = r["message_id"] == 0 and now - r["created"] > 300
            if expired or never_posted:
                s.user(r["guild_id"], r["creator_id"])
                s.add(r["guild_id"], r["creator_id"], wallet=r["amount"], reason="Coinflip expiré (mise rendue)")
                s.db.execute("DELETE FROM coinflips WHERE id=?", (r["id"],))
                self._expired.append(dict(r))
                n += 1
        if n:
            s.db.commit()
        return n

    async def update_closed_coinflip_messages(self):
        rows, self._expired = self._expired, []
        for r in rows:
            if not r["message_id"]:
                continue
            channel = self.bot.get_channel(r["channel_id"])
            partial = getattr(channel, "get_partial_message", None)
            if partial is None:
                continue
            embed = self.coinflip_closed_embed(
                r["id"], "⌛ Pari expiré",
                f"Personne n'a relevé ce pari : la mise de {self.m(r['guild_id'], r['amount'])} a été rendue à <@{r['creator_id']}>.")
            try:
                await partial(r["message_id"]).edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

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
            return None, "❌ Mise invalide (ex. `500`, `1,000`, `1k` ou `tout`)."
        if amount > maxi:
            return None, f"❌ La mise maximale du coinflip est de {self.m(g, maxi)}."
        if amount > wallet:
            return None, f"❌ Il te manque {self.m(g, amount - wallet)} dans ton portefeuille."
        mine = s.db.execute("SELECT COUNT(*) FROM coinflips WHERE guild_id=? AND creator_id=?", (g, u)).fetchone()[0]
        if mine >= COINFLIP_MAX_OPEN:
            return None, f"❌ Tu as déjà {COINFLIP_MAX_OPEN} paris ouverts : attends qu'ils soient relevés ou annules-en un."
        created = time.time()
        s.add(g, u, wallet=-amount, reason="Coinflip créé (mise bloquée)")
        cur = s.db.execute("INSERT INTO coinflips(guild_id,creator_id,amount,side,created) VALUES(?,?,?,?,?)",
                           (g, u, amount, side, created))
        s.db.commit()
        return dict(id=cur.lastrowid, amount=amount, side=side, expires=created + cfg(g, "coinflip_ttl") * 60), None

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
            return None, f"❌ Tu n'as pas assez : il te manque {self.m(g, cf['amount'] - wallet)} pour relever ce pari."
        s.db.execute("DELETE FROM coinflips WHERE id=?", (cf_id,))
        result = random.choice(["pile", "face"])
        creator_wins = result == cf["side"]
        winner, loser = (cf["creator_id"], u) if creator_wins else (u, cf["creator_id"])
        s.add(g, u, wallet=-cf["amount"], reason="Coinflip : mise")
        s.user(g, cf["creator_id"])
        s.add(g, winner, wallet=cf["amount"] * 2, reason="Coinflip gagné")
        s.db.commit()
        return dict(id=cf_id, result=result, winner=winner, loser=loser, amount=cf["amount"],
                    creator=cf["creator_id"], creator_side=cf["side"], joiner=u), None

    def cancel_coinflip(self, g, u, cf_id: int):
        """Annule un pari (créateur uniquement) et rend la mise. Retourne (pari, None) ou (None, erreur)."""
        s = self.store
        cf = s.db.execute("SELECT * FROM coinflips WHERE id=? AND guild_id=?", (cf_id, g)).fetchone()
        if not cf:
            return None, "❌ Ce pari n'est plus disponible (déjà relevé, annulé ou expiré)."
        if cf["creator_id"] != u:
            return None, "⛔ Seul le créateur du pari peut l'annuler."
        s.user(g, u)
        s.add(g, u, wallet=cf["amount"], reason="Coinflip annulé (mise rendue)")
        s.db.execute("DELETE FROM coinflips WHERE id=?", (cf_id,))
        s.db.commit()
        return dict(cf), None

    def coinflip_open_embed(self, g, user, res) -> discord.Embed:
        m = self.m
        e = discord.Embed(
            title="🪙 Pari coinflip", color=0xF1C40F,
            description=(f"{user.mention} met **{m(g, res['amount'])}** en jeu et prend **{res['side']}**.\n"
                         f"Clique sur **Participer** pour relever le pari : tu auras **{OTHER_SIDE[res['side']]}**.\n"
                         "Le gagnant empoche les deux mises, le perdant perd la sienne."))
        e.add_field(name="Cagnotte", value=m(g, res["amount"] * 2))
        e.add_field(name="Expire", value=f"<t:{int(res['expires'])}:R>")
        e.set_footer(text=f"Pari #{res['id']}")
        return e

    def coinflip_result_embed(self, g, r) -> discord.Embed:
        e = discord.Embed(
            title="🪙 Coinflip terminé", color=0x2ECC71,
            description=(f"La pièce tombe sur **{r['result']}** !\n"
                         f"🏆 <@{r['winner']}> remporte **{self.m(g, r['amount'] * 2)}** "
                         f"(mise de {self.m(g, r['amount'])} chacun).\n"
                         f"💀 <@{r['loser']}> perd sa mise."))
        e.set_footer(text=f"Pari #{r['id']}")
        return e

    def coinflip_closed_embed(self, cf_id, title, text) -> discord.Embed:
        e = discord.Embed(title=title, description=text, color=0x95A5A6)
        e.set_footer(text=f"Pari #{cf_id}")
        return e

    async def _drop_buttons_if_gone(self, interaction: discord.Interaction, cf_id: int):
        """Si le pari n'existe plus, on retire les boutons du message pour ne plus tromper personne."""
        gone = self.store.db.execute("SELECT 1 FROM coinflips WHERE id=?", (cf_id,)).fetchone() is None
        if gone and interaction.message is not None:
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def coinflip_click(self, interaction: discord.Interaction, action: str):
        g, u = interaction.guild_id, interaction.user.id
        embeds = interaction.message.embeds if interaction.message else []
        footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
        found = re.search(r"#(\d+)", footer or "")
        if not found:
            return await interaction.response.send_message("❌ Pari introuvable.", ephemeral=True)
        cf_id = int(found.group(1))
        if action == "cancel":
            cf, err = self.cancel_coinflip(g, u, cf_id)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return await self._drop_buttons_if_gone(interaction, cf_id)
            return await interaction.response.edit_message(
                embed=self.coinflip_closed_embed(
                    cf_id, "↩️ Pari annulé",
                    f"<@{u}> a annulé son pari : la mise de {self.m(g, cf['amount'])} lui a été rendue."), view=None)
        res, err = self.join_coinflip(g, u, cf_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return await self._drop_buttons_if_gone(interaction, cf_id)
        await interaction.response.edit_message(embed=self.coinflip_result_embed(g, res), view=None)
        await interaction.followup.send(
            content=(f"<@{res['creator']}> <@{res['joiner']}> : 🏆 <@{res['winner']}> remporte "
                     f"**{self.m(g, res['amount'] * 2)}** !"),
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=res["creator"]), discord.Object(id=res["joiner"])]))

    def coinflip_menu_embed(self, g, uid, notice: Optional[str] = None) -> discord.Embed:
        self.refund_expired_coinflips(g)
        rows = self.open_coinflips(g)
        ttl = self.store.cfg(g, "coinflip_ttl")
        lines = []
        for r in rows[:15]:
            link = (f" · [Aller au pari](https://discord.com/channels/{g}/{r['channel_id']}/{r['message_id']})"
                    if r["message_id"] else "")
            lines.append(f"**#{r['id']}** · <@{r['creator_id']}> · **{self.m(g, r['amount'])}** sur **{r['side']}** · "
                         f"expire <t:{int(r['created'] + ttl * 60)}:R>{link}"
                         + (" · *(le tien)*" if r["creator_id"] == uid else ""))
        e = discord.Embed(title="🪙 Coinflip : paris entre membres", color=0xF1C40F,
                          description=(f"{notice}\n\n" if notice else "") + ("\n".join(lines) or "Aucun pari disponible pour le moment."))
        e.add_field(name="Comment ça marche ?", value=(
            "• **Créer un pari** : clique sur « Créer : pile » ou « Créer : face », puis indique ta mise. "
            "Le pari est posté dans le salon avec un bouton **Participer**.\n"
            "• **Relever un pari** : clique sur **Participer** sous son message (il faut avoir la mise dans ton "
            "portefeuille). Tu prends l'autre côté de la pièce.\n"
            f"• Le gagnant empoche les deux mises, le perdant perd la sienne. Un pari que personne ne relève expire "
            f"après {dur_text(ttl)} (mise rendue)."), inline=False)
        e.set_footer(text=f"Mise max : {self.m(g, self.store.cfg(g, 'coinflip_max'))}")
        return e

    def play_slots(self, g, u, raw: str):
        """Une partie de machine à sous. Retourne (résultat, None) ou (None, erreur)."""
        bet, err = self._bet(g, u, raw)
        if err:
            return None, err
        reels = random.choices(SLOT_SYMBOLS, SLOT_WEIGHTS, k=3)
        if len(set(reels)) == 1:
            mult, label = SLOT_TRIPLE[reels[0]], "🎉 JACKPOT ! Trois symboles identiques"
        elif len(set(reels)) == 2:
            mult, label = SLOT_PAIR, "Deux symboles identiques"
        else:
            mult, label = 0, "Aucun symbole identique : perdu"
        net = int(bet * mult) - bet
        self.store.add(g, u, wallet=net, reason="Machine à sous")
        self.store.db.commit()
        outcome = (f"**+{self.m(g, net)}**" if net > 0 else f"**-{self.m(g, -net)}**" if net < 0
                   else "mise remboursée")
        mult_txt = f"×{fr(mult)}" if mult % 1 else f"×{int(mult)}"
        text = f"**{'  |  '.join(reels)}**\n{label}\nMise **{self.m(g, bet)}** · {mult_txt} · {outcome}"
        return dict(text=text, bet=bet, net=net), None


async def setup(bot: commands.Bot):
    await bot.add_cog(Economie(bot))

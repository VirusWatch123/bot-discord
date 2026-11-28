"""
giveaway.py — Système de giveaway ultra complet, 100 % indépendant.

DEUX FAÇONS DE L'UTILISER
─────────────────────────
1) Comme extension de ton bot existant (recommandé) :
       await bot.load_extension("giveaway")      # dans setup_hook, AVANT bot.tree.sync()
   → ajoute UNE seule commande : /giveaway (qui ouvre un panneau à boutons).

2) Tout seul (bot dédié) :
       python giveaway.py
   → démarre son propre bot + keep alive (serveur web sur $PORT, utile pour Render).

VARIABLES D'ENVIRONNEMENT (le token n'est jamais écrit dans le code)
────────────────────────────────────────────────────────────────────
DISCORD_TOKEN       token du bot (mode autonome uniquement ; "TOKEN" marche aussi)
GIVEAWAY_DB         chemin de la base SQLite           (défaut : giveaway.db)
GIVEAWAY_ADMIN_IDS  IDs Discord autorisés partout, séparés par des virgules
GIVEAWAY_STRICT     "1" → seuls le propriétaire du bot, GIVEAWAY_ADMIN_IDS et le
                    propriétaire du serveur peuvent utiliser /giveaway
PORT                port du keep alive (défaut : 8080)

Aucun intent privilégié n'est nécessaire (ni « members », ni « message content »).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("giveaway")

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Paris")
except Exception:  # tzdata absent
    TZ = timezone.utc

# ═══════════════════════════ CONSTANTES ═══════════════════════════
DB_PATH = os.getenv("GIVEAWAY_DB", "giveaway.db")
EXTRA_ADMINS = {int(x) for x in re.findall(r"\d+", os.getenv("GIVEAWAY_ADMIN_IDS", ""))}
STRICT = os.getenv("GIVEAWAY_STRICT", "0") == "1"

MAX_WINNERS = 50
MAX_RUNNING = 25  # = limite d'un menu déroulant Discord
MIN_DURATION = 10
MAX_DURATION = 365 * 24 * 3600
DEFAULT_DURATION = 24 * 3600
PAGE_SIZE = 15
VIEW_TIMEOUT = 840  # 14 min (< 15 min de validité d'une interaction)

STATUS_LABEL = {"running": "🟢 En cours", "ended": "🔴 Terminé", "cancelled": "⚫ Annulé"}

DEFAULT_CFG: dict[str, Any] = {
    "color": 0xF1C40F,
    "emoji": "🎉",
    "label": "Participer",
    "dm": True,
    "channel": None,
    "ping": None,
    "managers": [],
    "blacklist": [],
}


def new_cond() -> dict[str, Any]:
    return {
        "req_roles": [],
        "req_all": False,
        "forb_roles": [],
        "min_account_days": 0,
        "min_server_days": 0,
    }


# ═══════════════════════════ OUTILS ═══════════════════════════
UNITS = {
    "s": 1, "sec": 1, "seconde": 1, "secondes": 1,
    "m": 60, "mn": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "heure": 3600, "heures": 3600,
    "d": 86400, "j": 86400, "jour": 86400, "jours": 86400,
    "w": 604800, "sem": 604800, "semaine": 604800, "semaines": 604800,
}
_HOURS = {"h", "heure", "heures"}
_MINUTES = {"m", "mn", "min", "minute", "minutes"}


def parse_duration(text: str) -> Optional[int]:
    """'1h30', '2j', '1d 12h', '90 minutes'… → secondes (None si invalide)."""
    text = text.lower().strip().replace(",", " ")
    if not re.fullmatch(r"(?:\s*\d+\s*[a-zéèû]*)+", text):
        return None
    total, prev = 0, None
    for num, unit in re.findall(r"(\d+)\s*([a-zéèû]*)", text):
        n = int(num)
        if not unit:  # « 1h30 » → 30 minutes ; « 2m30 » → 30 secondes
            if prev in _HOURS:
                mult = 60
            elif prev in _MINUTES:
                mult = 1
            else:
                return None
        else:
            if unit not in UNITS:
                return None
            mult, prev = UNITS[unit], unit
        total += n * mult
    return total or None


def parse_date(text: str) -> Optional[float]:
    """'28/12/2026 20:00' ou '28/12/2026 20h30' (heure de Paris) → timestamp."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})(?:\s+(\d{1,2})\s*[:hH]\s*(\d{2})?)?\s*", text)
    if not m:
        return None
    d, mo, y = int(m[1]), int(m[2]), int(m[3])
    if y < 100:
        y += 2000
    h, mi = int(m[4] or 0), int(m[5] or 0)
    try:
        return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()
    except ValueError:
        return None


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    parts = []
    for name, size in (("j", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        q, s = divmod(s, size)
        if q:
            parts.append(f"{q} {name}")
    return " ".join(parts[:3]) or "0 s"


def ts(t: float, style: str = "R") -> str:
    return f"<t:{int(t)}:{style}>"


def parse_color(text: str) -> Optional[int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", text.strip())
    return int(m[1], 16) if m else None


def parse_emoji(text: str) -> discord.PartialEmoji:
    text = (text or "").strip()
    try:
        e = discord.PartialEmoji.from_str(text)
        if e.id or (e.name and not e.name.isascii()):
            return e
    except Exception:
        pass
    return discord.PartialEmoji(name="🎉")


def weighted_draw(pool: list[tuple[int, int]]):
    """Générateur : tire sans remise, chaque tirage pondéré (cryptographiquement sûr)."""
    rng = random.SystemRandom()
    pool = list(pool)
    while pool:
        total = sum(w for _, w in pool)
        r = rng.random() * total
        acc = 0.0
        for i, (uid, w) in enumerate(pool):
            acc += w
            if r < acc:
                break
        else:
            i = len(pool) - 1
        yield pool.pop(i)[0]


def check_conditions(member: discord.Member, cond: dict) -> list[str]:
    """Retourne la liste des raisons pour lesquelles le membre est refusé."""
    reasons: list[str] = []
    role_ids = {r.id for r in member.roles}

    req = cond.get("req_roles") or []
    if req:
        if cond.get("req_all"):
            missing = [r for r in req if r not in role_ids]
            if missing:
                reasons.append("Il te manque le(s) rôle(s) : " + ", ".join(f"<@&{r}>" for r in missing))
        elif not role_ids.intersection(req):
            reasons.append("Il te faut l'un de ces rôles : " + ", ".join(f"<@&{r}>" for r in req))

    if set(cond.get("forb_roles") or []) & role_ids:
        reasons.append("Tu possèdes un rôle qui t'exclut de ce giveaway.")

    now = datetime.now(timezone.utc)
    mad = cond.get("min_account_days") or 0
    if mad and (now - member.created_at).days < mad:
        reasons.append(f"Ton compte Discord doit avoir au moins **{mad} jour(s)**.")
    msd = cond.get("min_server_days") or 0
    if msd and (member.joined_at is None or (now - member.joined_at).days < msd):
        reasons.append(f"Tu dois être sur le serveur depuis au moins **{msd} jour(s)**.")
    return reasons


def compute_weight(member: discord.Member, bonus: dict) -> int:
    extra = sum(int(bonus.get(str(r.id), 0)) for r in member.roles)
    return max(1, min(1 + extra, 1000))


def describe_conditions(cond: dict) -> list[str]:
    out = []
    req = cond.get("req_roles") or []
    if req:
        pre = "Avoir **tous** ces rôles : " if cond.get("req_all") else "Avoir **l'un** de ces rôles : "
        out.append(pre + ", ".join(f"<@&{r}>" for r in req))
    forb = cond.get("forb_roles") or []
    if forb:
        out.append("Ne pas avoir : " + ", ".join(f"<@&{r}>" for r in forb))
    if cond.get("min_account_days"):
        out.append(f"Compte Discord de **{cond['min_account_days']}+ jours**")
    if cond.get("min_server_days"):
        out.append(f"**{cond['min_server_days']}+ jours** sur le serveur")
    return out


# ═══════════════════════════ BASE DE DONNÉES ═══════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS gw_config (
    guild_id INTEGER PRIMARY KEY,
    data     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaways (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL DEFAULT 0,
    host_id     INTEGER NOT NULL,
    prize       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    image       TEXT    NOT NULL DEFAULT '',
    color       INTEGER,
    winners     INTEGER NOT NULL DEFAULT 1,
    ends_at     REAL    NOT NULL,
    created_at  REAL    NOT NULL,
    ended_at    REAL,
    status      TEXT    NOT NULL DEFAULT 'running',
    conditions  TEXT    NOT NULL DEFAULT '{}',
    bonus       TEXT    NOT NULL DEFAULT '{}',
    winner_ids  TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_gw_status ON giveaways(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_gw_msg    ON giveaways(message_id);
CREATE INDEX IF NOT EXISTS idx_gw_guild  ON giveaways(guild_id, status);
CREATE TABLE IF NOT EXISTS gw_entries (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    weight      INTEGER NOT NULL DEFAULT 1,
    joined_at   REAL    NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
"""

GW_SELECT = (
    "SELECT g.*, (SELECT COUNT(*) FROM gw_entries e WHERE e.giveaway_id = g.id) AS n FROM giveaways g "
)


class DB:
    """SQLite léger, appelé hors de la boucle d'événements (to_thread)."""

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _run(self, sql: str, params: tuple, mode: str):
        with self._lock:
            cur = self._conn.execute(sql, params)
            if mode == "all":
                out: Any = [dict(r) for r in cur.fetchall()]
            elif mode == "one":
                r = cur.fetchone()
                out = dict(r) if r else None
            elif mode == "insert":
                out = cur.lastrowid
            else:
                out = cur.rowcount
            self._conn.commit()
            return out

    async def all(self, sql: str, params: tuple = ()) -> list[dict]:
        return await asyncio.to_thread(self._run, sql, params, "all")

    async def one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        return await asyncio.to_thread(self._run, sql, params, "one")

    async def insert(self, sql: str, params: tuple = ()) -> int:
        return await asyncio.to_thread(self._run, sql, params, "insert")

    async def execute(self, sql: str, params: tuple = ()) -> int:
        return await asyncio.to_thread(self._run, sql, params, "exec")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def decode_gw(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    gw = dict(row)
    gw["conditions"] = {**new_cond(), **json.loads(gw.get("conditions") or "{}")}
    gw["bonus"] = json.loads(gw.get("bonus") or "{}")
    gw["winner_ids"] = json.loads(gw.get("winner_ids") or "[]")
    return gw


# ═══════════════════════════ EMBED ═══════════════════════════
def build_embed(gw: dict, cfg: dict) -> discord.Embed:
    st = gw.get("status", "running")
    if st == "ended":
        color = 0x747F8D
    elif st == "cancelled":
        color = 0xED4245
    else:
        color = gw.get("color") or cfg["color"]

    e = discord.Embed(title=f"🎁 {gw['prize']}"[:256], color=discord.Color(color))
    lines: list[str] = []
    if gw.get("description"):
        lines += [gw["description"], ""]

    ends = gw["ends_at"]
    if st in ("running", "draft"):
        lines.append(f"⏰ **Fin :** {ts(ends)} ({ts(ends, 'f')})")
    elif st == "ended":
        lines.append(f"⏰ **Terminé :** {ts(gw.get('ended_at') or ends)}")
    else:
        lines.append("⛔ **Ce giveaway a été annulé.**")
    lines.append(f"👤 **Organisé par :** <@{gw['host_id']}>")
    lines.append(f"🏆 **Nombre de gagnants :** {gw['winners']}")
    lines.append(f"🎟️ **Participants :** {gw.get('n', 0)}")

    if st == "ended":
        ws = gw.get("winner_ids") or []
        lines += ["", "🎉 **Félicitations :** " + (", ".join(f"<@{u}>" for u in ws) if ws else "*aucun participant valide*")]
    e.description = "\n".join(lines)[:4000]

    cond_lines = describe_conditions(gw["conditions"])
    if cond_lines:
        e.add_field(name="📋 Conditions", value="\n".join("• " + c for c in cond_lines)[:1024], inline=False)
    if gw["bonus"]:
        txt = "\n".join(f"• <@&{rid}> : **+{n}** entrée(s)" for rid, n in gw["bonus"].items())
        e.add_field(name="⭐ Entrées bonus", value=txt[:1024], inline=False)

    if str(gw.get("image", "")).startswith("http"):
        e.set_image(url=gw["image"])

    if gw.get("id") is None:
        e.set_footer(text="Aperçu — pas encore publié")
    elif st == "running":
        e.set_footer(text=f"Giveaway #{gw['id']} • Fin")
        e.timestamp = datetime.fromtimestamp(ends, tz=timezone.utc)
    else:
        e.set_footer(text=f"Giveaway #{gw['id']} • {'Terminé' if st == 'ended' else 'Annulé'}")
    return e


def closed_view(status: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(
        discord.ui.Button(
            label="Giveaway terminé" if status == "ended" else "Giveaway annulé",
            emoji="🔒",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
    )
    return v


# ═══════════════════════════ BROUILLON (création) ═══════════════════════════
@dataclass
class Draft:
    host_id: int
    prize: str = ""
    description: str = ""
    image: str = ""
    color: Optional[int] = None
    duration: int = DEFAULT_DURATION
    end_at: Optional[float] = None  # date exacte (prioritaire sur la durée)
    winners: int = 1
    channel_id: Optional[int] = None
    ping: Optional[str] = None  # None | "everyone" | "here" | "<id de rôle>"
    cond: dict = field(default_factory=new_cond)
    bonus: dict = field(default_factory=dict)

    def end_ts(self) -> float:
        return self.end_at or (time.time() + self.duration)

    def as_gw(self) -> dict:
        return {
            "id": None, "status": "draft", "prize": self.prize or "Lot à définir",
            "description": self.description, "image": self.image, "color": self.color,
            "winners": self.winners, "ends_at": self.end_ts(), "ended_at": None,
            "host_id": self.host_id, "conditions": self.cond, "bonus": self.bonus,
            "winner_ids": [], "n": 0,
        }


# ═══════════════════════════ BASES D'INTERFACE ═══════════════════════════
async def safe_reply(interaction: discord.Interaction, msg: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


class BaseView(discord.ui.View):
    def __init__(self, cog: "Giveaway", owner_id: int, timeout: float = VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await safe_reply(interaction, "Ce panneau ne t'appartient pas : tape `/giveaway` pour ouvrir le tien.")
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Erreur dans une vue giveaway", exc_info=error)
        await safe_reply(interaction, "❌ Une erreur est survenue, réessaie.")


class BaseModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Erreur dans une fenêtre giveaway", exc_info=error)
        await safe_reply(interaction, "❌ Une erreur est survenue, réessaie.")


class Btn(discord.ui.Button):
    """Bouton générique : exécute la coroutine `fn(interaction)`."""

    def __init__(self, fn: Callable, **kwargs):
        super().__init__(**kwargs)
        self.fn = fn

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.fn(interaction)


def back_button(cog: "Giveaway", row: int = 4) -> Btn:
    async def go(interaction: discord.Interaction):
        embed, view = await cog.panel(interaction)
        await interaction.response.edit_message(embed=embed, view=view)

    return Btn(go, label="Menu principal", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)


# ═══════════════════════════ BOUTON PUBLIC « PARTICIPER » ═══════════════════════════
class JoinView(discord.ui.View):
    """Vue persistante : survit aux redémarrages du bot."""

    def __init__(self, cog: "Giveaway", count: int = 0, label: str = "Participer", emoji: str = "🎉"):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label=f"{label} ({count})" if count else label,
            emoji=parse_emoji(emoji),
            custom_id="gw:join",
        )
        btn.callback = self.on_join
        self.add_item(btn)

    async def on_join(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_join(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog: "Giveaway", gw_id: int, user_id: int):
        super().__init__(timeout=60)
        self.cog, self.gw_id, self.user_id = cog, gw_id, user_id

    @discord.ui.button(label="Quitter le giveaway", emoji="🚪", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        gw = await self.cog.get_gw(self.gw_id)
        if not gw or gw["status"] != "running":
            return await interaction.response.edit_message(content="Ce giveaway est terminé.", view=None)
        await self.cog.db.execute(
            "DELETE FROM gw_entries WHERE giveaway_id=? AND user_id=?", (self.gw_id, self.user_id)
        )
        self.cog.dirty.add(self.gw_id)
        await interaction.response.edit_message(content="🚪 Tu as quitté le giveaway.", view=None)


# ═══════════════════════════ PANNEAU PRINCIPAL ═══════════════════════════
class PanelView(BaseView):
    @discord.ui.button(label="Créer", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await self.cog.db.one(
            "SELECT COUNT(*) AS c FROM giveaways WHERE guild_id=? AND status='running'", (interaction.guild_id,)
        )
        if row["c"] >= MAX_RUNNING:
            return await safe_reply(interaction, f"⚠️ Limite atteinte : {MAX_RUNNING} giveaways en cours maximum.")
        cfg = await self.cog.get_cfg(interaction.guild_id)
        draft = Draft(
            host_id=interaction.user.id,
            channel_id=cfg["channel"] or interaction.channel_id,
            ping=str(cfg["ping"]) if cfg["ping"] else None,
        )
        view = BuilderView(self.cog, interaction.user.id, draft, cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="En cours", emoji="📋", style=discord.ButtonStyle.primary, row=0)
    async def running(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await self.cog.picker_payload(interaction.guild_id, interaction.user.id, history=False)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Historique", emoji="🗂️", style=discord.ButtonStyle.secondary, row=0)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await self.cog.picker_payload(interaction.guild_id, interaction.user.id, history=True)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Configuration", emoji="⚙️", style=discord.ButtonStyle.secondary, row=1)
    async def config(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ConfigView(self.cog, interaction.user.id, await self.cog.get_cfg(interaction.guild_id), interaction.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="Blacklist", emoji="🚫", style=discord.ButtonStyle.danger, row=1)
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = BlacklistView(self.cog, interaction.user.id, await self.cog.get_cfg(interaction.guild_id), interaction.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ═══════════════════════════ CONSTRUCTEUR DE GIVEAWAY ═══════════════════════════
class PrizeModal(BaseModal):
    def __init__(self, builder: "BuilderView"):
        super().__init__(title="Lot & détails")
        self.builder = builder
        d = builder.draft
        self.prize = discord.ui.TextInput(label="Lot à gagner", default=d.prize or None, max_length=100)
        self.desc = discord.ui.TextInput(
            label="Description (optionnel)", style=discord.TextStyle.paragraph,
            default=d.description or None, required=False, max_length=1000,
        )
        self.image = discord.ui.TextInput(
            label="Image (lien https, optionnel)", default=d.image or None, required=False, max_length=300
        )
        self.color = discord.ui.TextInput(
            label="Couleur hex (optionnel, ex : #ff9900)",
            default=f"#{d.color:06x}" if d.color else None, required=False, max_length=7,
        )
        for item in (self.prize, self.desc, self.image, self.color):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        d = self.builder.draft
        color = None
        if self.color.value.strip():
            color = parse_color(self.color.value)
            if color is None:
                return await safe_reply(interaction, "⚠️ Couleur invalide. Exemple : `#ff9900`.")
        img = self.image.value.strip()
        if img and not img.startswith("http"):
            return await safe_reply(interaction, "⚠️ Le lien de l'image doit commencer par `https://`.")
        d.prize, d.description, d.image, d.color = self.prize.value.strip(), self.desc.value.strip(), img, color
        await self.builder.refresh(interaction)


class DurationModal(BaseModal):
    def __init__(self, builder: "BuilderView"):
        super().__init__(title="Durée ou date de fin")
        self.builder = builder
        self.value = discord.ui.TextInput(
            label="Durée ou date de fin",
            placeholder="ex : 2h  •  1j12h  •  30min  •  28/12/2026 20:00",
            max_length=40,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        d = self.builder.draft
        text = self.value.value
        secs = parse_duration(text)
        if secs is not None:
            if not MIN_DURATION <= secs <= MAX_DURATION:
                return await safe_reply(interaction, "⚠️ La durée doit être comprise entre 10 secondes et 365 jours.")
            d.duration, d.end_at = secs, None
        else:
            when = parse_date(text)
            if when is None:
                return await safe_reply(interaction, "⚠️ Format invalide. Exemples : `2h`, `1j12h`, `28/12/2026 20:00`.")
            if not time.time() + MIN_DURATION <= when <= time.time() + MAX_DURATION:
                return await safe_reply(interaction, "⚠️ La date doit être dans le futur (et dans moins d'un an).")
            d.end_at = when
        await self.builder.refresh(interaction)


class WinnersModal(BaseModal):
    def __init__(self, builder: "BuilderView"):
        super().__init__(title="Nombre de gagnants")
        self.builder = builder
        self.value = discord.ui.TextInput(label=f"Gagnants (1 à {MAX_WINNERS})", default=str(builder.draft.winners), max_length=2)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        v = self.value.value.strip()
        if not v.isdigit() or not 1 <= int(v) <= MAX_WINNERS:
            return await safe_reply(interaction, f"⚠️ Entre un nombre entre 1 et {MAX_WINNERS}.")
        self.builder.draft.winners = int(v)
        await self.builder.refresh(interaction)


class ConditionsModal(BaseModal):
    def __init__(self, builder: "BuilderView"):
        super().__init__(title="Conditions de participation")
        self.builder = builder
        c = builder.draft.cond
        self.acc = discord.ui.TextInput(
            label="Âge minimum du compte Discord (jours)", default=str(c["min_account_days"]), max_length=4
        )
        self.srv = discord.ui.TextInput(
            label="Ancienneté minimum sur le serveur (jours)", default=str(c["min_server_days"]), max_length=4
        )
        self.mode = discord.ui.TextInput(
            label="Rôles requis : « un » ou « tous » ?",
            default="tous" if c["req_all"] else "un", max_length=4,
        )
        for item in (self.acc, self.srv, self.mode):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        a, s, m = self.acc.value.strip(), self.srv.value.strip(), self.mode.value.strip().lower()
        if not (a.isdigit() and s.isdigit()):
            return await safe_reply(interaction, "⚠️ Les âges doivent être des nombres entiers (0 pour désactiver).")
        if m not in ("un", "tous"):
            return await safe_reply(interaction, "⚠️ Pour les rôles, écris `un` ou `tous`.")
        c = self.builder.draft.cond
        c["min_account_days"], c["min_server_days"], c["req_all"] = int(a), int(s), m == "tous"
        await self.builder.refresh(interaction)


class BonusModal(BaseModal):
    def __init__(self, builder: "BuilderView", role: discord.Role):
        super().__init__(title="Entrées bonus"[:45])
        self.builder, self.role = builder, role
        cur = builder.draft.bonus.get(str(role.id), 1)
        self.value = discord.ui.TextInput(
            label=f"Entrées en plus pour @{role.name}"[:45], default=str(cur), max_length=3,
            placeholder="0 pour retirer le bonus",
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        v = self.value.value.strip()
        if not v.isdigit() or int(v) > 100:
            return await safe_reply(interaction, "⚠️ Entre un nombre entre 0 et 100.")
        bonus = self.builder.draft.bonus
        if int(v) == 0:
            bonus.pop(str(self.role.id), None)
        else:
            bonus[str(self.role.id)] = int(v)
        sub = BonusView(self.builder)
        await interaction.response.edit_message(embed=sub.embed(), view=sub)


class BuilderView(BaseView):
    def __init__(self, cog: "Giveaway", owner_id: int, draft: Draft, cfg: dict):
        super().__init__(cog, owner_id)
        self.draft, self.cfg = draft, cfg

    def embed(self) -> discord.Embed:
        d = self.draft
        e = build_embed(d.as_gw(), self.cfg)
        e.set_author(name="🛠️ Création d'un giveaway — aperçu")
        dur = ts(d.end_at, "f") if d.end_at else fmt_duration(d.duration)
        ping = {None: "Aucune", "everyone": "@everyone", "here": "@here"}.get(d.ping, f"<@&{d.ping}>")
        e.add_field(
            name="⚙️ Réglages",
            value=f"📍 Salon : <#{d.channel_id}>\n⏱️ Durée : {dur}\n📢 Mention : {ping}",
            inline=False,
        )
        return e

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ── ligne 0 : réglages principaux
    @discord.ui.button(label="Lot & détails", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def b_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PrizeModal(self))

    @discord.ui.button(label="Durée", emoji="⏱️", style=discord.ButtonStyle.secondary, row=0)
    async def b_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DurationModal(self))

    @discord.ui.button(label="Gagnants", emoji="🏆", style=discord.ButtonStyle.secondary, row=0)
    async def b_winners(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WinnersModal(self))

    @discord.ui.button(label="Conditions", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def b_cond(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ConditionsModal(self))

    # ── lignes 1-2 : sélecteurs
    @discord.ui.select(
        cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="📍 Salon du giveaway", min_values=1, max_values=1, row=1,
    )
    async def s_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.draft.channel_id = select.values[0].id
        await self.refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="🎭 Rôles requis pour participer (optionnel)",
        min_values=0, max_values=10, row=2,
    )
    async def s_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.draft.cond["req_roles"] = [r.id for r in select.values if not r.is_default()]
        await self.refresh(interaction)

    # ── ligne 3 : sous-menus
    @discord.ui.button(label="Rôles interdits", emoji="🚷", style=discord.ButtonStyle.secondary, row=3)
    async def b_forbidden(self, interaction: discord.Interaction, button: discord.ui.Button):
        sub = ForbiddenView(self)
        await interaction.response.edit_message(embed=sub.embed(), view=sub)

    @discord.ui.button(label="Bonus d'entrées", emoji="⭐", style=discord.ButtonStyle.secondary, row=3)
    async def b_bonus(self, interaction: discord.Interaction, button: discord.ui.Button):
        sub = BonusView(self)
        await interaction.response.edit_message(embed=sub.embed(), view=sub)

    @discord.ui.button(label="Mention", emoji="📢", style=discord.ButtonStyle.secondary, row=3)
    async def b_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        sub = MentionView(self)
        await interaction.response.edit_message(embed=sub.embed(), view=sub)

    # ── ligne 4 : lancement
    @discord.ui.button(label="Lancer", emoji="🚀", style=discord.ButtonStyle.success, row=4)
    async def b_launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        d = self.draft
        if not d.prize.strip():
            return await safe_reply(interaction, "⚠️ Définis d'abord le lot avec **Lot & détails**.")
        end = d.end_ts()
        if end - time.time() < MIN_DURATION:
            return await safe_reply(interaction, "⚠️ La date de fin est déjà passée : modifie la durée.")
        await interaction.response.defer()
        ok, result = await self.cog.launch(interaction.guild, d, end)
        if not ok:
            return await interaction.followup.send(f"❌ {result}", ephemeral=True)
        gw_id, url = result
        embed = discord.Embed(
            title="✅ Giveaway lancé !",
            description=f"**#{gw_id}** — [Voir le message]({url})\nFin {ts(end)}",
            color=discord.Color.green(),
        )
        view = BaseView(self.cog, interaction.user.id)
        view.add_item(back_button(self.cog, row=0))
        await interaction.edit_original_response(embed=embed, view=view)
        self.stop()

    @discord.ui.button(label="Annuler", emoji="✖️", style=discord.ButtonStyle.danger, row=4)
    async def b_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await self.cog.panel(interaction)
        await interaction.response.edit_message(embed=embed, view=view)
        self.stop()


class SubView(BaseView):
    """Sous-menu du constructeur : on revient à l'aperçu avec « Retour »."""

    def __init__(self, builder: BuilderView):
        super().__init__(builder.cog, builder.owner_id)
        self.builder = builder

        async def go_back(interaction: discord.Interaction):
            await builder.refresh(interaction)

        self.add_item(Btn(go_back, label="Retour à l'aperçu", emoji="⬅️", style=discord.ButtonStyle.secondary, row=4))


class ForbiddenView(SubView):
    def embed(self) -> discord.Embed:
        forb = self.builder.draft.cond["forb_roles"]
        txt = ", ".join(f"<@&{r}>" for r in forb) if forb else "*aucun*"
        return discord.Embed(
            title="🚷 Rôles interdits",
            description=f"Les membres ayant l'un de ces rôles ne pourront pas participer.\n\n**Actuellement :** {txt}",
            color=discord.Color.red(),
        )

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choisis les rôles interdits", min_values=0, max_values=10, row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.builder.draft.cond["forb_roles"] = [r.id for r in select.values if not r.is_default()]
        await interaction.response.edit_message(embed=self.embed(), view=self)


class BonusView(SubView):
    def embed(self) -> discord.Embed:
        b = self.builder.draft.bonus
        txt = "\n".join(f"• <@&{rid}> : **+{n}**" for rid, n in b.items()) if b else "*aucun bonus*"
        return discord.Embed(
            title="⭐ Entrées bonus par rôle",
            description=(
                "Choisis un rôle puis le nombre d'entrées supplémentaires qu'il donne "
                "(les bonus de plusieurs rôles s'additionnent). Plus d'entrées = plus de chances.\n\n" + txt
            ),
            color=discord.Color.gold(),
        )

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Ajouter / modifier un rôle bonus", min_values=1, max_values=1, row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        if role.is_default():
            return await safe_reply(interaction, "⚠️ Choisis un autre rôle que @everyone.")
        await interaction.response.send_modal(BonusModal(self.builder, role))

    @discord.ui.button(label="Tout effacer", emoji="🧹", style=discord.ButtonStyle.danger, row=3)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.builder.draft.bonus.clear()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class MentionView(SubView):
    def embed(self) -> discord.Embed:
        p = self.builder.draft.ping
        cur = {None: "Aucune", "everyone": "@everyone", "here": "@here"}.get(p, f"<@&{p}>")
        return discord.Embed(
            title="📢 Mention à la publication",
            description=f"Choisis qui est notifié quand le giveaway est posté.\n\n**Actuellement :** {cur}",
            color=discord.Color.blurple(),
        )

    async def _set(self, interaction: discord.Interaction, value: Optional[str]):
        self.builder.draft.ping = value
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Mentionner un rôle précis", min_values=1, max_values=1, row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        await self._set(interaction, None if role.is_default() else str(role.id))

    @discord.ui.button(label="Aucune", style=discord.ButtonStyle.secondary, row=1)
    async def none(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, None)

    @discord.ui.button(label="@everyone", style=discord.ButtonStyle.primary, row=1)
    async def everyone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "everyone")

    @discord.ui.button(label="@here", style=discord.ButtonStyle.primary, row=1)
    async def here(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "here")


# ═══════════════════════════ LISTES & GESTION D'UN GIVEAWAY ═══════════════════════════
class PickerView(BaseView):
    def __init__(self, cog: "Giveaway", owner_id: int, gws: list[dict], history: bool):
        super().__init__(cog, owner_id)
        if gws:
            options = []
            for g in gws:
                if history:
                    when = g.get("ended_at") or g["created_at"]
                    desc = f"{STATUS_LABEL[g['status']][2:]} • {g['n']} participant(s) • {datetime.fromtimestamp(when, TZ):%d/%m %H:%M}"
                else:
                    desc = f"{g['n']} participant(s) • fin {datetime.fromtimestamp(g['ends_at'], TZ):%d/%m à %H:%M}"
                options.append(discord.SelectOption(label=f"#{g['id']} · {g['prize']}"[:100], value=str(g["id"]), description=desc[:100]))
            select = discord.ui.Select(placeholder="Choisis un giveaway à gérer…", options=options, row=0)
            select.callback = self.on_pick
            self.add_item(select)
        self.add_item(back_button(cog, row=1))

    async def on_pick(self, interaction: discord.Interaction) -> None:
        gw = await self.cog.get_gw(int(interaction.data["values"][0]), interaction.guild_id)
        if not gw:
            return await safe_reply(interaction, "⚠️ Giveaway introuvable.")
        embed, view = await self.cog.manage_payload(gw, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


class ExtendModal(BaseModal):
    def __init__(self, cog: "Giveaway", gw_id: int):
        super().__init__(title="Prolonger le giveaway")
        self.cog, self.gw_id = cog, gw_id
        self.value = discord.ui.TextInput(label="Durée à ajouter", placeholder="ex : 1h, 2j, 30min", max_length=20)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        secs = parse_duration(self.value.value)
        if not secs:
            return await safe_reply(interaction, "⚠️ Durée invalide. Exemples : `1h`, `2j`, `30min`.")
        await self.cog.db.execute(
            "UPDATE giveaways SET ends_at = ends_at + ? WHERE id=? AND status='running'", (secs, self.gw_id)
        )
        gw = await self.cog.get_gw(self.gw_id)
        await self.cog.refresh_message(gw)
        embed, view = await self.cog.manage_payload(gw, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


class EditModal(BaseModal):
    def __init__(self, cog: "Giveaway", gw: dict):
        super().__init__(title=f"Modifier le giveaway #{gw['id']}")
        self.cog, self.gw_id = cog, gw["id"]
        self.prize = discord.ui.TextInput(label="Lot", default=gw["prize"], max_length=100)
        self.desc = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, default=gw["description"] or None,
            required=False, max_length=1000,
        )
        self.winners = discord.ui.TextInput(label="Nombre de gagnants", default=str(gw["winners"]), max_length=2)
        self.image = discord.ui.TextInput(label="Image (lien https)", default=gw["image"] or None, required=False, max_length=300)
        for item in (self.prize, self.desc, self.winners, self.image):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        w = self.winners.value.strip()
        img = self.image.value.strip()
        if not w.isdigit() or not 1 <= int(w) <= MAX_WINNERS:
            return await safe_reply(interaction, f"⚠️ Gagnants : entre 1 et {MAX_WINNERS}.")
        if img and not img.startswith("http"):
            return await safe_reply(interaction, "⚠️ Le lien de l'image doit commencer par `https://`.")
        await self.cog.db.execute(
            "UPDATE giveaways SET prize=?, description=?, winners=?, image=? WHERE id=?",
            (self.prize.value.strip(), self.desc.value.strip(), int(w), img, self.gw_id),
        )
        gw = await self.cog.get_gw(self.gw_id)
        await self.cog.refresh_message(gw)
        embed, view = await self.cog.manage_payload(gw, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


class RerollModal(BaseModal):
    def __init__(self, cog: "Giveaway", gw: dict):
        super().__init__(title=f"Relancer le giveaway #{gw['id']}")
        self.cog, self.gw_id = cog, gw["id"]
        self.count = discord.ui.TextInput(label="Combien de nouveaux gagnants ?", default=str(gw["winners"]), max_length=2)
        self.replace = discord.ui.TextInput(
            label="Remplacer les anciens gagnants ? (oui/non)", default="non", max_length=3
        )
        self.add_item(self.count)
        self.add_item(self.replace)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        c, r = self.count.value.strip(), self.replace.value.strip().lower()
        if not c.isdigit() or not 1 <= int(c) <= MAX_WINNERS:
            return await safe_reply(interaction, f"⚠️ Entre un nombre entre 1 et {MAX_WINNERS}.")
        if r not in ("oui", "non"):
            return await safe_reply(interaction, "⚠️ Écris `oui` ou `non`.")
        await interaction.response.defer()
        await self.cog.reroll(self.gw_id, int(c), r == "oui")
        gw = await self.cog.get_gw(self.gw_id)
        embed, view = await self.cog.manage_payload(gw, interaction.user.id)
        await interaction.edit_original_response(embed=embed, view=view)


class ConfirmView(BaseView):
    LABELS = {"end": "terminer maintenant", "cancel": "annuler", "delete": "supprimer définitivement"}

    def __init__(self, cog: "Giveaway", owner_id: int, gw_id: int, action: str):
        super().__init__(cog, owner_id)
        self.gw_id, self.action = gw_id, action

    @discord.ui.button(label="Confirmer", emoji="✅", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.do_action(self.action, self.gw_id)
        if self.action == "delete":
            embed, view = await self.cog.panel(interaction)
        else:
            embed, view = await self.cog.manage_payload(await self.cog.get_gw(self.gw_id), interaction.user.id)
        await interaction.edit_original_response(embed=embed, view=view)

    @discord.ui.button(label="Retour", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await self.cog.manage_payload(await self.cog.get_gw(self.gw_id), interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


class ManageView(BaseView):
    def __init__(self, cog: "Giveaway", owner_id: int, gw: dict):
        super().__init__(cog, owner_id)
        self.gw = gw
        gid, st = gw["id"], gw["status"]
        S = discord.ButtonStyle

        async def confirm(interaction: discord.Interaction, action: str):
            embed = discord.Embed(
                title="⚠️ Confirmation",
                description=f"Veux-tu vraiment **{ConfirmView.LABELS[action]}** le giveaway **#{gid} — {gw['prize']}** ?",
                color=discord.Color.orange(),
            )
            await interaction.response.edit_message(embed=embed, view=ConfirmView(cog, owner_id, gid, action))

        async def participants(interaction: discord.Interaction):
            view = ParticipantsView(cog, owner_id, gid)
            await interaction.response.edit_message(embed=await view.embed(), view=view)

        async def back(interaction: discord.Interaction):
            embed, view = await cog.picker_payload(interaction.guild_id, owner_id, history=st != "running")
            await interaction.response.edit_message(embed=embed, view=view)

        if st == "running":
            self.add_item(Btn(lambda i: confirm(i, "end"), label="Terminer maintenant", emoji="🏁", style=S.success, row=0))
            self.add_item(Btn(lambda i: i.response.send_modal(ExtendModal(cog, gid)), label="Prolonger", emoji="⏱️", style=S.secondary, row=0))
            self.add_item(Btn(lambda i: i.response.send_modal(EditModal(cog, gw)), label="Modifier", emoji="✏️", style=S.secondary, row=0))
            self.add_item(Btn(participants, label="Participants", emoji="👥", style=S.secondary, row=0))
            self.add_item(Btn(lambda i: confirm(i, "cancel"), label="Annuler", emoji="⛔", style=S.danger, row=0))
        elif st == "ended":
            self.add_item(Btn(lambda i: i.response.send_modal(RerollModal(cog, gw)), label="Relancer le tirage", emoji="🎲", style=S.primary, row=0))
            self.add_item(Btn(participants, label="Participants", emoji="👥", style=S.secondary, row=0))
            self.add_item(Btn(lambda i: confirm(i, "delete"), label="Supprimer", emoji="🗑️", style=S.danger, row=0))
        else:
            self.add_item(Btn(lambda i: confirm(i, "delete"), label="Supprimer", emoji="🗑️", style=S.danger, row=0))
        self.add_item(Btn(back, label="Retour à la liste", emoji="⬅️", style=S.secondary, row=1))


class ParticipantsView(BaseView):
    def __init__(self, cog: "Giveaway", owner_id: int, gw_id: int, page: int = 0):
        super().__init__(cog, owner_id)
        self.gw_id, self.page, self.total = gw_id, page, 0
        S = discord.ButtonStyle

        async def move(interaction: discord.Interaction, delta: int):
            self.page += delta
            await interaction.response.edit_message(embed=await self.embed(), view=self)

        async def back(interaction: discord.Interaction):
            embed, view = await cog.manage_payload(await cog.get_gw(gw_id), owner_id)
            await interaction.response.edit_message(embed=embed, view=view)

        self.prev = Btn(lambda i: move(i, -1), emoji="◀️", style=S.secondary, row=0)
        self.next = Btn(lambda i: move(i, 1), emoji="▶️", style=S.secondary, row=0)
        self.add_item(self.prev)
        self.add_item(self.next)
        self.add_item(Btn(back, label="Retour", emoji="⬅️", style=S.secondary, row=0))

    async def embed(self) -> discord.Embed:
        row = await self.cog.db.one("SELECT COUNT(*) AS c FROM gw_entries WHERE giveaway_id=?", (self.gw_id,))
        self.total = row["c"]
        pages = max(1, -(-self.total // PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        rows = await self.cog.db.all(
            "SELECT user_id, weight FROM gw_entries WHERE giveaway_id=? ORDER BY joined_at LIMIT ? OFFSET ?",
            (self.gw_id, PAGE_SIZE, self.page * PAGE_SIZE),
        )
        lines = [
            f"`{self.page * PAGE_SIZE + i + 1}.` <@{r['user_id']}>" + (f" ×{r['weight']}" if r["weight"] > 1 else "")
            for i, r in enumerate(rows)
        ]
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= pages - 1
        return discord.Embed(
            title=f"👥 Participants — giveaway #{self.gw_id}",
            description="\n".join(lines) or "*Personne n'a encore participé.*",
            color=discord.Color.blurple(),
        ).set_footer(text=f"Page {self.page + 1}/{pages} • {self.total} participant(s) • ×N = entrées bonus")


# ═══════════════════════════ CONFIGURATION & BLACKLIST ═══════════════════════════
class AppearanceModal(BaseModal):
    def __init__(self, view: "ConfigView"):
        super().__init__(title="Apparence")
        self.view = view
        c = view.cfg
        self.color = discord.ui.TextInput(label="Couleur par défaut (hex)", default=f"#{c['color']:06x}", max_length=7)
        self.emoji = discord.ui.TextInput(label="Emoji du bouton", default=c["emoji"], max_length=60)
        self.label = discord.ui.TextInput(label="Texte du bouton", default=c["label"], max_length=30)
        for item in (self.color, self.emoji, self.label):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color = parse_color(self.color.value)
        if color is None:
            return await safe_reply(interaction, "⚠️ Couleur invalide. Exemple : `#f1c40f`.")
        c = self.view.cfg
        c["color"], c["label"] = color, self.label.value.strip() or "Participer"
        e = parse_emoji(self.emoji.value)
        c["emoji"] = str(e)
        await self.view.save()
        await interaction.response.edit_message(embed=self.view.embed(), view=self.view)


class ConfigView(BaseView):
    def __init__(self, cog: "Giveaway", owner_id: int, cfg: dict, guild_id: int):
        super().__init__(cog, owner_id)
        self.cfg, self.guild_id = cfg, guild_id
        self._sync_toggle()
        self.add_item(back_button(cog, row=3))

    def _sync_toggle(self) -> None:
        self.toggle_dm.label = "MP aux gagnants : " + ("activé" if self.cfg["dm"] else "désactivé")
        self.toggle_dm.style = discord.ButtonStyle.success if self.cfg["dm"] else discord.ButtonStyle.secondary

    async def save(self) -> None:
        await self.cog.save_cfg(self.guild_id, self.cfg)

    def embed(self) -> discord.Embed:
        c = self.cfg
        ping = {None: "Aucune", "everyone": "@everyone", "here": "@here"}.get(c["ping"], f"<@&{c['ping']}>")
        e = discord.Embed(title="⚙️ Configuration des giveaways", color=discord.Color(c["color"]))
        e.add_field(name="📍 Salon par défaut", value=f"<#{c['channel']}>" if c["channel"] else "Salon où tu tapes la commande", inline=False)
        e.add_field(name="📢 Mention par défaut", value=ping, inline=True)
        e.add_field(name="📩 MP aux gagnants", value="Oui" if c["dm"] else "Non", inline=True)
        e.add_field(name="🎨 Apparence", value=f"Couleur `#{c['color']:06x}` • bouton {c['emoji']} **{c['label']}**", inline=False)
        e.add_field(
            name="🛡️ Rôles gestionnaires",
            value=(", ".join(f"<@&{r}>" for r in c["managers"]) or "Aucun") + "\n*Les admins et ceux qui ont « Gérer le serveur » ont toujours accès.*",
            inline=False,
        )
        return e

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="📍 Salon par défaut des giveaways", min_values=1, max_values=1, row=0,
    )
    async def s_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.cfg["channel"] = select.values[0].id
        await self.save()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="🛡️ Rôles autorisés à gérer les giveaways",
        min_values=0, max_values=10, row=1,
    )
    async def s_managers(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.cfg["managers"] = [r.id for r in select.values if not r.is_default()]
        await self.save()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="📢 Rôle mentionné par défaut", min_values=1, max_values=1, row=2,
    )
    async def s_ping(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        self.cfg["ping"] = None if role.is_default() else str(role.id)
        await self.save()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Apparence", emoji="🎨", style=discord.ButtonStyle.primary, row=3)
    async def b_look(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppearanceModal(self))

    @discord.ui.button(label="MP aux gagnants", row=3)
    async def toggle_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cfg["dm"] = not self.cfg["dm"]
        self._sync_toggle()
        await self.save()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Réinitialiser salon & mention", emoji="🧹", style=discord.ButtonStyle.secondary, row=3)
    async def b_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cfg["channel"] = None
        self.cfg["ping"] = None
        await self.save()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class BlacklistView(BaseView):
    def __init__(self, cog: "Giveaway", owner_id: int, cfg: dict, guild_id: int):
        super().__init__(cog, owner_id)
        self.cfg, self.guild_id = cfg, guild_id
        self.add_item(back_button(cog, row=2))

    def embed(self) -> discord.Embed:
        bl = self.cfg["blacklist"]
        txt = "\n".join(f"• <@{u}>" for u in bl[:40]) or "*Personne pour l'instant.*"
        if len(bl) > 40:
            txt += f"\n… et {len(bl) - 40} autre(s)"
        return discord.Embed(
            title="🚫 Blacklist des giveaways",
            description="Ces membres ne peuvent pas participer (et ne peuvent jamais être tirés au sort).\n\n" + txt,
            color=discord.Color.red(),
        )

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="➕ Ajouter à la blacklist", min_values=1, max_values=10, row=0)
    async def add(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        bl = self.cfg["blacklist"]
        for u in select.values:
            if not u.bot and u.id not in bl and len(bl) < 200:
                bl.append(u.id)
        await self.cog.save_cfg(self.guild_id, self.cfg)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="➖ Retirer de la blacklist", min_values=1, max_values=10, row=1)
    async def remove(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        gone = {u.id for u in select.values}
        self.cfg["blacklist"] = [u for u in self.cfg["blacklist"] if u not in gone]
        await self.cog.save_cfg(self.guild_id, self.cfg)
        await interaction.response.edit_message(embed=self.embed(), view=self)


# ═══════════════════════════ LE COG ═══════════════════════════
class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: DB = None  # type: ignore[assignment]
        self.dirty: set[int] = set()
        self._cfg: dict[int, dict] = {}
        self._owners: Optional[set[int]] = None

    # ── cycle de vie
    async def cog_load(self) -> None:
        self.db = DB(DB_PATH)
        self.bot.add_view(JoinView(self))  # boutons « Participer » persistants
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        if self.db:
            self.db.close()

    # ── accès
    async def owner_ids(self) -> set[int]:
        if self._owners is None:
            ids: set[int] = set()
            try:
                info = await self.bot.application_info()
                if info.team:
                    ids.update(m.id for m in info.team.members)
                    ids.add(info.team.owner_id)
                elif info.owner:
                    ids.add(info.owner.id)
            except discord.HTTPException:
                pass
            self._owners = ids
        return self._owners | EXTRA_ADMINS

    async def is_manager(self, member: discord.Member, guild: discord.Guild) -> bool:
        if member.id in await self.owner_ids() or member.id == guild.owner_id:
            return True
        if STRICT:
            return False
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            return True
        cfg = await self.get_cfg(guild.id)
        return any(r.id in cfg["managers"] for r in member.roles)

    # ── config
    async def get_cfg(self, guild_id: int) -> dict:
        if guild_id not in self._cfg:
            cfg = json.loads(json.dumps(DEFAULT_CFG))
            row = await self.db.one("SELECT data FROM gw_config WHERE guild_id=?", (guild_id,))
            if row:
                cfg.update(json.loads(row["data"]))
            self._cfg[guild_id] = cfg
        return self._cfg[guild_id]

    async def save_cfg(self, guild_id: int, cfg: dict) -> None:
        self._cfg[guild_id] = cfg
        await self.db.execute(
            "INSERT INTO gw_config (guild_id, data) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
            (guild_id, json.dumps(cfg)),
        )

    # ── accès aux giveaways
    async def get_gw(self, gw_id: int, guild_id: Optional[int] = None) -> Optional[dict]:
        sql, params = GW_SELECT + "WHERE g.id=?", [gw_id]
        if guild_id is not None:
            sql += " AND g.guild_id=?"
            params.append(guild_id)
        return decode_gw(await self.db.one(sql, tuple(params)))

    async def get_channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    @staticmethod
    def url_of(gw: dict) -> str:
        return f"https://discord.com/channels/{gw['guild_id']}/{gw['channel_id']}/{gw['message_id']}"

    # ── commande unique
    @app_commands.command(name="giveaway", description="🎁 Ouvre le panneau de gestion des giveaways")
    @app_commands.guild_only()
    async def giveaway_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.is_manager(interaction.user, interaction.guild):
            return await interaction.response.send_message("⛔ Tu n'as pas accès à la gestion des giveaways.", ephemeral=True)
        embed, view = await self.panel(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── payloads d'interface
    async def panel(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        s = await self.db.one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(status='running'),0) AS running, "
            "COALESCE(SUM(status='ended'),0) AS ended FROM giveaways WHERE guild_id=?",
            (gid,),
        )
        p = await self.db.one(
            "SELECT COUNT(*) AS c FROM gw_entries WHERE giveaway_id IN (SELECT id FROM giveaways WHERE guild_id=?)",
            (gid,),
        )
        e = discord.Embed(
            title="🎁 Giveaways — panneau de gestion",
            description=(
                "**➕ Créer** un giveaway (lot, durée, gagnants, conditions, bonus, mention…)\n"
                "**📋 En cours** : terminer, prolonger, modifier, voir les participants\n"
                "**🗂️ Historique** : relancer un tirage, supprimer\n"
                "**⚙️ Configuration** : salon, mention, apparence, rôles gestionnaires\n"
                "**🚫 Blacklist** : exclure des membres"
            ),
            color=discord.Color.gold(),
        )
        e.add_field(name="🟢 En cours", value=str(s["running"]))
        e.add_field(name="🔴 Terminés", value=str(s["ended"]))
        e.add_field(name="🎟️ Participations", value=str(p["c"]))
        return e, PanelView(self, interaction.user.id)

    async def picker_payload(self, guild_id: int, owner_id: int, history: bool):
        if history:
            where, order = "g.status IN ('ended','cancelled')", "COALESCE(g.ended_at, g.created_at) DESC"
        else:
            where, order = "g.status='running'", "g.ends_at ASC"
        rows = await self.db.all(GW_SELECT + f"WHERE g.guild_id=? AND {where} ORDER BY {order} LIMIT 25", (guild_id,))
        gws = [decode_gw(r) for r in rows]
        e = discord.Embed(
            title="🗂️ Historique des giveaways" if history else "📋 Giveaways en cours",
            description=(
                "Sélectionne un giveaway dans le menu pour le gérer."
                if gws else ("*Aucun giveaway terminé pour l'instant.*" if history else "*Aucun giveaway en cours.*")
            ),
            color=discord.Color.blurple(),
        )
        return e, PickerView(self, owner_id, gws, history)

    async def manage_payload(self, gw: dict, owner_id: int):
        cfg = await self.get_cfg(gw["guild_id"])
        e = build_embed(gw, cfg)
        e.set_author(name=f"Gestion du giveaway #{gw['id']} — {STATUS_LABEL[gw['status']]}")
        e.add_field(name="🔗 Message", value=f"[Aller au giveaway]({self.url_of(gw)}) • <#{gw['channel_id']}>", inline=False)
        return e, ManageView(self, owner_id, gw)

    # ── lancement
    async def launch(self, guild: discord.Guild, d: Draft, end_ts: float):
        channel = guild.get_channel(d.channel_id) or guild.get_thread(d.channel_id)
        if channel is None:
            return False, "Salon introuvable : choisis un salon dans le menu."
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return False, f"Il me manque des permissions dans {channel.mention} (voir, envoyer des messages, intégrer des liens)."

        cfg = await self.get_cfg(guild.id)
        gw = {
            "id": None, "guild_id": guild.id, "channel_id": channel.id, "message_id": 0, "host_id": d.host_id,
            "prize": d.prize.strip(), "description": d.description, "image": d.image, "color": d.color,
            "winners": d.winners, "ends_at": end_ts, "created_at": time.time(), "ended_at": None,
            "status": "running", "conditions": d.cond, "bonus": d.bonus, "winner_ids": [], "n": 0,
        }
        gw["id"] = await self.db.insert(
            "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, description, image, color, winners, "
            "ends_at, created_at, conditions, bonus) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (guild.id, channel.id, d.host_id, gw["prize"], d.description, d.image, d.color, d.winners,
             end_ts, gw["created_at"], json.dumps(d.cond), json.dumps(d.bonus)),
        )

        content, allowed = "🎉 **NOUVEAU GIVEAWAY !** 🎉", discord.AllowedMentions.none()
        if d.ping == "everyone":
            content, allowed = "@everyone " + content, discord.AllowedMentions(everyone=True)
        elif d.ping == "here":
            content, allowed = "@here " + content, discord.AllowedMentions(everyone=True)
        elif d.ping:
            content, allowed = f"<@&{d.ping}> " + content, discord.AllowedMentions(roles=[discord.Object(id=int(d.ping))])

        try:
            msg = await channel.send(
                content=content, embed=build_embed(gw, cfg), allowed_mentions=allowed,
                view=JoinView(self, 0, cfg["label"], cfg["emoji"]),
            )
        except discord.HTTPException as exc:
            await self.db.execute("DELETE FROM giveaways WHERE id=?", (gw["id"],))
            return False, f"Impossible de publier le message : {exc}"
        await self.db.execute("UPDATE giveaways SET message_id=? WHERE id=?", (msg.id, gw["id"]))
        return True, (gw["id"], msg.jump_url)

    # ── participation
    async def handle_join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        row = await self.db.one(GW_SELECT + "WHERE g.message_id=?", (interaction.message.id,))
        gw = decode_gw(row)
        if not gw or gw["status"] != "running" or gw["ends_at"] <= time.time():
            return await interaction.followup.send("⌛ Ce giveaway est terminé.", ephemeral=True)

        member = interaction.user
        if not isinstance(member, discord.Member) or member.bot:
            return await interaction.followup.send("⚠️ Participation impossible.", ephemeral=True)

        cfg = await self.get_cfg(gw["guild_id"])
        if member.id in cfg["blacklist"]:
            return await interaction.followup.send("🚫 Tu n'es pas autorisé à participer aux giveaways de ce serveur.", ephemeral=True)

        reasons = check_conditions(member, gw["conditions"])
        if reasons:
            return await interaction.followup.send(
                "❌ **Tu ne remplis pas les conditions :**\n" + "\n".join("• " + r for r in reasons), ephemeral=True
            )

        exists = await self.db.one(
            "SELECT 1 AS x FROM gw_entries WHERE giveaway_id=? AND user_id=?", (gw["id"], member.id)
        )
        if exists:
            return await interaction.followup.send(
                "✅ Tu participes déjà ! Veux-tu te retirer ?", ephemeral=True, view=LeaveView(self, gw["id"], member.id)
            )

        weight = compute_weight(member, gw["bonus"])
        await self.db.execute(
            "INSERT OR IGNORE INTO gw_entries (giveaway_id, user_id, weight, joined_at) VALUES (?,?,?,?)",
            (gw["id"], member.id, weight, time.time()),
        )
        self.dirty.add(gw["id"])
        bonus = f"\n⭐ Grâce à tes rôles, tu as **{weight} entrées** !" if weight > 1 else ""
        await interaction.followup.send(f"🎉 **Participation enregistrée !** Bonne chance.{bonus}", ephemeral=True)

    # ── tirage
    async def pick_winners(self, gw: dict, count: int, exclude: set[int] = frozenset()) -> list[int]:
        rows = await self.db.all("SELECT user_id, weight FROM gw_entries WHERE giveaway_id=?", (gw["id"],))
        cfg = await self.get_cfg(gw["guild_id"])
        banned = set(cfg["blacklist"])
        pool = [(r["user_id"], r["weight"]) for r in rows if r["user_id"] not in exclude and r["user_id"] not in banned]
        guild = self.bot.get_guild(gw["guild_id"])
        winners: list[int] = []
        for uid in weighted_draw(pool):
            if len(winners) >= count:
                break
            if guild is not None:  # vérifie que le gagnant est toujours là et éligible
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except discord.HTTPException:
                        continue
                if member.bot or check_conditions(member, gw["conditions"]):
                    continue
            winners.append(uid)
        return winners

    async def finish(self, gw_id: int) -> Optional[list[int]]:
        claimed = await self.db.execute(
            "UPDATE giveaways SET status='ended', ended_at=? WHERE id=? AND status='running'", (time.time(), gw_id)
        )
        if not claimed:
            return None
        gw = await self.get_gw(gw_id)
        winners = await self.pick_winners(gw, gw["winners"])
        await self.db.execute("UPDATE giveaways SET winner_ids=? WHERE id=?", (json.dumps(winners), gw_id))
        gw["winner_ids"] = winners
        await self.refresh_message(gw)
        await self.announce(gw, winners)
        await self.dm_winners(gw, winners)
        return winners

    async def reroll(self, gw_id: int, count: int, replace: bool) -> Optional[list[int]]:
        gw = await self.get_gw(gw_id)
        if not gw or gw["status"] != "ended":
            return None
        new = await self.pick_winners(gw, count, exclude=set(gw["winner_ids"]))
        gw["winner_ids"] = new if replace else gw["winner_ids"] + new
        await self.db.execute("UPDATE giveaways SET winner_ids=? WHERE id=?", (json.dumps(gw["winner_ids"]), gw_id))
        await self.refresh_message(gw)
        await self.announce(gw, new, reroll=True)
        await self.dm_winners(gw, new)
        return new

    async def do_action(self, action: str, gw_id: int) -> None:
        if action == "end":
            await self.finish(gw_id)
        elif action == "cancel":
            await self.db.execute(
                "UPDATE giveaways SET status='cancelled', ended_at=? WHERE id=? AND status='running'", (time.time(), gw_id)
            )
            await self.refresh_message(await self.get_gw(gw_id))
        elif action == "delete":
            gw = await self.get_gw(gw_id)
            if gw:
                ch = await self.get_channel(gw["channel_id"])
                if ch and gw["message_id"]:
                    try:
                        await ch.get_partial_message(gw["message_id"]).delete()
                    except discord.HTTPException:
                        pass
                await self.db.execute("DELETE FROM gw_entries WHERE giveaway_id=?", (gw_id,))
                await self.db.execute("DELETE FROM giveaways WHERE id=?", (gw_id,))

    # ── messages
    async def refresh_message(self, gw: Optional[dict]) -> None:
        if not gw or not gw["message_id"]:
            return
        channel = await self.get_channel(gw["channel_id"])
        if channel is None:
            return
        cfg = await self.get_cfg(gw["guild_id"])
        view = JoinView(self, gw["n"], cfg["label"], cfg["emoji"]) if gw["status"] == "running" else closed_view(gw["status"])
        try:
            await channel.get_partial_message(gw["message_id"]).edit(embed=build_embed(gw, cfg), view=view)
        except (discord.NotFound, discord.Forbidden):
            pass
        except discord.HTTPException as exc:
            log.warning("Impossible de mettre à jour le giveaway #%s : %s", gw["id"], exc)

    async def announce(self, gw: dict, winners: list[int], reroll: bool = False) -> None:
        channel = await self.get_channel(gw["channel_id"])
        if channel is None:
            return
        prize = gw["prize"]
        if winners:
            mentions = ", ".join(f"<@{u}>" for u in winners)
            if reroll:
                text = f"🔁 **Nouveau tirage !** Félicitations {mentions}, vous remportez **{prize}** !"
            elif len(winners) == 1:
                text = f"🎉 Félicitations {mentions} ! Tu remportes **{prize}** !"
            else:
                text = f"🎉 Félicitations {mentions} ! Vous remportez **{prize}** !"
        else:
            text = f"😕 Aucun participant valide pour **{prize}**… le giveaway se termine sans gagnant."
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Voir le giveaway", emoji="🎁", url=self.url_of(gw)))
        try:
            await channel.send(
                text, view=view,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                reference=discord.MessageReference(
                    message_id=gw["message_id"], channel_id=gw["channel_id"], guild_id=gw["guild_id"], fail_if_not_exists=False
                ),
            )
        except discord.HTTPException as exc:
            log.warning("Annonce impossible pour #%s : %s", gw["id"], exc)

    async def dm_winners(self, gw: dict, winners: list[int]) -> None:
        cfg = await self.get_cfg(gw["guild_id"])
        if not cfg["dm"] or not winners:
            return
        guild = self.bot.get_guild(gw["guild_id"])
        embed = discord.Embed(
            title="🎉 Tu as gagné !",
            description=(
                f"Tu as remporté **{gw['prize']}**"
                + (f" sur **{guild.name}**" if guild else "")
                + f" !\nContacte l'organisateur <@{gw['host_id']}> pour récupérer ton lot.\n\n[Voir le giveaway]({self.url_of(gw)})"
            ),
            color=discord.Color.gold(),
        )
        for uid in winners:
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                await user.send(embed=embed)
            except discord.HTTPException:
                pass  # MP fermés
            await asyncio.sleep(0.5)

    # ── boucle de fond
    @tasks.loop(seconds=5)
    async def tick(self) -> None:
        try:
            due = await self.db.all("SELECT id FROM giveaways WHERE status='running' AND ends_at<=?", (time.time(),))
            for r in due:
                try:
                    await self.finish(r["id"])
                except Exception:
                    log.exception("Échec de la fin du giveaway #%s", r["id"])
            if self.dirty:
                ids = list(self.dirty)
                self.dirty.clear()
                for gid in ids:
                    gw = await self.get_gw(gid)
                    if gw and gw["status"] == "running":
                        await self.refresh_message(gw)
        except Exception:
            log.exception("Erreur dans la boucle des giveaways")

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    # ── si le message d'un giveaway en cours est supprimé, on l'annule
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self.db is None or payload.guild_id is None:
            return
        await self.db.execute(
            "UPDATE giveaways SET status='cancelled', ended_at=? WHERE message_id=? AND status='running'",
            (time.time(), payload.message_id),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaway(bot))


# ═══════════════════════════ MODE AUTONOME ═══════════════════════════
async def _keep_alive() -> None:
    """Mini serveur web pour que Render/Replit gardent le bot éveillé."""
    from aiohttp import web

    async def ok(_request):
        return web.Response(text="Giveaway bot en ligne ✅")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()


def run_standalone() -> None:
    token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
    if not token:
        raise SystemExit("❌ Variable d'environnement DISCORD_TOKEN manquante (ne mets jamais le token dans le code).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    class GiveawayBot(commands.Bot):
        async def setup_hook(self) -> None:
            await _keep_alive()
            await self.add_cog(Giveaway(self))
            await self.tree.sync()

        async def on_ready(self) -> None:
            log.info("Connecté en tant que %s (%s)", self.user, self.user.id)

    bot = GiveawayBot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
    bot.run(token)


if __name__ == "__main__":
    run_standalone()

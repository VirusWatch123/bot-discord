# -*- coding: utf-8 -*-
"""
boutique.py — Boutique ultra développée, reliée à l'argent du bot (les rubis).

UNE SEULE commande : /shop  → ouvre une interface (même style que /eco) avec :
  • Parcourir la boutique (catégories, pages, achat avec confirmation)
  • Inventaire (+ revente configurable)
  • Populaires, Aide
  • Administration (réservée à l'admin du bot) :
        ➕ créer un article · ✏️ modifier · 🎭 lier à un rôle Discord (permanent ou temporaire)
        🔒 rôle requis · 📦 stock · limite par membre · 🎁 donner / 🧹 retirer un article
        ⚙️ réglages (nom, ouverte/fermée, % de revente, salon de logs)

─── Installation ────────────────────────────────────────────────────────────────
1) Mets ce fichier à côté de ton bot, puis dans ton fichier principal, AVANT tree.sync() :
        await bot.load_extension("boutique")
   (ou lance simplement ce fichier : `python boutique.py`, il démarre tout seul)

2) TOKEN CACHÉ : ne l'écris JAMAIS dans le code. Mets-le dans les « Secrets » de ton
   hébergeur (Replit : onglet Secrets) ou dans un fichier `.env` (jamais partagé) :
        DISCORD_TOKEN=ton_token_ici

3) KEEP ALIVE : démarré automatiquement quand on lance ce fichier seul. Si tu charges
   le fichier comme extension, ton bot a déjà son propre keep alive (ne le double pas).

4) Liaison avec l'argent : le fichier cherche tout seul la table des rubis dans ta base
   SQLite. Si ça ne la trouve pas, renseigne ECO_DB_PATH / ECO_TABLE / ECO_COL_* ci-dessous
   (ou en variables d'environnement).

Dépendances : discord.py >= 2.3   (+ flask optionnel pour le keep alive, python-dotenv optionnel)
"""
from __future__ import annotations

import glob
import logging
import math
import os
import re
import sqlite3
import threading
import time
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("boutique")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
SHOP_DB = os.getenv("SHOP_DB_PATH", "boutique.db")   # base propre à la boutique
ADMIN_IDS = {1572724368029646999}                    # + la personne qui a invité le bot
CURRENCY = "rubis"
EMBED_COLOR = 0xF1C40F
AUTO_SYNC = True        # synchronise /shop automatiquement quand le fichier est chargé en extension

# Liaison avec le système d'argent existant (laisser vide = détection automatique)
ECO_DB_PATH = os.getenv("ECO_DB_PATH", "")           # ex : "economie.db"
ECO_TABLE = os.getenv("ECO_TABLE", "")               # ex : "users"
ECO_COL_USER = os.getenv("ECO_COL_USER", "")         # ex : "user_id"
ECO_COL_GUILD = os.getenv("ECO_COL_GUILD", "")       # ex : "guild_id" (vide = argent global)
ECO_COL_WALLET = os.getenv("ECO_COL_WALLET", "")     # ex : "wallet"
ECO_COL_BANK = os.getenv("ECO_COL_BANK", "")         # ex : "bank" (optionnel)


# ══════════════════════════════════════════════════════════════════════════════
#  OUTILS
# ══════════════════════════════════════════════════════════════════════════════
def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + f" {CURRENCY}"


def parse_int(text: str) -> Optional[int]:
    """Accepte 1500 · 1,500 · 1 500 · 1.500 · 2k · 1,5k · 3m"""
    t = (text or "").strip().lower()
    for ch in (" ", "\u202f", "\xa0", "_", "'"):
        t = t.replace(ch, "")
    if not t:
        return None
    mult = 1
    if t[-1] in "km":
        mult = 1_000 if t[-1] == "k" else 1_000_000
        t = t[:-1]
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", t):
        t = re.sub(r"[.,]", "", t)
    else:
        t = t.replace(",", ".")
    try:
        return int(round(float(t) * mult))
    except ValueError:
        return None


_EMOJI_RE = re.compile("^[\u00a9\u00ae\u203c-\u3299\U0001F000-\U0001FAFF\u200d\ufe0f\u20e3]+$")


def clean_emoji(text: Optional[str], default: str = "🛍️") -> str:
    t = (text or "").strip()
    return t if t and len(t) <= 12 and _EMOJI_RE.match(t) else default


def now() -> int:
    return int(time.time())


# ══════════════════════════════════════════════════════════════════════════════
#  PONT AVEC L'ARGENT DU BOT (rubis)
# ══════════════════════════════════════════════════════════════════════════════
class EcoBridge:
    USER_COLS = ("user_id", "userid", "member_id", "discord_id", "uid", "id")
    GUILD_COLS = ("guild_id", "server_id", "guild")
    WALLET_COLS = ("wallet", "portefeuille", "cash", "balance", "rubis", "money",
                   "argent", "solde", "coins", "pocket")
    BANK_COLS = ("bank", "banque", "bank_balance", "deposit", "deposits")
    TABLE_HINTS = ("eco", "bal", "user", "member", "account", "wallet", "compte", "joueur", "player")

    def __init__(self):
        self.path = self.table = self.c_user = self.c_guild = self.c_wallet = self.c_bank = None
        self.ready = False
        try:
            self.detect()
        except Exception:
            log.exception("Détection de l'économie impossible")

    # ── détection ───────────────────────────────────────────────────────────
    def detect(self):
        if ECO_DB_PATH:
            paths = [ECO_DB_PATH]
        else:
            paths = []
            for d in {os.getcwd(), os.path.dirname(os.path.abspath(__file__))}:
                for pat in ("*.db", "*.sqlite", "*.sqlite3"):
                    paths += glob.glob(os.path.join(d, pat))
        shop_abs = os.path.abspath(SHOP_DB)
        paths = [p for p in dict.fromkeys(paths) if os.path.abspath(p) != shop_abs and os.path.exists(p)]

        best = None  # (score, path, table, user, guild, wallet, bank)
        for p in paths:
            try:
                con = sqlite3.connect(p)
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for t in tables:
                    if ECO_TABLE and t != ECO_TABLE:
                        continue
                    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
                    low = {c.lower(): c for c in cols}
                    user = ECO_COL_USER or next((low[c] for c in self.USER_COLS if c in low), None)
                    wallet = ECO_COL_WALLET or next((low[c] for c in self.WALLET_COLS if c in low), None)
                    if not user or not wallet:
                        continue
                    guild = ECO_COL_GUILD or next((low[c] for c in self.GUILD_COLS if c in low), None)
                    bank = ECO_COL_BANK or next((low[c] for c in self.BANK_COLS if c in low), None)
                    score = (3 if any(h in t.lower() for h in self.TABLE_HINTS) else 0) \
                        + (2 if bank else 0) + (1 if user.lower() != "id" else 0)
                    if best is None or score > best[0]:
                        best = (score, p, t, user, guild, wallet, bank)
                con.close()
            except sqlite3.Error:
                continue
        if best:
            _, self.path, self.table, self.c_user, self.c_guild, self.c_wallet, self.c_bank = best
            self.ready = True
            log.info("Économie liée : %s → %s (user=%s, guild=%s, wallet=%s, bank=%s)",
                     self.path, self.table, self.c_user, self.c_guild, self.c_wallet, self.c_bank)
        else:
            log.warning("Aucune table d'argent trouvée : renseigne ECO_DB_PATH / ECO_TABLE.")

    def describe(self) -> str:
        if not self.ready:
            return "non détectée"
        return f"{self.path} → {self.table} ({self.c_wallet})"

    # ── accès ───────────────────────────────────────────────────────────────
    def _con(self):
        return sqlite3.connect(self.path, timeout=15)

    def _where(self, gid: int, uid: int):
        if self.c_guild:
            return f'"{self.c_user}"=? AND "{self.c_guild}"=?', (uid, gid)
        return f'"{self.c_user}"=?', (uid,)

    def balance(self, gid: int, uid: int) -> tuple[int, int]:
        if not self.ready:
            return 0, 0
        w, p = self._where(gid, uid)
        bank = f', "{self.c_bank}"' if self.c_bank else ", 0"
        con = self._con()
        try:
            row = con.execute(f'SELECT "{self.c_wallet}"{bank} FROM "{self.table}" WHERE {w}', p).fetchone()
        finally:
            con.close()
        return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

    def rank(self, gid: int, uid: int) -> Optional[int]:
        if not self.ready:
            return None
        total = f'COALESCE("{self.c_wallet}",0)' + (f' + COALESCE("{self.c_bank}",0)' if self.c_bank else "")
        wal, bank = self.balance(gid, uid)
        flt, params = ("", ())
        if self.c_guild:
            flt, params = f' AND "{self.c_guild}"=?', (gid,)
        con = self._con()
        try:
            row = con.execute(f'SELECT COUNT(*)+1 FROM "{self.table}" WHERE ({total}) > ?{flt}',
                              (wal + bank, *params)).fetchone()
        finally:
            con.close()
        return int(row[0]) if row else None

    def debit(self, gid: int, uid: int, amount: int) -> bool:
        """Retire `amount` du portefeuille, de façon atomique. False si solde insuffisant."""
        if not self.ready:
            return False
        if amount <= 0:
            return True
        w, p = self._where(gid, uid)
        con = self._con()
        try:
            cur = con.execute(
                f'UPDATE "{self.table}" SET "{self.c_wallet}"="{self.c_wallet}"-? '
                f'WHERE {w} AND "{self.c_wallet}">=?', (amount, *p, amount))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    def credit(self, gid: int, uid: int, amount: int) -> None:
        if not self.ready or amount <= 0:
            return
        w, p = self._where(gid, uid)
        con = self._con()
        try:
            cur = con.execute(
                f'UPDATE "{self.table}" SET "{self.c_wallet}"=COALESCE("{self.c_wallet}",0)+? WHERE {w}',
                (amount, *p))
            if cur.rowcount == 0:
                cols = [self.c_user] + ([self.c_guild] if self.c_guild else []) + [self.c_wallet]
                vals = [uid] + ([gid] if self.c_guild else []) + [amount]
                con.execute(
                    f'INSERT INTO "{self.table}" ({",".join(chr(34) + c + chr(34) for c in cols)}) '
                    f'VALUES ({",".join("?" * len(cols))})', vals)
            con.commit()
        finally:
            con.close()


# ══════════════════════════════════════════════════════════════════════════════
#  BASE DE DONNÉES DE LA BOUTIQUE
# ══════════════════════════════════════════════════════════════════════════════
def _conn():
    con = sqlite3.connect(SHOP_DB, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def q_all(sql, params=()):
    con = _conn()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def q_one(sql, params=()):
    con = _conn()
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def q_exec(sql, params=()):
    con = _conn()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.rowcount, cur.lastrowid
    finally:
        con.close()


def init_db():
    con = _conn()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shop_config(
            guild_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'Boutique',
            is_open INTEGER NOT NULL DEFAULT 1,
            refund_pct INTEGER NOT NULL DEFAULT 50,
            log_channel INTEGER
        );
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price INTEGER NOT NULL DEFAULT 100,
            emoji TEXT NOT NULL DEFAULT '🛍️',
            category TEXT NOT NULL DEFAULT 'Général',
            role_id INTEGER,
            duration_days INTEGER NOT NULL DEFAULT 0,
            required_role_id INTEGER,
            stock INTEGER NOT NULL DEFAULT -1,
            max_per_user INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            sold INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_items_guild ON items(guild_id);
        CREATE TABLE IF NOT EXISTS inventory(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS role_grants(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(guild_id, user_id, role_id)
        );
        CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            total INTEGER NOT NULL,
            ts INTEGER NOT NULL
        );
        """)
        con.commit()
    finally:
        con.close()


def get_cfg(gid: int) -> sqlite3.Row:
    row = q_one("SELECT * FROM shop_config WHERE guild_id=?", (gid,))
    if not row:
        q_exec("INSERT OR IGNORE INTO shop_config(guild_id) VALUES(?)", (gid,))
        row = q_one("SELECT * FROM shop_config WHERE guild_id=?", (gid,))
    return row


def set_cfg(gid: int, **kw):
    get_cfg(gid)
    sets = ", ".join(f"{k}=?" for k in kw)
    q_exec(f"UPDATE shop_config SET {sets} WHERE guild_id=?", (*kw.values(), gid))


ITEM_FIELDS = {"name", "description", "price", "emoji", "category", "role_id", "duration_days",
               "required_role_id", "stock", "max_per_user", "enabled"}


def list_items(gid: int, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM items WHERE guild_id=?" + (" AND enabled=1" if enabled_only else "")
    sql += " ORDER BY category COLLATE NOCASE, price, id"
    return [dict(r) for r in q_all(sql, (gid,))]


def get_item(item_id: int) -> Optional[dict]:
    r = q_one("SELECT * FROM items WHERE id=?", (item_id,))
    return dict(r) if r else None


def create_item(gid, name, price, description, emoji, stock) -> int:
    _, rid = q_exec("INSERT INTO items(guild_id,name,price,description,emoji,stock) VALUES(?,?,?,?,?,?)",
                    (gid, name, price, description, emoji, stock))
    return rid


def update_item(item_id: int, **kw):
    kw = {k: v for k, v in kw.items() if k in ITEM_FIELDS}
    if kw:
        q_exec(f"UPDATE items SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), item_id))


def delete_item(item_id: int):
    q_exec("DELETE FROM inventory WHERE item_id=?", (item_id,))
    q_exec("DELETE FROM items WHERE id=?", (item_id,))


def reserve_stock(item_id: int, qty: int) -> bool:
    n, _ = q_exec("UPDATE items SET stock=CASE WHEN stock=-1 THEN -1 ELSE stock-? END, sold=sold+? "
                  "WHERE id=? AND (stock=-1 OR stock>=?)", (qty, qty, item_id, qty))
    return n > 0


def release_stock(item_id: int, qty: int):
    q_exec("UPDATE items SET stock=CASE WHEN stock=-1 THEN -1 ELSE stock+? END, sold=MAX(sold-?,0) WHERE id=?",
           (qty, qty, item_id))


def inv_list(gid: int, uid: int) -> list[dict]:
    rows = q_all("SELECT i.*, v.qty AS qty FROM inventory v JOIN items i ON i.id=v.item_id "
                 "WHERE v.guild_id=? AND v.user_id=? AND v.qty>0 ORDER BY i.name COLLATE NOCASE", (gid, uid))
    return [dict(r) for r in rows]


def inv_qty(gid: int, uid: int, item_id: int) -> int:
    r = q_one("SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?", (gid, uid, item_id))
    return int(r["qty"]) if r else 0


def inv_add(gid, uid, item_id, qty):
    q_exec("INSERT INTO inventory(guild_id,user_id,item_id,qty) VALUES(?,?,?,?) "
           "ON CONFLICT(guild_id,user_id,item_id) DO UPDATE SET qty=qty+excluded.qty", (gid, uid, item_id, qty))


def inv_set(gid, uid, item_id, qty):
    q_exec("INSERT INTO inventory(guild_id,user_id,item_id,qty) VALUES(?,?,?,?) "
           "ON CONFLICT(guild_id,user_id,item_id) DO UPDATE SET qty=excluded.qty", (gid, uid, item_id, qty))


def inv_remove(gid, uid, item_id, qty):
    q_exec("UPDATE inventory SET qty=qty-? WHERE guild_id=? AND user_id=? AND item_id=?", (qty, gid, uid, item_id))
    q_exec("DELETE FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty<=0", (gid, uid, item_id))


def grant_get(gid, uid, role_id) -> Optional[int]:
    r = q_one("SELECT expires_at FROM role_grants WHERE guild_id=? AND user_id=? AND role_id=?", (gid, uid, role_id))
    return int(r["expires_at"]) if r else None


def grant_set(gid, uid, role_id, expires_at):
    q_exec("INSERT INTO role_grants(guild_id,user_id,role_id,expires_at) VALUES(?,?,?,?) "
           "ON CONFLICT(guild_id,user_id,role_id) DO UPDATE SET expires_at=excluded.expires_at",
           (gid, uid, role_id, expires_at))


def grant_delete(gid, uid, role_id):
    q_exec("DELETE FROM role_grants WHERE guild_id=? AND user_id=? AND role_id=?", (gid, uid, role_id))


def log_purchase(gid, uid, item_id, name, qty, total):
    q_exec("INSERT INTO purchases(guild_id,user_id,item_id,item_name,qty,total,ts) VALUES(?,?,?,?,?,?,?)",
           (gid, uid, item_id, name, qty, total, now()))


def shop_stats(gid: int) -> dict:
    r = q_one("SELECT COUNT(*) AS n, COALESCE(SUM(qty),0) AS q, COALESCE(SUM(total),0) AS t "
              "FROM purchases WHERE guild_id=?", (gid,))
    top = [dict(x) for x in q_all("SELECT name, emoji, sold FROM items WHERE guild_id=? AND sold>0 "
                                  "ORDER BY sold DESC LIMIT 5", (gid,))]
    return {"orders": r["n"], "units": r["q"], "revenue": r["t"], "top": top}


# ══════════════════════════════════════════════════════════════════════════════
#  EMBEDS
# ══════════════════════════════════════════════════════════════════════════════
def tags(item: dict) -> list[str]:
    t = []
    if item["role_id"]:
        t.append("🎭 rôle" + (f" · {item['duration_days']} j" if item["duration_days"] else ""))
    if item["stock"] == 0:
        t.append("🚫 rupture")
    elif item["stock"] > 0:
        t.append(f"📦 {item['stock']}")
    if not item["enabled"]:
        t.append("⛔ désactivé")
    return t


def item_embed(guild: discord.Guild, item: dict, owned: int = 0, expires: Optional[int] = None,
               admin: bool = False) -> discord.Embed:
    e = discord.Embed(title=f"{item['emoji']} {item['name']}",
                      description=item["description"] or "*Aucune description.*", color=EMBED_COLOR)
    e.add_field(name="Prix", value=fmt(item["price"]))
    if item["role_id"]:
        e.add_field(name="Type", value=f"🎭 Rôle <@&{item['role_id']}>")
        e.add_field(name="Durée", value=f"{item['duration_days']} jour(s)" if item["duration_days"] else "Permanent")
    else:
        e.add_field(name="Type", value="📦 Objet")
        e.add_field(name="Limite / membre", value=str(item["max_per_user"]) if item["max_per_user"] else "Aucune")
    e.add_field(name="Stock", value="∞" if item["stock"] < 0 else str(item["stock"]))
    e.add_field(name="Catégorie", value=item["category"])
    e.add_field(name="Vendus", value=str(item["sold"]))
    if item["required_role_id"]:
        e.add_field(name="Rôle requis", value=f"🔒 <@&{item['required_role_id']}>")
    if owned:
        e.add_field(name="Tu possèdes", value=f"×{owned}")
    if expires:
        e.add_field(name="Expire", value=f"<t:{expires}:R>")
    if admin:
        e.set_footer(text=f"ID {item['id']} · {'🟢 visible en boutique' if item['enabled'] else '🔴 masqué'}")
    return e


# ══════════════════════════════════════════════════════════════════════════════
#  COMPOSANTS D'INTERFACE
# ══════════════════════════════════════════════════════════════════════════════
class BaseView(discord.ui.View):
    def __init__(self, cog: "ShopCog", user: discord.abc.User, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user = user
        self.origin: Optional[discord.Interaction] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Ce menu n'est pas le tien : tape **/shop**.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.origin:
            try:
                await self.origin.edit_original_response(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        log.exception("Erreur d'interface boutique", exc_info=error)
        msg = "❌ Une erreur est survenue. Réessaie avec **/shop**."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def show(interaction: discord.Interaction, embed: discord.Embed, view: Optional[BaseView] = None,
               old: Optional[discord.ui.View] = None):
    """Remplace le contenu du message de l'interface (bouton, menu ou formulaire)."""
    if old is not None and old is not view:
        old.stop()
    if view is not None:
        view.origin = interaction
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
        return
    try:
        await interaction.response.edit_message(embed=embed, view=view)
    except discord.HTTPException:
        pass
    if not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class Btn(discord.ui.Button):
    def __init__(self, label: str, cb: Callable[..., Awaitable], style=discord.ButtonStyle.secondary,
                 emoji: Optional[str] = None, row: Optional[int] = None, disabled: bool = False):
        super().__init__(label=label, style=style, emoji=emoji, row=row, disabled=disabled)
        self._cb = cb

    async def callback(self, interaction: discord.Interaction):
        await self._cb(interaction, self.view)


class Sel(discord.ui.Select):
    def __init__(self, placeholder: str, options: list[discord.SelectOption], cb, row: Optional[int] = None):
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)
        self._cb = cb

    async def callback(self, interaction: discord.Interaction):
        await self._cb(interaction, self.values[0], self.view)


class RoleSel(discord.ui.RoleSelect):
    def __init__(self, placeholder: str, cb, row: Optional[int] = None):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row)
        self._cb = cb

    async def callback(self, interaction: discord.Interaction):
        await self._cb(interaction, self.values[0], self.view)


class UserSel(discord.ui.UserSelect):
    def __init__(self, placeholder: str, cb, row: Optional[int] = None):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row)
        self._cb = cb

    async def callback(self, interaction: discord.Interaction):
        await self._cb(interaction, self.values[0], self.view)


class ChanSel(discord.ui.ChannelSelect):
    def __init__(self, placeholder: str, cb, row: Optional[int] = None):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row,
                         channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        self._cb = cb

    async def callback(self, interaction: discord.Interaction):
        await self._cb(interaction, self.values[0], self.view)


class ItemListView(BaseView):
    """Liste paginée d'articles avec menu de sélection et filtre par catégorie."""
    PAGE_SIZE = 6

    def __init__(self, cog, user, *, title: str, fetch: Callable[[], list[dict]], on_pick, on_back,
                 empty: str, intro: str = "", footer: Callable[[], str] = lambda: "", categories: bool = True):
        super().__init__(cog, user)
        self.title, self.fetch, self.on_pick, self.on_back = title, fetch, on_pick, on_back
        self.empty, self.intro, self.footer, self.use_cat = empty, intro, footer, categories
        self.page = 0
        self.category: Optional[str] = None
        self.build()

    def filtered(self) -> list[dict]:
        items = self.fetch()
        return [i for i in items if i["category"] == self.category] if self.category else items

    def pages(self) -> int:
        return max(1, math.ceil(len(self.filtered()) / self.PAGE_SIZE))

    def chunk(self) -> list[dict]:
        items = self.filtered()
        self.page = max(0, min(self.page, self.pages() - 1))
        return items[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]

    def embed(self) -> discord.Embed:
        chunk = self.chunk()
        e = discord.Embed(title=self.title, color=EMBED_COLOR)
        lines = []
        for i in chunk:
            extra = " · ".join(tags(i))
            qty = f" ×{i['qty']}" if "qty" in i else ""
            lines.append(f"{i['emoji']} **{i['name']}**{qty} — {fmt(i['price'])}" + (f"\n└ {extra}" if extra else ""))
        body = "\n".join(lines) if lines else self.empty
        e.description = (self.intro + "\n\n" if self.intro else "") + body
        foot = self.footer()
        cat = f" · Catégorie : {self.category}" if self.category else ""
        e.set_footer(text=f"Page {self.page + 1}/{self.pages()}{cat}" + (f" · {foot}" if foot else ""))
        return e

    def build(self):
        self.clear_items()
        chunk = self.chunk()
        if chunk:
            opts = [discord.SelectOption(label=i["name"][:100], value=str(i["id"]),
                                         description=f"{fmt(i['price'])} · {i['category']}"[:100],
                                         emoji=clean_emoji(i["emoji"])) for i in chunk]
            self.add_item(Sel("Choisis un article…", opts, self._pick, row=0))
        if self.use_cat:
            cats = sorted({i["category"] for i in self.fetch()}, key=str.lower)[:24]
            if len(cats) > 1:
                opts = [discord.SelectOption(label="Toutes les catégories", value="__all__",
                                             default=self.category is None)]
                opts += [discord.SelectOption(label=c[:100], value=c, default=(c == self.category)) for c in cats]
                self.add_item(Sel("Filtrer par catégorie", opts, self._cat, row=1))
        pages = self.pages()
        self.add_item(Btn("", self._prev, emoji="◀️", row=2, disabled=self.page <= 0))
        self.add_item(Btn(f"{self.page + 1}/{pages}", self._noop, row=2, disabled=True))
        self.add_item(Btn("", self._next, emoji="▶️", row=2, disabled=self.page >= pages - 1))
        self.add_item(Btn("Retour", self.on_back, style=discord.ButtonStyle.danger, emoji="↩️", row=2))

    async def _noop(self, i, v):
        pass

    async def _prev(self, i, v):
        self.page -= 1
        self.build()
        await show(i, self.embed(), self)

    async def _next(self, i, v):
        self.page += 1
        self.build()
        await show(i, self.embed(), self)

    async def _cat(self, i, value, v):
        self.category = None if value == "__all__" else value
        self.page = 0
        self.build()
        await show(i, self.embed(), self)

    async def _pick(self, i, value, v):
        item = get_item(int(value))
        if not item:
            self.build()
            return await show(i, self.embed(), self)
        await self.on_pick(i, item, self)


class ConfirmView(BaseView):
    def __init__(self, cog, user, on_yes, on_no, yes_label="Confirmer", yes_style=discord.ButtonStyle.success):
        super().__init__(cog, user, timeout=120)
        self.add_item(Btn(yes_label, on_yes, style=yes_style, emoji="✅"))
        self.add_item(Btn("Annuler", on_no, style=discord.ButtonStyle.secondary, emoji="✖️"))


# ── Formulaires ──────────────────────────────────────────────────────────────
class ItemInfoModal(discord.ui.Modal):
    def __init__(self, cog: "ShopCog", parent: discord.ui.View, item: Optional[dict]):
        super().__init__(title="Modifier l'article" if item else "Nouvel article")
        self.cog, self.parent, self.item = cog, parent, item
        self.f_name = discord.ui.TextInput(label="Nom", max_length=80, default=item["name"] if item else None)
        self.f_price = discord.ui.TextInput(label="Prix en rubis (ex : 1,500 ou 2k)", max_length=15,
                                            default=str(item["price"]) if item else "100")
        self.f_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False,
                                           max_length=300, default=item["description"] if item else None)
        self.f_emoji = discord.ui.TextInput(label="Emoji (optionnel)", required=False, max_length=12,
                                            default=item["emoji"] if item else None)
        self.f_stock = discord.ui.TextInput(label="Stock (vide = illimité)", required=False, max_length=12,
                                            default=(str(item["stock"]) if item and item["stock"] >= 0 else None))
        for f in (self.f_name, self.f_price, self.f_desc, self.f_emoji, self.f_stock):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.f_name.value.strip()
        price = parse_int(self.f_price.value)
        if not name or price is None or not (0 <= price <= 10 ** 12):
            return await interaction.response.send_message(
                "❌ Nom ou prix invalide (le prix doit être un nombre positif).", ephemeral=True)
        txt = self.f_stock.value.strip().lower()
        stock = -1 if txt in ("", "∞", "illimité", "illimite", "-1") else parse_int(txt)
        if stock is None or stock < -1:
            return await interaction.response.send_message("❌ Stock invalide.", ephemeral=True)
        emoji = clean_emoji(self.f_emoji.value)
        desc = self.f_desc.value.strip()
        if self.item:
            update_item(self.item["id"], name=name, price=price, description=desc, emoji=emoji, stock=stock)
            item_id = self.item["id"]
        else:
            item_id = create_item(interaction.guild.id, name, price, desc, emoji, stock)
        await self.cog.open_editor(interaction, item_id, old=self.parent)


class ItemOptionsModal(discord.ui.Modal):
    def __init__(self, cog: "ShopCog", parent: discord.ui.View, item: dict):
        super().__init__(title="Options de l'article")
        self.cog, self.parent, self.item = cog, parent, item
        self.f_cat = discord.ui.TextInput(label="Catégorie", max_length=40, default=item["category"])
        self.f_days = discord.ui.TextInput(label="Durée du rôle en jours (0 = permanent)", max_length=6,
                                           default=str(item["duration_days"]), required=False)
        self.f_max = discord.ui.TextInput(label="Limite par membre (0 = illimité)", max_length=6,
                                          default=str(item["max_per_user"]), required=False)
        for f in (self.f_cat, self.f_days, self.f_max):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        days = parse_int(self.f_days.value or "0")
        mx = parse_int(self.f_max.value or "0")
        if days is None or mx is None or days < 0 or mx < 0 or days > 3650:
            return await interaction.response.send_message("❌ Valeurs invalides (nombres positifs).", ephemeral=True)
        update_item(self.item["id"], category=self.f_cat.value.strip() or "Général",
                    duration_days=days, max_per_user=mx)
        await self.cog.open_editor(interaction, self.item["id"], old=self.parent)


class QtyModal(discord.ui.Modal):
    def __init__(self, parent: discord.ui.View, title: str, on_qty):
        super().__init__(title=title)
        self.parent, self.on_qty = parent, on_qty
        self.f_qty = discord.ui.TextInput(label="Quantité", default="1", max_length=6)
        self.add_item(self.f_qty)

    async def on_submit(self, interaction: discord.Interaction):
        qty = parse_int(self.f_qty.value)
        if qty is None or not (1 <= qty <= 1000):
            return await interaction.response.send_message("❌ Quantité invalide (1 à 1 000).", ephemeral=True)
        await self.on_qty(interaction, qty, self.parent)


class TextModal(discord.ui.Modal):
    def __init__(self, parent: discord.ui.View, title: str, label: str, default: str, on_value, max_length=40):
        super().__init__(title=title)
        self.parent, self.on_value = parent, on_value
        self.f = discord.ui.TextInput(label=label, default=default, max_length=max_length)
        self.add_item(self.f)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_value(interaction, self.f.value.strip(), self.parent)


# ══════════════════════════════════════════════════════════════════════════════
#  COG
# ══════════════════════════════════════════════════════════════════════════════
class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.eco = EcoBridge()

    async def cog_load(self):
        init_db()
        self.expire_roles.start()

    async def cog_unload(self):
        self.expire_roles.cancel()

    # ── droits ──────────────────────────────────────────────────────────────
    async def is_admin(self, user: discord.abc.User) -> bool:
        if user.id in ADMIN_IDS:
            return True
        try:
            return await self.bot.is_owner(user)
        except Exception:
            return False

    @staticmethod
    def can_manage(guild: discord.Guild, role: discord.Role) -> tuple[bool, str]:
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return False, "Il me manque la permission « Gérer les rôles »."
        if role.is_default() or role.managed:
            return False, "Ce rôle est un rôle système/intégration : impossible de l'attribuer."
        if role >= me.top_role:
            return False, f"Mon rôle doit être placé AU-DESSUS de {role.mention} dans la liste des rôles du serveur."
        return True, ""

    # ── commande unique ─────────────────────────────────────────────────────
    @app_commands.command(name="shop", description="Ouvre la boutique (achats, inventaire, administration)")
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction):
        embed, view = await self.build_home(interaction.user)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.origin = interaction

    # ── tâche : retire les rôles temporaires expirés ───────────────────────
    @tasks.loop(seconds=60)
    async def expire_roles(self):
        rows = q_all("SELECT * FROM role_grants WHERE expires_at<=?", (now(),))
        for g in rows:
            guild = self.bot.get_guild(g["guild_id"])
            if guild is None:
                continue
            role = guild.get_role(g["role_id"])
            try:
                member = guild.get_member(g["user_id"]) or await guild.fetch_member(g["user_id"])
            except discord.NotFound:
                member = None
            except discord.HTTPException:
                continue
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Boutique : rôle temporaire expiré")
                except discord.HTTPException:
                    continue  # on réessaiera à la prochaine minute
            grant_delete(g["guild_id"], g["user_id"], g["role_id"])
            q_exec("DELETE FROM inventory WHERE guild_id=? AND user_id=? AND item_id IN "
                   "(SELECT id FROM items WHERE role_id=?)", (g["guild_id"], g["user_id"], g["role_id"]))

    @expire_roles.before_loop
    async def _before_expire(self):
        await self.bot.wait_until_ready()

    # ══════════════════════════════════════════════════════════════════════
    #  LOGIQUE MÉTIER
    # ══════════════════════════════════════════════════════════════════════
    async def purchase(self, member: discord.Member, item_id: int, qty: int) -> tuple[bool, str]:
        guild = member.guild
        cfg = get_cfg(guild.id)
        if not self.eco.ready:
            return False, "⚠️ Le système d'argent n'est pas relié : préviens un admin."
        if not cfg["is_open"]:
            return False, "🔒 La boutique est fermée pour le moment."
        item = get_item(item_id)
        if not item or item["guild_id"] != guild.id or not item["enabled"]:
            return False, "❌ Cet article n'est plus disponible."
        is_role = item["role_id"] is not None
        qty = 1 if is_role else max(1, min(qty, 1000))
        if item["required_role_id"] and not any(r.id == item["required_role_id"] for r in member.roles):
            return False, f"🔒 Il te faut le rôle <@&{item['required_role_id']}> pour acheter ça."
        role = None
        if is_role:
            role = guild.get_role(item["role_id"])
            if role is None:
                return False, "❌ Le rôle lié à cet article n'existe plus : préviens un admin."
            ok, why = self.can_manage(guild, role)
            if not ok:
                return False, f"❌ {why}"
            if role in member.roles and not item["duration_days"]:
                return False, "✅ Tu as déjà ce rôle."
        elif item["max_per_user"] > 0 and inv_qty(guild.id, member.id, item_id) + qty > item["max_per_user"]:
            return False, f"🚫 Limite de **{item['max_per_user']}** par membre atteinte pour cet article."

        total = item["price"] * qty
        if not self.eco.debit(guild.id, member.id, total):
            wallet, _ = self.eco.balance(guild.id, member.id)
            return False, f"💸 Il te manque **{fmt(total - wallet)}** dans ton portefeuille."
        if not reserve_stock(item_id, qty):
            self.eco.credit(guild.id, member.id, total)
            return False, "📦 Rupture de stock, tu n'as pas été débité."

        expires = None
        if role:
            try:
                await member.add_roles(role, reason=f"Achat boutique : {item['name']}")
            except discord.HTTPException:
                self.eco.credit(guild.id, member.id, total)
                release_stock(item_id, qty)
                return False, "❌ Je n'ai pas pu donner le rôle : tu as été remboursé."
            if item["duration_days"]:
                base = max(grant_get(guild.id, member.id, role.id) or 0, now())
                expires = base + item["duration_days"] * 86400
                grant_set(guild.id, member.id, role.id, expires)
            inv_set(guild.id, member.id, item_id, 1)
        else:
            inv_add(guild.id, member.id, item_id, qty)
        log_purchase(guild.id, member.id, item_id, item["name"], qty, total)
        await self.send_log(guild, cfg, member, f"🛒 **{member}** a acheté **{qty}× {item['emoji']} {item['name']}** "
                                                f"pour **{fmt(total)}**" + (f" (rôle {role.mention})" if role else
                                                                           " — **à livrer si nécessaire**"))
        msg = f"✅ Tu as acheté **{qty}× {item['emoji']} {item['name']}** pour **{fmt(total)}**."
        if role:
            msg += f"\n🎭 Rôle {role.mention} attribué" + (f" jusqu'à <t:{expires}:R>." if expires else ".")
        else:
            msg += "\n🎒 Ajouté à ton inventaire."
        return True, msg

    async def refund(self, member: discord.Member, item_id: int) -> tuple[bool, str]:
        guild = member.guild
        cfg = get_cfg(guild.id)
        item = get_item(item_id)
        if not item or inv_qty(guild.id, member.id, item_id) < 1:
            return False, "❌ Tu ne possèdes pas cet article."
        if cfg["refund_pct"] <= 0:
            return False, "❌ La revente est désactivée."
        amount = item["price"] * cfg["refund_pct"] // 100
        if item["role_id"]:
            role = guild.get_role(item["role_id"])
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Revente boutique")
                except discord.HTTPException:
                    return False, "❌ Je n'ai pas pu retirer le rôle (ma position est trop basse ?)."
            grant_delete(guild.id, member.id, item["role_id"])
            inv_remove(guild.id, member.id, item_id, 1)
        else:
            inv_remove(guild.id, member.id, item_id, 1)
        self.eco.credit(guild.id, member.id, amount)
        await self.send_log(guild, cfg, member, f"💸 **{member}** a revendu **{item['emoji']} {item['name']}** "
                                                f"et récupère **{fmt(amount)}**")
        return True, f"✅ **{item['emoji']} {item['name']}** revendu : **+{fmt(amount)}**."

    async def admin_apply(self, guild: discord.Guild, mode: str, member: discord.Member, item_id: int,
                          qty: int) -> str:
        item = get_item(item_id)
        if not item:
            return "❌ Article introuvable."
        role = guild.get_role(item["role_id"]) if item["role_id"] else None
        if mode == "give":
            if item["role_id"]:
                if role is None:
                    return "❌ Le rôle lié n'existe plus."
                ok, why = self.can_manage(guild, role)
                if not ok:
                    return f"❌ {why}"
                try:
                    await member.add_roles(role, reason="Boutique : don admin")
                except discord.HTTPException:
                    return "❌ Impossible de donner le rôle."
                if item["duration_days"]:
                    grant_set(guild.id, member.id, role.id, now() + item["duration_days"] * 86400)
                inv_set(guild.id, member.id, item_id, 1)
                return f"🎁 {member.mention} reçoit {item['emoji']} **{item['name']}** (rôle {role.mention})."
            inv_add(guild.id, member.id, item_id, qty)
            return f"🎁 {member.mention} reçoit **{qty}× {item['emoji']} {item['name']}**."
        # retirer
        if inv_qty(guild.id, member.id, item_id) < 1 and not (role and role in member.roles):
            return f"❌ {member.mention} ne possède pas cet article."
        if role:
            if role in member.roles:
                try:
                    await member.remove_roles(role, reason="Boutique : retrait admin")
                except discord.HTTPException:
                    return "❌ Impossible de retirer le rôle."
            grant_delete(guild.id, member.id, role.id)
            inv_remove(guild.id, member.id, item_id, 1)
            return f"🧹 {item['emoji']} **{item['name']}** retiré à {member.mention} (rôle {role.mention} enlevé)."
        inv_remove(guild.id, member.id, item_id, qty)
        return f"🧹 **{qty}× {item['emoji']} {item['name']}** retiré(s) à {member.mention}."

    async def send_log(self, guild: discord.Guild, cfg, member, text: str):
        if not cfg["log_channel"]:
            return
        ch = guild.get_channel(cfg["log_channel"])
        if ch is None:
            return
        try:
            await ch.send(embed=discord.Embed(description=text, color=EMBED_COLOR, timestamp=discord.utils.utcnow()),
                          allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ══════════════════════════════════════════════════════════════════════
    #  ÉCRANS
    # ══════════════════════════════════════════════════════════════════════
    # ── Accueil ─────────────────────────────────────────────────────────────
    async def build_home(self, member: discord.Member):
        guild = member.guild
        cfg = get_cfg(guild.id)
        wallet, bank = self.eco.balance(guild.id, member.id)
        rank = self.eco.rank(guild.id, member.id)
        n_items = len(list_items(guild.id, enabled_only=True))
        owned = sum(i["qty"] for i in inv_list(guild.id, member.id))
        e = discord.Embed(title=f"🛒 {cfg['name']} : {member.display_name}",
                          description="Choisis une action avec les boutons ci-dessous.", color=EMBED_COLOR)
        e.add_field(name="Portefeuille", value=fmt(wallet))
        e.add_field(name="Articles", value=str(n_items))
        e.add_field(name="Inventaire", value=f"{owned} objet(s)")
        if not self.eco.ready:
            e.add_field(name="⚠️ Argent non relié", inline=False,
                        value="Le système de rubis n'a pas été trouvé : renseigne `ECO_DB_PATH` / `ECO_TABLE` "
                              "dans `boutique.py`.")
        status = "🟢 Boutique ouverte" if cfg["is_open"] else "🔒 Boutique fermée"
        refund = f"Revente à {cfg['refund_pct']} %" if cfg["refund_pct"] > 0 else "Revente désactivée"
        e.set_footer(text=f"{status} · {refund}" + (f" · Classement #{rank}" if rank else ""))

        v = BaseView(self, member)
        v.add_item(Btn("Parcourir la boutique", self._h_browse, discord.ButtonStyle.success, "🛍️", row=0))
        v.add_item(Btn("Inventaire", self._h_inv, discord.ButtonStyle.primary, "🎒", row=0))
        v.add_item(Btn("Populaires", self._h_pop, discord.ButtonStyle.secondary, "🔥", row=0))
        v.add_item(Btn("Aide", self._h_help, discord.ButtonStyle.secondary, "❓", row=1))
        if await self.is_admin(member):
            v.add_item(Btn("Administration", self._h_admin, discord.ButtonStyle.secondary, "⚙️", row=1))
        v.add_item(Btn("Fermer", self._h_close, discord.ButtonStyle.danger, "✖️", row=1))
        return e, v

    async def open_home(self, interaction, old=None):
        e, v = await self.build_home(interaction.user)
        await show(interaction, e, v, old=old)

    async def _h_browse(self, i, v): await self.open_browse(i, old=v)
    async def _h_inv(self, i, v): await self.open_inventory(i, old=v)
    async def _h_pop(self, i, v): await self.open_popular(i, old=v)
    async def _h_help(self, i, v): await self.open_help(i, old=v)
    async def _h_admin(self, i, v): await self.open_admin(i, old=v)

    async def _h_close(self, i, v):
        v.stop()
        await i.response.edit_message(content="🛒 Boutique fermée. Tape **/shop** pour la rouvrir.",
                                      embed=None, view=None)

    async def _back_home(self, i, v):
        await self.open_home(i, old=v)

    # ── Aide / Populaires ──────────────────────────────────────────────────
    async def open_help(self, interaction, old=None):
        cfg = get_cfg(interaction.guild.id)
        e = discord.Embed(title="❓ Aide de la boutique", color=EMBED_COLOR, description=(
            f"• **Parcourir** : choisis un article, puis **Acheter**. Tu paies avec ton **portefeuille** ({CURRENCY}).\n"
            "• **Rôles** : certains articles donnent un rôle Discord (parfois temporaire, il disparaît alors tout seul).\n"
            "• **Objets** : ils vont dans ton **inventaire** ; un admin peut te les livrer ou les utiliser avec toi.\n"
            + (f"• **Revente** : tu récupères **{cfg['refund_pct']} %** du prix depuis ton inventaire.\n"
               if cfg["refund_pct"] > 0 else "• La revente est désactivée.\n")
            + "• Un article peut être limité en stock, par membre, ou réservé à un rôle."))
        v = BaseView(self, interaction.user)
        v.add_item(Btn("Retour", self._back_home, discord.ButtonStyle.danger, "↩️"))
        await show(interaction, e, v, old=old)

    async def open_popular(self, interaction, old=None):
        rows = q_all("SELECT * FROM items WHERE guild_id=? AND enabled=1 AND sold>0 ORDER BY sold DESC LIMIT 5",
                     (interaction.guild.id,))
        text = "\n".join(f"**{n}.** {r['emoji']} **{r['name']}** — {r['sold']} vendu(s) · {fmt(r['price'])}"
                         for n, r in enumerate(rows, 1)) or "Aucune vente pour le moment."
        e = discord.Embed(title="🔥 Articles populaires", description=text, color=EMBED_COLOR)
        v = BaseView(self, interaction.user)
        v.add_item(Btn("Retour", self._back_home, discord.ButtonStyle.danger, "↩️"))
        await show(interaction, e, v, old=old)

    # ── Boutique ───────────────────────────────────────────────────────────
    async def open_browse(self, interaction, old=None):
        guild, member = interaction.guild, interaction.user
        cfg = get_cfg(guild.id)

        async def on_pick(i, item, v):
            await self.open_item(i, item["id"], old=v, mode="shop")

        view = ItemListView(
            self, member, title=f"🛍️ {cfg['name']}", fetch=lambda: list_items(guild.id, enabled_only=True),
            on_pick=on_pick, on_back=self._back_home, empty="La boutique est vide pour le moment.",
            intro="" if cfg["is_open"] else "🔒 **La boutique est fermée** : tu peux regarder mais pas acheter.",
            footer=lambda: f"Portefeuille : {fmt(self.eco.balance(guild.id, member.id)[0])}")
        await show(interaction, view.embed(), view, old=old)

    async def open_item(self, interaction, item_id: int, old=None, mode: str = "shop"):
        guild, member = interaction.guild, interaction.user
        item = get_item(item_id)
        if not item or item["guild_id"] != guild.id:
            return await (self.open_browse(interaction, old=old) if mode == "shop"
                          else self.open_inventory(interaction, old=old))
        cfg = get_cfg(guild.id)
        owned = inv_qty(guild.id, member.id, item_id)
        expires = grant_get(guild.id, member.id, item["role_id"]) if item["role_id"] else None
        e = item_embed(guild, item, owned=owned, expires=expires)
        v = BaseView(self, member)
        if mode == "shop":
            can_buy = cfg["is_open"] and item["stock"] != 0
            v.add_item(Btn("Acheter", lambda i, vv: self.ask_buy(i, item_id, 1, old=vv),
                           discord.ButtonStyle.success, "🛒", row=0, disabled=not can_buy))
            if not item["role_id"]:
                async def qty_modal(i, vv):
                    await i.response.send_modal(QtyModal(vv, "Combien en acheter ?",
                                                         lambda ii, q, p: self.ask_buy(ii, item_id, q, old=p)))
                v.add_item(Btn("Quantité…", qty_modal, discord.ButtonStyle.primary, "🔢", row=0, disabled=not can_buy))
            v.add_item(Btn("Retour", lambda i, vv: self.open_browse(i, old=vv), discord.ButtonStyle.danger, "↩️", row=0))
        else:
            if cfg["refund_pct"] > 0:
                amount = item["price"] * cfg["refund_pct"] // 100
                v.add_item(Btn(f"Revendre (+{fmt(amount)})", lambda i, vv: self.ask_sell(i, item_id, old=vv),
                               discord.ButtonStyle.success, "💸", row=0))
            v.add_item(Btn("Retour", lambda i, vv: self.open_inventory(i, old=vv), discord.ButtonStyle.danger, "↩️", row=0))
        await show(interaction, e, v, old=old)

    async def ask_buy(self, interaction, item_id: int, qty: int, old=None):
        item = get_item(item_id)
        if not item:
            return await self.open_browse(interaction, old=old)
        qty = 1 if item["role_id"] else qty
        total = item["price"] * qty
        wallet, _ = self.eco.balance(interaction.guild.id, interaction.user.id)
        e = discord.Embed(title="🧾 Confirmer l'achat", color=EMBED_COLOR,
                          description=f"Acheter **{qty}× {item['emoji']} {item['name']}** pour **{fmt(total)}** ?")
        e.add_field(name="Portefeuille", value=fmt(wallet))
        e.add_field(name="Après l'achat", value=fmt(max(wallet - total, 0)) if wallet >= total else "❌ Solde insuffisant")

        async def yes(i, v):
            await i.response.defer()
            ok, msg = await self.purchase(i.user, item_id, qty)
            await self.show_result(i, ok, msg, old=v)

        async def no(i, v):
            await self.open_item(i, item_id, old=v)

        await show(interaction, e, ConfirmView(self, interaction.user, yes, no, "Acheter"), old=old)

    async def ask_sell(self, interaction, item_id: int, old=None):
        item = get_item(item_id)
        cfg = get_cfg(interaction.guild.id)
        if not item:
            return await self.open_inventory(interaction, old=old)
        amount = item["price"] * cfg["refund_pct"] // 100
        e = discord.Embed(title="💸 Confirmer la revente", color=EMBED_COLOR,
                          description=f"Revendre **1× {item['emoji']} {item['name']}** pour **{fmt(amount)}** ?"
                                      + ("\n⚠️ Le rôle te sera retiré." if item["role_id"] else ""))

        async def yes(i, v):
            await i.response.defer()
            ok, msg = await self.refund(i.user, item_id)
            await self.show_result(i, ok, msg, old=v)

        async def no(i, v):
            await self.open_item(i, item_id, old=v, mode="inv")

        await show(interaction, e, ConfirmView(self, interaction.user, yes, no, "Revendre"), old=old)

    async def show_result(self, interaction, ok: bool, msg: str, old=None):
        e = discord.Embed(title="🧾 Résultat" if ok else "⚠️ Oups", description=msg,
                          color=0x2ECC71 if ok else 0xE74C3C)
        wallet, _ = self.eco.balance(interaction.guild.id, interaction.user.id)
        e.set_footer(text=f"Portefeuille : {fmt(wallet)}")
        v = BaseView(self, interaction.user)
        v.add_item(Btn("Continuer", lambda i, vv: self.open_browse(i, old=vv), discord.ButtonStyle.success, "🛍️"))
        v.add_item(Btn("Inventaire", lambda i, vv: self.open_inventory(i, old=vv), discord.ButtonStyle.primary, "🎒"))
        v.add_item(Btn("Menu", self._back_home, discord.ButtonStyle.secondary, "🏠"))
        await show(interaction, e, v, old=old)

    # ── Inventaire ─────────────────────────────────────────────────────────
    async def open_inventory(self, interaction, old=None):
        guild, member = interaction.guild, interaction.user
        cfg = get_cfg(guild.id)

        async def on_pick(i, item, v):
            await self.open_item(i, item["id"], old=v, mode="inv")

        view = ItemListView(
            self, member, title=f"🎒 Inventaire : {member.display_name}",
            fetch=lambda: inv_list(guild.id, member.id), on_pick=on_pick, on_back=self._back_home,
            empty="Ton inventaire est vide. Va faire un tour en boutique !", categories=False,
            intro=(f"Sélectionne un article pour le revendre ({cfg['refund_pct']} % du prix)."
                   if cfg["refund_pct"] > 0 else ""))
        await show(interaction, view.embed(), view, old=old)

    # ══════════════════════════════════════════════════════════════════════
    #  ADMINISTRATION
    # ══════════════════════════════════════════════════════════════════════
    async def _deny(self, interaction):
        await interaction.response.send_message("⛔ Réservé à l'administrateur du bot.", ephemeral=True)

    async def open_admin(self, interaction, old=None):
        if not await self.is_admin(interaction.user):
            return await self._deny(interaction)
        guild = interaction.guild
        cfg = get_cfg(guild.id)
        items = list_items(guild.id)
        st = shop_stats(guild.id)
        e = discord.Embed(title="⚙️ Administration de la boutique", color=EMBED_COLOR,
                          description="Crée et gère tes articles, lie-les à des rôles, règle la boutique.")
        e.add_field(name="Articles", value=f"{sum(1 for i in items if i['enabled'])} actifs / {len(items)}")
        e.add_field(name="Rôles liés", value=str(sum(1 for i in items if i["role_id"])))
        e.add_field(name="État", value="🟢 Ouverte" if cfg["is_open"] else "🔒 Fermée")
        e.add_field(name="Ventes", value=f"{st['units']} article(s) · {st['orders']} commande(s)")
        e.add_field(name="Revenus", value=fmt(st["revenue"]))
        e.add_field(name="Revente", value=f"{cfg['refund_pct']} %")
        if st["top"]:
            e.add_field(name="🔥 Top ventes", inline=False,
                        value="\n".join(f"{t['emoji']} {t['name']} — {t['sold']}" for t in st["top"][:3]))
        e.set_footer(text=f"Argent lié : {self.eco.describe()}")

        v = BaseView(self, interaction.user, timeout=600)

        async def add(i, vv):
            await i.response.send_modal(ItemInfoModal(self, vv, None))

        v.add_item(Btn("Ajouter un article", add, discord.ButtonStyle.success, "➕", row=0))
        v.add_item(Btn("Gérer les articles", lambda i, vv: self.open_admin_pick(i, "edit", old=vv),
                       discord.ButtonStyle.primary, "✏️", row=0))
        v.add_item(Btn("Réglages", lambda i, vv: self.open_settings(i, old=vv), discord.ButtonStyle.secondary, "⚙️", row=0))
        v.add_item(Btn("Donner un article", lambda i, vv: self.open_admin_pick(i, "give", old=vv),
                       discord.ButtonStyle.secondary, "🎁", row=1))
        v.add_item(Btn("Retirer un article", lambda i, vv: self.open_admin_pick(i, "take", old=vv),
                       discord.ButtonStyle.secondary, "🧹", row=1))
        v.add_item(Btn("Retour", self._back_home, discord.ButtonStyle.danger, "↩️", row=1))
        await show(interaction, e, v, old=old)

    async def _back_admin(self, i, v):
        await self.open_admin(i, old=v)

    async def open_admin_pick(self, interaction, mode: str, old=None):
        if not await self.is_admin(interaction.user):
            return await self._deny(interaction)
        guild = interaction.guild
        titles = {"edit": "✏️ Gérer les articles", "give": "🎁 Donner : choisis l'article",
                  "take": "🧹 Retirer : choisis l'article"}

        async def on_pick(i, item, v):
            if mode == "edit":
                await self.open_editor(i, item["id"], old=v)
            else:
                await self.open_member_pick(i, item["id"], mode, old=v)

        view = ItemListView(self, interaction.user, title=titles[mode], fetch=lambda: list_items(guild.id),
                            on_pick=on_pick, on_back=self._back_admin,
                            empty="Aucun article. Utilise ➕ **Ajouter un article**.")
        view.timeout = 600
        await show(interaction, view.embed(), view, old=old)

    async def open_member_pick(self, interaction, item_id: int, mode: str, old=None):
        item = get_item(item_id)
        if not item:
            return await self.open_admin(interaction, old=old)
        verb = "recevoir" if mode == "give" else "se voir retirer"
        e = discord.Embed(title=f"{'🎁' if mode == 'give' else '🧹'} {item['emoji']} {item['name']}",
                          description=f"Choisis le membre qui doit **{verb}** cet article.", color=EMBED_COLOR)

        async def picked(i, user, v):
            member = user if isinstance(user, discord.Member) else i.guild.get_member(user.id)
            if member is None:
                return await i.response.send_message("❌ Ce membre n'est pas sur le serveur.", ephemeral=True)

            async def run(ii, qty, parent):
                msg = await self.admin_apply(ii.guild, mode, member, item_id, qty)
                await self.show_admin_result(ii, msg, old=parent)

            if item["role_id"]:
                msg = await self.admin_apply(i.guild, mode, member, item_id, 1)
                return await self.show_admin_result(i, msg, old=v)
            await i.response.send_modal(QtyModal(v, "Quantité", run))

        v = BaseView(self, interaction.user, timeout=600)
        v.add_item(UserSel("Choisis un membre…", picked, row=0))
        v.add_item(Btn("Retour", lambda i, vv: self.open_admin_pick(i, mode, old=vv), discord.ButtonStyle.danger, "↩️", row=1))
        await show(interaction, e, v, old=old)

    async def show_admin_result(self, interaction, msg: str, old=None):
        e = discord.Embed(title="⚙️ Administration", description=msg,
                          color=0x2ECC71 if not msg.startswith("❌") else 0xE74C3C)
        v = BaseView(self, interaction.user, timeout=600)
        v.add_item(Btn("Retour à l'administration", self._back_admin, discord.ButtonStyle.secondary, "⚙️"))
        await show(interaction, e, v, old=old)

    # ── Éditeur d'article ──────────────────────────────────────────────────
    async def open_editor(self, interaction, item_id: int, old=None):
        if not await self.is_admin(interaction.user):
            return await self._deny(interaction)
        item = get_item(item_id)
        if not item or item["guild_id"] != interaction.guild.id:
            return await self.open_admin(interaction, old=old)
        e = item_embed(interaction.guild, item, admin=True)
        v = BaseView(self, interaction.user, timeout=600)

        async def infos(i, vv):
            await i.response.send_modal(ItemInfoModal(self, vv, get_item(item_id)))

        async def options(i, vv):
            await i.response.send_modal(ItemOptionsModal(self, vv, get_item(item_id)))

        async def toggle(i, vv):
            cur = get_item(item_id)
            update_item(item_id, enabled=0 if cur["enabled"] else 1)
            await self.open_editor(i, item_id, old=vv)

        async def link_role(i, role, vv):
            ok, why = self.can_manage(i.guild, role)
            if not ok:
                return await i.response.send_message(f"❌ {why}", ephemeral=True)
            update_item(item_id, role_id=role.id)
            await self.open_editor(i, item_id, old=vv)

        async def need_role(i, role, vv):
            if role.is_default():
                return await i.response.send_message("❌ @everyone ne peut pas servir de condition.", ephemeral=True)
            update_item(item_id, required_role_id=role.id)
            await self.open_editor(i, item_id, old=vv)

        async def unlink(i, vv):
            update_item(item_id, role_id=None, duration_days=0)
            await self.open_editor(i, item_id, old=vv)

        async def unneed(i, vv):
            update_item(item_id, required_role_id=None)
            await self.open_editor(i, item_id, old=vv)

        async def delete(i, vv):
            ev = discord.Embed(title="🗑️ Supprimer l'article ?", color=0xE74C3C,
                               description=f"**{item['emoji']} {item['name']}** sera supprimé, ainsi que "
                                           "les exemplaires dans les inventaires. Les rôles déjà donnés restent.")

            async def yes(ii, v2):
                delete_item(item_id)
                await self.open_admin(ii, old=v2)

            async def no(ii, v2):
                await self.open_editor(ii, item_id, old=v2)

            await show(i, ev, ConfirmView(self, i.user, yes, no, "Supprimer", discord.ButtonStyle.danger), old=vv)

        v.add_item(Btn("Infos", infos, discord.ButtonStyle.primary, "✏️", row=0))
        v.add_item(Btn("Options", options, discord.ButtonStyle.primary, "🛠️", row=0))
        v.add_item(Btn("Masquer" if item["enabled"] else "Afficher", toggle,
                       discord.ButtonStyle.secondary, "🔴" if item["enabled"] else "🟢", row=0))
        v.add_item(RoleSel("🎭 Lier à un rôle Discord (donné à l'achat)", link_role, row=1))
        v.add_item(RoleSel("🔒 Rôle requis pour pouvoir acheter (optionnel)", need_role, row=2))
        v.add_item(Btn("Délier le rôle", unlink, discord.ButtonStyle.secondary, "🎭", row=3, disabled=not item["role_id"]))
        v.add_item(Btn("Retirer la condition", unneed, discord.ButtonStyle.secondary, "🔓", row=3,
                       disabled=not item["required_role_id"]))
        v.add_item(Btn("Supprimer", delete, discord.ButtonStyle.danger, "🗑️", row=3))
        v.add_item(Btn("Retour", lambda i, vv: self.open_admin_pick(i, "edit", old=vv), discord.ButtonStyle.secondary, "↩️", row=4))
        await show(interaction, e, v, old=old)

    # ── Réglages ───────────────────────────────────────────────────────────
    async def open_settings(self, interaction, old=None):
        if not await self.is_admin(interaction.user):
            return await self._deny(interaction)
        gid = interaction.guild.id
        cfg = get_cfg(gid)
        e = discord.Embed(title="⚙️ Réglages de la boutique", color=EMBED_COLOR)
        e.add_field(name="Nom", value=cfg["name"])
        e.add_field(name="État", value="🟢 Ouverte" if cfg["is_open"] else "🔒 Fermée")
        e.add_field(name="Revente", value=f"{cfg['refund_pct']} %" if cfg["refund_pct"] else "Désactivée")
        e.add_field(name="Salon de logs", inline=False,
                    value=f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "Aucun (choisis-en un ci-dessous)")
        v = BaseView(self, interaction.user, timeout=600)

        async def rename(i, vv):
            async def done(ii, text, parent):
                if text:
                    set_cfg(gid, name=text)
                await self.open_settings(ii, old=parent)
            await i.response.send_modal(TextModal(vv, "Nom de la boutique", "Nom", get_cfg(gid)["name"], done))

        async def refund(i, vv):
            async def done(ii, text, parent):
                pct = parse_int(text)
                if pct is None or not (0 <= pct <= 100):
                    return await ii.response.send_message("❌ Entre un nombre de 0 à 100.", ephemeral=True)
                set_cfg(gid, refund_pct=pct)
                await self.open_settings(ii, old=parent)
            await i.response.send_modal(TextModal(vv, "Revente", "Pourcentage remboursé (0 = désactivée)",
                                                  str(get_cfg(gid)["refund_pct"]), done, max_length=3))

        async def toggle(i, vv):
            set_cfg(gid, is_open=0 if get_cfg(gid)["is_open"] else 1)
            await self.open_settings(i, old=vv)

        async def pick_channel(i, channel, vv):
            set_cfg(gid, log_channel=channel.id)
            await self.open_settings(i, old=vv)

        async def no_logs(i, vv):
            set_cfg(gid, log_channel=None)
            await self.open_settings(i, old=vv)

        v.add_item(Btn("Nom", rename, discord.ButtonStyle.primary, "✏️", row=0))
        v.add_item(Btn("Revente %", refund, discord.ButtonStyle.primary, "💸", row=0))
        v.add_item(Btn("Fermer la boutique" if cfg["is_open"] else "Ouvrir la boutique", toggle,
                       discord.ButtonStyle.secondary, "🔒" if cfg["is_open"] else "🔓", row=0))
        v.add_item(ChanSel("📜 Salon où envoyer les logs d'achats", pick_channel, row=1))
        v.add_item(Btn("Désactiver les logs", no_logs, discord.ButtonStyle.secondary, "🔕", row=2,
                       disabled=not cfg["log_channel"]))
        v.add_item(Btn("Retour", self._back_admin, discord.ButtonStyle.danger, "↩️", row=2))
        await show(interaction, e, v, old=old)


# ══════════════════════════════════════════════════════════════════════════════
#  CHARGEMENT EN EXTENSION
# ══════════════════════════════════════════════════════════════════════════════
async def _sync_when_ready(bot: commands.Bot):
    await bot.wait_until_ready()
    try:
        await bot.tree.sync()
    except discord.HTTPException:
        log.exception("Synchronisation des commandes impossible")


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))
    if AUTO_SYNC:
        bot.loop.create_task(_sync_when_ready(bot))


# ══════════════════════════════════════════════════════════════════════════════
#  LANCEMENT SEUL : keep alive + token caché
# ══════════════════════════════════════════════════════════════════════════════
def keep_alive():
    """Petit serveur web pour que l'hébergeur garde le bot éveillé (UptimeRobot, etc.)."""
    port = int(os.environ.get("PORT", 8080))
    try:
        from flask import Flask
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        app = Flask("keep_alive")

        @app.route("/")
        def home():
            return "Bot en ligne ✅"

        def run():
            app.run(host="0.0.0.0", port=port)
    except ImportError:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write("Bot en ligne".encode())

            def log_message(self, *args):
                pass

        def run():
            HTTPServer(("0.0.0.0", port), Handler).serve_forever()

    def safe_run():
        try:
            run()
        except OSError as exc:  # port déjà pris (keep alive déjà lancé par ailleurs)
            log.warning("Keep alive non démarré : %s", exc)

    threading.Thread(target=safe_run, daemon=True).start()


class _StandaloneBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(ShopCog(self))
        await self.tree.sync()


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = os.environ.get("DISCORD_TOKEN")  # ← le token vit dans les Secrets / .env, jamais ici
    if not token:
        raise SystemExit("❌ DISCORD_TOKEN introuvable : ajoute-le dans les Secrets (ou un fichier .env).")
    keep_alive()
    intents = discord.Intents.default()
    intents.members = True
    _StandaloneBot(command_prefix="!", intents=intents).run(token)


if __name__ == "__main__":
    main()

"""
Bot Discord de quiz : questions, scores, planification quotidienne (SQLite).

Dépendance : pip install "discord.py>=2.3"
Configuration : crée un fichier .env à côté du script avec la ligne DISCORD_TOKEN=ton_token
(le token n'est jamais écrit dans ce fichier). Le salon de jeu se choisit avec l'option « salon » de /addquiz.
À activer dans le portail développeur : « Message Content Intent ».
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks
import sutom 

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_env_file() -> None:
    """Lit le fichier .env placé à côté du script (lignes CLE=valeur), sans rien installer."""
    for name in (".env", ".env.txt"):  # Windows ajoute parfois « .txt » sans le montrer
        path = Path(__file__).with_name(name)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Une variable d'environnement déjà définie reste prioritaire.
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break


load_env_file()

# Le token n'est JAMAIS écrit dans ce fichier : il vient du fichier .env (variable DISCORD_TOKEN).
TOKEN = os.getenv("DISCORD_TOKEN", "")

# ID Discord du propriétaire : seul lui, et les personnes ajoutées avec /add,
# peut utiliser les commandes d'administration.
OWNER_ID = 1258814145969782845

# Facultatif : ID du salon où ouvrir un ticket pour récupérer la récompense (0 = non précisé).
TICKET_CHANNEL_ID = 0

DB_PATH = Path(__file__).with_name("quiz.db")
TIMEZONE = "Europe/Paris"          # fuseau utilisé pour l'heure de diffusion
LEADERBOARD_SIZE = 10              # nombre de joueurs affichés dans /leaderboard
MAX_QUESTION_LENGTH = 1500         # longueur max d'une question
MAX_IMPORT_FILE_SIZE = 200_000     # taille max d'un fichier d'import (octets)
MAX_DRAFTS = 15                    # questions max par envoi avec /addquiz
MAX_FILES_PER_QUESTION = 3         # fichiers / images par question
MAX_QUESTION_FILES_BYTES = 8_000_000   # poids total des fichiers d'une question (8 Mo)
MAX_PANEL_FILE_BYTES = 30_000_000      # poids total des fichiers d'un envoi /addquiz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("quizbot")

# Le champ « glisser un fichier » des fenêtres demande discord.py 2.6 ou plus récent.
SUPPORTS_FILE_UPLOAD = hasattr(discord.ui, "FileUpload") and hasattr(discord.ui, "Label")


def load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows n'a pas toujours la base des fuseaux : pip install tzdata
        log.warning("Fuseau %s introuvable (pip install tzdata) : heure locale du PC utilisée.", name)
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


TZ = load_timezone(TIMEZONE)


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """Minuscules, sans accents, espaces compactés. La ponctuation et les symboles comptent."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def parse_answers(raw: str) -> list[str]:
    """« Paris ; ville de Paris » -> ['Paris', 'ville de Paris']"""
    return [a.strip() for a in raw.split(";") if a.strip()]


def parse_lines(text: str) -> tuple[list[tuple[str, list[str]]], list[int]]:
    """Une question par ligne, au format « Question | Réponse1 ; Réponse2 »."""
    items: list[tuple[str, list[str]]] = []
    errors: list[int] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        question, sep, raw_answers = line.rpartition("|")
        question = question.strip()
        answers = parse_answers(raw_answers)
        if (
            not sep
            or not question
            or len(question) > MAX_QUESTION_LENGTH
            or not any(normalize(a) for a in answers)
        ):
            errors.append(number)
            continue
        items.append((question, answers))
    return items, errors


def next_run(hour: int, minute: int, last_posted: str | None, now: datetime) -> datetime:
    """Date de la prochaine diffusion programmée."""
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if last_posted == now.date().isoformat():
        run += timedelta(days=1)
    return max(run, now)


def parse_time(raw: str) -> tuple[int, int] | None:
    """« 18:30 », « 18h30 », « 18h » ou « 18 » -> (18, 30) ; None si invalide."""
    match = re.fullmatch(r"(\d{1,2})\s*(?:[:hH]\s*(\d{1,2})?)?", raw.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def next_occurrence(hour: int, minute: int, now: datetime) -> datetime:
    """Prochaine fois qu'il sera hh:mm (aujourd'hui si c'est à venir, sinon demain)."""
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run


class ScheduleError(ValueError):
    """Jour ou heure invalide (le message est affiché tel quel à l'utilisateur)."""


WEEKDAYS = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
}


def parse_day(raw: str, today: date) -> date | None:
    """« 25/12 », « 25/12/2026 », « demain », « lundi »... -> date ; None si invalide."""
    text = normalize(raw).replace("’", "'")
    if text in ("aujourd'hui", "aujourdhui"):
        return today
    if text == "demain":
        return today + timedelta(days=1)
    if text in ("apres-demain", "apres demain"):
        return today + timedelta(days=2)
    if text in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS[text] - today.weekday()) % 7)
    match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{4}|\d{2}))?", text)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year = int(match.group(3)) if match.group(3) else today.year
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def build_post_at(raw_day: str, raw_hour: str, now: datetime) -> datetime | None:
    """Jour et heure facultatifs -> moment d'envoi (None = stock quotidien)."""
    raw_day, raw_hour = raw_day.strip(), raw_hour.strip()
    if not raw_day and not raw_hour:
        return None

    parsed = None
    if raw_hour:
        parsed = parse_time(raw_hour)
        if parsed is None:
            raise ScheduleError("Heure invalide. Exemples : `18:30`, `18h30`, `9h`.")
    if not raw_day:
        return next_occurrence(*parsed, now)

    if parsed is None:
        raise ScheduleError("Indique aussi une heure pour ce jour.")
    day = parse_day(raw_day, now.date())
    if day is None:
        raise ScheduleError("Jour invalide. Exemples : `25/12`, `25/12/2026`, `demain`, `lundi`.")
    post_at = datetime.combine(day, dtime(*parsed), tzinfo=now.tzinfo)
    if post_at <= now:
        if normalize(raw_day) in WEEKDAYS:
            return post_at + timedelta(days=7)  # « lundi » dit un lundi : lundi prochain
        raise ScheduleError(
            "Cette date et cette heure sont déjà passées "
            "(pour l'an prochain, écris l'année : `05/01/2027`)."
        )
    return post_at


def safe_filename(name: str, used: set[str]) -> str:
    """Nom de fichier sans caractères spéciaux et unique dans le message."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "fichier"
    candidate = base if base not in used else f"{len(used) + 1}_{base}"
    used.add(candidate)
    return candidate


def is_image(filename: str) -> bool:
    return filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


def missing_permissions(channel, member, needs_files: bool = False) -> list[str]:
    """Permissions (en français) qui manquent au bot pour poster une question dans ce salon."""
    perms = channel.permissions_for(member)
    wanted = [
        ("view_channel", "Voir le salon"),
        ("send_messages", "Envoyer des messages"),
        ("embed_links", "Intégrer des liens"),
    ]
    if needs_files:
        wanted.append(("attach_files", "Joindre des fichiers"))
    return [label for attr, label in wanted if not getattr(perms, attr, False)]


def allowed_mentions_for(text: str | None) -> discord.AllowedMentions:
    """Autorise uniquement les mentions présentes dans le texte (@everyone, @here, rôles, personnes)."""
    if not text:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone="@everyone" in text or "@here" in text,
        roles=[discord.Object(id=int(i)) for i in re.findall(r"<@&(\d+)>", text)] or False,
        users=[discord.Object(id=int(i)) for i in re.findall(r"<@!?(\d+)>", text)] or False,
    )


# --------------------------------------------------------------------------- #
# Base de données SQLite
# --------------------------------------------------------------------------- #
class Database:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS questions (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id  INTEGER NOT NULL,
                    question  TEXT    NOT NULL,
                    answers   TEXT    NOT NULL,              -- liste JSON
                    used      INTEGER NOT NULL DEFAULT 0     -- 0 = en stock
                );
                CREATE TABLE IF NOT EXISTS scores (
                    guild_id  INTEGER NOT NULL,
                    user_id   INTEGER NOT NULL,
                    points    INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS active_question (
                    guild_id    INTEGER PRIMARY KEY,
                    question_id INTEGER NOT NULL,
                    channel_id  INTEGER NOT NULL,
                    message_id  INTEGER NOT NULL,
                    active      INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS question_files (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id  INTEGER NOT NULL,
                    filename     TEXT    NOT NULL,
                    content_type TEXT,
                    data         BLOB    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guild_helpers (
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS guild_admins (
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id     INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS schedule (
                    guild_id    INTEGER PRIMARY KEY,
                    channel_id  INTEGER NOT NULL,
                    hour        INTEGER NOT NULL,
                    minute      INTEGER NOT NULL,
                    days_left   INTEGER NOT NULL,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    last_posted TEXT                         -- date ISO du dernier envoi
                );
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """Ajoute les colonnes de la planification par question (anciennes bases comprises)."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(questions)")}
        wanted = {
            "post_at": "TEXT",                          # heure d'envoi (UTC, ISO) ; NULL = stock quotidien
            "channel_id": "INTEGER",
            "mention": "INTEGER NOT NULL DEFAULT 0",
            "role_id": "INTEGER",
            "mention_text": "TEXT",                     # ex. « @everyone » ou « <@&123> <@456> »
        }
        with self.conn:
            for name, ddl in wanted.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE questions ADD COLUMN {name} {ddl}")

    # -- questions --------------------------------------------------------- #
    def add_questions(self, guild_id: int, items: list[tuple[str, list[str]]]) -> int:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO questions (guild_id, question, answers) VALUES (?, ?, ?)",
                [(guild_id, q, json.dumps(a, ensure_ascii=False)) for q, a in items],
            )
        return len(items)

    def stock(self, guild_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND used = 0 AND post_at IS NULL",
            (guild_id,),
        ).fetchone()
        return row[0]

    def next_question(self, guild_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM questions WHERE guild_id = ? AND used = 0 AND post_at IS NULL "
            "ORDER BY id LIMIT 1",
            (guild_id,),
        ).fetchone()

    def mark_used(self, question_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE questions SET used = 1 WHERE id = ?", (question_id,))

    def add_drafts(
        self, guild_id: int, drafts: list["Draft"], channel_id: int, mention_text: str | None
    ) -> None:
        with self.conn:
            for d in drafts:
                post_at = (
                    d.post_at.astimezone(timezone.utc).isoformat(timespec="seconds") if d.post_at else None
                )
                cur = self.conn.execute(
                    "INSERT INTO questions (guild_id, question, answers, post_at, channel_id, mention, mention_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        guild_id,
                        d.question,
                        json.dumps(d.answers, ensure_ascii=False),
                        post_at,
                        channel_id,
                        int(bool(mention_text)),
                        mention_text,
                    ),
                )
                for f in d.files:
                    self.conn.execute(
                        "INSERT INTO question_files (question_id, filename, content_type, data) "
                        "VALUES (?, ?, ?, ?)",
                        (cur.lastrowid, f.filename, f.content_type, f.data),
                    )

    def get_files(self, question_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT filename, content_type, data FROM question_files WHERE question_id = ? ORDER BY id",
            (question_id,),
        ).fetchall()

    def delete_files(self, question_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM question_files WHERE question_id = ?", (question_id,))

    def due_timed_questions(self, now_iso: str) -> list[sqlite3.Row]:
        """Questions à heure précise dont l'heure est arrivée (dates UTC ISO comparables)."""
        return self.conn.execute(
            "SELECT * FROM questions WHERE used = 0 AND post_at IS NOT NULL AND post_at <= ? "
            "ORDER BY post_at, id",
            (now_iso,),
        ).fetchall()

    def pending_timed(self, guild_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, question, post_at FROM questions "
            "WHERE guild_id = ? AND used = 0 AND post_at IS NOT NULL ORDER BY post_at, id",
            (guild_id,),
        ).fetchall()

    # -- question active --------------------------------------------------- #
    def set_active(self, guild_id: int, question_id: int, channel_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO active_question (guild_id, question_id, channel_id, message_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    question_id = excluded.question_id,
                    channel_id  = excluded.channel_id,
                    message_id  = excluded.message_id,
                    active      = 1
                """,
                (guild_id, question_id, channel_id, message_id),
            )

    def expire_active(self, guild_id: int) -> sqlite3.Row | None:
        """Désactive la question en cours et la retourne (None s'il n'y en avait pas)."""
        row = self.conn.execute(
            """
            SELECT q.*, a.channel_id FROM active_question a
            JOIN questions q ON q.id = a.question_id
            WHERE a.guild_id = ? AND a.active = 1
            """,
            (guild_id,),
        ).fetchone()
        if row:
            with self.conn:
                self.conn.execute(
                    "UPDATE active_question SET active = 0 WHERE guild_id = ?", (guild_id,)
                )
        return row

    def close_active(self, guild_id: int, question_id: int) -> bool:
        """Clôture atomique : True uniquement pour le premier appel (le gagnant)."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE active_question SET active = 0 "
                "WHERE guild_id = ? AND question_id = ? AND active = 1",
                (guild_id, question_id),
            )
        return cur.rowcount == 1

    def active_questions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT q.*, a.channel_id, a.message_id FROM active_question a
            JOIN questions q ON q.id = a.question_id
            WHERE a.active = 1
            """
        ).fetchall()

    # -- scores ------------------------------------------------------------ #
    def add_point(self, guild_id: int, user_id: int) -> int:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scores (guild_id, user_id, points) VALUES (?, ?, 1)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + 1
                """,
                (guild_id, user_id),
            )
        return self.score(guild_id, user_id)

    def score(self, guild_id: int, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT points FROM scores WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row["points"] if row else 0

    def top(self, guild_id: int, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT user_id, points FROM scores WHERE guild_id = ? AND points > 0 "
            "ORDER BY points DESC, user_id LIMIT ?",
            (guild_id, limit),
        ).fetchall()

    def reset_scores(self, guild_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM scores WHERE guild_id = ?", (guild_id,))

    def winners(self, guild_id: int) -> list[sqlite3.Row]:
        """Joueur(s) en tête du classement (plusieurs en cas d'égalité), vide si personne n'a de point."""
        return self.conn.execute(
            "SELECT user_id, points FROM scores WHERE guild_id = ? AND points > 0 "
            "AND points = (SELECT MAX(points) FROM scores WHERE guild_id = ?) ORDER BY user_id",
            (guild_id, guild_id),
        ).fetchall()

    def cancel_timed(self, guild_id: int) -> int:
        """Annule les questions à heure précise pas encore postées ; retourne leur nombre."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE questions SET used = 1 WHERE guild_id = ? AND used = 0 AND post_at IS NOT NULL",
                (guild_id,),
            )
        return cur.rowcount

    # -- planification ----------------------------------------------------- #
    def save_schedule(
        self, guild_id: int, channel_id: int, hour: int, minute: int, days: int, last_posted: str | None
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO schedule (guild_id, channel_id, hour, minute, days_left, enabled, last_posted)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id  = excluded.channel_id,
                    hour        = excluded.hour,
                    minute      = excluded.minute,
                    days_left   = excluded.days_left,
                    enabled     = 1,
                    last_posted = excluded.last_posted
                """,
                (guild_id, channel_id, hour, minute, days, last_posted),
            )

    def get_schedule(self, guild_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM schedule WHERE guild_id = ?", (guild_id,)).fetchone()

    def enabled_schedules(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM schedule WHERE enabled = 1").fetchall()

    def update_schedule_after_post(
        self, guild_id: int, last_posted: str, days_left: int, enabled: bool
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE schedule SET last_posted = ?, days_left = ?, enabled = ? WHERE guild_id = ?",
                (last_posted, days_left, int(enabled), guild_id),
            )

    def disable_schedule(self, guild_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE schedule SET enabled = 0 WHERE guild_id = ?", (guild_id,))

    # -- accès ------------------------------------------------------------- #
    # « guild_admins » : la personne qui a invité le bot (mêmes droits que toi, /add compris).
    # « guild_helpers » : les personnes ajoutées avec /add (commandes d'administration, sans /add).
    # Tout est propre à chaque serveur : seul OWNER_ID a accès partout.
    def add_guild_admin(self, guild_id: int, user_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO guild_admins (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id)
            )
        return cur.rowcount == 1

    def is_guild_admin(self, guild_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM guild_admins WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row is not None

    def has_guild_admin(self, guild_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM guild_admins WHERE guild_id = ?", (guild_id,)).fetchone()
        return row is not None

    def list_guild_admins(self, guild_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT user_id FROM guild_admins WHERE guild_id = ? ORDER BY rowid", (guild_id,)
        )
        return [r["user_id"] for r in rows]

    def add_helper(self, guild_id: int, user_id: int) -> bool:
        """True si la personne vient d'être ajoutée, False si elle l'était déjà."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO guild_helpers (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id)
            )
        return cur.rowcount == 1

    def remove_helper(self, guild_id: int, user_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM guild_helpers WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )
        return cur.rowcount == 1

    def is_helper(self, guild_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM guild_helpers WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row is not None

    def list_helpers(self, guild_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT user_id FROM guild_helpers WHERE guild_id = ? ORDER BY rowid", (guild_id,)
        )
        return [r["user_id"] for r in rows]

    def has_access(self, user_id: int, guild_id: int | None) -> bool:
        """Commandes d'administration : toi, la personne qui a invité le bot, ou une personne ajoutée
        avec /add, uniquement sur le serveur concerné."""
        if user_id == OWNER_ID:
            return True
        if guild_id is None:
            return False
        return self.is_guild_admin(guild_id, user_id) or self.is_helper(guild_id, user_id)

    def can_manage_access(self, user_id: int, guild_id: int | None) -> bool:
        """/add : toi et la personne qui a invité le bot (mêmes droits)."""
        if user_id == OWNER_ID:
            return True
        return guild_id is not None and self.is_guild_admin(guild_id, user_id)

    # -- suppression de questions ------------------------------------------ #
    def pending_count(self, guild_id: int) -> int:
        """Questions pas encore postées (stock quotidien + programmées)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND used = 0", (guild_id,)
        ).fetchone()
        return row[0]

    def delete_last_pending(self, guild_id: int) -> sqlite3.Row | None:
        """Supprime la dernière question ajoutée parmi celles pas encore postées."""
        row = self.conn.execute(
            "SELECT id, question, post_at FROM questions WHERE guild_id = ? AND used = 0 "
            "ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        if row:
            with self.conn:
                self.conn.execute("DELETE FROM question_files WHERE question_id = ?", (row["id"],))
                self.conn.execute("DELETE FROM questions WHERE id = ?", (row["id"],))
        return row

    def delete_all_pending(self, guild_id: int) -> int:
        with self.conn:
            self.conn.execute(
                "DELETE FROM question_files WHERE question_id IN "
                "(SELECT id FROM questions WHERE guild_id = ? AND used = 0)",
                (guild_id,),
            )
            cur = self.conn.execute("DELETE FROM questions WHERE guild_id = ? AND used = 0", (guild_id,))
        return cur.rowcount

    def pending_questions(self, guild_id: int) -> list[sqlite3.Row]:
        """Questions pas encore postées : d'abord le stock quotidien, puis les programmées par date."""
        return self.conn.execute(
            "SELECT q.*, (SELECT COUNT(*) FROM question_files f WHERE f.question_id = q.id) AS nfiles "
            "FROM questions q WHERE q.guild_id = ? AND q.used = 0 "
            "ORDER BY q.post_at IS NOT NULL, q.post_at, q.id",
            (guild_id,),
        ).fetchall()

    def get_pending_question(self, guild_id: int, question_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM questions WHERE id = ? AND guild_id = ? AND used = 0", (question_id, guild_id)
        ).fetchone()


# --------------------------------------------------------------------------- #
# Question active (cache mémoire pour ne pas interroger la base à chaque message)
# --------------------------------------------------------------------------- #
@dataclass
class ActiveQuestion:
    guild_id: int
    question_id: int
    answers: set[str]  # réponses normalisées
    question: str = ""
    message_id: int = 0


def to_active(row: sqlite3.Row) -> ActiveQuestion:
    answers = {normalize(a) for a in json.loads(row["answers"])}
    answers.discard("")
    message_id = row["message_id"] if "message_id" in row.keys() else 0
    return ActiveQuestion(row["guild_id"], row["id"], answers, row["question"], message_id)


@dataclass
class FileData:
    """Fichier ou image joint à une question (conservé dans la base jusqu'à l'envoi)."""

    filename: str
    content_type: str | None
    data: bytes


@dataclass
class Draft:
    """Question en cours de préparation dans le panneau /addquiz."""

    question: str
    answers: list[str]
    post_at: datetime | None = None  # None = stock quotidien
    files: list[FileData] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Fenêtres (modales) et boutons
# --------------------------------------------------------------------------- #
def import_questions(db: Database, guild_id: int, text: str) -> str:
    items, errors = parse_lines(text)
    if items:
        db.add_questions(guild_id, items)
        msg = f"✅ **{len(items)}** question(s) importée(s). Stock : **{db.stock(guild_id)}**."
    else:
        msg = "⚠️ Aucune question valide trouvée."
    if errors:
        shown = ", ".join(map(str, errors[:20])) + ("…" if len(errors) > 20 else "")
        msg += f"\n⚠️ Lignes ignorées (format `Question | Réponse` invalide) : {shown}"
    return msg


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Erreur dans une fenêtre de saisie", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)


class DraftModal(SafeModal, title="Nouvelle question"):
    question = discord.ui.TextInput(
        label="Question",
        style=discord.TextStyle.paragraph,
        max_length=MAX_QUESTION_LENGTH,
    )
    answers = discord.ui.TextInput(
        label="Réponses acceptées (séparées par ;)",
        placeholder="Paris ; ville de Paris",
        max_length=500,
    )
    day = discord.ui.TextInput(
        label="Jour (facultatif)",
        placeholder="JJ/MM, demain, lundi… (sans jour : prochaine heure)",
        required=False,
        max_length=12,
    )
    hour = discord.ui.TextInput(
        label="Heure (facultatif)",
        placeholder="18:30 (jour et heure vides = en stock)",
        required=False,
        max_length=5,
    )

    def __init__(self, panel: "QuestionPanel", with_upload: bool = True) -> None:
        super().__init__()
        self.panel = panel
        self.upload = None
        if with_upload and SUPPORTS_FILE_UPLOAD:
            try:
                self.upload = discord.ui.FileUpload(
                    required=False, min_values=0, max_values=MAX_FILES_PER_QUESTION
                )
                self.add_item(
                    discord.ui.Label(
                        text="Image ou fichier (facultatif)",
                        description="Glisse-le ici (8 Mo max au total)",
                        component=self.upload,
                    )
                )
            except Exception:
                log.exception("Champ de fichier indisponible : fenêtre ouverte sans ce champ")
                self.upload = None

    def _uploaded(self) -> list[discord.Attachment]:
        if self.upload is None:
            return []
        return list(getattr(self.upload, "values", None) or [])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deferred = False

        async def refuse(message: str) -> None:
            if deferred:
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

        answers = parse_answers(self.answers.value)
        if not any(normalize(a) for a in answers):
            await refuse("⚠️ Aucune réponse valide saisie.")
            return

        try:
            post_at = build_post_at(self.day.value, self.hour.value, datetime.now(TZ))
        except ScheduleError as err:
            await refuse(f"⚠️ {err}")
            return

        attachments = self._uploaded()
        if sum(a.size for a in attachments) > MAX_QUESTION_FILES_BYTES:
            await refuse("⚠️ Fichiers trop lourds : 8 Mo maximum au total pour une question.")
            return

        files: list[FileData] = []
        if attachments:
            await interaction.response.defer()  # le téléchargement peut prendre quelques secondes
            deferred = True
            try:
                for attachment in attachments:
                    files.append(
                        FileData(attachment.filename, attachment.content_type, await attachment.read())
                    )
            except discord.HTTPException:
                await refuse("⚠️ Impossible de récupérer un des fichiers. Réessaie.")
                return

        error = self.panel.add_draft(Draft(self.question.value.strip(), answers, post_at, files))
        if error:
            await refuse(error)
            return

        embed = self.panel.build_embed()
        if deferred:
            await interaction.edit_original_response(embed=embed, view=self.panel)
        else:
            await interaction.response.edit_message(embed=embed, view=self.panel)


class QuestionPanel(discord.ui.View):
    """Panneau : ➕ ajouter des questions, 🔔 mention au choix, 📎 fichiers, ✅ valider."""

    def __init__(
        self, db: Database, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        super().__init__(timeout=900)
        self.db = db
        self.interaction = interaction
        self.channel = channel
        self.drafts: list[Draft] = []
        self.mention_text: str | None = None  # texte envoyé avec la question
        self.mention_label = ""               # version courte pour le bouton
        self.mention_warning: str | None = None
        self.chosen_roles: list = []  # rôles choisis dans le menu de mention
        self.chosen_users: list = []  # personnes choisies dans le menu de mention
        self.in_picker = False
        self._waiting_file = False
        self._sync_buttons()

    # -- mention ----------------------------------------------------------- #
    def mention_summary(self) -> str:
        if not self.mention_text:
            return "🔕 Aucune mention"
        text = f"🔔 {self.mention_text}"
        if self.mention_warning:
            text += f"\n{self.mention_warning}"
        return text

    def set_mention(self, text: str | None, label: str = "", needs_everyone_perm: bool = False) -> None:
        self.mention_text = text or None
        self.mention_label = label
        self.mention_warning = None
        if text and needs_everyone_perm:
            perms = self.channel.permissions_for(self.channel.guild.me)
            if not perms.mention_everyone:
                self.mention_warning = "⚠️ Je n'ai pas la permission de mentionner ça dans ce salon."
        self._sync_buttons()

    def set_mention_from_selection(self, values: list) -> None:
        parts: list[str] = []
        names: list[str] = []
        needs_perm = False
        for value in values:
            if isinstance(value, discord.Role):
                if value.is_default():  # le rôle « @everyone » du serveur
                    parts.append("@everyone")
                    names.append("@everyone")
                    needs_perm = True
                else:
                    parts.append(value.mention)
                    names.append(f"@{value.name}")
                    needs_perm = needs_perm or not value.mentionable
            else:
                parts.append(value.mention)
                names.append(value.display_name)
        self.set_mention(" ".join(parts), ", ".join(names), needs_perm)

    # -- affichage --------------------------------------------------------- #
    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📝 Poser des questions",
            description=(
                "Clique sur **➕ Ajouter une question**, puis sur **Valider** quand tu as fini.\n"
                "Avec une heure (et un jour si tu veux), la question est postée à ce moment précis ; "
                "sans heure, elle reste en stock : tu la postes quand tu veux avec `/nextquiz`.\n"
                "**🔔 Mention** : choisis qui est mentionné. "
                "**📎** : joins une image ou un fichier à la dernière question."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Salon", value=self.channel.mention)
        embed.add_field(name="Mention", value=self.mention_summary())

        if self.drafts:
            lines = []
            for i, draft in enumerate(self.drafts, start=1):
                question = draft.question.replace("\n", " ")
                if len(question) > 60:
                    question = question[:59] + "…"
                when = (
                    f"🕒 {discord.utils.format_dt(draft.post_at, 'f')}"
                    if draft.post_at
                    else "📦 en stock"
                )
                extra = f" · 📎 {len(draft.files)}" if draft.files else ""
                lines.append(f"**{i}.** {question} → *{draft.answers[0][:40]}* · {when}{extra}")
            value = "\n".join(lines)
        else:
            value = "*Aucune question pour l'instant.*"
        embed.add_field(name=f"Questions ({len(self.drafts)})", value=value[:1024], inline=False)
        return embed

    def _sync_buttons(self) -> None:
        if self.mention_text:
            self.mention_button.label = f"Mention : {self.mention_label}"[:80]
            self.mention_button.emoji = "🔔"
            self.mention_button.style = discord.ButtonStyle.success
        else:
            self.mention_button.label = "Mention : aucune"
            self.mention_button.emoji = "🔕"
            self.mention_button.style = discord.ButtonStyle.secondary
        last = self.drafts[-1] if self.drafts else None
        self.add_button.disabled = len(self.drafts) >= MAX_DRAFTS
        self.attach_button.disabled = last is None or len(last.files) >= MAX_FILES_PER_QUESTION
        self.undo_button.disabled = not self.drafts
        self.validate_button.disabled = not self.drafts
        self.validate_button.label = f"Valider ({len(self.drafts)})"

    def add_draft(self, draft: Draft) -> str | None:
        """Ajoute une question ; retourne un message d'erreur si c'est impossible."""
        if len(self.drafts) >= MAX_DRAFTS:
            return f"⚠️ Maximum {MAX_DRAFTS} questions par envoi."
        if draft.post_at and any(d.post_at == draft.post_at for d in self.drafts):
            return "⚠️ Une autre question est déjà prévue à cette heure : choisis une heure différente."
        total = sum(len(f.data) for d in [*self.drafts, draft] for f in d.files)
        if total > MAX_PANEL_FILE_BYTES:
            return "⚠️ Trop de fichiers dans cet envoi (30 Mo maximum au total)."
        self.drafts.append(draft)
        self._sync_buttons()
        return None

    # -- boutons ----------------------------------------------------------- #
    @discord.ui.button(label="Ajouter une question", emoji="➕", style=discord.ButtonStyle.primary)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            await interaction.response.send_modal(DraftModal(self))
        except discord.HTTPException:
            # Discord a refusé la fenêtre avec le champ de fichier : on l'ouvre sans.
            log.exception("Fenêtre avec champ de fichier refusée : ouverture sans ce champ")
            await interaction.response.send_modal(DraftModal(self, with_upload=False))

    @discord.ui.button(label="Mention : aucune", emoji="🔕", style=discord.ButtonStyle.secondary)
    async def mention_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = MentionPicker(self)
        self.in_picker = True
        await interaction.response.edit_message(embed=picker.build_embed(), view=picker)

    @discord.ui.button(label="Joindre un fichier", emoji="📎", style=discord.ButtonStyle.secondary, disabled=True)
    async def attach_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Les fichiers se déposent en message privé : ça marche avec toutes les versions de discord.py."""
        if not self.drafts:
            await interaction.response.defer()
            return
        if self._waiting_file:
            await interaction.response.send_message(
                "⏳ J'attends déjà un fichier en message privé.", ephemeral=True
            )
            return

        draft = self.drafts[-1]
        number = len(self.drafts)
        await interaction.response.send_message("📩 Je t'écris en message privé…", ephemeral=True)
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                f"📎 Envoie-moi ici l'image ou le fichier pour la **question n°{number}** "
                f"({MAX_FILES_PER_QUESTION} fichiers maximum, 8 Mo au total). Tu as 2 minutes."
            )
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="⚠️ Je n'arrive pas à t'écrire en privé. Autorise les messages privés des "
                "membres de ce serveur, puis réessaie."
            )
            return
        await interaction.edit_original_response(
            content=f"📩 Je t'ai écrit en privé : [ouvre la conversation](https://discord.com/channels/@me/{dm.id}) "
            "et envoie-y ton fichier (2 minutes)."
        )

        self._waiting_file = True
        try:
            message = await interaction.client.wait_for(  # type: ignore[attr-defined]
                "message",
                check=lambda m: (
                    m.author.id == interaction.user.id and m.channel.id == dm.id and bool(m.attachments)
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            try:
                await dm.send("⌛ Temps écoulé. Clique de nouveau sur 📎 dans le panneau pour réessayer.")
            except discord.HTTPException:
                pass
            return
        finally:
            self._waiting_file = False

        reply = await self._attach_from_dm(draft, message.attachments)
        try:
            await dm.send(reply)
        except discord.HTTPException:
            pass
        if not self.in_picker:
            try:
                await self.interaction.edit_original_response(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass

    async def _attach_from_dm(self, draft: Draft, attachments: list[discord.Attachment]) -> str:
        """Ajoute les fichiers reçus en privé à la question ; retourne le message de réponse."""
        number = next((i for i, d in enumerate(self.drafts, start=1) if d is draft), None)
        if number is None:
            return "⚠️ Cette question a été retirée entre-temps : fichier ignoré."
        room = MAX_FILES_PER_QUESTION - len(draft.files)
        if room <= 0:
            return f"⚠️ Cette question a déjà {MAX_FILES_PER_QUESTION} fichiers."
        chosen = list(attachments)[:room]
        if sum(len(f.data) for f in draft.files) + sum(a.size for a in chosen) > MAX_QUESTION_FILES_BYTES:
            return "⚠️ Fichiers trop lourds : 8 Mo maximum au total pour une question."
        new: list[FileData] = []
        try:
            for attachment in chosen:
                new.append(FileData(attachment.filename, attachment.content_type, await attachment.read()))
        except discord.HTTPException:
            return "⚠️ Impossible de récupérer le fichier. Réessaie."
        total = sum(len(f.data) for d in self.drafts for f in d.files) + sum(len(f.data) for f in new)
        if total > MAX_PANEL_FILE_BYTES:
            return "⚠️ Trop de fichiers dans cet envoi (30 Mo maximum au total)."
        draft.files.extend(new)
        self._sync_buttons()
        return f"✅ {len(new)} fichier(s) ajouté(s) à la question n°{number}. Retourne sur le panneau."

    @discord.ui.button(label="Retirer la dernière", emoji="↩️", style=discord.ButtonStyle.secondary, disabled=True)
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.drafts:
            self.drafts.pop()
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Valider (0)", emoji="✅", style=discord.ButtonStyle.success, disabled=True)
    async def validate_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.drafts:
            await interaction.response.defer()
            return
        self.db.add_drafts(interaction.guild_id, self.drafts, self.channel.id, self.mention_text)
        timed = sum(1 for d in self.drafts if d.post_at)
        stock = len(self.drafts) - timed
        parts = []
        if timed:
            parts.append(f"{timed} à heure précise")
        if stock:
            parts.append(f"{stock} en stock")
        msg = f"✅ **{len(self.drafts)}** question(s) enregistrée(s) ({', '.join(parts)})."
        if stock:
            msg += "\n💡 Les questions en stock se postent avec `/nextquiz`."
        self.stop()
        await interaction.response.edit_message(content=msg, embed=None, view=None)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await self.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class MentionPicker(discord.ui.View):
    """Choix de la mention : rôles du serveur, personnes, @everyone, @here ou aucune."""

    def __init__(self, panel: QuestionPanel) -> None:
        super().__init__(timeout=600)
        self.panel = panel

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔔 Choisir la mention",
            description=(
                "Compose la mention que tu veux :\n"
                "• **Rôles du serveur** : un ou plusieurs rôles (ex. `@Quiz`, `@Membres`)\n"
                "• **Personnes** : une ou plusieurs personnes\n"
                "• Les deux menus se **combinent** ; chaque menu remplace son choix précédent\n"
                "• `@everyone` et `@here` remplacent tout le reste"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Choix actuel", value=self.panel.mention_summary(), inline=False)
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def _apply(self) -> None:
        """Met à jour la mention du panneau avec les rôles et personnes choisis."""
        self.panel.set_mention_from_selection([*self.panel.chosen_roles, *self.panel.chosen_users])

    def _clear_choices(self) -> None:
        self.panel.chosen_roles = []
        self.panel.chosen_users = []

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Rôles du serveur à mentionner",
        min_values=1,
        max_values=25,
        row=0,
    )
    async def pick_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        self.panel.chosen_roles = list(select.values)
        self._apply()
        await self._refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Personnes à mentionner",
        min_values=1,
        max_values=10,
        row=1,
    )
    async def pick_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.panel.chosen_users = list(select.values)
        self._apply()
        await self._refresh(interaction)

    @discord.ui.button(label="@everyone", style=discord.ButtonStyle.secondary, row=2)
    async def everyone_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._clear_choices()
        self.panel.set_mention("@everyone", "@everyone", needs_everyone_perm=True)
        await self._refresh(interaction)

    @discord.ui.button(label="@here", style=discord.ButtonStyle.secondary, row=2)
    async def here_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._clear_choices()
        self.panel.set_mention("@here", "@here", needs_everyone_perm=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Aucune mention", emoji="🔕", style=discord.ButtonStyle.secondary, row=2)
    async def none_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._clear_choices()
        self.panel.set_mention(None)
        await self._refresh(interaction)

    @discord.ui.button(label="Terminé", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def done_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.panel.in_picker = False
        self.stop()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)


class ImportModal(SafeModal, title="Importer des questions"):
    lines = discord.ui.TextInput(
        label="Une question par ligne",
        style=discord.TextStyle.paragraph,
        placeholder="Capitale de la France ? | Paris\n2 + 2 ? | 4 ; quatre",
        max_length=4000,
    )

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            import_questions(self.db, interaction.guild_id, self.lines.value), ephemeral=True
        )


class ConfirmEndView(discord.ui.View):
    def __init__(self, cog: "Quiz", guild_id: int, channel: discord.abc.Messageable) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.channel = channel

    @discord.ui.button(label="Terminer l'événement", emoji="🏁", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="⏳ Fin de l'événement…", view=None)
        report = await self.cog.finish_event(self.guild_id, self.channel)
        await interaction.edit_original_response(content=report)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Annulé : l'événement continue.", view=None)


class NextQuizView(discord.ui.View):
    """/nextquiz : toutes les questions en attente (stock + programmées) ; on en choisit une et on la poste."""

    PAGE_SIZE = 25  # limite d'un menu déroulant Discord

    def __init__(self, cog: "Quiz", interaction: discord.Interaction, rows: list[sqlite3.Row]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.interaction = interaction
        self.rows = rows
        self.page = 0
        self.selected_id: int | None = None
        self.select_item: discord.ui.Select | None = None
        self._render()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.rows) // self.PAGE_SIZE))

    def _selected_row(self) -> sqlite3.Row | None:
        return next((r for r in self.rows if r["id"] == self.selected_id), None)

    def _option(self, row: sqlite3.Row) -> discord.SelectOption:
        question = row["question"].replace("\n", " ")
        label = ("🕒 " if row["post_at"] else "📦 ") + question
        if len(label) > 100:
            label = label[:99] + "…"
        if row["post_at"]:
            when = datetime.fromisoformat(row["post_at"]).astimezone(TZ).strftime("%d/%m/%Y à %H:%M")
            description = f"Programmée le {when}"
        else:
            description = "En stock"
        if row["nfiles"]:
            description += f" · 📎 {row['nfiles']}"
        return discord.SelectOption(
            label=label,
            description=description[:100],
            value=str(row["id"]),
            default=row["id"] == self.selected_id,
        )

    def _render(self) -> None:
        """Reconstruit le menu de la page courante et l'état des boutons."""
        if self.select_item is not None:
            self.remove_item(self.select_item)
        start = self.page * self.PAGE_SIZE
        self.select_item = discord.ui.Select(
            placeholder=f"Choisis la question à poser (page {self.page + 1}/{self.pages})",
            options=[self._option(r) for r in self.rows[start : start + self.PAGE_SIZE]],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.select_item.callback = self._on_select
        self.add_item(self.select_item)
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.pages - 1
        self.post_button.disabled = self._selected_row() is None

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📣 Poser une question maintenant",
            description=(
                f"**{len(self.rows)}** question(s) en attente · page {self.page + 1}/{self.pages}\n"
                "🕒 = programmée · 📦 = en stock\n"
                "Choisis une question dans le menu, puis clique sur **Poser maintenant**. "
                "La question en cours, s'il y en a une, sera clôturée."
            ),
            color=discord.Color.blurple(),
        )
        row = self._selected_row()
        if row:
            embed.add_field(name="Question choisie", value=row["question"][:1000], inline=False)
            embed.add_field(
                name="Réponse(s)", value=", ".join(json.loads(row["answers"]))[:1000] or "—", inline=False
            )
            if row["post_at"]:
                when = discord.utils.format_dt(datetime.fromisoformat(row["post_at"]), "f")
                embed.add_field(name="Prévue", value=when)
            else:
                embed.add_field(name="Prévue", value="En stock")
            mention = row["mention_text"] or (
                (f"<@&{row['role_id']}>" if row["role_id"] else "@everyone") if row["mention"] else None
            )
            embed.add_field(name="Mention", value=mention or "🔕 Aucune")
            embed.add_field(name="Fichiers", value=str(row["nfiles"] or 0))
        return embed

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_id = int(self.select_item.values[0])  # type: ignore[union-attr]
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Poser maintenant", emoji="📣", style=discord.ButtonStyle.success, row=1, disabled=True)
    async def post_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        row = self.cog.db.get_pending_question(interaction.guild_id, self.selected_id or 0)
        if row is None:
            await interaction.response.send_message(
                "⚠️ Cette question n'est plus en attente : relance `/nextquiz`.", ephemeral=True
            )
            return

        # Salons possibles, dans l'ordre : celui prévu pour la question, celui du planning, celui-ci.
        guild = interaction.guild
        cfg = self.cog.db.get_schedule(interaction.guild_id)
        raw = [
            guild.get_channel(row["channel_id"]) if row["channel_id"] else None,
            guild.get_channel(cfg["channel_id"]) if cfg else None,
            interaction.channel,
        ]
        candidates: list = []
        for channel in raw:
            if isinstance(channel, discord.abc.Messageable) and all(
                getattr(channel, "id", None) != getattr(c, "id", None) for c in candidates
            ):
                candidates.append(channel)

        # On prend le premier salon où le bot a les permissions nécessaires.
        selected = self._selected_row()
        needs_files = bool(selected["nfiles"]) if selected else False
        target = None
        for channel in candidates:
            if not missing_permissions(channel, guild.me, needs_files):
                target = channel
                break
        if target is None:
            if not candidates:
                reason = "⚠️ Je ne trouve aucun salon où poster la question."
            else:
                first = candidates[0]
                missing = ", ".join(f"**{name}**" for name in missing_permissions(first, guild.me, needs_files))
                reason = (
                    f"❌ Je ne peux pas poster dans {getattr(first, 'mention', 'ce salon')} : "
                    f"il me manque {missing}.\n"
                    "Un administrateur de ce serveur doit me donner ces permissions "
                    "(sur mon rôle ou dans les réglages du salon), puis tu pourras réessayer."
                )
            await interaction.response.send_message(reason, ephemeral=True)
            return

        await interaction.response.defer()
        if await self.cog.post_question(interaction.guild_id, target, row):
            self.stop()
            note = ""
            if row["channel_id"] and getattr(target, "id", None) != row["channel_id"]:
                note = "\nℹ️ Le salon prévu pour cette question n'était pas utilisable : elle a été posée ici à la place."
            await interaction.edit_original_response(
                content=f"✅ Question posée dans {getattr(target, 'mention', 'le salon')}.{note}",
                embed=None,
                view=None,
            )
        else:
            detail = f"\nDétail Discord : `{self.cog.last_send_error}`" if self.cog.last_send_error else ""
            await interaction.edit_original_response(
                content=f"❌ Discord a refusé l'envoi dans {getattr(target, 'mention', 'ce salon')}.{detail}",
                embed=self.build_embed(),
                view=self,
            )

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.secondary, row=1)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Fermé.", embed=None, view=None)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await self.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class ConfirmDeleteAllView(discord.ui.View):
    def __init__(self, db: Database, guild_id: int) -> None:
        super().__init__(timeout=60)
        self.db = db
        self.guild_id = guild_id

    @discord.ui.button(label="Tout supprimer", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        deleted = self.db.delete_all_pending(self.guild_id)
        await interaction.response.edit_message(
            content=f"🗑️ **{deleted}** question(s) supprimée(s).", view=None
        )

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Annulé : rien n'a été supprimé.", view=None)


class ConfirmResetView(discord.ui.View):
    def __init__(self, db: Database) -> None:
        super().__init__(timeout=30)
        self.db = db

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.db.reset_scores(interaction.guild_id)
        self.stop()
        await interaction.response.edit_message(
            content="🧹 Tous les scores ont été remis à zéro.", view=None
        )

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Annulé.", view=None)


# --------------------------------------------------------------------------- #
# Cog principal
# --------------------------------------------------------------------------- #
def authorized_only():
    """Réservé à toi, à la personne qui a invité le bot et aux personnes ajoutées avec /add (sur ce serveur)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        db = interaction.client.db  # type: ignore[attr-defined]
        return db.has_access(interaction.user.id, interaction.guild_id)

    return app_commands.check(predicate)


def access_manager_only():
    """Réservé à toi et à la personne qui a invité le bot (pour /add), sur ce serveur."""

    async def predicate(interaction: discord.Interaction) -> bool:
        db = interaction.client.db  # type: ignore[attr-defined]
        return db.can_manage_access(interaction.user.id, interaction.guild_id)

    return app_commands.check(predicate)


class Quiz(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.active: dict[int, ActiveQuestion] = {}  # channel_id -> question en cours
        self.last_send_error: str | None = None      # raison du dernier refus d'envoi par Discord

    async def cog_load(self) -> None:
        for row in self.db.active_questions():
            self.active[row["channel_id"]] = to_active(row)
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    # ------------------------- Ajout de questions ------------------------- #
    @app_commands.command(
        name="addquiz", description="Poser des questions (jour, heure, mention et fichiers au choix)"
    )
    @app_commands.describe(
        salon="Salon où poser les questions (par défaut : le salon où tu tapes la commande)",
    )
    @app_commands.guild_only()
    @authorized_only()
    async def addquiz(
        self, interaction: discord.Interaction, salon: discord.TextChannel | None = None
    ) -> None:
        cfg = self.db.get_schedule(interaction.guild_id)
        channel = (
            salon
            or (interaction.guild.get_channel(cfg["channel_id"]) if cfg else None)
            or interaction.channel
        )
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "⚠️ Choisis un salon textuel avec l'option `salon`.", ephemeral=True
            )
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"⛔ Je ne peux pas écrire (avec des embeds) dans {channel.mention}.", ephemeral=True
            )
            return

        panel = QuestionPanel(self.db, interaction, channel)
        await interaction.response.send_message(embed=panel.build_embed(), view=panel, ephemeral=True)

    @app_commands.command(
        name="importquiz", description="Importer plusieurs questions (fenêtre ou fichier .txt)"
    )
    @app_commands.describe(fichier="Fichier texte UTF-8 : une question par ligne, « Question | Réponse »")
    @app_commands.guild_only()
    @authorized_only()
    async def importquiz(
        self, interaction: discord.Interaction, fichier: discord.Attachment | None = None
    ) -> None:
        if fichier is None:
            await interaction.response.send_modal(ImportModal(self.db))
            return
        if fichier.size > MAX_IMPORT_FILE_SIZE:
            await interaction.response.send_message("⚠️ Fichier trop volumineux.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text = (await fichier.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            await interaction.followup.send("⚠️ Le fichier doit être encodé en UTF-8.", ephemeral=True)
            return
        await interaction.followup.send(
            import_questions(self.db, interaction.guild_id, text), ephemeral=True
        )

    @app_commands.command(
        name="nextquiz", description="Choisir une question en attente (stock ou programmée) et la poser tout de suite"
    )
    @app_commands.guild_only()
    @authorized_only()
    async def nextquiz(self, interaction: discord.Interaction) -> None:
        rows = self.db.pending_questions(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "📭 Aucune question en attente. Ajoutes-en avec `/addquiz` ou `/importquiz`.", ephemeral=True
            )
            return
        view = NextQuizView(self, interaction, rows)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    # ------------------------- Rappel de la question ---------------------- #
    @app_commands.command(name="question", description="Rappeler la question en cours")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.channel_id)
    async def question(self, interaction: discord.Interaction) -> None:
        no_ping = discord.AllowedMentions.none()  # aucune mention, quoi qu'il arrive
        active = self.active.get(interaction.channel_id)
        if active is None:
            elsewhere = next(
                (cid for cid, a in self.active.items() if a.guild_id == interaction.guild_id), None
            )
            msg = (
                f"📭 Pas de question dans ce salon. La question en cours est dans <#{elsewhere}>."
                if elsewhere
                else "📭 Aucune question en cours."
            )
            await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=no_ping)
            return

        embed = discord.Embed(
            title="❓ Rappel de la question",
            description=active.question,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Réponds directement dans ce salon !")
        if active.message_id:
            url = f"https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{active.message_id}"
            embed.add_field(name="Message d'origine", value=f"[Aller à la question]({url})", inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=no_ping)

    # ------------------------------ Accès au bot --------------------------- #
    @app_commands.command(name="add", description="Donner (ou retirer) l'accès aux commandes du bot sur ce serveur")
    @app_commands.describe(
        utilisateur="Membre de ce serveur à qui donner l'accès",
        retirer="Mets True pour retirer son accès au lieu de le donner",
    )
    @app_commands.guild_only()
    @access_manager_only()
    async def add(
        self, interaction: discord.Interaction, utilisateur: discord.Member, retirer: bool = False
    ) -> None:
        guild_id = interaction.guild_id
        if utilisateur.bot:
            msg = "⚠️ Un bot ne peut pas être autorisé."
        elif utilisateur.id == OWNER_ID or self.db.is_guild_admin(guild_id, utilisateur.id):
            msg = f"ℹ️ {utilisateur.mention} a déjà tous les droits sur ce serveur."
        elif retirer:
            if self.db.remove_helper(guild_id, utilisateur.id):
                msg = f"🗑️ {utilisateur.mention} n'a plus accès aux commandes du bot sur ce serveur."
            else:
                msg = f"ℹ️ {utilisateur.mention} n'avait pas accès."
        else:
            if self.db.add_helper(guild_id, utilisateur.id):
                msg = f"✅ {utilisateur.mention} peut maintenant utiliser les commandes du bot sur ce serveur."
            else:
                msg = f"ℹ️ {utilisateur.mention} avait déjà accès."

        admins = self.db.list_guild_admins(guild_id)
        helpers = self.db.list_helpers(guild_id)
        lines = [msg]
        if admins:
            lines.append("👑 Accès complet : " + ", ".join(f"<@{uid}>" for uid in admins))
        lines.append(
            "✅ Autorisés : " + (", ".join(f"<@{uid}>" for uid in helpers) if helpers else "personne")
        )
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    # ---------------------------- Supprimer des questions ------------------ #
    @app_commands.command(name="supprimer", description="Supprimer la dernière question ou toutes celles en attente")
    @app_commands.describe(choix="Que veux-tu supprimer ?")
    @app_commands.choices(
        choix=[
            app_commands.Choice(name="La dernière question ajoutée", value="derniere"),
            app_commands.Choice(name="Toutes les questions en attente", value="toutes"),
        ]
    )
    @app_commands.guild_only()
    @authorized_only()
    async def supprimer(self, interaction: discord.Interaction, choix: app_commands.Choice[str]) -> None:
        guild_id = interaction.guild_id
        if choix.value == "derniere":
            row = self.db.delete_last_pending(guild_id)
            if row is None:
                await interaction.response.send_message("📭 Aucune question en attente.", ephemeral=True)
                return
            question = row["question"].replace("\n", " ")
            if len(question) > 150:
                question = question[:149] + "…"
            when = (
                f" (prévue le {discord.utils.format_dt(datetime.fromisoformat(row['post_at']), 'f')})"
                if row["post_at"]
                else " (en stock)"
            )
            await interaction.response.send_message(
                f"🗑️ Dernière question supprimée{when} :\n> {question}\n"
                f"Il reste **{self.db.pending_count(guild_id)}** question(s) en attente.",
                ephemeral=True,
            )
            return

        count = self.db.pending_count(guild_id)
        if count == 0:
            await interaction.response.send_message("📭 Aucune question en attente.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⚠️ Supprimer **toutes** les questions en attente (**{count}**) ? "
            "Cela concerne les questions en stock et les questions programmées, pas la question déjà postée. "
            "Cette action est irréversible.",
            view=ConfirmDeleteAllView(self.db, guild_id),
            ephemeral=True,
        )

    # ----------------------------- Fin de l'événement --------------------- #
    @app_commands.command(name="end", description="Terminer l'événement et annoncer le gagnant")
    @app_commands.guild_only()
    @authorized_only()
    async def end_event(self, interaction: discord.Interaction) -> None:
        winners = self.db.winners(interaction.guild_id)
        if not winners:
            await interaction.response.send_message(
                "📭 Personne n'a encore de point : il n'y a pas de gagnant à annoncer.", ephemeral=True
            )
            return

        cfg = self.db.get_schedule(interaction.guild_id)
        channel = (
            interaction.guild.get_channel(cfg["channel_id"]) if cfg else None
        ) or interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message("⚠️ Salon d'annonce introuvable.", ephemeral=True)
            return

        mentions = " ".join(f"<@{w['user_id']}>" for w in winners)
        label = "Gagnant" if len(winners) == 1 else "Égalité"
        points = winners[0]["points"]
        await interaction.response.send_message(
            f"🏁 **Terminer l'événement ?**\n"
            f"🏆 {label} : {mentions} avec **{points}** point{'s' if points > 1 else ''}\n\n"
            f"L'annonce sera postée dans {getattr(channel, 'mention', 'ce salon')}. "
            "Cela clôture aussi la question en cours et annule les questions "
            "programmées à heure précise (les questions en stock restent).",
            view=ConfirmEndView(self, interaction.guild_id, channel),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def finish_event(self, guild_id: int, channel: discord.abc.Messageable) -> str:
        """Termine l'événement, annonce le gagnant ; retourne le message pour l'administrateur."""
        winners = self.db.winners(guild_id)
        if not winners:
            return "📭 Plus aucun point : l'événement n'a pas été terminé."
        points = winners[0]["points"]
        plural = "s" if points > 1 else ""

        # 1) On arrête tout : planning, question en cours, questions programmées
        self.db.disable_schedule(guild_id)
        previous = self.db.expire_active(guild_id)
        if previous:
            self.active.pop(previous["channel_id"], None)
        cancelled = self.db.cancel_timed(guild_id)

        # 2) Annonce publique : le(s) gagnant(s) sont mentionnés
        mentions = " ".join(f"<@{w['user_id']}>" for w in winners)
        ticket = f" dans <#{TICKET_CHANNEL_ID}>" if TICKET_CHANNEL_ID else ""
        if len(winners) == 1:
            text = (
                f"🏆 Bravo {mentions} ! Tu remportes l'événement avec **{points} point{plural}**.\n"
                f"🎟️ Ouvre un ticket{ticket} pour récupérer ta récompense."
            )
        else:
            text = (
                f"🏆 Égalité ! Bravo {mentions} : vous terminez avec **{points} point{plural}** chacun.\n"
                f"🎟️ Ouvrez un ticket{ticket} pour récupérer votre récompense."
            )
        embed = discord.Embed(
            title="🏁 L'événement est terminé !",
            description=text,
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        if previous:
            solution = json.loads(previous["answers"])[0]
            embed.add_field(
                name="Question en cours clôturée", value=f"Réponse : **{solution[:900]}**", inline=False
            )

        channel_name = getattr(channel, "mention", "le salon")
        try:
            await channel.send(
                content=mentions,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=w["user_id"]) for w in winners]
                ),
            )
            announced = f"📣 Annonce envoyée dans {channel_name}."
        except discord.HTTPException:
            log.exception("Impossible d'annoncer le gagnant")
            announced = f"⚠️ Je n'ai pas pu envoyer l'annonce dans {channel_name} : préviens le gagnant toi-même."

        # 3) Message pour la personne qui a lancé /end
        label = "🏆 Gagnant" if len(winners) == 1 else "🏆 Égalité"
        lines = [
            f"✅ Événement terminé. {label} : {mentions} avec **{points}** point{plural}.",
            announced,
        ]
        if cancelled:
            lines.append(f"🗑️ {cancelled} question(s) programmée(s) annulée(s).")
        lines.append("ℹ️ Les scores sont conservés : utilise `/reset` pour repartir de zéro.")
        return "\n".join(lines)

    # ------------------------ Classement et reset ------------------------- #
    @app_commands.command(name="leaderboard", description="Afficher le classement des meilleurs joueurs")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = self.db.top(interaction.guild_id, LEADERBOARD_SIZE)
        if not rows:
            await interaction.response.send_message("📭 Personne n'a encore de point.", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            rank = medals[i] if i < len(medals) else f"**{i + 1}.**"
            plural = "s" if row["points"] > 1 else ""
            lines.append(f"{rank} <@{row['user_id']}> — **{row['points']}** point{plural}")

        embed = discord.Embed(
            title="🏆 Classement du quiz",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        own = self.db.score(interaction.guild_id, interaction.user.id)
        embed.set_footer(text=f"Ton score : {own} point(s)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reset", description="Remettre tous les scores à zéro")
    @app_commands.guild_only()
    @authorized_only()
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "⚠️ Remettre **tous** les scores à zéro ? Cette action est irréversible.",
            view=ConfirmResetView(self.db),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Patiente encore {error.retry_after:.0f} s avant de réutiliser cette commande."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "⛔ Tu n'as pas la permission d'utiliser cette commande."
        else:
            log.exception("Erreur dans une commande slash", exc_info=error)
            msg = "❌ Une erreur est survenue."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # --------------------- Poster / expirer une question ------------------- #
    async def post_question(
        self, guild_id: int, channel: discord.abc.Messageable, row: sqlite3.Row
    ) -> bool:
        self.last_send_error = None
        # L'ancienne question est désactivée dès qu'on en poste une nouvelle.
        previous = self.db.expire_active(guild_id)
        if previous:
            self.active.pop(previous["channel_id"], None)

        embed = discord.Embed(
            title="❓ Question du jour",
            description=row["question"],
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Réponds directement dans ce salon !")
        if previous:
            solution = json.loads(previous["answers"])[0]
            embed.add_field(
                name="⌛ Question précédente expirée",
                value=f"Personne n'a trouvé. Réponse : **{solution[:900]}**",
                inline=False,
            )

        mention_text = row["mention_text"]
        if not mention_text and row["mention"]:  # questions créées avant le choix de la mention
            mention_text = f"<@&{row['role_id']}>" if row["role_id"] else "@everyone"
        content = mention_text or None
        allowed = allowed_mentions_for(mention_text)

        # Fichiers / image joints à la question (stockés dans la base à la création)
        files: list[discord.File] = []
        used_names: set[str] = set()
        image_set = False
        for stored in self.db.get_files(row["id"]):
            name = safe_filename(stored["filename"], used_names)
            files.append(discord.File(io.BytesIO(stored["data"]), filename=name))
            if not image_set and is_image(name):
                embed.set_image(url=f"attachment://{name}")
                image_set = True

        message = None
        for attempt in (files, []):
            try:
                message = await channel.send(
                    content=content, embed=embed, files=attempt, allowed_mentions=allowed
                )
                break
            except discord.HTTPException as err:
                self.last_send_error = f"{err.status} (code {err.code}) {err.text}"
                log.exception("Impossible d'envoyer la question dans le salon %s", channel.id)  # type: ignore[attr-defined]
                if not attempt:
                    break
                embed.set_image(url=None)  # nouvel essai sans les fichiers
        if message is None:
            return False

        self.db.delete_files(row["id"])
        self.db.mark_used(row["id"])
        self.db.set_active(guild_id, row["id"], message.channel.id, message.id)
        active = to_active(row)
        active.message_id = message.id
        self.active[message.channel.id] = active
        return True

    # ----------------------------- Planificateur --------------------------- #
    @tasks.loop(seconds=30)
    async def scheduler(self) -> None:
        # 1) Questions à heure précise
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in self.db.due_timed_questions(now_iso):
            try:
                await self._run_timed(row)
            except Exception:
                log.exception("Erreur lors d'une question à heure précise (id %s)", row["id"])
                self.db.mark_used(row["id"])

        # 2) Stock quotidien
        now = datetime.now(TZ)
        today = now.date().isoformat()
        for cfg in self.db.enabled_schedules():
            if cfg["last_posted"] == today or (now.hour, now.minute) < (cfg["hour"], cfg["minute"]):
                continue
            try:
                await self._run_scheduled(cfg, today)
            except Exception:
                log.exception("Erreur lors de la question programmée (serveur %s)", cfg["guild_id"])
                # On note quand même le jour pour ne pas retenter toutes les 30 secondes.
                self.db.update_schedule_after_post(cfg["guild_id"], today, cfg["days_left"], True)

    @scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_timed(self, row: sqlite3.Row) -> None:
        channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
        if not isinstance(channel, discord.abc.Messageable):
            log.error("Salon introuvable pour la question %s", row["id"])
            self.db.mark_used(row["id"])
            return
        if not await self.post_question(row["guild_id"], channel, row):
            self.db.mark_used(row["id"])  # évite de réessayer toutes les 30 secondes

    async def _run_scheduled(self, cfg: sqlite3.Row, today: str) -> None:
        guild_id = cfg["guild_id"]
        days_left = cfg["days_left"]

        channel = self.bot.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.abc.Messageable):
            log.error("Salon du quiz introuvable (serveur %s)", guild_id)
            self.db.update_schedule_after_post(guild_id, today, days_left, True)
            return

        row = self.db.next_question(guild_id)
        if row is None:
            self.db.disable_schedule(guild_id)
            try:
                await channel.send(
                    "📭 Le stock de questions est vide : la planification est arrêtée. "
                    "Un administrateur peut ajouter des questions avec `/addquiz`."
                )
            except discord.HTTPException:
                pass
            return

        if await self.post_question(guild_id, channel, row):
            days_left -= 1
        self.db.update_schedule_after_post(guild_id, today, days_left, enabled=days_left > 0)

    # ------------------- Accès pour la personne qui ajoute le bot ----------- #
    async def register_inviter(self, guild: discord.Guild, wait: bool = False) -> None:
        """Donne accès aux commandes (sur ce serveur) à la personne qui a ajouté le bot.

        1) les logs du serveur (si le bot a la permission « Voir les logs du serveur »)
        2) l'événement d'intégration envoyé par Discord (voir on_integration_create)
        3) à défaut, le propriétaire du serveur
        """
        inviter_id = None
        try:
            async for entry in guild.audit_logs(limit=25, action=discord.AuditLogAction.bot_add):
                if entry.target is not None and entry.target.id == self.bot.user.id:  # type: ignore[union-attr]
                    inviter_id = entry.user_id
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass  # le bot n'a pas la permission de lire les logs
        if inviter_id is not None:
            if self.db.add_guild_admin(guild.id, inviter_id):
                log.info("Accès aux commandes donné à %s sur le serveur %s (logs)", inviter_id, guild.id)
            return

        if wait:
            await asyncio.sleep(10)  # laisse à Discord le temps d'envoyer l'événement d'intégration
        if self.db.has_guild_admin(guild.id):
            return
        if guild.owner_id and self.db.add_guild_admin(guild.id, guild.owner_id):
            log.info("Personne qui a invité le bot introuvable sur %s : accès donné au propriétaire.", guild.id)

    @commands.Cog.listener()
    async def on_integration_create(self, integration: discord.Integration) -> None:
        """Quand le bot est ajouté, Discord crée une « intégration » qui indique qui l'a ajouté."""
        try:
            application = getattr(integration, "application", None)
            bot_user = getattr(application, "user", None)
            if bot_user is None or bot_user.id != self.bot.user.id:  # type: ignore[union-attr]
                return  # autre intégration (Twitch, YouTube, autre bot...)
            user = integration.user
            if user is not None and not user.bot:
                if self.db.add_guild_admin(integration.guild.id, user.id):
                    log.info(
                        "Accès aux commandes donné à %s sur le serveur %s (intégration)",
                        user.id,
                        integration.guild.id,
                    )
        except Exception:
            log.exception("Lecture de l'intégration impossible")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.register_inviter(guild, wait=True)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Serveurs où le bot était déjà présent : on prépare l'accès s'il n'existe pas encore.
        for guild in self.bot.guilds:
            if not self.db.has_guild_admin(guild.id):
                await self.register_inviter(guild)

    # ------------------------------ Écoute du chat ------------------------- #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        active = self.active.get(message.channel.id)
        if active is None or normalize(message.content) not in active.answers:
            return

        # Bloc synchrone jusqu'au prochain « await » : un seul gagnant, même si
        # plusieurs bonnes réponses arrivent au même moment.
        self.active.pop(message.channel.id, None)
        if not self.db.close_active(active.guild_id, active.question_id):
            return
        total = self.db.add_point(message.guild.id, message.author.id)

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(
                f"🎉 Bravo {message.author.mention} ! Tu gagnes **1 point** (total : **{total}**).",
                allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )
        except discord.HTTPException:
            log.exception("Impossible d'annoncer le gagnant")


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class QuizBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # à activer aussi dans le portail développeur
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = Database(DB_PATH)

    async def setup_hook(self) -> None:
        await self.add_cog(Quiz(self, self.db))
        if not SUPPORTS_FILE_UPLOAD:
            log.warning("discord.py trop ancien : champ de fichier désactivé (pip install -U discord.py).")
        await sutom.setup(self)  # commandes + page du SUTOM (doit passer avant la synchronisation)
        try:
            await self.load_extension("economie")  # système d'économie et de boutique (economie.py)
        except Exception:
            log.exception("Impossible de charger economie.py : le bot continue sans l'économie.")
        synced = await self.tree.sync()
        log.info("%d commande(s) slash synchronisée(s).", len(synced))

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (ID %s)", self.user, self.user.id)  # type: ignore[union-attr]


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "Token introuvable : crée un fichier .env à côté de quiz_bot.py "
            "contenant la ligne DISCORD_TOKEN=ton_token"
        )
    # Keep alive : assuré par sutom.py (route /ping), l'ancien keep_alive.py n'est plus lancé.
    QuizBot().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()

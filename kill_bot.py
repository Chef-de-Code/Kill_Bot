# kill_bot.py
# Discord.py v2+ bot with:
# - /bosspick: pick bosses -> roll -> confirm -> role signup board (multi-role, max unique enforced) + Ready Now
# - /updaterole: staff assign/remove roles for others on latest GoTime signup board in the channel
# - /pk: PK scoreboard + record 1v1 PKs (persistent to pk_data.json)
# - /teampenguin: show + staff add/remove/clear (persistent)
# - /blamekyle: Kyle blame engine + persistent KGP investigation number
# - /pvmtonight + /gotime: private PVM availability poll + collated eligible boss picker + signup sheet
# - /blameuser: blame any selected user with KGP-approved nonsense
# - /remindme: persistent user reminder command
# - /updatebot: staff restart command for GitHub pull via launcher
# - /rsassign: staff assign RSNs to Discord users for tracking
# - /kbcommands: help command listing formats
# - /rank + /rankboard + /rankadmin: activity points and earnable rank progression

import os
import json
import random
import asyncio
import urllib.parse
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from pathlib import Path

import discord
import aiohttp
from discord import app_commands
from dotenv import load_dotenv

# -----------------------------
# Timezone handling (Windows may need tzdata package)
# -----------------------------
try:
    from zoneinfo import ZoneInfo
    from zoneinfo import ZoneInfoNotFoundError
except Exception:
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = 852625963820777482  # Josh

# Optional: fast command sync to specific guilds (comma-separated IDs)
# Example: GUILD_IDS=977234828476968970,1138219849131241542
GUILD_IDS_ENV = os.getenv("GUILD_IDS", "").strip()

LOCAL_TZ_NAME = "Europe/London"
EVENT_DURATION_MINUTES = 60


def get_local_tz():
    """Return tzinfo for Europe/London; fall back to UTC if tzdata missing."""
    if ZoneInfo is None:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(LOCAL_TZ_NAME)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


LOCAL_TZ = get_local_tz()
UTC_TZ = ZoneInfo("UTC") if ZoneInfo else None


def now_unix_utc() -> int:
    if UTC_TZ:
        return int(datetime.now(tz=UTC_TZ).timestamp())
    return int(datetime.utcnow().timestamp())


BOT_START_UNIX = now_unix_utc()


INCIDENT_REPORTS_CHANNEL_NAME = "incident-reports"
DAILY_PVM_CHANNEL_NAME = "daily-pvm"
ROOKERY_CHANNEL_NAME = "the-rookery"
GAME_TIME_OFFSET_HOURS = -1  # RuneScape/Game Time is UTC and stays one hour behind UK during BST.
AUTO_PVM_TASK_STARTED = False

# Incident report batching prevents Discord 429 rate limits caused by logging every event as its own message.
INCIDENT_LOG_BUFFER: List[str] = []
INCIDENT_LOG_WORKER_STARTED = False
INCIDENT_LOG_FLUSH_SECONDS = 30
INCIDENT_LOG_MAX_LINES_PER_POST = 20


# These messages still print to the local console, but are not mirrored into #incident-reports.
# This keeps Discord cleaner while still letting you debug filters locally.
INCIDENT_LOG_SUPPRESS_PHRASES = (
    "activity ignored by filter",
    "suppressed as low-value drop",
    "xp activity ignored",
    "ignored because",
    "filtered",
)


def should_mirror_to_incident_reports(message: str) -> bool:
    lowered = message.lower()
    return not any(phrase in lowered for phrase in INCIDENT_LOG_SUPPRESS_PHRASES)


def log_event(message: str):
    """Write all events to console; mirror only non-filter/noise events to #incident-reports."""
    stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)

    # Keep filter/debug noise on the console only.
    if should_mirror_to_incident_reports(message):
        INCIDENT_LOG_BUFFER.append(line)


async def incident_report_worker(client: discord.Client):
    """Batch console logs into #incident-reports so Discord is not spammed/rate-limited."""
    await client.wait_until_ready()

    while not client.is_closed():
        await asyncio.sleep(INCIDENT_LOG_FLUSH_SECONDS)

        if not INCIDENT_LOG_BUFFER:
            continue

        # Pull a bounded batch, leaving any excess for the next cycle.
        batch = INCIDENT_LOG_BUFFER[:INCIDENT_LOG_MAX_LINES_PER_POST]
        del INCIDENT_LOG_BUFFER[:INCIDENT_LOG_MAX_LINES_PER_POST]

        text = "\n".join(batch)
        if len(text) > 1850:
            text = text[-1850:]

        try:
            for guild in client.guilds:
                channel = discord.utils.get(guild.text_channels, name=INCIDENT_REPORTS_CHANNEL_NAME)
                if channel:
                    await channel.send(f"```{text}```")
                    await asyncio.sleep(1.2)
        except Exception as e:
            # Do not call log_event here, or logging failures can loop forever.
            print(f"[IncidentReportWorker] Failed to post batched logs: {e}", flush=True)


def find_text_channel(guild: Optional[discord.Guild], name: str) -> Optional[discord.TextChannel]:
    if not guild:
        return None
    return discord.utils.get(guild.text_channels, name=name)


def game_time_for_local(dt: datetime) -> datetime:
    return dt + timedelta(hours=GAME_TIME_OFFSET_HOURS)


def format_uk_game_time(hour: int, minute: int = 0) -> str:
    uk = datetime.now(LOCAL_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    gt = game_time_for_local(uk)
    return f"{uk:%H:%M} UK / {gt:%H:%M} Game Time"


# -----------------------------
# Boss definitions
# -----------------------------
BOSS_DATA: Dict[str, Dict] = {
    "Vorago": {"max_group": 7, "roles": ["Base Tank", "Bomb Tank", "TL5", "DPS", "DPS", "DPS", "DPS"]},
    "Solak": {"max_group": 7, "roles": ["Base Tank", "Elf 1", "Elf 2", "DPS", "DPS", "DPS", "DPS"]},
    "Rise of the Six": {"max_group": 4, "roles": ["East Runner", "East", "West Runner", "West"]},
    "Araxxor": {"max_group": 2, "roles": ["Base Tank", "DPS"]},
    "Kalphite King": {"max_group": 3, "roles": ["Base Tank", "Voker", "DPS"]},
    "Angel Of Death": {"max_group": 7, "roles": ["Base Tank", "MTU", "MTG", "MTC", "MTF", "Ham", "SC"]},
    "Croesus": {"max_group": 4, "roles": ["Hunter", "Fishing", "Mining", "Woodcutting"]},
    "HM Sanctum": {"max_group": 4, "roles": ["Base Tank", "DPS", "DPS", "DPS"]},
    "Zamorak": {"max_group": 5, "roles": ["Base Tank", "Witch", "DPS", "DPS", "DPS"]},
    "ED1": {"max_group": 3, "roles": []},
    "ED2": {"max_group": 3, "roles": []},
    "ED3": {"max_group": 3, "roles": []},
    "GWD 1": {"max_group": 5, "roles": []},
    "GWD 2": {"max_group": 5, "roles": []},
    "The Gate of Elidinis": {"max_group": 10, "roles": []},
    "Beastmaster Duzag": {"max_group": 10, "roles": ["Base Tank", "Pet 1/3", "Pet 2", "NC"]},
    "Yakamaru": {
        "max_group": 10,
        "roles": ["Base Tank", "NT", "PT1", "PT2", "PT3", "PT4 CPR", "ST0", "JW", "ST5 1", "ST5 2", "SH10", "MS"],
    },
    "Raksha": {"max_group": 2, "roles": ["Base Tank", "DPS"]},
    "Zemouregal & Vorkath": {"max_group": 10, "roles": ["Vorkath Tank", "Zemouregal Tank", "DPS", "DPS", "DPS"]},
    "Amascut": {
        "max_group": 5,
        "roles": [
            "Base Tank",
            "West in (P7 NW)",
            "West out (P7 SW)",
            "East in (P7 NE)",
            "East out (P7 SE)",
            "Green 1",
            "Green 2",
            "Solo Charge 1",
            "Solo Charge 2",
            "Dogs",
            "Glyphs",
            "Jumper",
        ],
    },
}
BOSS_NAMES = list(BOSS_DATA.keys())

# Generic poll reaction emojis
POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# -----------------------------
# PK tracking (persistent JSON)
# -----------------------------
PK_DATA_FILE = Path(__file__).with_name("pk_data.json")
PK_SCORES: Dict[int, int] = {}
PK_HISTORY: List[Dict[str, str]] = []


def pk_load():
    global PK_SCORES, PK_HISTORY
    if not PK_DATA_FILE.exists():
        PK_SCORES, PK_HISTORY = {}, []
        return
    try:
        data = json.loads(PK_DATA_FILE.read_text(encoding="utf-8"))
        PK_SCORES = {int(k): int(v) for k, v in data.get("scores", {}).items()}
        PK_HISTORY = list(data.get("history", []))
    except Exception:
        PK_SCORES, PK_HISTORY = {}, []


def pk_save():
    data = {"scores": {str(k): v for k, v in PK_SCORES.items()}, "history": PK_HISTORY[-200:]}
    PK_DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# -----------------------------
# KGP Investigation Counter (persistent)
# -----------------------------
KGP_FILE = Path(__file__).with_name("kgp_data.json")
KGP_INVESTIGATION_NUMBER = 0


def kgp_load():
    global KGP_INVESTIGATION_NUMBER
    if not KGP_FILE.exists():
        KGP_INVESTIGATION_NUMBER = 0
        return
    try:
        data = json.loads(KGP_FILE.read_text(encoding="utf-8"))
        KGP_INVESTIGATION_NUMBER = int(data.get("investigation_number", 0))
    except Exception:
        KGP_INVESTIGATION_NUMBER = 0


def kgp_save():
    KGP_FILE.write_text(json.dumps({"investigation_number": KGP_INVESTIGATION_NUMBER}, indent=2), encoding="utf-8")


# -----------------------------
# Team Penguin (persistent)
# -----------------------------
TEAM_PENGUIN_FILE = Path(__file__).with_name("teampenguin.json")
TEAM_PENGUIN: List[int] = []


def teampenguin_load():
    global TEAM_PENGUIN
    if not TEAM_PENGUIN_FILE.exists():
        TEAM_PENGUIN = []
        return
    try:
        data = json.loads(TEAM_PENGUIN_FILE.read_text(encoding="utf-8"))
        TEAM_PENGUIN = [int(x) for x in data.get("members", [])]
    except Exception:
        TEAM_PENGUIN = []


def teampenguin_save():
    TEAM_PENGUIN_FILE.write_text(json.dumps({"members": TEAM_PENGUIN}, indent=2), encoding="utf-8")


# -----------------------------
# PVM Tonight Polls (persistent)
# -----------------------------
PVM_POLL_FILE = Path(__file__).with_name("pvm_polls.json")

# poll_message_id -> {user_id -> [bosses]}
PVM_POLLS: Dict[int, Dict[int, List[str]]] = {}
# poll_message_id -> {"free": [user_ids], "not_free": [user_ids]}
PVM_AVAILABILITY: Dict[int, Dict[str, List[int]]] = {}
# poll_message_id -> {user_id -> confirmed_bool}
PVM_CONFIRMED: Dict[int, Dict[int, bool]] = {}

# channel_id -> latest poll_message_id
CHANNEL_LATEST_PVM_POLL: Dict[int, int] = {}


def pvm_load():
    global PVM_POLLS, PVM_AVAILABILITY, PVM_CONFIRMED, CHANNEL_LATEST_PVM_POLL
    if not PVM_POLL_FILE.exists():
        PVM_POLLS = {}
        PVM_AVAILABILITY = {}
        PVM_CONFIRMED = {}
        CHANNEL_LATEST_PVM_POLL = {}
        return
    try:
        raw = json.loads(PVM_POLL_FILE.read_text(encoding="utf-8"))

        # Backwards compatibility: old format was {poll_id: {user_id: [bosses]}}.
        if raw and all(isinstance(v, dict) and "votes" not in v for v in raw.values()):
            PVM_POLLS = {int(pid): {int(uid): list(bosses) for uid, bosses in users.items()} for pid, users in raw.items()}
            PVM_AVAILABILITY = {pid: {"free": list(users.keys()), "not_free": []} for pid, users in PVM_POLLS.items()}
            PVM_CONFIRMED = {pid: {} for pid in PVM_POLLS}
            CHANNEL_LATEST_PVM_POLL = {}
            return

        PVM_POLLS = {
            int(pid): {int(uid): list(bosses) for uid, bosses in data.get("votes", {}).items()}
            for pid, data in raw.get("polls", {}).items()
        }
        PVM_AVAILABILITY = {
            int(pid): {
                "free": [int(x) for x in data.get("free", [])],
                "not_free": [int(x) for x in data.get("not_free", [])],
            }
            for pid, data in raw.get("availability", {}).items()
        }
        PVM_CONFIRMED = {
            int(pid): {int(uid): bool(value) for uid, value in data.items()}
            for pid, data in raw.get("confirmed", {}).items()
        }
        CHANNEL_LATEST_PVM_POLL = {int(cid): int(pid) for cid, pid in raw.get("latest_by_channel", {}).items()}
    except Exception as e:
        log_event(f"Failed to load PVM poll data: {e}")
        PVM_POLLS = {}
        PVM_AVAILABILITY = {}
        PVM_CONFIRMED = {}
        CHANNEL_LATEST_PVM_POLL = {}


def pvm_save():
    serial = {
        "polls": {str(pid): {"votes": {str(uid): bosses for uid, bosses in users.items()}} for pid, users in PVM_POLLS.items()},
        "availability": {str(pid): {"free": data.get("free", []), "not_free": data.get("not_free", [])} for pid, data in PVM_AVAILABILITY.items()},
        "confirmed": {str(pid): {str(uid): value for uid, value in users.items()} for pid, users in PVM_CONFIRMED.items()},
        "latest_by_channel": {str(cid): pid for cid, pid in CHANNEL_LATEST_PVM_POLL.items()},
    }
    PVM_POLL_FILE.write_text(json.dumps(serial, indent=2), encoding="utf-8")


# -----------------------------
# Remind Me (persistent)
# -----------------------------
REMINDER_FILE = Path(__file__).with_name("reminders.json")
REMINDERS: Dict[str, Dict] = {}
ACTIVE_REMINDER_TASKS: set[str] = set()


def reminders_load():
    global REMINDERS
    if not REMINDER_FILE.exists():
        REMINDERS = {}
        return
    try:
        REMINDERS = json.loads(REMINDER_FILE.read_text(encoding="utf-8"))
    except Exception:
        REMINDERS = {}


def reminders_save():
    REMINDER_FILE.write_text(json.dumps(REMINDERS, indent=2), encoding="utf-8")



# -----------------------------
# RuneScape / RuneMetrics Achievement Tracking (persistent)
# -----------------------------
RSN_DATA_FILE = Path(__file__).with_name("rsn_tracking.json")

RSN_REGISTRATIONS: Dict[int, str] = {}          # user_id -> RSN
RSN_DISCORD_NAMES: Dict[int, str] = {}           # user_id -> last known Discord display name
RSN_LAST_ACTIVITY_KEYS: Dict[int, List[str]] = {}
RSN_PROFILE_BASELINES: Dict[int, Dict] = {}      # user_id -> profile snapshot baseline
RSN_ACHIEVEMENT_CHANNELS: Dict[int, int] = {}   # guild_id -> channel_id
ACHIEVEMENT_TASK_STARTED = False
ACHIEVEMENT_POLL_SECONDS = 300                  # 5 minutes
SKILL_XP_MILESTONES = [50_000_000, 100_000_000, 150_000_000, 200_000_000]  # post when a skill crosses these XP milestones
SKILL_LEVEL_XP_MILESTONES = {99: 13_034_431, 110: 38_737_661, 120: 104_273_167}  # post when a skill crosses these exact XP milestones
PERMANENT_ACHIEVEMENT_CHANNEL_NAME = "kill-bot-achievements"
HTTP_SESSION: Optional[aiohttp.ClientSession] = None

# Long-term RuneMetrics snapshots for /gains. This is forward-looking: it starts
# tracking from the moment this version runs and improves over time.
RSN_HISTORY_FILE = Path(__file__).with_name("rsn_history.json")
RSN_HISTORY: Dict[int, List[Dict[str, Any]]] = {}
RSN_HISTORY_MAX_DAYS = 370
GE_SEARCH_CACHE: Dict[str, Dict[str, Any]] = {}


# RuneMetrics skillvalue IDs. If Jagex adds a new skill, add it here.
SKILL_ID_TO_NAME = {
    0: "Attack",
    1: "Defence",
    2: "Strength",
    3: "Constitution",
    4: "Ranged",
    5: "Prayer",
    6: "Magic",
    7: "Cooking",
    8: "Woodcutting",
    9: "Fletching",
    10: "Fishing",
    11: "Firemaking",
    12: "Crafting",
    13: "Smithing",
    14: "Mining",
    15: "Herblore",
    16: "Agility",
    17: "Thieving",
    18: "Slayer",
    19: "Farming",
    20: "Runecrafting",
    21: "Hunter",
    22: "Construction",
    23: "Summoning",
    24: "Dungeoneering",
    25: "Divination",
    26: "Invention",
    27: "Archaeology",
    28: "Necromancy",
}

# Low-value drop/activity text filters. Add more items here if Kill Bot is posting noisy drops.
LOW_VALUE_DROP_ITEMS = {
    "abyssal whip",
    "dragon med helm",
    "dragon medium helm",
    "dragon helm",
    "dragon dagger",
    "dragon longsword",
    "dragon scimitar",
    "dragon spear",
    "dragon 2h sword",
    "dragon battleaxe",
    "dragon platelegs",
    "dragon plateskirt",
    "dragon boots",
    "rune platebody",
    "rune platelegs",
    "rune kiteshield",
    "rune full helm",
    "whip vine",
    "tuska wrath ability codex",
    "crystal triskelion fragment",
    "crystal triselion fragment",
    "glavien wing-tip",
    "glavien wing tip",
    "latent offering",
    "abyssal wand",
    "abyssal orb",
    "dark bow",
    "blood necklace shard",
    "celestial handwraps",
    "crystal triskelion fragment 1",
    "crystal triskelion fragment 2",
    "crystal triskelion fragment 3",
    "demon slayer boots",
    "demon slayer circlet",
    "demon slayer crossbow",
    "demon slayer gloves",
    "demon slayer skirt",
    "demon slayer torso",
    "dormant anima core body",
    "dormant anima core helm",
    "dormant anima core legs",
    "draconic visage",
    "dragon chainbody",
    "dragon claw",
    "dragon full helm",
    "dragon hatchet",
    "dragon kiteshield",
    "dragon limbs",
    "focus sight",
    "glaiven boots",
    "god sword shard 1",
    "godsword shard 1",
    "god sword shard 2",
    "godsword shard 2",
    "god sword shard 3",
    "godsword shard 3",
    "gown of subjugation",
    "granite legs",
    "granite maul",
    "hexcrest",
    "leaf-bladed sword",
    "necromancer kit",
    "pneumatic gloves",
    "ragefire boots",
    "razorback gauntlets",
    "saradomin's hiss",
    "saradomin's murmur",
    "saradomin's whisper",
    "saradomin hilt",
    "saradomin sword",
    "seers' ring",
    "shield left half",
    "staff of light",
    "starved ancient effigy",
    "static gloves",
    "steadfast boots",
    "steam battlestaff",
    "tracking gloves",
    "warrior ring",
}

# Staff-managed low-value/drop suppression entries live outside Git so each host can tune them safely.
CUSTOM_LOW_VALUE_DROP_FILE = Path(__file__).with_name("custom_low_value_drops.json")
CUSTOM_LOW_VALUE_DROP_ITEMS: set[str] = set()


def normalise_drop_item_name(item: str) -> str:
    return " ".join(str(item).lower().replace("’", "'").replace("triselion", "triskelion").strip(" .").split())


# Normalise the static table once so matching is consistent.
LOW_VALUE_DROP_ITEMS = {normalise_drop_item_name(item) for item in LOW_VALUE_DROP_ITEMS}


def custom_low_value_load():
    global CUSTOM_LOW_VALUE_DROP_ITEMS
    if not CUSTOM_LOW_VALUE_DROP_FILE.exists():
        CUSTOM_LOW_VALUE_DROP_ITEMS = set()
        return
    try:
        data = json.loads(CUSTOM_LOW_VALUE_DROP_FILE.read_text(encoding="utf-8"))
        CUSTOM_LOW_VALUE_DROP_ITEMS = {normalise_drop_item_name(x) for x in data.get("items", []) if str(x).strip()}
        log_event(f"Loaded {len(CUSTOM_LOW_VALUE_DROP_ITEMS)} custom ignored drop item(s).")
    except Exception as e:
        CUSTOM_LOW_VALUE_DROP_ITEMS = set()
        log_event(f"Failed to load custom ignored drops: {e}")


def custom_low_value_save():
    data = {"items": sorted(CUSTOM_LOW_VALUE_DROP_ITEMS)}
    CUSTOM_LOW_VALUE_DROP_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def all_low_value_drop_items() -> set[str]:
    return LOW_VALUE_DROP_ITEMS | CUSTOM_LOW_VALUE_DROP_ITEMS

# RuneMetrics activity text allow-list. This keeps #kill-bot-achievements focused on worthwhile logs.
# Add future approved activity formats here. Use lower-case regex patterns.
ALLOWED_RUNEMETRICS_PATTERNS = [
    r"^quest complete:",
    r".*levelled all skills over .*",
    r".*leveled all skills over .*",
    r".*dig ?site.*(complete|completed|fully excavated|finished|restored|restoring|uncovered|discovered|qualification|qualified).*$",
    r".*completed.*dig ?site.*$",
    r".*completed.*archaeology.*collection.*$",
    r".*archaeology.*(collection|collector|artefact|artifact|qualification|qualified|dig ?site|excavation|restored|restoration|mystery|monolith|relic).*$",
    r".*(associate|professor|guildmaster|intern|assistant|associate professor).*qualification.*$",
    r".*(completed|finished|uncovered|discovered|restored|excavated).*dig ?site.*$",
    r"^i now have a total level of \d+",
    r"^\d{1,3}(?:,\d{3})*xp in .+",
    r"^i now have at least \d{1,3}(?:,\d{3})* experience points in the .+ skill",
    r"^i have completed an? .+ treasure trail\. i got .+ out of it",
    r"^i caught \d+ .+ charm sprites",
    r"^i climbed the ranks in the crucible and was named the supreme champion",
    r"^i brought a total of 25 additional chimp ices to king awowogei",
    r"^a message was dropped by an? .+ from the .+ champion, challenging me to a fight",
    r"^i defeated many waves of tokhaar, before vanquishing the mighty har'aken and conquering the fight kiln",
    r"^i[’']ve uncovered volume \d+ of daemonheim's history",
    r"^i have breached floor \d+ of daemonheim for the first time",
    r"^after completing the deadliest catch quest, i hunted and found the thalassus all 10 times",
    r"^after exchanging 100 zeal at soul wars, i adopted a pet tzrek-jad",
    r"^each time i rebuilt the statue of dahmaroc",
    r"^i killed the player .+",
    r"^i have killed (100|200|300|400|450|500) bosses in the dominion tower",
    r"^killed the sunfreet",
    r"^for the first time, i managed to be the one to capture the most enemy flags",
    r"^i reached a total of (500|1,000|5,000) matches of castle wars",
    r"^unlocked the golden cannon ability",
    r"^i unlocked the (golden cannon|royale cannon|master student) ability at the artisans workshop",
    r"^after finding a court summons, i won the case of .+",
    r"^i visited varrock museum and took rightful ownership of my completionist cape",
    r"^for the first time after training all skills to level 99, i bought a max cape",
    r"^ocellus helped me create an ascension crossbow",
    r"^after collecting shark's teeth from the fishing trawler, i crafted a shark's tooth necklace",
    r"^after killing an? .+, it dropped an? .+",
    r"^after killing edimmu, it dropped an edimmu",
    r"^after killing an edimmu, it dropped an edimmu",
    r"^while plundering the barrows, i looted .+",
    r"^after defeating telos, i looted .+",
    r"^whilst playing .+, i found .+",
    r"^whilst plundering the pyramids, i looted black ibis .+",
    r"^whilst plundering the pyramids, i looted the sceptre of the gods",
    r"^while laying barbarian spirits to rest, i was given a dragon full helm",
    r"^after killing an? elite rune dragon, it dropped a kethsi outfit scroll",
    r"^whilst playing the great orb project, i won master runecrafter's .+",
    r"^i found a piece of dragonstone armour",
    r"^i found .+, the .+ pet",
    r"^while skilling, i found .+, the .+ pet",
    r"^five thousand victories in the duel arena",
    r"^five thousand victories in the wilderness",
    r"^after incredible effort, i unlocked the final enchantment of dahmaroc",
    r"^after opening a dragonkin lamp, i unlocked effy the effigy pet",
    r"^i defeated the queen black dragon \d+ time",
    r"^i killed \d+ boss monsters in daemonheim",
    r"^i killed tztok-jad, and can now claim my fire cape",
]


# Precompiled once at startup rather than on every RuneMetrics activity check.
ALLOWED_RUNEMETRICS_REGEX = [re.compile(pattern) for pattern in ALLOWED_RUNEMETRICS_PATTERNS]
SKILL_XP_MILESTONE_SET = set(SKILL_XP_MILESTONES)


def rsn_load():
    global RSN_REGISTRATIONS, RSN_DISCORD_NAMES, RSN_LAST_ACTIVITY_KEYS, RSN_PROFILE_BASELINES, RSN_ACHIEVEMENT_CHANNELS
    if not RSN_DATA_FILE.exists():
        RSN_REGISTRATIONS = {}
        RSN_DISCORD_NAMES = {}
        RSN_LAST_ACTIVITY_KEYS = {}
        RSN_PROFILE_BASELINES = {}
        RSN_ACHIEVEMENT_CHANNELS = {}
        log_event("RuneMetrics tracking file not found yet; starting with empty RSN list.")
        return
    try:
        data = json.loads(RSN_DATA_FILE.read_text(encoding="utf-8"))
        RSN_REGISTRATIONS = {int(k): str(v) for k, v in data.get("registrations", {}).items()}
        RSN_DISCORD_NAMES = {int(k): str(v) for k, v in data.get("discord_names", {}).items()}
        RSN_LAST_ACTIVITY_KEYS = {int(k): list(v) for k, v in data.get("last_activity_keys", {}).items()}
        RSN_PROFILE_BASELINES = {int(k): dict(v) for k, v in data.get("profile_baselines", {}).items()}
        RSN_ACHIEVEMENT_CHANNELS = {int(k): int(v) for k, v in data.get("achievement_channels", {}).items()}
        log_event(f"Loaded RuneMetrics tracking: {len(RSN_REGISTRATIONS)} registered RSN(s).")
    except Exception as e:
        RSN_REGISTRATIONS = {}
        RSN_DISCORD_NAMES = {}
        RSN_LAST_ACTIVITY_KEYS = {}
        RSN_PROFILE_BASELINES = {}
        RSN_ACHIEVEMENT_CHANNELS = {}
        log_event(f"Failed to load RuneMetrics tracking file: {e}")


def rsn_save():
    data = {
        "registrations": {str(k): v for k, v in RSN_REGISTRATIONS.items()},
        "discord_names": {str(k): v for k, v in RSN_DISCORD_NAMES.items()},
        "last_activity_keys": {str(k): v for k, v in RSN_LAST_ACTIVITY_KEYS.items()},
        "profile_baselines": {str(k): v for k, v in RSN_PROFILE_BASELINES.items()},
        "achievement_channels": {str(k): v for k, v in RSN_ACHIEVEMENT_CHANNELS.items()},
    }
    RSN_DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def rsn_history_load():
    global RSN_HISTORY
    if not RSN_HISTORY_FILE.exists():
        RSN_HISTORY = {}
        return
    try:
        raw = json.loads(RSN_HISTORY_FILE.read_text(encoding="utf-8"))
        RSN_HISTORY = {int(uid): list(entries) for uid, entries in raw.items()}
        log_event(f"Loaded RuneMetrics gains history for {len(RSN_HISTORY)} user(s).")
    except Exception as e:
        log_event(f"Failed to load RuneMetrics gains history: {e}")
        RSN_HISTORY = {}


def rsn_history_save():
    serial = {str(uid): entries for uid, entries in RSN_HISTORY.items()}
    RSN_HISTORY_FILE.write_text(json.dumps(serial, indent=2), encoding="utf-8")


def record_rsn_history_snapshot(user_id: int, snapshot: Dict[str, Any]):
    now = now_unix_utc()
    entries = RSN_HISTORY.setdefault(user_id, [])
    # Avoid writing effectively identical snapshots every few seconds if a forced check happens.
    if entries and now - int(entries[-1].get("ts", 0)) < 240:
        entries[-1] = {"ts": now, "snapshot": snapshot}
    else:
        entries.append({"ts": now, "snapshot": snapshot})

    cutoff = now - RSN_HISTORY_MAX_DAYS * 86400
    RSN_HISTORY[user_id] = [entry for entry in entries if int(entry.get("ts", 0)) >= cutoff]
    rsn_history_save()


def get_history_baseline(user_id: int, seconds_back: int) -> Optional[Dict[str, Any]]:
    entries = RSN_HISTORY.get(user_id, [])
    if not entries:
        return None
    target = now_unix_utc() - seconds_back
    older = [entry for entry in entries if int(entry.get("ts", 0)) <= target]
    if older:
        return max(older, key=lambda entry: int(entry.get("ts", 0)))
    return min(entries, key=lambda entry: abs(int(entry.get("ts", 0)) - target))


def period_to_seconds(period: str) -> Optional[int]:
    lookup = {
        "day": 86400,
        "daily": 86400,
        "week": 7 * 86400,
        "weekly": 7 * 86400,
        "month": 30 * 86400,
        "monthly": 30 * 86400,
        "year": 365 * 86400,
        "yearly": 365 * 86400,
    }
    return lookup.get(period.lower().strip())


def user_display_for_rsn(user_id: int) -> str:
    saved_name = RSN_DISCORD_NAMES.get(user_id)
    if saved_name:
        return f"{saved_name} (<@{user_id}>)"
    return f"<@{user_id}>"


def find_permanent_achievement_channels(client: discord.Client) -> Dict[int, int]:
    """Find #tuxedo-tales in every guild and return guild_id -> channel_id."""
    found: Dict[int, int] = {}
    desired = PERMANENT_ACHIEVEMENT_CHANNEL_NAME.lower()
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name.lower() == desired:
                found[guild.id] = channel.id
                break
    return found


def ensure_permanent_achievement_channels(client: discord.Client):
    """Achievement posts are now permanently sent only to #kill-bot-achievements when present."""
    found = find_permanent_achievement_channels(client)
    if found:
        for guild_id, channel_id in found.items():
            log_event(f"RuneMetrics achievement channel fixed to #{PERMANENT_ACHIEVEMENT_CHANNEL_NAME} for guild {guild_id}.")
    else:
        log_event(f"RuneMetrics warning: no #{PERMANENT_ACHIEVEMENT_CHANNEL_NAME} channel found in connected guilds.")


def get_achievement_channels(client: discord.Client) -> List[discord.abc.Messageable]:
    """Return only #kill-bot-achievements channels. Manual achievement-channel configuration has been removed."""
    channels = []
    for channel_id in set(find_permanent_achievement_channels(client).values()):
        channel = client.get_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            channels.append(channel)
    return channels

def runemetrics_activity_key(activity: Dict) -> str:
    text = str(activity.get("text", "")).strip()
    date = str(activity.get("date", "")).strip()
    return f"{date}|{text}"


def _normalise_activity_text(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").strip().split())


def _strip_leading_article(item: str) -> str:
    item = item.strip(" .")
    for prefix in ("a ", "an ", "the "):
        if item.startswith(prefix):
            return item[len(prefix):].strip(" .")
    return item


def extract_drop_item(text: str) -> Optional[str]:
    """Best-effort extraction of the item name from RuneMetrics activity text.

    This is deliberately blacklist-based: if an item can be extracted and it is not
    in LOW_VALUE_DROP_ITEMS, it is allowed to post. This prevents new valuable
    items, such as Devourer's Guard or Tumeken's Light, being ignored just because
    they are not in an allow-list yet.
    """
    lowered = _normalise_activity_text(text).strip(" .")

    # Specific RuneMetrics patterns first.
    patterns = [
        r"^i found (?P<item>.+)$",
        r"^while skilling, i found (?P<item>.+)$",
        r"^after killing an? .+, it dropped (?P<item>.+)$",
        r"^after defeating .+, i looted (?P<item>.+)$",
        r"^while plundering the barrows, i looted (?P<item>.+)$",
        r"^whilst plundering the pyramids, i looted (?P<item>.+)$",
        r"^while laying barbarian spirits to rest, i was given (?P<item>.+)$",
        r"^i have completed an? .+ treasure trail\. i got (?P<item>.+) out of it$",
        r"^i received (?P<item>.+)$",
        r"^i obtained (?P<item>.+)$",
    ]

    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            item = _strip_leading_article(match.group("item"))
            # Drop pet suffixes/milestone clauses from item-like strings.
            item = item.split(" on ", 1)[0].strip(" .")
            return item or None

    return None


def contains_low_value_drop(text: str) -> bool:
    lowered = _normalise_activity_text(text)
    extracted = extract_drop_item(lowered)

    # Prefer exact item matching where possible.
    if extracted:
        return normalise_drop_item_name(extracted) in all_low_value_drop_items()

    # Fallback: substring matching for awkward RuneMetrics phrasing.
    haystack = normalise_drop_item_name(lowered)
    return any(item in haystack for item in all_low_value_drop_items())


def is_generic_drop_activity(text: str) -> bool:
    lowered = _normalise_activity_text(text)
    indicators = (
        "i found ",
        "it dropped ",
        "i looted ",
        "i received ",
        "i obtained ",
        "was given ",
        "treasure trail",
    )
    return any(indicator in lowered for indicator in indicators)


def _parse_runemetrics_xp_number(text: str) -> Optional[int]:
    match = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:xp|experience points)", text.lower())
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except Exception:
        return None


def is_approved_xp_activity(text: str) -> bool:
    xp_value = _parse_runemetrics_xp_number(text)
    return xp_value in SKILL_XP_MILESTONE_SET


def should_post_runemetrics_activity(activity: Dict) -> bool:
    """Return True if this RuneMetrics activity is worth posting to #kill-bot-achievements.

    Drop logic is blacklist-based: all detected drops are posted unless the item is
    listed in LOW_VALUE_DROP_ITEMS. This means future valuable drops do not need
    to be manually added to an allow-list.
    """
    raw_text = str(activity.get("text", "")).strip()
    if not raw_text:
        return False

    lowered = _normalise_activity_text(raw_text)

    # RuneMetrics can post lots of routine XP activity. Only allow defined XP milestones.
    if "xp in" in lowered or "experience points in" in lowered:
        if is_approved_xp_activity(lowered):
            return True
        log_event(f"RuneMetrics XP activity ignored because it is not a tracked milestone: {raw_text}")
        return False

    # Generic drops are allowed unless the extracted item appears in LOW_VALUE_DROP_ITEMS.
    # Examples that will now post: "I found a devourer's guard", "I found a tumeken's light".
    if is_generic_drop_activity(lowered):
        if contains_low_value_drop(lowered):
            log_event(f"RuneMetrics activity suppressed as low-value drop: {raw_text}")
            return False
        return True

    for pattern in ALLOWED_RUNEMETRICS_REGEX:
        if pattern.search(lowered):
            return True

    log_event(f"RuneMetrics activity ignored by filter: {raw_text}")
    return False


async def get_http_session() -> aiohttp.ClientSession:
    """Reuse one aiohttp session for RuneMetrics instead of creating one per request."""
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        timeout = aiohttp.ClientTimeout(total=15)
        HTTP_SESSION = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "KillBot Discord Bot"})
    return HTTP_SESSION


async def fetch_runemetrics_profile(rsn: str) -> Optional[Dict]:
    """Fetch the public RuneMetrics profile payload for an RSN."""
    log_event(f"RuneMetrics search started for RSN: {rsn}")
    quoted = urllib.parse.quote(rsn)
    url = f"https://apps.runescape.com/runemetrics/profile/profile?user={quoted}&activities=20"
    try:
        session = await get_http_session()
        async with session.get(url) as response:
            if response.status != 200:
                log_event(f"RuneMetrics search failed for {rsn}: HTTP {response.status}")
                return None
            data = await response.json(content_type=None)
    except Exception as e:
        log_event(f"RuneMetrics search error for {rsn}: {e}")
        return None

    if isinstance(data, dict):
        log_event(f"RuneMetrics search completed for RSN: {rsn}")
        return data

    log_event(f"RuneMetrics search returned unexpected data for RSN: {rsn}")
    return None


async def fetch_runemetrics_activities(rsn: str) -> Optional[List[Dict]]:
    profile = await fetch_runemetrics_profile(rsn)
    if profile is None:
        return None
    activities = profile.get("activities")
    if not isinstance(activities, list):
        return None
    return activities


def profile_snapshot(profile: Dict) -> Dict:
    """Make a compact snapshot of total XP, total level, and skill levels/xp."""
    snapshot = {
        "totalxp": int(profile.get("totalxp", 0) or 0),
        "totalskill": int(profile.get("totalskill", 0) or 0),
        "skills": {},
    }

    skillvalues = profile.get("skillvalues")
    if isinstance(skillvalues, list):
        for item in skillvalues:
            if not isinstance(item, dict):
                continue
            try:
                skill_id = int(item.get("id"))
            except Exception:
                continue
            skill_name = SKILL_ID_TO_NAME.get(skill_id, f"Skill {skill_id}")
            snapshot["skills"][skill_name] = {
                "level": int(item.get("level", 0) or 0),
                "xp": int(item.get("xp", 0) or 0),
            }

    return snapshot


def achievement_emoji(text: str) -> str:
    lowered = text.lower()
    if "levelled" in lowered or "level" in lowered:
        return "🏃"
    if "xp" in lowered:
        return "📊"
    if "found" in lowered or "drop" in lowered or "received" in lowered or "obtained" in lowered:
        return "💎"
    if "kill" in lowered or "defeated" in lowered or "slain" in lowered:
        return "⚔️"
    if "completed" in lowered:
        return "✅"
    return "🎉"


def format_achievement_message(user_id: int, rsn: str, activity: Dict) -> str:
    text = str(activity.get("text", "an achievement")).strip()
    date = str(activity.get("date", "")).strip()
    emoji = achievement_emoji(text)
    date_text = f" on {date}" if date else ""
    return f"{emoji} **{rsn}** — {text}{date_text} (<@{user_id}>)"


def _crossed_milestone(old_value: int, new_value: int, milestones: List[int]) -> List[int]:
    """Return milestones crossed moving from old_value to new_value."""
    return [m for m in milestones if old_value < m <= new_value]


def profile_progress_messages(user_id: int, rsn: str, old: Dict, new: Dict) -> List[str]:
    """Return high-value skill progress messages.

    Uses XP thresholds rather than relying only on RuneMetrics level values, because
    virtual level milestones can be missed or displayed inconsistently by the API.
    Posts:
    - Level 99 equivalent: 13,034,431 XP
    - Level 110 equivalent: 38,737,661 XP
    - Level 120 equivalent: 104,273,167 XP
    - 50m / 100m / 150m / 200m XP skill milestones
    """
    messages: List[str] = []

    old_skills = old.get("skills", {}) if isinstance(old.get("skills"), dict) else {}
    new_skills = new.get("skills", {}) if isinstance(new.get("skills"), dict) else {}

    for skill_name, new_data in new_skills.items():
        old_data = old_skills.get(skill_name, {})
        old_level = int(old_data.get("level", 0) or 0)
        new_level = int(new_data.get("level", 0) or 0)
        old_xp = int(old_data.get("xp", 0) or 0)
        new_xp = int(new_data.get("xp", 0) or 0)

        # Major level-equivalent XP thresholds. This catches 99/110/120 even if
        # RuneMetrics does not produce an activity-feed broadcast for it.
        for level, xp_required in SKILL_LEVEL_XP_MILESTONES.items():
            if old_xp < xp_required <= new_xp:
                messages.append(
                    f"🏆 **{rsn}** reached level **{level} {skill_name}**! (<@{user_id}>)"
                )

        # Safety net: if the API level value jumps straight over a key level,
        # still post it even when XP data is missing/odd.
        for level in SKILL_LEVEL_XP_MILESTONES.keys():
            if old_level < level <= new_level:
                duplicate = any(f"level **{level} {skill_name}**" in msg for msg in messages)
                if not duplicate:
                    messages.append(
                        f"🏆 **{rsn}** reached level **{level} {skill_name}**! (<@{user_id}>)"
                    )

        # Only post major skill XP milestones, not routine XP gained.
        for milestone in _crossed_milestone(old_xp, new_xp, SKILL_XP_MILESTONES):
            messages.append(
                f"📊 **{rsn}** reached **{milestone:,} XP** in **{skill_name}**! (<@{user_id}>)"
            )

    return messages

async def send_rune_metrics_messages(client: discord.Client, messages: List[str]):
    if not messages:
        return

    channels = get_achievement_channels(client)
    if not channels:
        log_event(
            f"RuneMetrics found {len(messages)} update(s), but no #"
            f"{PERMANENT_ACHIEVEMENT_CHANNEL_NAME} channel or configured achievement channel was found."
        )
        return

    for channel in channels:
        for message in messages:
            await channel.send(message, allowed_mentions=discord.AllowedMentions(users=True))
            log_event(f"RuneMetrics posted to #{getattr(channel, 'name', channel.id)}: {message}")


async def run_startup_achievement_catchup(client: discord.Client):
    """Run once when Kill Bot starts so offline RuneMetrics progress is posted after downtime."""
    await client.wait_until_ready()

    ensure_permanent_achievement_channels(client)

    if not RSN_REGISTRATIONS:
        log_event("RuneMetrics catch-up skipped: no RSNs registered.")
        return

    log_event(f"RuneMetrics catch-up started for {len(RSN_REGISTRATIONS)} registered RSN(s).")

    for user_id, rsn in list(RSN_REGISTRATIONS.items()):
        try:
            log_event(f"RuneMetrics catch-up checking {rsn} for user_id {user_id}.")
            profile = await fetch_runemetrics_profile(rsn)
            if not profile:
                log_event(f"RuneMetrics catch-up failed for {rsn}: profile unavailable.")
                continue

            activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []
            current_keys = [
                runemetrics_activity_key(a)
                for a in activities
                if isinstance(a, dict) and a.get("text")
            ]

            previous_keys = RSN_LAST_ACTIVITY_KEYS.get(user_id)
            current_snapshot = profile_snapshot(profile)
            record_rsn_history_snapshot(user_id, current_snapshot)
            previous_snapshot = RSN_PROFILE_BASELINES.get(user_id)

            # If this is the first time seeing this RSN on this machine/file, create baseline only.
            if not previous_keys or not previous_snapshot:
                RSN_LAST_ACTIVITY_KEYS[user_id] = current_keys[:20]
                RSN_PROFILE_BASELINES[user_id] = current_snapshot
                rsn_save()
                log_event(f"RuneMetrics catch-up baseline created for {rsn}; no old data posted.")
                continue

            activity_messages = [
                format_achievement_message(user_id, rsn, a)
                for a in reversed(activities)
                if isinstance(a, dict)
                and a.get("text")
                and should_post_runemetrics_activity(a)
                and runemetrics_activity_key(a) not in previous_keys
            ]

            stat_messages = profile_progress_messages(user_id, rsn, previous_snapshot, current_snapshot)
            messages_to_send = activity_messages + stat_messages

            if messages_to_send:
                log_event(f"RuneMetrics catch-up found {len(messages_to_send)} update(s) for {rsn}.")
                await send_rune_metrics_messages(client, messages_to_send)
            else:
                log_event(f"RuneMetrics catch-up found no updates for {rsn}.")

            # Always update baselines after catch-up so future checks don't repeat these posts.
            RSN_LAST_ACTIVITY_KEYS[user_id] = current_keys[:20]
            RSN_PROFILE_BASELINES[user_id] = current_snapshot
            rsn_save()

        except Exception as e:
            log_event(f"RuneMetrics catch-up error for {rsn}: {e}")

    log_event("RuneMetrics catch-up completed.")


async def achievement_poll_loop(client: discord.Client):
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            log_event(f"RuneMetrics poll cycle started. Registered RSNs: {len(RSN_REGISTRATIONS)}")
            for user_id, rsn in list(RSN_REGISTRATIONS.items()):
                log_event(f"RuneMetrics checking {rsn} for user_id {user_id}.")
                profile = await fetch_runemetrics_profile(rsn)
                if not profile:
                    log_event(f"RuneMetrics check skipped for {rsn}: profile unavailable.")
                    continue

                activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []
                current_keys = [runemetrics_activity_key(a) for a in activities if isinstance(a, dict) and a.get("text")]
                previous_keys = RSN_LAST_ACTIVITY_KEYS.get(user_id)
                current_snapshot = profile_snapshot(profile)
                record_rsn_history_snapshot(user_id, current_snapshot)
                previous_snapshot = RSN_PROFILE_BASELINES.get(user_id)

                # First run after registration or after upgrading code: baseline only, don't spam old data.
                if not previous_keys or not previous_snapshot:
                    RSN_LAST_ACTIVITY_KEYS[user_id] = current_keys[:20]
                    RSN_PROFILE_BASELINES[user_id] = current_snapshot
                    rsn_save()
                    log_event(f"RuneMetrics baseline set/refreshed for {rsn}; no old data posted.")
                    continue

                new_activity_messages = [
                    format_achievement_message(user_id, rsn, a)
                    for a in reversed(activities)
                    if isinstance(a, dict) and a.get("text") and should_post_runemetrics_activity(a) and runemetrics_activity_key(a) not in previous_keys
                ]

                stat_messages = profile_progress_messages(user_id, rsn, previous_snapshot, current_snapshot)
                messages_to_send = new_activity_messages + stat_messages

                if messages_to_send:
                    log_event(f"RuneMetrics found {len(messages_to_send)} new update(s) for {rsn}.")
                    await send_rune_metrics_messages(client, messages_to_send)
                    if stat_messages:
                        RSN_PROFILE_BASELINES[user_id] = current_snapshot
                else:
                    log_event(f"RuneMetrics found no new updates for {rsn}.")

                # Always update activity keys so old activity messages don't repost.
                RSN_LAST_ACTIVITY_KEYS[user_id] = current_keys[:20]
                rsn_save()

        except Exception as e:
            log_event(f"RuneMetrics achievement poll error: {e}")

        await asyncio.sleep(ACHIEVEMENT_POLL_SECONDS)


async def run_rune_metrics_worker(client: discord.Client):
    """Run startup catch-up once, then enter the regular poll loop.

    This avoids the catch-up task and regular poll loop hitting RuneMetrics at the
    same time on startup, which can cause duplicate posts and wasted API calls.
    """
    await run_startup_achievement_catchup(client)
    await achievement_poll_loop(client)

# -----------------------------
# Activity Points / Earnable Rank System (persistent)
# -----------------------------
RANK_DATA_FILE = Path(__file__).with_name("rank_data.json")

# These roles are deliberately NOT earnable by points.
PROTECTED_RANK_ROLE_NAMES = {"Emperor Penguin", "Ice Marshall", "Moderators"}

# Earnable ranks, lowest -> highest. Edit thresholds here whenever you want.
EARNABLE_RANKS = [
    {"name": "Saddlers", "points": 0},
    {"name": "Penguin", "points": 100},
    {"name": "Veteran Saddlers", "points": 750},
    {"name": "Elite Penguins", "points": 2000},
    {"name": "KGP Operative", "points": 5000},
]

MESSAGE_POINTS = 2
MESSAGE_COOLDOWN_SECONDS = 60
INTERACTION_POINTS = 5
INTERACTION_COOLDOWN_SECONDS = 20

# user_id -> {"points": int, "last_message": int, "last_interaction": int}
RANK_DATA: Dict[int, Dict[str, int]] = {}


def rank_load():
    global RANK_DATA
    if not RANK_DATA_FILE.exists():
        RANK_DATA = {}
        return
    try:
        raw = json.loads(RANK_DATA_FILE.read_text(encoding="utf-8"))
        RANK_DATA = {
            int(uid): {
                "points": int(data.get("points", 0)),
                "last_message": int(data.get("last_message", 0)),
                "last_interaction": int(data.get("last_interaction", 0)),
            }
            for uid, data in raw.items()
        }
    except Exception:
        RANK_DATA = {}


def rank_save():
    serial = {str(uid): data for uid, data in RANK_DATA.items()}
    RANK_DATA_FILE.write_text(json.dumps(serial, indent=2), encoding="utf-8")


def get_rank_points(user_id: int) -> int:
    return int(RANK_DATA.get(user_id, {}).get("points", 0))


def get_rank_for_points(points: int) -> Dict[str, int]:
    current = EARNABLE_RANKS[0]
    for rank in EARNABLE_RANKS:
        if points >= rank["points"]:
            current = rank
    return current


def get_next_rank(points: int) -> Optional[Dict[str, int]]:
    for rank in EARNABLE_RANKS:
        if points < rank["points"]:
            return rank
    return None


async def post_rank_promotion(member: discord.Member, rank_name: str):
    if rank_name == "Saddlers":
        return
    channel = find_text_channel(member.guild, ROOKERY_CHANNEL_NAME)
    if not channel:
        return
    embed = discord.Embed(
        title="🐧 Promotion Earned!",
        description=(
            f"{member.mention} has been promoted by Kill Bot!\n\n"
            f"**New Discord rank:** {rank_name}\n"
            "Keep waddling, keep posting, keep blaming Kyle."
        ),
        color=discord.Color.gold(),
    )
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
        log_event(f"Rank promotion posted for {member} -> {rank_name}")
    except Exception as e:
        log_event(f"Failed to post rank promotion for {member}: {e}")


async def apply_earnable_rank(member: discord.Member) -> Optional[str]:
    """Apply the member's highest earned rank role. Staff roles are protected and skipped."""
    if not member.guild:
        return None

    if is_staff(member):
        return None

    points = get_rank_points(member.id)
    earned = get_rank_for_points(points)
    earned_name = earned["name"]

    guild_roles = {role.name: role for role in member.guild.roles}
    earned_role_names = {rank["name"] for rank in EARNABLE_RANKS}
    earned_role = guild_roles.get(earned_name)

    if earned_role is None:
        return None

    # Never touch protected roles. Only remove lower/other earnable roles.
    roles_to_remove = [
        role for role in member.roles
        if role.name in earned_role_names and role.name != earned_name
    ]

    changed = False
    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Kill Bot earnable rank update")
            changed = True

        if earned_role not in member.roles:
            await member.add_roles(earned_role, reason="Kill Bot earnable rank update")
            changed = True
    except (discord.Forbidden, discord.HTTPException):
        return None

    if changed:
        await post_rank_promotion(member, earned_name)

    return earned_name if changed else None


async def award_activity_points(member: discord.Member, points: int, activity_key: str, cooldown: int) -> bool:
    """Award points if user is off cooldown. Staff roles do not earn activity points."""
    if member.bot or member.guild is None:
        return False

    # Protected staff/admin roles should not earn activity points or rank up through activity.
    if is_staff(member):
        return False

    now = now_unix_utc()
    user_data = RANK_DATA.setdefault(member.id, {"points": 0, "last_message": 0, "last_interaction": 0})

    if now - int(user_data.get(activity_key, 0)) < cooldown:
        return False

    user_data["points"] = int(user_data.get("points", 0)) + points
    user_data[activity_key] = now
    rank_save()

    await apply_earnable_rank(member)
    return True


def build_rank_embed(member: discord.Member) -> discord.Embed:
    points = get_rank_points(member.id)
    current = get_rank_for_points(points)
    next_rank = get_next_rank(points)

    if next_rank:
        remaining = next_rank["points"] - points
        progress = f"{remaining} points until **{next_rank['name']}**"
    else:
        progress = "Maximum earnable rank achieved. The penguin throne trembles."

    embed = discord.Embed(
        title="🐧 KGP Rank Progress",
        description=(
            f"**Member:** {member.mention}\n"
            f"**Points:** {points}\n"
            f"**Current earnable rank:** {current['name']}\n"
            f"**Progress:** {progress}"
        ),
        color=discord.Color.blurple(),
    )

    thresholds = "\n".join(f"• **{rank['name']}** — {rank['points']} pts" for rank in EARNABLE_RANKS)
    embed.add_field(name="Earnable Rank Path", value=thresholds, inline=False)
    embed.set_footer(text="Protected ranks cannot be earned: Emperor Penguin, Ice Marshall, Moderators.")
    return embed


def build_rankboard_embed(guild: Optional[discord.Guild]) -> discord.Embed:
    top = sorted(RANK_DATA.items(), key=lambda item: int(item[1].get("points", 0)), reverse=True)[:10]
    embed = discord.Embed(
        title="🏆 KGP Activity Leaderboard",
        description="Top penguins by activity points.",
        color=discord.Color.gold(),
    )

    if not top:
        embed.add_field(name="Leaderboard", value="*No activity logged yet.*", inline=False)
        return embed

    lines = []
    for idx, (uid, data) in enumerate(top, start=1):
        points = int(data.get("points", 0))
        rank = get_rank_for_points(points)["name"]
        name = f"<@{uid}>"
        if guild:
            member = guild.get_member(uid)
            if member:
                name = member.display_name
        lines.append(f"**{idx}.** {name} — {points} pts ({rank})")

    embed.add_field(name="Leaderboard", value="\n".join(lines), inline=False)
    return embed


# -----------------------------
# Signup Sessions (persistent)
# -----------------------------
HOST_DATA_FILE = Path(__file__).with_name("signup_sessions.json")


@dataclass
class HostSessionState:
    boss_name: str
    roles: List[str]
    max_group: int
    start_unix: int
    guild_id: int
    channel_id: int
    host_user_id: int
    event_url: Optional[str] = None
    slot_assignments: Dict[int, int] = field(default_factory=dict)
    last_reminder_message_id: Optional[int] = None


HOST_SESSIONS: Dict[int, HostSessionState] = {}          # signup_message_id -> state
CHANNEL_LATEST_HOST: Dict[int, int] = {}                 # channel_id -> signup_message_id


def host_load():
    global HOST_SESSIONS
    if not HOST_DATA_FILE.exists():
        HOST_SESSIONS = {}
        return
    try:
        raw = json.loads(HOST_DATA_FILE.read_text(encoding="utf-8"))
        out: Dict[int, HostSessionState] = {}
        for k, v in raw.items():
            out[int(k)] = HostSessionState(
                boss_name=v["boss_name"],
                roles=list(v["roles"]),
                max_group=int(v["max_group"]),
                start_unix=int(v["start_unix"]),
                guild_id=int(v["guild_id"]),
                channel_id=int(v["channel_id"]),
                host_user_id=int(v["host_user_id"]),
                event_url=v.get("event_url"),
                slot_assignments={int(sk): int(sv) for sk, sv in v.get("slot_assignments", {}).items()},
                last_reminder_message_id=v.get("last_reminder_message_id"),
            )
        HOST_SESSIONS = out
    except Exception:
        HOST_SESSIONS = {}


def host_save():
    serial = {
        str(k): {
            "boss_name": v.boss_name,
            "roles": v.roles,
            "max_group": v.max_group,
            "start_unix": v.start_unix,
            "guild_id": v.guild_id,
            "channel_id": v.channel_id,
            "host_user_id": v.host_user_id,
            "event_url": v.event_url,
            "slot_assignments": {str(sk): sv for sk, sv in v.slot_assignments.items()},
            "last_reminder_message_id": v.last_reminder_message_id,
        }
        for k, v in HOST_SESSIONS.items()
    }
    HOST_DATA_FILE.write_text(json.dumps(serial, indent=2), encoding="utf-8")


# -----------------------------
# Bosspick session state
# -----------------------------
@dataclass
class BossPickState:
    selected_bosses: List[str] = field(default_factory=list)
    rolled: Optional[str] = None


@dataclass
class RoleSignupState:
    boss_name: str
    roles: List[str]
    max_group: int
    slot_assignments: Dict[int, int] = field(default_factory=dict)


ROLE_SESSIONS: Dict[int, RoleSignupState] = {}  # message_id -> state


# -----------------------------
# Helpers
# -----------------------------
def bot_thumbnail_url(interaction: discord.Interaction) -> Optional[str]:
    me = interaction.client.user
    if not me:
        return None
    return me.display_avatar.replace(size=512, static_format="png").url


def is_staff(member: discord.Member) -> bool:
    """Return True for Kill Bot staff roles. Keep all staff-role logic in one place."""
    staff_roles = {
        "Moderator",
        "Moderators",
        "Ice Marshall",
        "Emperor Penguin",
    }
    return any(role.name in staff_roles for role in member.roles)


def can_announce(member: discord.Member) -> bool:
    """Return True if the member has a role allowed to use /announce."""
    return is_staff(member)


def can_manage_killbot(member: discord.Member) -> bool:
    """Return True for Kill Bot admin actions: Josh, Ice Marshall, or Emperor Penguin."""
    manager_roles = {"Ice Marshall", "Emperor Penguin"}
    return member.id == OWNER_ID or any(role.name in manager_roles for role in member.roles)


def _display_role_names_with_numbers(roles: List[str]) -> List[str]:
    counts: Dict[str, int] = {}
    for r in roles:
        counts[r] = counts.get(r, 0) + 1

    seen: Dict[str, int] = {}
    display: List[str] = []
    for r in roles:
        if counts[r] > 1:
            seen[r] = seen.get(r, 0) + 1
            display.append(f"{r} {seen[r]}")
        else:
            display.append(r)
    return display


def _unique_players(assignments: Dict[int, int]) -> int:
    return len(set(assignments.values()))


def all_slots_filled(state: RoleSignupState) -> bool:
    return len(state.slot_assignments) == len(state.roles)


def signup_roles_for_boss(boss_name: str) -> List[str]:
    """Return role slots for signup boards. Bosses with no formal roles get generic Player slots."""
    boss = BOSS_DATA[boss_name]
    roles = list(boss.get("roles", []))
    if roles:
        return roles
    return ["Player"] * int(boss["max_group"])


def parse_hhmm_today_uk(hhmm: str) -> Optional[datetime]:
    """Parse HHMM as UK local time; if passed today, schedule tomorrow."""
    if len(hhmm) != 4 or not hhmm.isdigit():
        return None
    hour = int(hhmm[:2])
    minute = int(hhmm[2:])
    if hour > 23 or minute > 59:
        return None

    now_local = datetime.now(LOCAL_TZ)
    start_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start_local <= now_local:
        start_local += timedelta(days=1)
    return start_local


# -----------------------------
# Embeds
# -----------------------------
def build_welcome_embed(interaction: discord.Interaction, state: BossPickState) -> discord.Embed:
    desc = (
        "🕯️ **Hark!** Welcome, my fellow *Mages, Rangers, Warriors, and Necromancers*.\n\n"
        "I spy a spot of bother upon the horizon — and thou seekest a challenge suitably dire.\n"
        "Pray, choose all bosses thou wouldst consider (and for which thy party is meet) from the sigils below.\n\n"
        "When thou art ready, smite the **Start** button — and I shall cast the bones of fate."
    )
    embed = discord.Embed(title="Kill bot’s Grim Grimoire of Group Bosses", description=desc, color=discord.Color.blurple())
    chosen = state.selected_bosses or ["*none yet*"]
    embed.add_field(name="Chosen Bosses", value="\n".join(f"• {b}" for b in chosen), inline=False)

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.set_footer(text="Tip: click a boss again to unselect it.")
    return embed


def build_roll_embed(interaction: discord.Interaction, rolled: str) -> discord.Embed:
    embed = discord.Embed(
        title="Fate Has Spoken",
        description=f"🎲 I have selected **{rolled}** — *dost thou dare accept this challenge?*",
        color=discord.Color.gold(),
    )
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.set_footer(text="Choose wisely: Yea seals thy doom. Nay invites another roll.")
    return embed


def build_roles_embed(interaction: discord.Interaction, state: RoleSignupState) -> discord.Embed:
    embed = discord.Embed(
        title=f"{state.boss_name} — Roles",
        description=(
            "Click a role button to claim a slot. Click it again to unclaim.\n"
            "Thou may claim **multiple** roles — yet we shall allow no more than the party limit of unique champions."
        ),
        color=discord.Color.green(),
    )
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    display = _display_role_names_with_numbers(state.roles)
    for idx, disp in enumerate(display):
        uid = state.slot_assignments.get(idx)
        embed.add_field(name=f"{disp}:", value=(f"<@{uid}>" if uid else "*empty*"), inline=False)

    embed.set_footer(text=f"Max group size: {state.max_group} | Unique players signed: {_unique_players(state.slot_assignments)}")
    return embed


def build_host_event_embed(interaction: discord.Interaction, boss_name: str, start_local: datetime, event_url: Optional[str]) -> discord.Embed:
    boss = BOSS_DATA[boss_name]
    roles_text = "\n".join(f"• {r}" for r in boss["roles"])
    unix = int(start_local.timestamp())

    embed = discord.Embed(
        title=f"📜 {boss_name} — Hosted Encounter",
        description=(
            f"**Host:** {interaction.user.mention}\n"
            f"**Starts:** <t:{unix}:F>  (<t:{unix}:R>)\n"
            f"**Duration:** {EVENT_DURATION_MINUTES} minutes\n"
            f"**Max group size:** {boss['max_group']}\n\n"
            f"**Roles:**\n{roles_text}"
        ),
        color=discord.Color.purple(),
    )

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    if event_url:
        embed.add_field(name="Event", value=f"[Open Scheduled Event]({event_url})", inline=False)

    if LOCAL_TZ_NAME != "UTC" and str(LOCAL_TZ) == "UTC":
        embed.set_footer(text="Note: tzdata missing on host; time parsing using UTC fallback. Install: py -m pip install tzdata")
    else:
        embed.set_footer(text=f"Time input interpreted as UK time ({LOCAL_TZ_NAME}).")
    return embed


def build_host_signup_embed(guild: Optional[discord.Guild], state: HostSessionState) -> discord.Embed:
    # Reused for GoTime signup boards. If event_url exists, it can still display an event link,
    # but /hostboss has been removed.
    if state.event_url:
        unix = state.start_unix
        title = f"📯 Hosted Run — {state.boss_name}"
        description = (
            f"**Host:** <@{state.host_user_id}>\n"
            f"**Starts:** <t:{unix}:F>  (<t:{unix}:R>)\n"
            f"**Duration:** {EVENT_DURATION_MINUTES} minutes\n"
            f"**Max group size:** {state.max_group}\n\n"
            "Claim roles below. Click again to unclaim.\n"
            "**You may claim multiple roles.**"
        )
    else:
        title = f"📯 Go Time Sign-up — {state.boss_name}"
        description = (
            f"**Created by:** <@{state.host_user_id}>\n"
            f"**Max group size:** {state.max_group}\n\n"
            "Claim roles below. Click again to unclaim.\n"
            "**You may claim multiple roles.**"
        )

    embed = discord.Embed(title=title, description=description, color=discord.Color.purple())

    if state.event_url:
        embed.add_field(name="Scheduled Event", value=f"[Open Event]({state.event_url})", inline=False)

    display = _display_role_names_with_numbers(state.roles)
    for idx, disp in enumerate(display):
        uid = state.slot_assignments.get(idx)
        embed.add_field(name=f"{disp}:", value=(f"<@{uid}>" if uid else "*empty*"), inline=False)

    embed.set_footer(text=f"Unique players signed: {_unique_players(state.slot_assignments)}")
    return embed


def build_pvm_poll_embed(source, poll_message_id: Optional[int] = None) -> discord.Embed:
    """Build the public PVM Tonight embed, including free/not-free lists."""
    free_ids: List[int] = []
    not_free_ids: List[int] = []
    if poll_message_id is not None:
        availability = PVM_AVAILABILITY.get(poll_message_id, {})
        free_ids = availability.get("free", [])
        not_free_ids = availability.get("not_free", [])

    def fmt_users(ids: List[int]) -> str:
        return "\n".join(f"• <@{uid}>" for uid in ids) if ids else "*No one yet.*"

    embed = discord.Embed(
        title="⚔️ PVM Tonight?",
        description=(
            "Declare whether thou art free for PvM tonight.\n\n"
            "✅ **I am free tonight** — opens a private boss selection panel.\n"
            "❌ **I am not free tonight** — records that thou art unavailable.\n\n"
            f"Daily post: **{format_uk_game_time(13, 0)}**. Go Time: **{format_uk_game_time(21, 0)}**."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="✅ Free tonight", value=fmt_users(free_ids), inline=True)
    embed.add_field(name="❌ Not free tonight", value=fmt_users(not_free_ids), inline=True)
    embed.set_footer(text="Free players can privately select bosses, confirm choices, and edit them later if needed.")

    thumb = bot_thumbnail_url(source) if isinstance(source, discord.Interaction) else None
    if thumb:
        embed.set_thumbnail(url=thumb)
    return embed


def build_pvm_selection_embed(user: discord.User | discord.Member, selected: List[str], confirmed: bool = False) -> discord.Embed:
    status = "✅ Confirmed" if confirmed else "✏️ Editing"
    embed = discord.Embed(
        title="⚔️ Select Thy PVM Options",
        description=(
            f"{user.mention}, select every boss thou art willing to fight tonight.\n\n"
            f"**Status:** {status}\n\n"
            "**Current selections:**\n"
            + ("\n".join(f"• {boss}" for boss in selected) if selected else "*None selected yet.*")
        ),
        color=discord.Color.green() if not confirmed else discord.Color.dark_grey(),
    )
    embed.set_footer(text="Confirm locks your choices. Edit unlocks them again.")
    return embed

# -----------------------------
# Remind Me scheduler
# -----------------------------
async def _run_reminder(client: discord.Client, reminder_id: str):
    reminder = REMINDERS.get(reminder_id)
    if not reminder:
        ACTIVE_REMINDER_TASKS.discard(reminder_id)
        return

    delay = int(reminder["due_unix"]) - now_unix_utc()
    if delay > 0:
        await asyncio.sleep(delay)

    reminder = REMINDERS.get(reminder_id)
    if not reminder:
        ACTIVE_REMINDER_TASKS.discard(reminder_id)
        return

    channel = client.get_channel(int(reminder["channel_id"]))
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        days = int(reminder.get("days", 1))
        day_text = "1 day" if days == 1 else f"{days} days"
        jump_url = reminder.get("jump_url")
        link_text = f"\n{jump_url}" if jump_url else ""
        await channel.send(
            f"<@{reminder['user_id']}> — you asked me to remind you {day_text} ago.{link_text}",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    REMINDERS.pop(reminder_id, None)
    reminders_save()
    ACTIVE_REMINDER_TASKS.discard(reminder_id)


def schedule_reminder(client: discord.Client, reminder_id: str):
    if reminder_id in ACTIVE_REMINDER_TASKS:
        return
    ACTIVE_REMINDER_TASKS.add(reminder_id)
    asyncio.create_task(_run_reminder(client, reminder_id))



# -----------------------------
# PK helpers
# -----------------------------
def _name(guild: Optional[discord.Guild], user_id: int) -> str:
    if guild:
        m = guild.get_member(user_id)
        if m:
            return m.display_name
    return f"User {user_id}"


def build_pk_embed(guild: Optional[discord.Guild]) -> discord.Embed:
    embed = discord.Embed(title="__PK Score Sheet__", description="Latest 1v1 results and running totals.", color=discord.Color.red())

    if PK_HISTORY:
        lines = []
        for entry in PK_HISTORY[-10:][::-1]:
            w = int(entry["winner"])
            l = int(entry["loser"])
            lines.append(f"{_name(guild, w)} {PK_SCORES.get(w,0)} - {_name(guild, l)} {PK_SCORES.get(l,0)}")
        embed.add_field(name="Latest PKs", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Latest PKs", value="*No PKs recorded yet.*", inline=False)

    if PK_SCORES:
        top = sorted(PK_SCORES.items(), key=lambda kv: kv[1], reverse=True)[:10]
        embed.add_field(name="Leaderboard (wins)", value="\n".join(f"{_name(guild, uid)} — {wins}" for uid, wins in top), inline=False)

    embed.set_footer(text="Use /pk winner:@Winner loser:@Loser to record. Use /pk to view.")
    return embed


# -----------------------------
# Views: Boss selection + confirm (bosspick)
# -----------------------------
class BossToggleButton(discord.ui.Button):
    def __init__(self, boss_name: str):
        super().__init__(style=discord.ButtonStyle.secondary, label=boss_name)
        self.boss_name = boss_name

    async def callback(self, interaction: discord.Interaction):
        view: "BossPickView" = self.view  # type: ignore
        if self.boss_name in view.state.selected_bosses:
            view.state.selected_bosses.remove(self.boss_name)
        else:
            view.state.selected_bosses.append(self.boss_name)

        for child in view.children:
            if isinstance(child, BossToggleButton) and child.boss_name == self.boss_name:
                child.style = discord.ButtonStyle.success if self.boss_name in view.state.selected_bosses else discord.ButtonStyle.secondary

        await interaction.response.edit_message(embed=build_welcome_embed(interaction, view.state), view=view)


class StartRollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.primary, label="Start", emoji="🎲")

    async def callback(self, interaction: discord.Interaction):
        view: "BossPickView" = self.view  # type: ignore
        if not view.state.selected_bosses:
            await interaction.response.send_message("Thou must select at least one boss first!", ephemeral=True)
            return
        rolled = random.choice(view.state.selected_bosses)
        view.state.rolled = rolled
        await interaction.response.edit_message(embed=build_roll_embed(interaction, rolled), view=ConfirmView(view.state))


class BossPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60 * 30)
        self.state = BossPickState()
        for boss in BOSS_NAMES:
            self.add_item(BossToggleButton(boss))
        self.add_item(StartRollButton())


class YesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.success, label="Yea", emoji="✅")

    async def callback(self, interaction: discord.Interaction):
        view: "ConfirmView" = self.view  # type: ignore
        rolled = view.state.rolled
        if not rolled:
            await interaction.response.send_message("Fate is strangely silent. Try again.", ephemeral=True)
            return

        for child in view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="It is decided!",
                description=f"Thou hast accepted **{rolled}**. Gather thy strength; roles shall be proclaimed anon…",
                color=discord.Color.green(),
            ),
            view=view,
        )

        boss_info = BOSS_DATA[rolled]
        role_state = RoleSignupState(boss_name=rolled, roles=signup_roles_for_boss(rolled), max_group=boss_info["max_group"])
        msg = await interaction.followup.send(embed=build_roles_embed(interaction, role_state), view=RoleSignupView(role_state))
        ROLE_SESSIONS[msg.id] = role_state


class NoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.danger, label="Nay", emoji="❌")

    async def callback(self, interaction: discord.Interaction):
        view: "ConfirmView" = self.view  # type: ignore
        state = view.state

        if not state.selected_bosses:
            await interaction.response.send_message("No bosses remain in thy list!", ephemeral=True)
            return
        if len(state.selected_bosses) == 1:
            await interaction.response.send_message("Alas! Thou hast chosen but one boss — there is naught else to reroll unto.", ephemeral=True)
            return

        last = state.rolled
        options = [b for b in state.selected_bosses if b != last]
        state.rolled = random.choice(options)
        await interaction.response.edit_message(embed=build_roll_embed(interaction, state.rolled), view=view)


class ConfirmView(discord.ui.View):
    def __init__(self, state: BossPickState):
        super().__init__(timeout=60 * 10)
        self.state = state
        self.add_item(YesButton())
        self.add_item(NoButton())


# -----------------------------
# Views: HostBoss signup board (buttons live on host embed + reminders)
# -----------------------------
class HostSignupRoleButton(discord.ui.Button):
    def __init__(self, label: str, slot_index: int, signup_message_id: int):
        super().__init__(style=discord.ButtonStyle.secondary, label=label)
        self.slot_index = slot_index
        self.signup_message_id = signup_message_id

    async def callback(self, interaction: discord.Interaction):
        state = HOST_SESSIONS.get(self.signup_message_id)
        if not state:
            await interaction.response.send_message("This hosted board has expired or the bot restarted.", ephemeral=True)
            return

        user_id = interaction.user.id
        taken_by = state.slot_assignments.get(self.slot_index)

        # Toggle off
        if taken_by == user_id:
            state.slot_assignments.pop(self.slot_index, None)
            HOST_SESSIONS[self.signup_message_id] = state
            host_save()
            await interaction.response.edit_message(embed=build_host_signup_embed(interaction.guild, state), view=self.view)
            return

        # Already taken
        if taken_by and taken_by != user_id:
            await interaction.response.send_message("That role is already claimed!", ephemeral=True)
            return

        # Enforce max UNIQUE players
        current_unique = set(state.slot_assignments.values())
        if user_id not in current_unique and len(current_unique) >= state.max_group:
            await interaction.response.send_message(
                f"The party is already full ({state.max_group} unique players). Only those already signed may claim extra roles.",
                ephemeral=True,
            )
            return

        # Claim
        state.slot_assignments[self.slot_index] = user_id
        HOST_SESSIONS[self.signup_message_id] = state
        host_save()

        await interaction.response.edit_message(embed=build_host_signup_embed(interaction.guild, state), view=self.view)


class HostSignupView(discord.ui.View):
    def __init__(self, signup_message_id: int):
        super().__init__(timeout=60 * 60 * 6)
        state = HOST_SESSIONS.get(signup_message_id)
        if not state:
            return
        labels = _display_role_names_with_numbers(state.roles)
        for idx, label in enumerate(labels):
            self.add_item(HostSignupRoleButton(label=label, slot_index=idx, signup_message_id=signup_message_id))


# -----------------------------
# Views: PVM Tonight poll
# -----------------------------
class PvmBossToggleButton(discord.ui.Button):
    def __init__(self, boss_name: str, poll_message_id: int, user_id: int):
        selected = boss_name in PVM_POLLS.get(poll_message_id, {}).get(user_id, [])
        confirmed = PVM_CONFIRMED.get(poll_message_id, {}).get(user_id, False)
        super().__init__(
            style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
            label=boss_name,
            disabled=confirmed,
        )
        self.boss_name = boss_name
        self.poll_message_id = poll_message_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This selection panel is not yours.", ephemeral=True)
            return

        if PVM_CONFIRMED.get(self.poll_message_id, {}).get(self.user_id, False):
            await interaction.response.send_message("Your choices are confirmed. Press **Edit** to unlock them.", ephemeral=True)
            return

        PVM_POLLS.setdefault(self.poll_message_id, {})
        PVM_POLLS[self.poll_message_id].setdefault(self.user_id, [])

        selected = PVM_POLLS[self.poll_message_id][self.user_id]
        if self.boss_name in selected:
            selected.remove(self.boss_name)
        else:
            selected.append(self.boss_name)

        pvm_save()

        await interaction.response.edit_message(
            embed=build_pvm_selection_embed(interaction.user, selected, confirmed=False),
            view=PvmSelectionView(self.poll_message_id, self.user_id),
        )


class PvmConfirmButton(discord.ui.Button):
    def __init__(self, poll_message_id: int, user_id: int):
        confirmed = PVM_CONFIRMED.get(poll_message_id, {}).get(user_id, False)
        super().__init__(style=discord.ButtonStyle.success, label="Confirm", emoji="✅", disabled=confirmed)
        self.poll_message_id = poll_message_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This selection panel is not yours.", ephemeral=True)
            return
        PVM_CONFIRMED.setdefault(self.poll_message_id, {})[self.user_id] = True
        pvm_save()
        selected = PVM_POLLS.get(self.poll_message_id, {}).get(self.user_id, [])
        log_event(f"PVM Tonight choices confirmed by {interaction.user}: {', '.join(selected) if selected else 'none'}")
        await interaction.response.edit_message(
            embed=build_pvm_selection_embed(interaction.user, selected, confirmed=True),
            view=PvmSelectionView(self.poll_message_id, self.user_id),
        )


class PvmEditButton(discord.ui.Button):
    def __init__(self, poll_message_id: int, user_id: int):
        confirmed = PVM_CONFIRMED.get(poll_message_id, {}).get(user_id, False)
        super().__init__(style=discord.ButtonStyle.primary, label="Edit", emoji="✏️", disabled=not confirmed)
        self.poll_message_id = poll_message_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This selection panel is not yours.", ephemeral=True)
            return
        PVM_CONFIRMED.setdefault(self.poll_message_id, {})[self.user_id] = False
        pvm_save()
        selected = PVM_POLLS.get(self.poll_message_id, {}).get(self.user_id, [])
        log_event(f"PVM Tonight choices unlocked for editing by {interaction.user}")
        await interaction.response.edit_message(
            embed=build_pvm_selection_embed(interaction.user, selected, confirmed=False),
            view=PvmSelectionView(self.poll_message_id, self.user_id),
        )


class PvmSelectionView(discord.ui.View):
    def __init__(self, poll_message_id: int, user_id: int):
        super().__init__(timeout=60 * 60 * 12)
        for boss in BOSS_NAMES:
            self.add_item(PvmBossToggleButton(boss, poll_message_id, user_id))
        self.add_item(PvmConfirmButton(poll_message_id, user_id))
        self.add_item(PvmEditButton(poll_message_id, user_id))


class PvmAvailabilityButton(discord.ui.Button):
    def __init__(self, poll_message_id: int, free: bool):
        label = "I am free tonight" if free else "I am not free tonight"
        emoji = "✅" if free else "❌"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(style=style, label=label, emoji=emoji)
        self.poll_message_id = poll_message_id
        self.free = free

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        PVM_AVAILABILITY.setdefault(self.poll_message_id, {"free": [], "not_free": []})
        availability = PVM_AVAILABILITY[self.poll_message_id]
        free_list = availability.setdefault("free", [])
        not_free_list = availability.setdefault("not_free", [])

        if self.free:
            if uid not in free_list:
                free_list.append(uid)
            if uid in not_free_list:
                not_free_list.remove(uid)
            PVM_POLLS.setdefault(self.poll_message_id, {}).setdefault(uid, [])
            PVM_CONFIRMED.setdefault(self.poll_message_id, {}).setdefault(uid, False)
            pvm_save()
            log_event(f"PVM Tonight availability: {interaction.user} is FREE")
            await interaction.response.edit_message(
                embed=build_pvm_poll_embed(interaction, self.poll_message_id),
                view=PvmPollView(self.poll_message_id),
            )
            selected = PVM_POLLS[self.poll_message_id][uid]
            confirmed = PVM_CONFIRMED.get(self.poll_message_id, {}).get(uid, False)
            await interaction.followup.send(
                embed=build_pvm_selection_embed(interaction.user, selected, confirmed),
                view=PvmSelectionView(self.poll_message_id, uid),
                ephemeral=True,
            )
        else:
            if uid not in not_free_list:
                not_free_list.append(uid)
            if uid in free_list:
                free_list.remove(uid)
            PVM_POLLS.setdefault(self.poll_message_id, {}).pop(uid, None)
            PVM_CONFIRMED.setdefault(self.poll_message_id, {}).pop(uid, None)
            pvm_save()
            log_event(f"PVM Tonight availability: {interaction.user} is NOT FREE")
            await interaction.response.edit_message(
                embed=build_pvm_poll_embed(interaction, self.poll_message_id),
                view=PvmPollView(self.poll_message_id),
            )


class PvmPollView(discord.ui.View):
    def __init__(self, poll_message_id: int):
        super().__init__(timeout=60 * 60 * 12)
        self.add_item(PvmAvailabilityButton(poll_message_id, True))
        self.add_item(PvmAvailabilityButton(poll_message_id, False))

# -----------------------------
# Views: Role signup board (bosspick)
# -----------------------------
class RoleButton(discord.ui.Button):
    def __init__(self, label: str, slot_index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label=label)
        self.slot_index = slot_index

    async def callback(self, interaction: discord.Interaction):
        assert interaction.message is not None
        msg_id = interaction.message.id
        state = ROLE_SESSIONS.get(msg_id)
        if not state:
            await interaction.response.send_message("This role board has expired or was restarted.", ephemeral=True)
            return

        user_id = interaction.user.id
        taken_by = state.slot_assignments.get(self.slot_index)

        if taken_by == user_id:
            state.slot_assignments.pop(self.slot_index, None)
            await interaction.response.edit_message(embed=build_roles_embed(interaction, state), view=self.view)
            return

        if taken_by and taken_by != user_id:
            await interaction.response.send_message("That role is already claimed!", ephemeral=True)
            return

        current_unique = set(state.slot_assignments.values())
        if user_id not in current_unique and len(current_unique) >= state.max_group:
            await interaction.response.send_message(
                f"The party is already full ({state.max_group} unique players). Only those already in the party may claim additional roles.",
                ephemeral=True,
            )
            return

        state.slot_assignments[self.slot_index] = user_id
        await interaction.response.edit_message(embed=build_roles_embed(interaction, state), view=self.view)

        if all_slots_filled(state):
            unique_users = list(dict.fromkeys(state.slot_assignments.values()))
            mentions = " ".join(f"<@{uid}>" for uid in unique_users)
            await interaction.followup.send(
                f"📯 {mentions}\nHearken! Begin boosting thy health — the fight is upon thee. All roles have been completed, and ’tis time to unite once more!",
                allowed_mentions=discord.AllowedMentions(users=True),
            )


class ReadyNowButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.primary, label="Ready now!", emoji="📯")

    async def callback(self, interaction: discord.Interaction):
        assert interaction.message is not None
        msg_id = interaction.message.id
        state = ROLE_SESSIONS.get(msg_id)
        if not state:
            await interaction.response.send_message("This role board has expired or was restarted.", ephemeral=True)
            return

        view: discord.ui.View = self.view  # type: ignore
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        await interaction.response.edit_message(view=view)

        unique_users = list(dict.fromkeys(state.slot_assignments.values()))
        if not unique_users:
            await interaction.followup.send("None have claimed a role yet — I cannot rally an empty warband!", ephemeral=True)
            return
        mentions = " ".join(f"<@{uid}>" for uid in unique_users)
        await interaction.followup.send(
            f"📯 {mentions}\nHearken! Begin boosting thy health — the fight is upon thee. Though not all stations be manned, we march regardless. Unite once more!",
            allowed_mentions=discord.AllowedMentions(users=True),
        )


class RoleSignupView(discord.ui.View):
    def __init__(self, state: RoleSignupState):
        super().__init__(timeout=60 * 60)
        labels = _display_role_names_with_numbers(state.roles)
        for idx, label in enumerate(labels):
            self.add_item(RoleButton(label=label, slot_index=idx))
        self.add_item(ReadyNowButton())


# -----------------------------
# Bot client
# -----------------------------
class KillBotClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.messages = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild_ids: List[int] = []
        if GUILD_IDS_ENV:
            for part in GUILD_IDS_ENV.split(","):
                part = part.strip()
                if part.isdigit():
                    guild_ids.append(int(part))

        if guild_ids:
            # Avoid Discord showing duplicate global + guild commands by clearing stale global commands first.
            global_commands = list(self.tree.get_commands(guild=None))
            for gid in guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()  # clears old global commands from Discord
            for command in global_commands:
                try:
                    self.tree.add_command(command)
                except app_commands.CommandAlreadyRegistered:
                    pass
            for gid in guild_ids:
                guild = discord.Object(id=gid)
                await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


    async def close(self):
        global HTTP_SESSION
        if HTTP_SESSION is not None and not HTTP_SESSION.closed:
            await HTTP_SESSION.close()
        await super().close()


client = KillBotClient()


async def seconds_until_next_local(hour: int, minute: int = 0) -> float:
    now = datetime.now(LOCAL_TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


async def daily_pvm_scheduler():
    await client.wait_until_ready()
    log_event("Daily PVM scheduler started.")
    while not client.is_closed():
        # Daily PVM poll at 13:00 UK.
        await asyncio.sleep(await seconds_until_next_local(13, 0))
        for guild in client.guilds:
            channel = find_text_channel(guild, DAILY_PVM_CHANNEL_NAME)
            if channel:
                await create_pvmtonight_poll(channel, guild)
            else:
                log_event(f"Daily PVM scheduler: #{DAILY_PVM_CHANNEL_NAME} not found in {guild.name}")

        # Go Time at 21:00 UK.
        await asyncio.sleep(await seconds_until_next_local(21, 0))
        for guild in client.guilds:
            channel = find_text_channel(guild, DAILY_PVM_CHANNEL_NAME)
            if channel:
                await run_gotime_for_channel(channel, guild, client.user.id if client.user else 0)
            else:
                log_event(f"Daily GoTime scheduler: #{DAILY_PVM_CHANNEL_NAME} not found in {guild.name}")


@client.event
async def on_ready():
    pk_load()
    teampenguin_load()
    kgp_load()
    host_load()
    pvm_load()
    rank_load()
    reminders_load()
    rsn_load()
    custom_low_value_load()
    ensure_permanent_achievement_channels(client)

    global INCIDENT_LOG_WORKER_STARTED
    if not INCIDENT_LOG_WORKER_STARTED:
        INCIDENT_LOG_WORKER_STARTED = True
        asyncio.create_task(incident_report_worker(client))

    log_event(f"Logged in as {client.user}")
    log_event(f"PK loaded: {len(PK_SCORES)} players, {len(PK_HISTORY)} results")
    log_event(f"RuneMetrics registered users: {len(RSN_REGISTRATIONS)}")
    if str(LOCAL_TZ) == "UTC" and LOCAL_TZ_NAME != "UTC":
        print("NOTE: Timezone data missing on this machine. Install tzdata:")
        print("      py -m pip install tzdata")

    # Reschedule pending /remindme reminders after restart
    now_u = now_unix_utc()
    for reminder_id, reminder in list(REMINDERS.items()):
        if int(reminder.get("due_unix", 0)) > now_u:
            schedule_reminder(client, reminder_id)
        else:
            REMINDERS.pop(reminder_id, None)
    reminders_save()

    global ACHIEVEMENT_TASK_STARTED
    if not ACHIEVEMENT_TASK_STARTED:
        ACHIEVEMENT_TASK_STARTED = True
        log_event("Starting RuneMetrics catch-up and regular poll worker.")
        asyncio.create_task(run_rune_metrics_worker(client))

    global AUTO_PVM_TASK_STARTED
    if not AUTO_PVM_TASK_STARTED:
        AUTO_PVM_TASK_STARTED = True
        asyncio.create_task(daily_pvm_scheduler())


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if isinstance(message.author, discord.Member):
        await award_activity_points(message.author, MESSAGE_POINTS, "last_message", MESSAGE_COOLDOWN_SECONDS)


@client.event
async def on_interaction(interaction: discord.Interaction):
    command_name = getattr(interaction.command, "name", None) if interaction.command else None
    if command_name:
        guild_name = interaction.guild.name if interaction.guild else "DM/Unknown Guild"
        log_event(f"Command executed: /{command_name} by {interaction.user} in {guild_name}")

    if interaction.guild is None:
        return
    user = interaction.user
    if isinstance(user, discord.Member):
        await award_activity_points(user, INTERACTION_POINTS, "last_interaction", INTERACTION_COOLDOWN_SECONDS)


# -----------------------------
# Commands
# -----------------------------
@client.tree.command(name="bosspick", description="Pick bosses, roll one, confirm, then assign roles.")
async def bosspick(interaction: discord.Interaction):
    view = BossPickView()
    await interaction.response.send_message(embed=build_welcome_embed(interaction, view.state), view=view)


@client.tree.command(name="updaterole", description="Staff: assign/remove roles on the latest GoTime signup board in this channel.")
@app_commands.describe(
    user="Discord user to assign/remove",
    role="Role label exactly as shown, e.g. Green 2 or DPS 3",
    action="add or remove"
)
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def updaterole(
    interaction: discord.Interaction,
    user: discord.Member,
    role: str,
    action: app_commands.Choice[str],
):
    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message("Nay. Only **Ice Marshalls** or **Emperor Penguins** may wield the Admin Quill.", ephemeral=True)
        return

    signup_msg_id = CHANNEL_LATEST_HOST.get(interaction.channel_id)
    if not signup_msg_id or signup_msg_id not in HOST_SESSIONS:
        await interaction.response.send_message("No GoTime signup board found in this channel. Use `/gotime` after `/pvmtonight` first.", ephemeral=True)
        return

    state = HOST_SESSIONS[signup_msg_id]
    display = _display_role_names_with_numbers(state.roles)
    wanted = role.strip().lower()

    matches = [i for i, lbl in enumerate(display) if lbl.lower() == wanted]
    if not matches:
        await interaction.response.send_message("Role not found. Try one of:\n" + "\n".join(f"• {x}" for x in display), ephemeral=True)
        return

    if action.value == "add":
        target = next((i for i in matches if i not in state.slot_assignments), None)
        if target is None:
            await interaction.response.send_message("All slots for that role label are already filled.", ephemeral=True)
            return

        current_unique = set(state.slot_assignments.values())
        if user.id not in current_unique and len(current_unique) >= state.max_group:
            await interaction.response.send_message(f"Party is full ({state.max_group} unique players).", ephemeral=True)
            return

        state.slot_assignments[target] = user.id

    else:
        removed = False
        for i in matches:
            if state.slot_assignments.get(i) == user.id:
                state.slot_assignments.pop(i, None)
                removed = True
                break
        if not removed:
            await interaction.response.send_message("That user does not currently hold that role.", ephemeral=True)
            return

    HOST_SESSIONS[signup_msg_id] = state
    host_save()

    # Update the main signup message
    try:
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            signup_msg = await channel.fetch_message(signup_msg_id)
            await signup_msg.edit(embed=build_host_signup_embed(interaction.guild, state), view=HostSignupView(signup_msg_id))
    except Exception:
        pass

    await interaction.response.send_message(
        f"✍️ Updated: {action.value} `{role.strip()}` for {user.mention}.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions(users=True),
    )


async def create_pvmtonight_poll(channel: discord.TextChannel, source=None) -> Optional[discord.Message]:
    msg = await channel.send(embed=build_pvm_poll_embed(source or channel.guild), view=discord.ui.View())
    PVM_POLLS[msg.id] = {}
    PVM_AVAILABILITY[msg.id] = {"free": [], "not_free": []}
    PVM_CONFIRMED[msg.id] = {}
    CHANNEL_LATEST_PVM_POLL[channel.id] = msg.id
    pvm_save()
    await msg.edit(embed=build_pvm_poll_embed(source or channel.guild, msg.id), view=PvmPollView(msg.id))
    log_event(f"PVM Tonight poll posted in #{channel.name} (message {msg.id})")
    return msg


async def run_gotime_for_channel(channel: discord.TextChannel, guild: discord.Guild, actor_id: int, source_interaction: Optional[discord.Interaction] = None):
    poll_id = CHANNEL_LATEST_PVM_POLL.get(channel.id)

    async def respond(content: str, ephemeral: bool = True):
        if source_interaction:
            if source_interaction.response.is_done():
                await source_interaction.followup.send(content, ephemeral=ephemeral)
            else:
                await source_interaction.response.send_message(content, ephemeral=ephemeral)
        else:
            await channel.send(content)

    if not poll_id or poll_id not in PVM_POLLS:
        await respond("No PVM poll found in this channel. Use `/pvmtonight` first.")
        return

    free_users = set(PVM_AVAILABILITY.get(poll_id, {}).get("free", []))
    responses = {uid: bosses for uid, bosses in PVM_POLLS[poll_id].items() if uid in free_users}
    if not responses:
        await respond("No free players have selected bosses yet. The warband is empty.")
        return

    boss_counts: Dict[str, int] = {}
    for bosses in responses.values():
        for boss in bosses:
            boss_counts[boss] = boss_counts.get(boss, 0) + 1

    if not boss_counts:
        await respond("Free players responded, but no bosses were selected.")
        return

    eligible_counts = {
        boss: count
        for boss, count in boss_counts.items()
        if boss in BOSS_DATA and count <= int(BOSS_DATA[boss]["max_group"])
    }

    if not eligible_counts:
        overfilled = sorted(boss_counts.items(), key=lambda x: x[1], reverse=True)
        text = (
            "Every selected boss is over its max group size, so I cannot fairly choose one.\n\n"
            + "\n".join(
                f"• **{boss}** — {count} interested / max {BOSS_DATA.get(boss, {}).get('max_group', 'unknown')}"
                for boss, count in overfilled
            )
        )
        await respond(text)
        return

    highest = max(eligible_counts.values())
    top_choices = [boss for boss, count in eligible_counts.items() if count == highest]
    chosen = random.choice(top_choices)
    sorted_counts = sorted(boss_counts.items(), key=lambda x: x[1], reverse=True)

    embed = discord.Embed(
        title="📯 Go Time!",
        description=(
            "The people have spoken. I have ignored any boss that had more interested players than its max group size.\n\n"
            f"**Chosen PVM:** {chosen}\n"
            f"**Votes:** {highest}\n"
            f"**Max group size:** {BOSS_DATA[chosen]['max_group']}\n\n"
            "Sharpen thy blades, charge thy runes, and blame Kyle if this goes poorly."
        ),
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="Vote Breakdown",
        value="\n".join(
            f"• **{boss}** — {count} / max {BOSS_DATA.get(boss, {}).get('max_group', 'unknown')}"
            for boss, count in sorted_counts
        ),
        inline=False,
    )

    player_lines = []
    for uid, bosses in responses.items():
        picks = ", ".join(bosses) if bosses else "*none selected*"
        confirmed = " ✅" if PVM_CONFIRMED.get(poll_id, {}).get(uid, False) else ""
        player_lines.append(f"• <@{uid}>{confirmed} — {picks}")

    embed.add_field(name="Available Players", value="\n".join(player_lines), inline=False)
    if client.user:
        embed.set_thumbnail(url=client.user.display_avatar.replace(size=512, static_format="png").url)

    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    boss_info = BOSS_DATA[chosen]
    state = HostSessionState(
        boss_name=chosen,
        roles=signup_roles_for_boss(chosen),
        max_group=boss_info["max_group"],
        start_unix=now_unix_utc(),
        guild_id=guild.id,
        channel_id=channel.id,
        host_user_id=actor_id,
        event_url=None,
    )

    signup_embed = build_host_signup_embed(guild, state)
    if client.user:
        signup_embed.set_thumbnail(url=client.user.display_avatar.replace(size=512, static_format="png").url)

    signup_msg = await channel.send(embed=signup_embed, view=discord.ui.View())
    HOST_SESSIONS[signup_msg.id] = state
    CHANNEL_LATEST_HOST[channel.id] = signup_msg.id
    host_save()
    await signup_msg.edit(view=HostSignupView(signup_msg.id))
    log_event(f"GoTime completed in #{channel.name}: chose {chosen}")


@client.tree.command(name="pvmtonight", description="Post a PVM availability poll for tonight.")
async def pvmtonight(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("This command must be used in a normal text channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await create_pvmtonight_poll(interaction.channel, interaction)
    await interaction.followup.send("PVM Tonight poll posted.", ephemeral=True)


@client.tree.command(name="gotime", description="Collate tonight's PVM responses, pick an eligible option, and post a signup sheet.")
async def gotime(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel) or not interaction.guild:
        await interaction.response.send_message("This command must be used in a normal server text channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await run_gotime_for_channel(interaction.channel, interaction.guild, interaction.user.id, interaction)
    await interaction.followup.send("Go Time processed.", ephemeral=True)


@client.tree.command(name="remindme", description="Ask Kill Bot to remind you in a number of days.")
@app_commands.describe(days="Number of days from now to remind you")
async def remindme(interaction: discord.Interaction, days: int):
    if days < 1 or days > 365:
        await interaction.response.send_message("Please choose between **1** and **365** days.", ephemeral=True)
        return

    created = now_unix_utc()
    due = created + days * 24 * 60 * 60
    day_text = "1 day" if days == 1 else f"{days} days"

    await interaction.response.send_message(
        f"⏰ {interaction.user.mention}, I will remind you in **{day_text}**.",
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    original = await interaction.original_response()

    reminder_id = f"{created}-{interaction.user.id}-{interaction.channel_id}"
    REMINDERS[reminder_id] = {
        "user_id": interaction.user.id,
        "guild_id": interaction.guild.id if interaction.guild else 0,
        "channel_id": interaction.channel_id,
        "created_unix": created,
        "due_unix": due,
        "days": days,
        "jump_url": original.jump_url,
    }
    reminders_save()
    schedule_reminder(client, reminder_id)


@client.tree.command(name="pk", description="Show PK scoreboard, or record a 1v1 PK (winner/loser).")
@app_commands.describe(winner="Winner (optional)", loser="Loser (optional)")
async def pk(interaction: discord.Interaction, winner: Optional[discord.Member] = None, loser: Optional[discord.Member] = None):
    if winner is None and loser is None:
        await interaction.response.send_message(embed=build_pk_embed(interaction.guild))
        return

    if winner is None or loser is None:
        await interaction.response.send_message("Provide both `winner` and `loser`, or neither.", ephemeral=True)
        return

    if winner.id == loser.id:
        await interaction.response.send_message("Winner and loser must be different people.", ephemeral=True)
        return

    PK_SCORES[winner.id] = PK_SCORES.get(winner.id, 0) + 1
    PK_SCORES.setdefault(loser.id, PK_SCORES.get(loser.id, 0))

    PK_HISTORY.append({"winner": str(winner.id), "loser": str(loser.id), "ts": datetime.utcnow().isoformat()})
    pk_save()

    await interaction.response.send_message(
        content=f"⚔️ Recorded PK: {winner.mention} defeated {loser.mention}",
        embed=build_pk_embed(interaction.guild),
        allowed_mentions=discord.AllowedMentions(users=True),
    )


def build_teampenguin_embed(guild: Optional[discord.Guild]) -> discord.Embed:
    embed = discord.Embed(
        title="🐧 Team Penguin",
        description="A most noble fellowship of questionable decisions and impeccable vibes.",
        color=discord.Color.blurple(),
    )

    if not TEAM_PENGUIN:
        embed.add_field(name="Members", value="*None added yet.*", inline=False)
        embed.set_footer(text="Ice Marshalls and Emperor Penguins can add members with /teampenguin action:add user:@Someone")
        return embed

    lines = []
    for uid in TEAM_PENGUIN:
        if guild:
            m = guild.get_member(uid)
            if m:
                lines.append(f"• {m.mention} ({m.display_name})")
                continue
        lines.append(f"• <@{uid}>")

    embed.add_field(name="Members", value="\n".join(lines), inline=False)
    embed.set_footer(text="Only Ice Marshalls and Emperor Penguins may add/remove members.")
    return embed


@client.tree.command(name="teampenguin", description="Show Team Penguin, or staff add/remove members.")
@app_commands.describe(action="Optional staff action", user="User to add/remove")
@app_commands.choices(action=[
    app_commands.Choice(name="show", value="show"),
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="clear", value="clear"),
])
async def teampenguin(interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None, user: Optional[discord.Member] = None):
    act = action.value if action else "show"

    if act == "show":
        embed = build_teampenguin_embed(interaction.guild)
        thumb = bot_thumbnail_url(interaction)
        if thumb:
            embed.set_thumbnail(url=thumb)
        await interaction.response.send_message(embed=embed)
        return

    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message("Nay. Only **Ice Marshalls** or **Emperor Penguins** may alter Team Penguin.", ephemeral=True)
        return

    if act == "clear":
        TEAM_PENGUIN.clear()
        teampenguin_save()
        embed = build_teampenguin_embed(interaction.guild)
        await interaction.response.send_message("🧹 Team Penguin hath been wiped clean.", embed=embed)
        return

    if user is None:
        await interaction.response.send_message("Provide a `user` to add/remove.", ephemeral=True)
        return

    if act == "add":
        if user.id in TEAM_PENGUIN:
            await interaction.response.send_message(f"{user.mention} is already in Team Penguin.", ephemeral=True)
            return
        TEAM_PENGUIN.append(user.id)
        teampenguin_save()
        await interaction.response.send_message(f"🐧 {user.mention} hath joined Team Penguin!", embed=build_teampenguin_embed(interaction.guild))
        return

    if act == "remove":
        if user.id not in TEAM_PENGUIN:
            await interaction.response.send_message(f"{user.mention} is not in Team Penguin.", ephemeral=True)
            return
        TEAM_PENGUIN.remove(user.id)
        teampenguin_save()
        await interaction.response.send_message(f"🪶 {user.mention} hath been removed from Team Penguin.", embed=build_teampenguin_embed(interaction.guild))
        return


# -----------------------------
# /blamekyle
# -----------------------------
BLAME_RESPONSES = [
    "Ah yes… this is clearly Kyle’s doing. Had he not once suggested we ‘just send it,’ the fabric of causality would not have unravelled, the Blue Wizard would not have miscast his rune, and thou wouldst not now be face-down upon the floor. The KGP has confirmed this timeline deviation.",
    "Tragic. Entirely tragic. Had Kyle filed Form 27B-Necromancy with the KGP in a timely manner, this catastrophic mechanic would have been foreseen, documented, and prevented. Alas — bureaucracy fails us once more.",
    "This reeks of Team Penguin interference. Kyle, clearly operating as Supreme Warden of the Flippers, neglected to stabilise the Penguin Alignment Matrix. The boss enraged as a direct consequence. Classic Kyle.",
    "Let us be honest — had Kyle never introduced thee to this individual, thou wouldst not have formed this party. Had this party not formed, thou wouldst not be here. Therefore, by ancient law of transitive blame… this is Kyle’s fault.",
    "The Blue Wizard sensed disturbance in the weave. It began the moment Kyle logged on.",
    "Let us trace this logically: Kyle introduced thee to this group → this group attempted this boss → thou art dead. Therefore, Kyle.",
    "Behold the mechanic thou failed to handle. And where was Kyle? Not present. Suspicious. A coincidence? The KGP thinks not. Had he merely existed slightly closer to the danger, events would have unfolded differently.",
    "There exists an ancient prophecy: 'Where there is wipe, there is Kyle.' It was foretold in the Third Age patch notes. Scholars dismissed it. Fools.",
    "The KGP Risk Assessment clearly marked this scenario as 'Kyle Probable.'",
    "In an alternate timeline, Kyle called the mechanic correctly. In this timeline, he did not. Thus we suffer. Quantum Kyle remains both helpful and useless simultaneously.",
    "Kyle once said 'don’t worry about it.' This is the direct consequence.",
    "Consider the cost of this death — supplies lost, dignity shattered, aura wasted. If Kyle had invested properly in TeamPenguin’s Risk Mitigation Fund, this tragedy would have been absorbed. Financial mismanagement, once again.",
    "Team Penguin financial modelling predicted this collapse if Kyle skipped leg day.",
    "The ley lines faltered when Kyle doubted the Blue Wizard’s drip.",
    "In 2019 Kyle misclicked once. The timeline has never recovered.",
    "It is mathematically impossible for this not to be Kyle’s fault.",
    "After thorough investigation by the KGP, Team Penguin, the Blue Wizard, and three independent necromancers… it has been unanimously decided that this outcome was, is, and forever shall be Kyle’s fault.",
    "Had Kyle simply existed more responsibly, thou wouldst still stand.",
]

RARE_BLAME_RESPONSES = [
    "🚨 ULTRA KYLE EVENT DETECTED 🚨\nThe KGP has escalated this to Level Crimson. Reality itself bent slightly left when Kyle entered the instance.",
    "🐧 Team Penguin Emergency Council convened. After 4 hours of heated debate, it was concluded unanimously: Kyle initiated this wipe in 2007.",
    "🧙 The Blue Wizard has foreseen all timelines. In 14,000,605 possible futures… every single one blamed Kyle.",
    "⚠️ Quantum Kyle Collapse ⚠️\nIn one universe Kyle saved the run. Unfortunately, this is not that universe.",
    "📜 The Ancient Codex of Blame has opened. Page 1 simply reads: 'Kyle.'",
]


@client.tree.command(name="blamekyle", description="Determine, once and for all, that Kyle is responsible.")
@app_commands.describe(boss="Optional: boss name to enhance the blame")
async def blamekyle(interaction: discord.Interaction, boss: Optional[str] = None):
    global KGP_INVESTIGATION_NUMBER
    KGP_INVESTIGATION_NUMBER += 1
    kgp_save()

    investigation_id = f"{KGP_INVESTIGATION_NUMBER:05d}"

    response = random.choice(RARE_BLAME_RESPONSES) if random.random() < 0.05 else random.choice(BLAME_RESPONSES)
    boss_text = f" during **{boss}**" if boss else ""

    embed = discord.Embed(
        title=f"📜 Official Blame Report — KGP Investigation #{investigation_id}",
        description=(
            f"{interaction.user.mention}, after thorough investigation{boss_text}, it has been determined beyond reasonable doubt:\n\n"
            f"**This is Kyle’s fault.**\n\n{response}"
        ),
        color=discord.Color.dark_red(),
    )
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.set_footer(text="KGP Certified • TeamPenguin Approved • Blue Wizard Verified")
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))


# -----------------------------
# /blameuser
# -----------------------------
BLAME_USER_RESPONSES = [
    "After an exhaustive KGP inquiry, three witness statements, and one suspiciously damp penguin feather, it has been concluded that {target} is entirely responsible.",
    "The Blue Wizard examined the timeline and found one constant thread of chaos. That thread was {target}.",
    "{target} claims innocence, which is exactly what a guilty person with low Prayer bonus would say.",
    "Team Penguin convened an emergency council. The vote was unanimous: blame {target}, then pretend this was the plan all along.",
    "Had {target} merely stood somewhere else, clicked something else, or breathed less suspiciously, this disaster may have been avoided.",
    "The KGP Risk Register has marked this as a Category Five {target} Incident.",
    "The mechanic was avoidable. The wipe was preventable. Unfortunately, {target} was present.",
    "Ancient goblin law states: if confusion exists and {target} is nearby, responsibility is automatically assigned.",
    "The evidence is circumstantial, emotional, and completely unfair. In other words, perfect. {target} is to blame.",
    "{target} has been found guilty of first-degree vibes-based negligence.",
]

RARE_BLAME_USER_RESPONSES = [
    "🚨 LEGENDARY BLAME EVENT 🚨\nReality itself submitted a complaint. The named party? {target}.",
    "🐧 Team Penguin has sealed the chamber. The verdict is carved into ice: {target}.",
    "🧙 The Blue Wizard entered a trance and spoke only one name for six minutes: {target}.",
    "📜 The KGP opened Investigation Omega. Page one says: blame {target}. Page two is just a drawing of a disappointed penguin.",
    "⚠️ Causality Collapse Detected ⚠️\nEvery timeline was reviewed. In all of them, {target} somehow made it worse.",
]


@client.tree.command(name="blameuser", description="Blame any selected user with official KGP authority.")
@app_commands.describe(user="The unfortunate soul to blame", reason="Optional reason or boss/mechanic")
async def blameuser(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    global KGP_INVESTIGATION_NUMBER
    KGP_INVESTIGATION_NUMBER += 1
    kgp_save()

    investigation_id = f"{KGP_INVESTIGATION_NUMBER:05d}"
    template = random.choice(RARE_BLAME_USER_RESPONSES) if random.random() < 0.05 else random.choice(BLAME_USER_RESPONSES)
    response = template.format(target=user.mention)
    reason_text = f"\n\n**Reason submitted:** {reason}" if reason else ""

    embed = discord.Embed(
        title=f"📜 Official Blame Report — KGP Investigation #{investigation_id}",
        description=(
            f"{interaction.user.mention} has requested formal blame allocation.\n\n"
            f"**Accused:** {user.mention}\n"
            f"**Verdict:** Guilty, obviously.{reason_text}\n\n"
            f"{response}"
        ),
        color=discord.Color.dark_red(),
    )

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.set_footer(text="KGP Certified • TeamPenguin Approved • Blue Wizard Verified")
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))



@client.tree.command(name="rank", description="Show your KGP activity points and earnable rank progress.")
@app_commands.describe(user="Optional: check another member")
async def rank(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    member = user or interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    embed = build_rank_embed(member)
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="rankboard", description="Show the KGP activity points leaderboard.")
async def rankboard(interaction: discord.Interaction):
    embed = build_rankboard_embed(interaction.guild)
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="rankadmin", description="Staff: add, remove, set or sync activity points for a member.")
@app_commands.describe(user="Member to update", action="add/remove/set/sync", points="Points amount, ignored for sync")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="set", value="set"),
    app_commands.Choice(name="sync", value="sync"),
])
async def rankadmin(
    interaction: discord.Interaction,
    user: discord.Member,
    action: app_commands.Choice[str],
    points: Optional[int] = None,
):
    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message("Nay. Only **Ice Marshalls** or **Emperor Penguins** may adjust the sacred penguin ledger.", ephemeral=True)
        return

    data = RANK_DATA.setdefault(user.id, {"points": 0, "last_message": 0, "last_interaction": 0})

    if action.value == "sync":
        await apply_earnable_rank(user)
    else:
        if points is None:
            await interaction.response.send_message("Provide a points value for add/remove/set.", ephemeral=True)
            return
        if action.value == "add":
            data["points"] = int(data.get("points", 0)) + points
        elif action.value == "remove":
            data["points"] = max(0, int(data.get("points", 0)) - points)
        elif action.value == "set":
            data["points"] = max(0, points)
        rank_save()
        await apply_earnable_rank(user)

    await interaction.response.send_message(embed=build_rank_embed(user), ephemeral=True)


# -----------------------------
# /poll
# -----------------------------
@client.tree.command(name="poll", description="Create a generic poll with up to 10 options.")
@app_commands.describe(
    question="The poll question",
    options="Comma-separated options, e.g. Option 1, Option 2, Option 3"
)
async def poll(interaction: discord.Interaction, question: str, options: str):
    choices = [x.strip() for x in options.split(",") if x.strip()]

    if len(choices) < 2:
        await interaction.response.send_message(
            "A poll needs at least **2 options**. Example: `/poll question:What should we do? options:Amascut, Croesus, Vorago`",
            ephemeral=True
        )
        return

    if len(choices) > 10:
        await interaction.response.send_message(
            "Maximum **10 options** allowed.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📊 KillBot Poll",
        description=f"**{question}**",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Options",
        value="\n".join(f"{POLL_EMOJIS[i]} {choice}" for i, choice in enumerate(choices)),
        inline=False,
    )

    embed.set_footer(text=f"Poll created by {interaction.user.display_name}")

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()

    for i in range(len(choices)):
        await msg.add_reaction(POLL_EMOJIS[i])




@client.tree.command(name="announce", description="Make Kill Bot post an announcement. Restricted to staff roles.")
@app_commands.describe(message="The announcement message for Kill Bot to post")
async def announce(interaction: discord.Interaction, message: str):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    if not can_announce(interaction.user):
        await interaction.response.send_message(
            "Nay. Thou lackest the authority to issue announcements.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="📯 Kill Bot Announcement",
        description=message,
        color=discord.Color.gold(),
    )

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.set_footer(text=f"Announcement by {interaction.user.display_name}")

    await interaction.response.send_message("📯 Announcement posted.", ephemeral=True)
    await interaction.channel.send(embed=embed)


# -----------------------------
# RuneScape / RuneMetrics Commands
# -----------------------------
@client.tree.command(name="rsregister", description="Register your RuneScape in-game name for achievement tracking.")
@app_commands.describe(rsn="Your RuneScape display name")
async def rsregister(interaction: discord.Interaction, rsn: str):
    await interaction.response.defer(ephemeral=True)

    profile = await fetch_runemetrics_profile(rsn)
    if profile is None:
        await interaction.followup.send(
            "I could not read that RuneMetrics profile. Check the RSN spelling and that the profile/activity data is visible.",
            ephemeral=True,
        )
        return

    activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []

    RSN_REGISTRATIONS[interaction.user.id] = rsn.strip()
    RSN_DISCORD_NAMES[interaction.user.id] = interaction.user.display_name
    RSN_LAST_ACTIVITY_KEYS[interaction.user.id] = [runemetrics_activity_key(a) for a in activities if isinstance(a, dict) and a.get("text")][:20]
    RSN_PROFILE_BASELINES[interaction.user.id] = profile_snapshot(profile)
    rsn_save()

    log_event(f"RSN registered: Discord={interaction.user} ({interaction.user.id}) RSN={rsn.strip()}")

    await interaction.followup.send(
        f"✅ Registered **{rsn.strip()}** for {interaction.user.mention}. I have stored the current RuneMetrics activity, levels and skill XP milestones as a baseline, so only new progress will be posted. This registration is saved in `rsn_tracking.json`.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions(users=True),
    )


@client.tree.command(name="rsunregister", description="Remove your RuneScape achievement tracking registration.")
async def rsunregister(interaction: discord.Interaction):
    removed = RSN_REGISTRATIONS.pop(interaction.user.id, None)
    RSN_DISCORD_NAMES.pop(interaction.user.id, None)
    RSN_LAST_ACTIVITY_KEYS.pop(interaction.user.id, None)
    RSN_PROFILE_BASELINES.pop(interaction.user.id, None)
    rsn_save()

    if removed:
        log_event(f"RSN unregistered: Discord={interaction.user} ({interaction.user.id}) RSN={removed}")
        await interaction.response.send_message(f"Removed RuneMetrics tracking for **{removed}**.", ephemeral=True)
    else:
        await interaction.response.send_message("You do not currently have an RSN registered.", ephemeral=True)


@client.tree.command(name="rsassign", description="Staff: assign a RuneScape name to a Discord member for tracking.")
@app_commands.describe(user="Discord member to assign the RSN to", rsn="RuneScape display name to track")
async def rsassign(interaction: discord.Interaction, user: discord.Member, rsn: str):
    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message(
            "Nay. Only **Ice Marshalls** or **Emperor Penguins** may assign RSNs for others.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    profile = await fetch_runemetrics_profile(rsn)
    if profile is None:
        await interaction.followup.send(
            "I could not read that RuneMetrics profile. Check the RSN spelling and that the profile/activity data is visible.",
            ephemeral=True,
        )
        return

    activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []

    RSN_REGISTRATIONS[user.id] = rsn.strip()
    RSN_DISCORD_NAMES[user.id] = user.display_name
    RSN_LAST_ACTIVITY_KEYS[user.id] = [runemetrics_activity_key(a) for a in activities if isinstance(a, dict) and a.get("text")][:20]
    RSN_PROFILE_BASELINES[user.id] = profile_snapshot(profile)
    rsn_save()

    log_event(
        f"RSN assigned by {interaction.user} ({interaction.user.id}): "
        f"Discord={user} ({user.id}) RSN={rsn.strip()}"
    )

    await interaction.followup.send(
        f"✅ Assigned RSN **{rsn.strip()}** to {user.mention}. Current RuneMetrics activity, levels and skill XP milestones have been stored as the baseline.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions(users=True),
    )


@client.tree.command(name="rsregistered", description="Show registered RuneScape names for achievement tracking.")
async def rsregistered(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Registered RuneScape Names",
        description="Kill Bot stores this list in `rsn_tracking.json`, so it survives restarts and device moves as long as that file is copied with the bot.",
        color=discord.Color.blurple(),
    )
    if not RSN_REGISTRATIONS:
        embed.add_field(name="Registrations", value="No RuneScape names registered yet. Use `/rsregister rsn:<name>`.", inline=False)
    else:
        lines = []
        for uid, rsn in RSN_REGISTRATIONS.items():
            discord_name = RSN_DISCORD_NAMES.get(uid, "Unknown Discord name")
            lines.append(f"• **{discord_name}** / <@{uid}> — RSN: **{rsn}**")
        embed.add_field(name="Discord ↔ RSN List", value="\n".join(lines), inline=False)

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    await interaction.response.send_message(embed=embed)



@client.tree.command(name="rschecknow", description="Force-check your registered RuneScape profile now.")
async def rschecknow(interaction: discord.Interaction):
    rsn = RSN_REGISTRATIONS.get(interaction.user.id)
    RSN_DISCORD_NAMES[interaction.user.id] = interaction.user.display_name
    if not rsn:
        await interaction.response.send_message("You do not have an RSN registered. Use `/rsregister rsn:<name>` first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    log_event(f"Manual RuneMetrics check requested by {interaction.user} for RSN={rsn}")
    profile = await fetch_runemetrics_profile(rsn)
    if not profile:
        await interaction.followup.send("I could not read your RuneMetrics profile right now.", ephemeral=True)
        return

    current_snapshot = profile_snapshot(profile)
    previous_snapshot = RSN_PROFILE_BASELINES.get(interaction.user.id)

    if not previous_snapshot:
        RSN_PROFILE_BASELINES[interaction.user.id] = current_snapshot
        rsn_save()
        await interaction.followup.send("Baseline created. Future level and XP changes will now be tracked.", ephemeral=True)
        return

    messages = profile_progress_messages(interaction.user.id, rsn, previous_snapshot, current_snapshot)
    activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []
    previous_keys = RSN_LAST_ACTIVITY_KEYS.get(interaction.user.id, [])
    activity_messages = [
        format_achievement_message(interaction.user.id, rsn, a)
        for a in reversed(activities)
        if isinstance(a, dict) and a.get("text") and should_post_runemetrics_activity(a) and runemetrics_activity_key(a) not in previous_keys
    ]
    messages = activity_messages + messages

    RSN_LAST_ACTIVITY_KEYS[interaction.user.id] = [runemetrics_activity_key(a) for a in activities if isinstance(a, dict) and a.get("text")][:20]
    if messages and profile_progress_messages(interaction.user.id, rsn, previous_snapshot, current_snapshot):
        RSN_PROFILE_BASELINES[interaction.user.id] = current_snapshot
    rsn_save()

    if not messages:
        log_event(f"Manual RuneMetrics check completed for {rsn}: no new updates.")
        await interaction.followup.send("Checked successfully. No new RuneMetrics activity, level 99/120 milestones, or 50m/100m/150m/200m skill XP milestones found.", ephemeral=True)
        return

    log_event(f"Manual RuneMetrics check completed for {rsn}: {len(messages)} update(s) found.")
    await send_rune_metrics_messages(client, messages)
    await interaction.followup.send(f"Posted **{len(messages)}** RuneScape update(s) to the achievement channel.", ephemeral=True)





# -----------------------------
# Admin Dashboard / Runtime Drop Suppression
# -----------------------------
def build_admin_dashboard_embed(interaction: discord.Interaction) -> discord.Embed:
    uptime_seconds = now_unix_utc() - BOT_START_UNIX
    embed = discord.Embed(
        title="🛠️ Kill Bot Administrator Dashboard",
        description=(
            "Operational controls for Ice Marshalls and Emperor Penguins.\n\n"
            "Use the buttons below to restart/update, check health, and manage the local drop suppression list."
        ),
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="Status", value=f"Online for **{format_uptime(uptime_seconds)}**", inline=True)
    embed.add_field(name="WebSocket", value=f"**{round(client.latency * 1000)}ms**", inline=True)
    embed.add_field(name="Tracked RSNs", value=str(len(RSN_REGISTRATIONS)), inline=True)
    embed.add_field(name="Static ignored drops", value=str(len(LOW_VALUE_DROP_ITEMS)), inline=True)
    embed.add_field(name="Custom ignored drops", value=str(len(CUSTOM_LOW_VALUE_DROP_ITEMS)), inline=True)
    embed.add_field(name="Achievement channel", value=f"#{PERMANENT_ACHIEVEMENT_CHANNEL_NAME}", inline=True)
    embed.add_field(
        name="Useful commands",
        value=(
            "`/updatebot` — restart and let the launcher pull GitHub updates\n"
            "`/ignoredrop action:add item:<item>` — suppress a future broadcast\n"
            "`/ignoredrop action:remove item:<item>` — remove a custom suppression\n"
            "`/rschecknow` — force a RuneMetrics check for yourself"
        ),
        inline=False,
    )
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.set_footer(text="KGP Administration • Do not feed the bot after midnight")
    return embed


class IgnoredDropModal(discord.ui.Modal):
    def __init__(self, action: str):
        title = "Add ignored drop" if action == "add" else "Remove ignored drop"
        super().__init__(title=title)
        self.action = action
        self.item = discord.ui.TextInput(
            label="Drop/item name",
            placeholder="Example: shield left half",
            required=True,
            max_length=120,
        )
        self.add_item(self.item)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
            await interaction.response.send_message("Nay. Thou lackest dashboard authority.", ephemeral=True)
            return

        item = normalise_drop_item_name(str(self.item.value))
        if not item:
            await interaction.response.send_message("No item name supplied.", ephemeral=True)
            return

        if self.action == "add":
            CUSTOM_LOW_VALUE_DROP_ITEMS.add(item)
            custom_low_value_save()
            log_event(f"Admin dashboard: {interaction.user} added custom ignored drop '{item}'.")
            await interaction.response.send_message(f"✅ Added **{item}** to the custom ignored drop list.", ephemeral=True)
        else:
            if item in CUSTOM_LOW_VALUE_DROP_ITEMS:
                CUSTOM_LOW_VALUE_DROP_ITEMS.remove(item)
                custom_low_value_save()
                log_event(f"Admin dashboard: {interaction.user} removed custom ignored drop '{item}'.")
                await interaction.response.send_message(f"✅ Removed **{item}** from the custom ignored drop list.", ephemeral=True)
            elif item in LOW_VALUE_DROP_ITEMS:
                await interaction.response.send_message(
                    f"**{item}** is part of the built-in ignored drop list, so it cannot be removed from Discord. Edit the code if you truly want it broadcast.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(f"**{item}** was not in the custom ignored drop list.", ephemeral=True)


class AdminDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
            await interaction.response.send_message("Nay. Only Ice Marshalls and Emperor Penguins may use this dashboard.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ping", emoji="🏓", style=discord.ButtonStyle.secondary)
    async def ping_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            f"🏓 Pong. WebSocket latency: **{round(client.latency * 1000)}ms**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Uptime", emoji="⏱️", style=discord.ButtonStyle.secondary)
    async def uptime_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        uptime_seconds = now_unix_utc() - BOT_START_UNIX
        await interaction.response.send_message(
            f"⏱️ Kill Bot has been online for **{format_uptime(uptime_seconds)}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="List ignored drops", emoji="📋", style=discord.ButtonStyle.secondary)
    async def list_ignored_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        custom = sorted(CUSTOM_LOW_VALUE_DROP_ITEMS)
        if not custom:
            await interaction.response.send_message("No custom ignored drops yet. Built-in list is active in code.", ephemeral=True)
            return
        shown = custom[:60]
        more = len(custom) - len(shown)
        text = "\n".join(f"• {x}" for x in shown)
        if more > 0:
            text += f"\n…and {more} more."
        await interaction.response.send_message(f"📋 **Custom ignored drops:**\n{text}", ephemeral=True)

    @discord.ui.button(label="Add ignored drop", emoji="➕", style=discord.ButtonStyle.success)
    async def add_ignored_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
            await interaction.response.send_message("Nay. Thou lackest dashboard authority.", ephemeral=True)
            return
        await interaction.response.send_modal(IgnoredDropModal("add"))

    @discord.ui.button(label="Remove ignored drop", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove_ignored_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
            await interaction.response.send_message("Nay. Thou lackest dashboard authority.", ephemeral=True)
            return
        await interaction.response.send_modal(IgnoredDropModal("remove"))

    @discord.ui.button(label="Restart / Update", emoji="🔄", style=discord.ButtonStyle.primary)
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            "🔄 Restart requested. If the host is running the auto-update launcher, it will pull GitHub updates and relaunch.",
            ephemeral=True,
        )
        log_event(f"Admin dashboard restart used by {interaction.user} ({interaction.user.id}).")
        asyncio.create_task(_restart_bot_after_response())


@client.tree.command(name="admindashboard", description="Staff only: open the Kill Bot administrator dashboard.")
async def admindashboard(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message("Nay. Only Ice Marshalls and Emperor Penguins may open the admin dashboard.", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_admin_dashboard_embed(interaction), view=AdminDashboardView(), ephemeral=True)
    log_event(f"/admindashboard opened by {interaction.user} ({interaction.user.id}).")


@client.tree.command(name="ignoredrop", description="Staff only: add, remove, or list drops that should not broadcast.")
@app_commands.describe(action="add/remove/list", item="Drop or item name, e.g. shield left half")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
])
async def ignoredrop(interaction: discord.Interaction, action: app_commands.Choice[str], item: Optional[str] = None):
    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message("Nay. Only Ice Marshalls and Emperor Penguins may manage ignored drops.", ephemeral=True)
        return

    if action.value == "list":
        custom = sorted(CUSTOM_LOW_VALUE_DROP_ITEMS)
        built_in_count = len(LOW_VALUE_DROP_ITEMS)
        if not custom:
            await interaction.response.send_message(
                f"📋 No custom ignored drops. Built-in ignored drop list contains **{built_in_count}** item(s).",
                ephemeral=True,
            )
            return
        shown = custom[:80]
        more = len(custom) - len(shown)
        text = "\n".join(f"• {x}" for x in shown)
        if more > 0:
            text += f"\n…and {more} more."
        await interaction.response.send_message(
            f"📋 Built-in ignored drops: **{built_in_count}**\nCustom ignored drops:\n{text}",
            ephemeral=True,
        )
        return

    if not item:
        await interaction.response.send_message("Provide an item name for add/remove.", ephemeral=True)
        return

    clean = normalise_drop_item_name(item)
    if action.value == "add":
        CUSTOM_LOW_VALUE_DROP_ITEMS.add(clean)
        custom_low_value_save()
        log_event(f"/ignoredrop add used by {interaction.user}: {clean}")
        await interaction.response.send_message(f"✅ Added **{clean}** to custom ignored drops.", ephemeral=True)
        return

    if action.value == "remove":
        if clean in CUSTOM_LOW_VALUE_DROP_ITEMS:
            CUSTOM_LOW_VALUE_DROP_ITEMS.remove(clean)
            custom_low_value_save()
            log_event(f"/ignoredrop remove used by {interaction.user}: {clean}")
            await interaction.response.send_message(f"✅ Removed **{clean}** from custom ignored drops.", ephemeral=True)
        elif clean in LOW_VALUE_DROP_ITEMS:
            await interaction.response.send_message(
                f"**{clean}** is built into the code-level ignored drop list and cannot be removed with this command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(f"**{clean}** is not currently custom-ignored.", ephemeral=True)


@client.tree.command(name="ping", description="Check whether Kill Bot is online and responding.")
async def ping(interaction: discord.Interaction):
    started = now_unix_utc()
    await interaction.response.send_message("🏓 Pinging Kill Bot...", ephemeral=True)
    msg = await interaction.original_response()
    elapsed_ms = max(0, (now_unix_utc() - started) * 1000)
    websocket_ms = round(client.latency * 1000)
    await msg.edit(content=f"🏓 Pong! Kill Bot is online. WebSocket latency: **{websocket_ms}ms**. Response time: **~{elapsed_ms}ms**.")
    log_event(f"/ping used by {interaction.user}: websocket={websocket_ms}ms response~{elapsed_ms}ms")


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


@client.tree.command(name="uptime", description="Show how long Kill Bot has been online.")
async def uptime(interaction: discord.Interaction):
    uptime_seconds = now_unix_utc() - BOT_START_UNIX
    embed = discord.Embed(
        title="⏱️ Kill Bot Uptime",
        description=(
            f"Kill Bot has been online for **{format_uptime(uptime_seconds)}**.\n"
            f"Started: <t:{BOT_START_UNIX}:F> (<t:{BOT_START_UNIX}:R>)"
        ),
        color=discord.Color.green(),
    )
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    await interaction.response.send_message(embed=embed)
    log_event(f"/uptime used by {interaction.user}: {format_uptime(uptime_seconds)}")



async def _restart_bot_after_response(delay_seconds: int = 3):
    """Close the Discord client and exit Python.

    If PC2 is running via run_killbot_auto_update.bat, the batch file will then
    perform git pull and start Kill Bot again.
    """
    await asyncio.sleep(delay_seconds)
    log_event("/updatebot restarting Kill Bot process now. The launcher should git pull and restart it.")
    try:
        await client.close()
    finally:
        os._exit(0)


@client.tree.command(name="updatebot", description="Staff only: restart Kill Bot so the host launcher can pull GitHub updates.")
async def updatebot(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not can_manage_killbot(interaction.user):
        await interaction.response.send_message("Nay. Only an Ice Marshall or Emperor Penguin may update Kill Bot.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔄 Kill Bot Update Requested",
        description=(
            f"Update requested by {interaction.user.mention}.\n\n"
            "Kill Bot will restart in a few seconds. If the host is running via "
            "`run_killbot_auto_update.bat`, it will check GitHub, pull any updates, and come back online."
        ),
        color=discord.Color.gold(),
    )
    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    log_event(f"/updatebot used by {interaction.user} ({interaction.user.id}). Restart scheduled.")
    asyncio.create_task(_restart_bot_after_response())



# -----------------------------
# RuneScape Player Tools: /gains, /alog, /ge, /price, /wsid
# -----------------------------
def _skill_rows_from_snapshot(snapshot: Dict[str, Any]) -> List[Tuple[str, int, int]]:
    skills = snapshot.get("skills", {}) if isinstance(snapshot.get("skills"), dict) else {}
    rows: List[Tuple[str, int, int]] = []
    for skill_name, data in skills.items():
        try:
            rows.append((skill_name, int(data.get("level", 0) or 0), int(data.get("xp", 0) or 0)))
        except Exception:
            continue
    return rows


def build_gains_embed(member: discord.Member | discord.User, rsn: str, period: str, old_snapshot: Dict[str, Any], new_snapshot: Dict[str, Any], baseline_ts: int) -> discord.Embed:
    old_total = int(old_snapshot.get("totalxp", 0) or 0)
    new_total = int(new_snapshot.get("totalxp", 0) or 0)
    total_gain = max(0, new_total - old_total)

    old_skills = old_snapshot.get("skills", {}) if isinstance(old_snapshot.get("skills"), dict) else {}
    new_skills = new_snapshot.get("skills", {}) if isinstance(new_snapshot.get("skills"), dict) else {}
    skill_gains: List[Tuple[str, int, int, int]] = []
    for skill_name, new_data in new_skills.items():
        old_data = old_skills.get(skill_name, {})
        old_xp = int(old_data.get("xp", 0) or 0)
        new_xp = int(new_data.get("xp", 0) or 0)
        gain = new_xp - old_xp
        if gain > 0:
            skill_gains.append((skill_name, gain, int(new_data.get("level", 0) or 0), new_xp))

    skill_gains.sort(key=lambda row: row[1], reverse=True)
    title_period = period.lower().strip().capitalize()
    embed = discord.Embed(
        title=f"📈 {title_period} Gains — {rsn}",
        description=(
            f"**Player:** {member.mention}\n"
            f"**Tracked since:** <t:{baseline_ts}:R>\n"
            f"**Total XP gained:** **{total_gain:,}**"
        ),
        color=discord.Color.green(),
    )

    if skill_gains:
        embed.add_field(
            name="Top Skill Gains",
            value="\n".join(f"• **{name}** — +{gain:,} XP (Lvl {level})" for name, gain, level, _ in skill_gains[:10]),
            inline=False,
        )
    else:
        embed.add_field(name="Top Skill Gains", value="No XP gains recorded for this period yet.", inline=False)

    embed.set_footer(text="Gains are tracked from Kill Bot snapshots. Accuracy improves the longer the bot has been online.")
    return embed


async def ge_itemdb_search(item_name: str) -> Optional[Dict[str, Any]]:
    """Best-effort official RuneScape GE lookup.

    The official itemdb API does not provide a clean global name-search endpoint, so this
    scans catalogue pages by first letter and caches results. This is intentionally RS3-only
    for now; the command structure leaves room for OSRS providers later.
    """
    query = item_name.strip().lower()
    if not query:
        return None
    if query in GE_SEARCH_CACHE:
        return GE_SEARCH_CACHE[query]

    session = await get_http_session()
    alpha = urllib.parse.quote(query[0])
    best: Optional[Dict[str, Any]] = None

    # RS3 item categories are numbered; scanning all is slower but reliable enough for an on-demand command.
    for category in range(0, 42):
        for page in range(1, 8):
            url = f"https://secure.runescape.com/m=itemdb_rs/api/catalogue/items.json?category={category}&alpha={alpha}&page={page}"
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        break
                    data = await response.json(content_type=None)
            except Exception:
                break

            items = data.get("items") if isinstance(data, dict) else None
            if not items:
                break

            for item in items:
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                lname = name.lower()
                if lname == query:
                    GE_SEARCH_CACHE[query] = item
                    return item
                if query in lname and best is None:
                    best = item

    if best:
        GE_SEARCH_CACHE[query] = best
    return best


def _format_ge_price(value: Any) -> str:
    if value is None:
        return "Unknown"
    return str(value)


async def ge_detail_from_id(item_id: int) -> Optional[Dict[str, Any]]:
    session = await get_http_session()
    url = f"https://secure.runescape.com/m=itemdb_rs/api/catalogue/detail.json?item={item_id}"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
    except Exception:
        return None
    item = data.get("item") if isinstance(data, dict) else None
    return item if isinstance(item, dict) else None


WSID_TASKS = [
    {"name": "Complete a Treasure Trail", "min_total": 0, "skills": {}, "activity_contains": ["treasure trail"], "tip": "Grab a clue scroll and complete one. Bonus penguin points for dyes."},
    {"name": "Complete a Quest", "min_total": 0, "skills": {}, "activity_contains": ["quest complete:"], "tip": "Pick one unfinished quest and knock it out."},
    {"name": "Train Archaeology", "min_total": 0, "skills": {"Archaeology": 5}, "activity_contains": ["archaeology", "dig site", "digsite", "mystery"], "tip": "Restore artefacts, complete a collection, or make progress at a dig site."},
    {"name": "Kill Queen Black Dragon", "min_total": 1500, "skills": {"Defence": 70, "Ranged": 70}, "activity_contains": ["queen black dragon"], "tip": "A classic bossing task. Bring antifires and dignity."},
    {"name": "Attempt Fight Kiln", "min_total": 1800, "skills": {"Constitution": 80}, "activity_contains": ["fight kiln", "har'aken"], "tip": "Push for a cape upgrade or just prove the lava floor is optional."},
    {"name": "Do a Barrows Run", "min_total": 1000, "skills": {"Magic": 50}, "activity_contains": ["barrows"], "tip": "One quick Barrows run. Blame tunnels, not Kyle."},
    {"name": "Push a 99/110/120 Milestone", "min_total": 1500, "skills": {}, "activity_contains": ["levelled", "xp in"], "tip": "Choose your closest skill milestone and make visible progress."},
]


def _activity_texts(profile: Dict[str, Any]) -> List[str]:
    activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []
    return [str(a.get("text", "")).lower() for a in activities if isinstance(a, dict)]


def _skill_level_from_profile(profile: Dict[str, Any], skill_name: str) -> int:
    snapshot = profile_snapshot(profile)
    skills = snapshot.get("skills", {}) if isinstance(snapshot.get("skills"), dict) else {}
    return int(skills.get(skill_name, {}).get("level", 0) or 0)


def choose_wsid_task(profile: Dict[str, Any], exclude_names: Optional[set[str]] = None) -> Optional[Dict[str, Any]]:
    exclude_names = exclude_names or set()
    total = int(profile.get("totalskill", 0) or 0)
    activity_texts = _activity_texts(profile)
    candidates = []
    for task in WSID_TASKS:
        if task["name"] in exclude_names:
            continue
        if total < int(task.get("min_total", 0)):
            continue
        if any(_skill_level_from_profile(profile, skill) < level for skill, level in task.get("skills", {}).items()):
            continue
        # Recent Adventurer Log only gives recent activity, not true completion state. Avoid recommending
        # something that appears recently completed, but keep tasks available if not seen recently.
        if any(any(marker in text for text in activity_texts) for marker in task.get("activity_contains", [])):
            continue
        candidates.append(task)
    if not candidates:
        candidates = [task for task in WSID_TASKS if task["name"] not in exclude_names]
    return random.choice(candidates) if candidates else None


class WsidView(discord.ui.View):
    def __init__(self, user_id: int, rsn: str, profile: Dict[str, Any], rejected: Optional[set[str]] = None):
        super().__init__(timeout=60 * 10)
        self.user_id = user_id
        self.rsn = rsn
        self.profile = profile
        self.rejected = rejected or set()
        self.task = choose_wsid_task(profile, self.rejected)

    def build_embed(self) -> discord.Embed:
        if not self.task:
            return discord.Embed(title="🎲 What Should I Do?", description="I could not find a suitable task right now.", color=discord.Color.red())
        embed = discord.Embed(
            title="🎲 What Should I Do?",
            description=f"**Suggested task for {self.rsn}:**\n{self.task['name']}\n\n{self.task['tip']}",
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Based on public RuneMetrics data and recent Adventurer Log entries. Full account completion may not be exposed by the API.")
        return embed

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This task is not yours, noble penguin.", ephemeral=True)
            return
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(content="Task accepted. May the drops be purple and Kyle be elsewhere.", embed=self.build_embed(), view=self)

    @discord.ui.button(label="Reroll", style=discord.ButtonStyle.secondary, emoji="🎲")
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This reroll is not yours, sneaky flipper.", ephemeral=True)
            return
        if self.task:
            self.rejected.add(self.task["name"])
        self.task = choose_wsid_task(self.profile, self.rejected)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


@client.tree.command(name="gains", description="Show tracked RuneScape XP gains for a registered player.")
@app_commands.describe(period="day, week, month, or year", user="Optional registered Discord user")
@app_commands.choices(period=[
    app_commands.Choice(name="day", value="day"),
    app_commands.Choice(name="week", value="week"),
    app_commands.Choice(name="month", value="month"),
    app_commands.Choice(name="year", value="year"),
])
async def gains(interaction: discord.Interaction, period: app_commands.Choice[str], user: Optional[discord.Member] = None):
    target = user or interaction.user
    rsn = RSN_REGISTRATIONS.get(target.id)
    if not rsn:
        await interaction.response.send_message("That user has no registered RSN. Use `/rsregister` or ask staff to use `/rsassign`.", ephemeral=True)
        return
    seconds = period_to_seconds(period.value)
    if not seconds:
        await interaction.response.send_message("Choose day, week, month, or year.", ephemeral=True)
        return
    await interaction.response.defer()
    profile = await fetch_runemetrics_profile(rsn)
    if not profile:
        await interaction.followup.send(f"I could not read public RuneMetrics data for **{rsn}**.")
        return
    current = profile_snapshot(profile)
    record_rsn_history_snapshot(target.id, current)
    baseline = get_history_baseline(target.id, seconds)
    if not baseline:
        await interaction.followup.send("No gains history exists yet. Kill Bot will start building it from now.", ephemeral=True)
        return
    embed = build_gains_embed(target, rsn, period.value, baseline.get("snapshot", {}), current, int(baseline.get("ts", now_unix_utc())))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="gainz", description="Alias for /gains because penguins respect the z.")
@app_commands.describe(period="day, week, month, or year", user="Optional registered Discord user")
@app_commands.choices(period=[
    app_commands.Choice(name="day", value="day"),
    app_commands.Choice(name="week", value="week"),
    app_commands.Choice(name="month", value="month"),
    app_commands.Choice(name="year", value="year"),
])
async def gainz(interaction: discord.Interaction, period: app_commands.Choice[str], user: Optional[discord.Member] = None):
    await gains.callback(interaction, period, user)  # type: ignore


@client.tree.command(name="alog", description="Show a RuneScape player's recent Adventurer Log.")
@app_commands.describe(rsn="Optional RSN. Defaults to your registered RSN.")
async def alog(interaction: discord.Interaction, rsn: Optional[str] = None):
    target_rsn = rsn or RSN_REGISTRATIONS.get(interaction.user.id)
    if not target_rsn:
        await interaction.response.send_message("Provide an RSN or register one first with `/rsregister`.", ephemeral=True)
        return
    await interaction.response.defer()
    profile = await fetch_runemetrics_profile(target_rsn)
    if not profile:
        await interaction.followup.send(f"Could not read public Adventurer Log data for **{target_rsn}**.", ephemeral=True)
        return
    name = profile.get("name", target_rsn)
    activities = profile.get("activities") if isinstance(profile.get("activities"), list) else []
    embed = discord.Embed(title=f"📖 Adventurer Log — {name}", color=discord.Color.blurple())
    if activities:
        lines = []
        for activity in activities[:10]:
            text = str(activity.get("text", "")).strip()
            date = str(activity.get("date", "")).strip()
            if text:
                lines.append(f"• {text}" + (f" — *{date}*" if date else ""))
        embed.description = "\n".join(lines) if lines else "No recent public Adventurer Log entries found."
    else:
        embed.description = "No recent public Adventurer Log entries found."
    embed.set_footer(text="Data from public RuneMetrics. Private profiles may not show.")
    await interaction.followup.send(embed=embed)


async def send_ge_price(interaction: discord.Interaction, item: str):
    await interaction.response.defer()
    found = await ge_itemdb_search(item)
    if not found:
        await interaction.followup.send(f"I could not find **{item}** on the RS3 Grand Exchange catalogue.", ephemeral=True)
        return
    item_id = int(found.get("id", 0) or 0)
    detail = await ge_detail_from_id(item_id) if item_id else None
    data = detail or found
    name = data.get("name", item)
    embed = discord.Embed(
        title=f"💰 GE Price — {name}",
        description=data.get("description", "RS3 Grand Exchange item."),
        color=discord.Color.gold(),
        url=f"https://secure.runescape.com/m=itemdb_rs/objects?id={item_id}" if item_id else None,
    )
    current = data.get("current", {}) if isinstance(data.get("current"), dict) else {}
    today = data.get("today", {}) if isinstance(data.get("today"), dict) else {}
    embed.add_field(name="Current", value=_format_ge_price(current.get("price")), inline=True)
    embed.add_field(name="Today", value=f"{today.get('trend', 'unknown')} {_format_ge_price(today.get('price'))}", inline=True)
    if data.get("icon_large"):
        embed.set_thumbnail(url=str(data.get("icon_large")))
    embed.set_footer(text="Source: official RuneScape Grand Exchange itemdb. RS3 only for now.")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="ge", description="Check an RS3 Grand Exchange price.")
@app_commands.describe(item="Item name, e.g. Praesul codex")
async def ge(interaction: discord.Interaction, item: str):
    await send_ge_price(interaction, item)


@client.tree.command(name="price", description="Alias for /ge.")
@app_commands.describe(item="Item name, e.g. Praesul codex")
async def price(interaction: discord.Interaction, item: str):
    await send_ge_price(interaction, item)


@client.tree.command(name="wsid", description="Suggest a RuneScape task you can do based on your public profile.")
@app_commands.describe(rsn="Optional RSN. Defaults to your registered RSN.")
async def wsid(interaction: discord.Interaction, rsn: Optional[str] = None):
    target_rsn = rsn or RSN_REGISTRATIONS.get(interaction.user.id)
    if not target_rsn:
        await interaction.response.send_message("Provide an RSN or register one first with `/rsregister`.", ephemeral=True)
        return
    await interaction.response.defer()
    profile = await fetch_runemetrics_profile(target_rsn)
    if not profile:
        await interaction.followup.send(f"Could not read public RuneMetrics data for **{target_rsn}**.", ephemeral=True)
        return
    view = WsidView(interaction.user.id, str(profile.get("name", target_rsn)), profile)
    await interaction.followup.send(embed=view.build_embed(), view=view)

# -----------------------------
# /rslookup
# -----------------------------
def _format_int(value, default: str = "Unknown") -> str:
    try:
        if value is None or value == "":
            return default
        return f"{int(value):,}"
    except Exception:
        return str(value) if value is not None else default


def _rs_profile_url(rsn: str) -> str:
    return f"https://apps.runescape.com/runemetrics/app/overview/player/{urllib.parse.quote(rsn)}"


def _skills_from_profile(profile: Dict) -> List[tuple[str, int, int]]:
    skills: List[tuple[str, int, int]] = []
    skillvalues = profile.get("skillvalues")
    if not isinstance(skillvalues, list):
        return skills

    for item in skillvalues:
        if not isinstance(item, dict):
            continue
        try:
            skill_id = int(item.get("id"))
            skill_name = SKILL_ID_TO_NAME.get(skill_id, f"Skill {skill_id}")
            level = int(item.get("level", 0) or 0)
            xp = int(item.get("xp", 0) or 0)
            skills.append((skill_name, level, xp))
        except Exception:
            continue
    return skills


def _skill_column_lines(skills: List[tuple[str, int, int]], start: int, end: int) -> str:
    chunk = skills[start:end]
    if not chunk:
        return "*No data*"
    return "\n".join(f"**{name}:** {level} — {xp:,} XP" for name, level, xp in chunk)


@client.tree.command(name="rslookup", description="Look up a RuneScape player's public RuneMetrics profile.")
@app_commands.describe(rsn="RuneScape display name to look up")
async def rslookup(interaction: discord.Interaction, rsn: str):
    log_event(f"/rslookup used by {interaction.user}: {rsn}")
    await interaction.response.defer()

    profile = await fetch_runemetrics_profile(rsn)
    if not profile:
        await interaction.followup.send(
            f"I could not read public RuneMetrics data for **{rsn}**. The profile may be private, unavailable, or the name may be incorrect.",
            ephemeral=True,
        )
        return

    display_name = str(profile.get("name") or rsn).strip()
    combat = profile.get("combatlevel", "Unknown")
    total_level = profile.get("totalskill", "Unknown")
    total_xp = _format_int(profile.get("totalxp", 0))
    quests_complete = profile.get("questscomplete")
    quest_points = profile.get("questpoints") or profile.get("quest_points")

    description_bits = [
        "A public RuneMetrics snapshot from the Adventurer's Log.",
        f"[Open RuneMetrics profile]({_rs_profile_url(display_name)})",
    ]

    embed = discord.Embed(
        title=f"📜 RuneScape Profile — {display_name}",
        description="\n".join(description_bits),
        url=_rs_profile_url(display_name),
        color=discord.Color.gold(),
    )

    embed.add_field(name="⚔️ Combat", value=str(combat), inline=True)
    embed.add_field(name="📊 Total Level", value=str(total_level), inline=True)
    embed.add_field(name="✨ Total XP", value=total_xp, inline=True)

    quest_value_parts = []
    if quests_complete not in (None, ""):
        quest_value_parts.append(f"Completed: **{_format_int(quests_complete)}**")
    if quest_points not in (None, ""):
        quest_value_parts.append(f"Quest points: **{_format_int(quest_points)}**")
    embed.add_field(
        name="📚 Quests",
        value="\n".join(quest_value_parts) if quest_value_parts else "Not exposed by public RuneMetrics",
        inline=True,
    )

    skills = _skills_from_profile(profile)
    if skills:
        skills_by_level = sorted(skills, key=lambda item: (item[1], item[2]), reverse=True)
        top_5 = skills_by_level[:5]
        embed.add_field(
            name="🏆 Top Skills",
            value="\n".join(f"**{name}** — {level} ({xp:,} XP)" for name, level, xp in top_5),
            inline=False,
        )

        skills_alpha = sorted(skills, key=lambda item: item[0])
        embed.add_field(name="📘 Skills A-H", value=_skill_column_lines(skills_alpha, 0, 10), inline=True)
        embed.add_field(name="📗 Skills I-R", value=_skill_column_lines(skills_alpha, 10, 20), inline=True)
        embed.add_field(name="📙 Skills S-Z", value=_skill_column_lines(skills_alpha, 20, 30), inline=True)

    activities = profile.get("activities")
    if isinstance(activities, list) and activities:
        recent_lines = []
        for activity in activities[:6]:
            if not isinstance(activity, dict):
                continue
            text = str(activity.get("text", "")).strip()
            date = str(activity.get("date", "")).strip()
            if not text:
                continue
            line = f"• {text}"
            if date:
                line += f" — *{date}*"
            if len(line) > 190:
                line = line[:187].rstrip() + "..."
            recent_lines.append(line)
        if recent_lines:
            embed.add_field(name="📖 Recent Adventurer Log", value="\n".join(recent_lines), inline=False)

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.set_footer(text="Data from public RuneMetrics. Private profiles may not show all data.")

    await interaction.followup.send(embed=embed)


# -----------------------------
# /lookup
# -----------------------------
async def fetch_rs_wiki_summary(term: str) -> Optional[Dict[str, str]]:
    session = await get_http_session()
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts|pageimages|info",
        "exintro": "1",
        "explaintext": "1",
        "redirects": "1",
        "inprop": "url",
        "pithumbsize": "300",
        "titles": term,
    }
    url = "https://runescape.wiki/api.php?" + urllib.parse.urlencode(params)
    try:
        async with session.get(url, headers={"User-Agent": "KillBot Discord Bot (Discord community lookup)"}) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
    except Exception as e:
        log_event(f"RS Wiki lookup failed for '{term}': {e}")
        return None

    pages = data.get("query", {}).get("pages", {}) if isinstance(data, dict) else {}
    if not pages:
        return None
    page = next(iter(pages.values()))
    if "missing" in page:
        return None
    return {
        "title": str(page.get("title", term)),
        "extract": str(page.get("extract", "No summary found.")),
        "url": str(page.get("fullurl", "https://runescape.wiki/")),
        "thumb": str(page.get("thumbnail", {}).get("source", "")) if isinstance(page.get("thumbnail"), dict) else "",
    }


@client.tree.command(name="lookup", description="Look up a RuneScape Wiki term and show a short summary.")
@app_commands.describe(term="The RuneScape Wiki term to search for")
async def lookup(interaction: discord.Interaction, term: str):
    await interaction.response.defer()
    result = await fetch_rs_wiki_summary(term)
    if not result:
        await interaction.followup.send(f"I could not find **{term}** on the RuneScape Wiki.", ephemeral=True)
        return

    extract = result["extract"].strip()
    if len(extract) > 900:
        extract = extract[:897].rstrip() + "..."

    embed = discord.Embed(
        title=f"📚 {result['title']}",
        description=extract or "No summary found.",
        url=result["url"],
        color=discord.Color.blurple(),
    )
    if result.get("thumb"):
        embed.set_thumbnail(url=result["thumb"])
    embed.set_footer(text="Source: RuneScape Wiki")
    await interaction.followup.send(embed=embed)
    log_event(f"/lookup used by {interaction.user}: {term}")


# -----------------------------
# /kbcommands (updated)
# -----------------------------
@client.tree.command(name="kbcommands", description="Show Kill Bot commands and how to use them.")
async def kbcommands(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Kill Bot Commands",
        description="Here be the scroll of incantations. Use these commands as shown:",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="🎲 /bosspick",
        value="Pick from multiple bosses → roll → confirm → role board.\n**Type:** `/bosspick`",
        inline=False,
    )

    embed.add_field(
        name="⚔️ /pvmtonight",
        value=(
            "Post a PVM availability poll. Users click privately and select every boss they are willing to do.\n"
            "**Type:** `/pvmtonight`"
        ),
        inline=False,
    )

    embed.add_field(
        name="📯 /gotime",
        value=(
            "Collate `/pvmtonight` responses, ignore bosses where interested players exceed max group size, "
            "pick from the most selected eligible options, then post a role signup sheet.\n"
            "**Type:** `/gotime`"
        ),
        inline=False,
    )

    embed.add_field(
        name="✍️ /updaterole (Staff)",
        value=(
            "Assign/remove roles for others on the latest GoTime signup board in the channel.\n"
            "**Type:** `/updaterole user:@Someone role:\"Green 2\" action:add/remove`"
        ),
        inline=False,
    )

    embed.add_field(
        name="⏰ /remindme",
        value=(
            "Set a reminder for a number of days. Kill Bot will tag you and link back to the original reminder message.\n"
            "**Type:** `/remindme days:<number>`\n"
            "**Example:** `/remindme days:1`"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 /poll",
        value=(
            "Create a generic reaction poll for any question.\n"
            "**Type:** `/poll question:<question> options:<option 1>, <option 2>, <option 3>`\n"
            "**Example:** `/poll question:What should our GIM team name be? options:GIM Noobs, Ultimate Ironmeme, The 5 Legends`"
        ),
        inline=False,
    )

    embed.add_field(
        name="🛠️ /admindashboard",
        value=(
            "Staff only: open the interactive Kill Bot admin dashboard.\n"
            "**Type:** `/admindashboard`"
        ),
        inline=False,
    )

    embed.add_field(
        name="🚫 /ignoredrop",
        value=(
            "Staff only: add/remove/list drops that should not broadcast.\n"
            '**Type:** `/ignoredrop action:add item:"shield left half"`'
        ),
        inline=False,
    )

    embed.add_field(
        name="🏓 /ping",
        value="Check that Kill Bot is online and see latency.\n**Type:** `/ping`",
        inline=False,
    )

    embed.add_field(
        name="⏱️ /uptime",
        value="Show how long Kill Bot has been online since the last restart.\n**Type:** `/uptime`",
        inline=False,
    )

    embed.add_field(
        name="🔄 /updatebot",
        value=(
            "Staff only: restart Kill Bot so the host launcher can pull the latest GitHub version.\n"
            "**Type:** `/updatebot`\n"
            "**Requires:** PC2 must be running via `run_killbot_auto_update.bat`."
        ),
        inline=False,
    )

    embed.add_field(
        name="⚔️ /pk",
        value="Show scoreboard or record a 1v1 PK.\n**Show:** `/pk`\n**Record:** `/pk winner:@Winner loser:@Loser`",
        inline=False,
    )

    embed.add_field(
        name="🐧 /teampenguin",
        value="Show Team Penguin, or Josh can add/remove/clear.\n**Type:** `/teampenguin action:show/add/remove/clear`",
        inline=False,
    )

    embed.add_field(
        name="📜 /blamekyle",
        value="Generate an official KGP-certified report proving Kyle is responsible.\n**Type:** `/blamekyle` (optional `boss:`)",
        inline=False,
    )

    embed.add_field(
        name="📜 /blameuser",
        value="Blame any selected user with official KGP authority.\n**Type:** `/blameuser user:@Someone reason:<optional>`",
        inline=False,
    )

    embed.add_field(
        name="🏅 /rank",
        value="Show your activity points and earnable rank progress.\n**Type:** `/rank` or `/rank user:@Someone`",
        inline=False,
    )

    embed.add_field(
        name="🏆 /rankboard",
        value="Show the activity points leaderboard.\n**Type:** `/rankboard`",
        inline=False,
    )

    embed.add_field(
        name="🛠️ /rankadmin (Staff)",
        value="Add, remove, set, or sync activity points.\n**Type:** `/rankadmin user:@Someone action:add/remove/set/sync amount:<number>`",
        inline=False,
    )



    embed.add_field(
        name="📜 /rsregister",
        value=(
            "Register your RuneScape in-game name for RuneMetrics achievement tracking.\n"
            "**Type:** `/rsregister rsn:<your RSN>`"
        ),
        inline=False,
    )

    embed.add_field(
        name="🧾 /rsassign (Staff)",
        value=(
            "Ice Marshall/Emperor Penguin: assign a RuneScape name to another Discord member for tracking.\n"
            "**Type:** `/rsassign user:@Someone rsn:<their RSN>`"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 /rsregistered",
        value="Show the saved Discord ↔ RSN tracking list. Stored in `rsn_tracking.json`.\n**Type:** `/rsregistered`",
        inline=False,
    )

    embed.add_field(
        name="🔎 /rschecknow",
        value="Force-check your registered RuneScape profile now.\n**Type:** `/rschecknow`",
        inline=False,
    )

    embed.add_field(
        name="🧹 /rsunregister",
        value="Remove your registered RuneScape name.\n**Type:** `/rsunregister`",
        inline=False,
    )

    embed.add_field(
        name="📯 /announce",
        value=(
            "Make Kill Bot post a staff announcement. Restricted to Moderator, Ice Marshall, Emperor Penguin, roles.\n"
            "**Type:** `/announce message:<text>`\n"
            "**Example:** `/announce message:Kyle is a noob`"
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 /gains or /gainz",
        value="Show tracked XP gains for a registered player.\n**Type:** `/gains period:day/week/month/year`",
        inline=False,
    )

    embed.add_field(
        name="📖 /alog",
        value="Show a player’s recent public Adventurer Log.\n**Type:** `/alog rsn:<optional>`",
        inline=False,
    )

    embed.add_field(
        name="💰 /ge or /price",
        value="Check an RS3 Grand Exchange price from the official itemdb.\n**Type:** `/ge item:<item name>`",
        inline=False,
    )

    embed.add_field(
        name="🎲 /wsid",
        value="Suggest something to do based on public RuneMetrics data, with Accept/Reroll buttons.\n**Type:** `/wsid`",
        inline=False,
    )

    embed.add_field(
        name="📚 /lookup",
        value="Look up a RuneScape Wiki term and show a short summary.\n**Type:** `/lookup term:<search>`",
        inline=False,
    )

    embed.add_field(
        name="📚 /kbcommands",
        value="Show this command list.\n**Type:** `/kbcommands`",
        inline=False,
    )

    thumb = bot_thumbnail_url(interaction)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.set_footer(text="If a command doesn’t appear, check channel permission: Use Application Commands.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    log_event("Starting Kill bot...")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing. Put it in your .env file.")
    client.run(TOKEN)
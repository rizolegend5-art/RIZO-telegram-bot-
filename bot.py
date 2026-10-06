import asyncio
import logging
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, ReplyKeyboardRemove
)

# ===================== CONFIGURATION =====================
BOT_TOKEN = "8742797786:AAEAZ5K9ME6r9woxAMOpnVcellI3VmXtWPg"
ADMIN_IDS = {7638086103}
DB_PATH = "bot.db"
FILES_DIR = Path("files")
FILES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("referral-bot")
bot_router = Router()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            referrals INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            blocked INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reward_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            file_name TEXT,
            required_referrals INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS force_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            title TEXT NOT NULL,
            invite_link TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS premium_access (
            user_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            granted_by INTEGER NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (user_id, file_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS redeem_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            referral_bonus INTEGER NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 0,
            used_count INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS redeem_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code_id INTEGER NOT NULL,
            redeemed_at TEXT NOT NULL,
            UNIQUE(user_id, code_id)
        );
        """)
        for admin_id in ADMIN_IDS:
            con.execute("INSERT OR IGNORE INTO admins(user_id, added_at) VALUES (?, ?)", (admin_id, now()))
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('support_username', 'admin')")
        con.commit()


def is_admin(user_id: int) -> bool:
    with closing(db()) as con:
        return con.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def save_user(message: Message, referred_by: Optional[int] = None) -> None:
    user = message.from_user
    if not user:
        return
    with closing(db()) as con:
        existing = con.execute("SELECT id FROM users WHERE id=?", (user.id,)).fetchone()
        if existing:
            con.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?",
                        (user.username, user.first_name, now(), user.id))
        else:
            valid_referrer = referred_by if referred_by and referred_by != user.id and con.execute(
                "SELECT id FROM users WHERE id=?", (referred_by,)).fetchone() else None
            con.execute("INSERT INTO users(id, username, first_name, referred_by, joined_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                        (user.id, user.username, user.first_name, valid_referrer, now(), now()))
            if valid_referrer:
                con.execute("INSERT OR IGNORE INTO referrals(inviter_id, invited_id, created_at) VALUES (?, ?, ?)",
                            (valid_referrer, user.id, now()))
                if con.execute("SELECT changes()").fetchone()[0]:
                    con.execute("UPDATE users SET referrals=referrals+1 WHERE id=?", (valid_referrer,))
        con.commit()


def user_stats(user_id: int) -> sqlite3.Row:
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


# ===================== KEYBOARDS =====================

def user_menu_kb(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💎 VIP Get Files", callback_data="user:vip", style="success")],
        [
            InlineKeyboardButton(text="🔗 Refer & Earn", callback_data="user:refer", style="success"),
            InlineKeyboardButton(text="🎀 Redeem Code", callback_data="user:redeem", style="success")
        ],
        [
            InlineKeyboardButton(text="📊 My Stats", callback_data="user:stats", style="primary"),
            InlineKeyboardButton(text="🏆 Leaderboard", callback_data="user:leaderboard", style="primary")
        ],
        [InlineKeyboardButton(text="🎧 Premium Support", callback_data="user:support", style="success")]
    ]
    # Admin access is also an INLINE button. No ReplyKeyboard is used.
    if is_admin_user:
        rows.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="adm:home", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ File Add", callback_data="adm:addfile", style="success"),
            InlineKeyboardButton(text="🗑 File Delete", callback_data="adm:deletefile", style="danger")
        ],
        [
            InlineKeyboardButton(text="✏️ Edit Referrals", callback_data="adm:editfile", style="primary"),
            InlineKeyboardButton(text="📁 Files", callback_data="adm:files", style="primary")
        ],
        [
            InlineKeyboardButton(text="📢 Force Join", callback_data="adm:channels", style="primary"),
            InlineKeyboardButton(text="📊 Statistics", callback_data="adm:stats", style="primary")
        ],
        [
            InlineKeyboardButton(text="🎁 Grant Premium", callback_data="adm:grantaccess", style="success"),
            InlineKeyboardButton(text="👥 Users", callback_data="adm:users", style="primary")
        ],
        [
            InlineKeyboardButton(text="💬 Set Support", callback_data="adm:setsupport", style="success"),
            InlineKeyboardButton(text="🎟 Redeem Codes", callback_data="adm:codes", style="success")
        ],
        [
            InlineKeyboardButton(text="➕ Add Admin", callback_data="adm:addadmin", style="success"),
            InlineKeyboardButton(text="➖ Remove Admin", callback_data="adm:removeadmin", style="danger")
        ],
        [
            InlineKeyboardButton(text="🚫 Block User", callback_data="adm:block", style="danger"),
            InlineKeyboardButton(text="✅ Unblock User", callback_data="adm:unblock", style="success")
        ],
        [
            InlineKeyboardButton(text="🔄 Refresh", callback_data="adm:home", style="primary"),
            InlineKeyboardButton(text="⬅️ Back to User", callback_data="adm:backuser", style="primary")
        ],
    ])


def file_delete_kb(files: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 Delete #{item['id']} — {item['title'][:32]}", callback_data=f"adm:removefile:{item['id']}", style="danger")]
        for item in files
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="adm:home", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def file_edit_kb(files: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"✏️ {item['title'][:35]} — {item['required_referrals']} refs", callback_data=f"adm:editref:{item['id']}", style="primary")]
        for item in files
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="adm:home", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_files_kb(files: list[sqlite3.Row], referrals: int) -> InlineKeyboardMarkup:
    rows = []
    for item in files:
        unlocked = referrals >= item["required_referrals"] or bool(item["granted"])
        label = f"📄 {item['title']}"
        if not unlocked:
            label = f"🔒 {item['title']} ({item['required_referrals']} refs)"
        callback = f"user:claim:{item['id']}" if unlocked else "user:locked"
        style = "primary" if unlocked else "danger"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback, style=style)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_grant_files_kb(files: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🎁 {item['title'][:35]}", callback_data=f"adm:grantfile:{item['id']}", style="success")] for item in files]
    rows.append([InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="adm:home", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channels_manage_kb(channels: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    """Force Join manage view: list channels with delete + add new"""
    rows = []
    for ch in channels:
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {ch['title'][:30]}",
                callback_data=f"adm:delchannel:{ch['id']}",
                style="danger"
            )
        ])
    rows.append([InlineKeyboardButton(text="➕ Add New Channel", callback_data="adm:addchannel", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="adm:home", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def codes_manage_kb(codes: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    """Redeem codes manage view"""
    rows = []
    for c in codes:
        status = "🟢" if c["enabled"] else "🔴"
        uses = "∞" if c["max_uses"] == 0 else f"{c['used_count']}/{c['max_uses']}"
        rows.append([
            InlineKeyboardButton(
                text=f"{status} {c['code']} | +{c['referral_bonus']} refs | {uses}",
                callback_data=f"adm:codeinfo:{c['id']}",
                style="primary"
            )
        ])
    rows.append([InlineKeyboardButton(text="➕ Create New Code", callback_data="adm:createcode", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="adm:home", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def code_detail_kb(code_id: int, enabled: int) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Disable" if enabled else "🟢 Enable"
    toggle_style = "danger" if enabled else "success"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"adm:togglecode:{code_id}", style=toggle_style)],
        [InlineKeyboardButton(text="🗑 Delete Code", callback_data=f"adm:deletecode:{code_id}", style="danger")],
        [InlineKeyboardButton(text="⬅️ Back to Codes", callback_data="adm:codes", style="primary")]
    ])


def join_kb(channels: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        if ch["invite_link"]:
            rows.append([InlineKeyboardButton(text=f"📢 Join {ch['title']}", url=ch["invite_link"], style="primary")])
    rows.append([InlineKeyboardButton(text="✅ I Joined — Check", callback_data="check_join", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def resolve_chat_id_from_link(link: str) -> Optional[str]:
    """Try to get chat_id from a public t.me link. Returns @username or None."""
    if not link:
        return None
    link = link.strip().rstrip("/")
    if link.startswith("https://t.me/") and not link.startswith("https://t.me/+"):
        username = link.replace("https://t.me/", "")
        if username and not username.startswith("+") and "/" not in username:
            return f"@{username}"
    return None


async def missing_channels(user_id: int) -> list[sqlite3.Row]:
    with closing(db()) as con:
        channels = con.execute("SELECT * FROM force_channels WHERE enabled=1").fetchall()
    missing = []
    for ch in channels:
        chat_id = ch["chat_id"]
        # If no valid chat_id, skip verification but still ask user to join
        if not chat_id or chat_id.startswith("http"):
            missing.append(ch)
            continue
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(ch)
        except Exception as exc:
            log.warning("Could not check %s: %s", chat_id, exc)
            # If we can't check, don't block the user
    return missing


async def enforce_join(message: Message) -> bool:
    current = user_stats(message.from_user.id)
    if current and current["blocked"]:
        await message.answer("🚫 Your account is blocked. Contact the bot administrator.")
        return False
    missing = await missing_channels(message.from_user.id)
    if missing:
        await message.answer("🔒 <b>Join required channels first</b>\n\nJoin all channels below, then tap Check.", reply_markup=join_kb(missing))
        return False
    return True


def referral_link(user_id: int, username: str) -> str:
    return f"https://t.me/{username}?start=ref_{user_id}"


# ===================== STATES =====================
class AdminStates(StatesGroup):
    add_file_caption = State()
    add_file_document = State()
    add_file_referrals = State()
    edit_file = State()
    grant_user = State()
    grant_file = State()
    add_channel = State()
    add_admin = State()
    remove_admin = State()
    block_user = State()
    unblock_user = State()
    set_support = State()
    create_code = State()


class UserStates(StatesGroup):
    redeem_code = State()


# ===================== USER HANDLERS =====================

@bot_router.message(CommandStart())
async def start(message: Message) -> None:
    payload = (message.text or "").split(maxsplit=1)[1] if len((message.text or "").split()) > 1 else ""
    referrer = int(payload[4:]) if payload.startswith("ref_") and payload[4:].isdigit() else None
    save_user(message, referrer)
    # Remove any old Reply Keyboard from previous versions of the bot.
    # All menus in this version use InlineKeyboardMarkup.
    await message.answer("\u2063", reply_markup=ReplyKeyboardRemove())
    if not await enforce_join(message):
        return
    stats = user_stats(message.from_user.id)
    await message.answer(
        f"👋 <b>WELCOME {message.from_user.first_name}</b>\n\n"
        "🎁 Get premium files by inviting friends!\n\n"
        f"📊 <b>Your Stats:</b>\n├ Referrals: <b>{stats['referrals']}</b>\n└ Status: 🔓 Unlocked\n\n"
        "👇 Use buttons below:", reply_markup=user_menu_kb(is_admin(message.from_user.id)))


@bot_router.callback_query(F.data == "check_join")
async def check_join(call: CallbackQuery) -> None:
    if await enforce_join(call.message):
        await call.message.edit_text("✅ Join verified. Send /start to open the bot.")
    await call.answer()


@bot_router.callback_query(F.data == "user:stats")
async def status(call: CallbackQuery) -> None:
    if not await enforce_join(call.message): return
    s = user_stats(call.from_user.id)
    await call.message.answer(f"📊 <b>Your Status</b>\n\n👤 ID: <code>{s['id']}</code>\n👥 Referrals: <b>{s['referrals']}</b>\n🚦 Account: {'Blocked' if s['blocked'] else 'Active'}")
    await call.answer()


@bot_router.callback_query(F.data == "user:refer")
async def share(call: CallbackQuery, bot: Bot) -> None:
    if not await enforce_join(call.message): return
    me = await bot.get_me()
    if not me.username:
        await call.message.answer("❌ Bot username not available.")
        await call.answer()
        return
    stats = user_stats(call.from_user.id)
    await call.message.answer(
        f"🔗 <b>Your referral link</b>\n\n"
        f"<code>{referral_link(call.from_user.id, me.username)}</code>\n\n"
        "Invite a new user through this link. The referral is counted only when that user starts the bot for the first time.\n\n"
        f"👥 Your referrals: <b>{stats['referrals']}</b>\n"
        "🎁 Use <b>VIP Get Files</b> after reaching the required referral count."
    )
    await call.answer()


@bot_router.callback_query(F.data == "user:vip")
async def claim(call: CallbackQuery) -> None:
    if not await enforce_join(call.message): return
    s = user_stats(call.from_user.id)
    with closing(db()) as con:
        files = con.execute("""
            SELECT r.*, CASE WHEN p.user_id IS NULL THEN 0 ELSE 1 END AS granted
            FROM reward_files r LEFT JOIN premium_access p
              ON p.file_id=r.id AND p.user_id=?
            WHERE r.enabled=1 ORDER BY r.id DESC
        """, (call.from_user.id,)).fetchall()
    if not files:
        await call.message.answer("📭 No files are available right now.")
        await call.answer()
        return
    await call.message.answer(
        f"🎁 <b>Select a file</b>\nYour referrals: <b>{s['referrals']}</b>\n\n"
        "Unlocked files are clickable. Locked files show the referral count needed.",
        reply_markup=user_files_kb(files, s["referrals"])
    )
    await call.answer()


@bot_router.callback_query(F.data.startswith("user:"))
async def user_file_action(call: CallbackQuery, state: FSMContext) -> None:
    current = user_stats(call.from_user.id)
    if current and current["blocked"]:
        await call.answer("Your account is blocked.", show_alert=True)
        return
    missing = await missing_channels(call.from_user.id)
    if missing:
        await call.message.answer("🔒 Join the required channels first, then try the file button again.", reply_markup=join_kb(missing))
        await call.answer()
        return

    action = call.data.split(":", 1)[1]
    if action == "locked":
        await call.answer("Complete the required referrals first.", show_alert=True)
        return
    if action == "leaderboard":
        with closing(db()) as con:
            rows = con.execute("SELECT first_name, username, referrals FROM users ORDER BY referrals DESC LIMIT 10").fetchall()
        text = "🏆 <b>Top Referrers</b>\n\n" + "\n".join(
            f"{i}. {r['first_name'] or r['username'] or r['id']} — {r['referrals']} referrals"
            for i, r in enumerate(rows, 1)
        )
        await call.message.answer(text or "No referrals yet.")
        await call.answer()
        return
    if action == "support":
        with closing(db()) as con:
            row = con.execute("SELECT value FROM settings WHERE key='support_username'").fetchone()
        if row and row["value"]:
            support_username = row["value"]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Contact Support", url=f"https://t.me/{support_username}", style="success")]
            ])
            await call.message.answer(f"🎧 <b>Premium Support</b>\n\nClick the button below to chat with our support team.", reply_markup=kb)
        else:
            await call.message.answer("🎧 Premium support is not set up by the admin yet.")
        await call.answer()
        return
    if action == "redeem":
        await state.set_state(UserStates.redeem_code)
        await call.message.answer("🎀 <b>Redeem Code</b>\n\nPlease send your redeem code below 👇")
        await call.answer()
        return
    if not action.startswith("claim:"):
        await call.answer()
        return
    try:
        file_id = int(action.split(":", 1)[1])
    except ValueError:
        await call.answer("Invalid file", show_alert=True)
        return
    with closing(db()) as con:
        item = con.execute("""
            SELECT r.*, CASE WHEN p.user_id IS NULL THEN 0 ELSE 1 END AS granted
            FROM reward_files r LEFT JOIN premium_access p
              ON p.file_id=r.id AND p.user_id=?
            WHERE r.id=? AND r.enabled=1
        """, (call.from_user.id, file_id)).fetchone()
    if not item:
        await call.answer("File not found or disabled.", show_alert=True)
        return
    stats = user_stats(call.from_user.id)
    if stats["referrals"] < item["required_referrals"] and not item["granted"]:
        await call.answer(f"You need {item['required_referrals']} referrals.", show_alert=True)
        return
    try:
        await call.message.answer_document(item["file_id"], caption=f"🎁 <b>{item['title']}</b>\n📄 {item['file_name'] or 'document'}")
        await call.answer("File sent ✅")
    except Exception:
        log.exception("Failed to send file %s", file_id)
        await call.answer("Could not send the file.", show_alert=True)


# ===================== USER REDEEM CODE HANDLER =====================
@bot_router.message(UserStates.redeem_code, F.text)
async def user_redeem(message: Message, state: FSMContext) -> None:
    code_text = (message.text or "").strip()
    if not code_text:
        await message.answer("❌ Please send a valid code.")
        return
    with closing(db()) as con:
        code_row = con.execute(
            "SELECT * FROM redeem_codes WHERE code=? AND enabled=1",
            (code_text,)
        ).fetchone()
        if not code_row:
            await state.clear()
            await message.answer("❌ Invalid or disabled redeem code.", reply_markup=user_menu_kb())
            return
        # Check if user already used it
        already = con.execute(
            "SELECT 1 FROM redeem_logs WHERE user_id=? AND code_id=?",
            (message.from_user.id, code_row["id"])
        ).fetchone()
        if already:
            await state.clear()
            await message.answer("⚠️ You have already used this code.", reply_markup=user_menu_kb())
            return
        # Check max uses
        if code_row["max_uses"] > 0 and code_row["used_count"] >= code_row["max_uses"]:
            await state.clear()
            await message.answer("⚠️ This code has reached its maximum usage limit.", reply_markup=user_menu_kb())
            return
        # Apply bonus
        con.execute("UPDATE users SET referrals = referrals + ? WHERE id=?",
                    (code_row["referral_bonus"], message.from_user.id))
        con.execute("INSERT INTO redeem_logs(user_id, code_id, redeemed_at) VALUES (?,?,?)",
                    (message.from_user.id, code_row["id"], now()))
        con.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE id=?", (code_row["id"],))
        con.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>Redeem Successful!</b>\n\n"
        f"🎁 Bonus: <b>+{code_row['referral_bonus']} referrals</b> added to your account.\n\n"
        "Use <b>💎 VIP Get Files</b> to claim your reward.",
        reply_markup=user_menu_kb()
    )


# ===================== ADMIN HANDLERS =====================

@bot_router.message(Command("admin"))
async def admin(message: Message) -> None:
    if not is_admin(message.from_user.id): return
    await message.answer("🛠 <b>Admin Panel</b>", reply_markup=admin_kb())


@bot_router.callback_query(F.data.startswith("adm:"))
async def admin_actions(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized", show_alert=True); return
    action = call.data.split(":", 1)[1]

    if action == "backuser":
        stats = user_stats(call.from_user.id)
        if not stats:
            save_user(call.message)
            stats = user_stats(call.from_user.id)
        await call.message.edit_text(
            f"👋 <b>WELCOME {call.from_user.first_name}</b>\\n\\n"
            "🎁 Get premium files by inviting friends!\\n\\n"
            f"📊 <b>Your Stats:</b>\\n├ Referrals: <b>{stats['referrals']}</b>\\n└ Status: "
            f"{'🚫 Blocked' if stats['blocked'] else '🔓 Unlocked'}\\n\\n"
            "👇 Use buttons below:",
            reply_markup=user_menu_kb(True)
        )

    elif action == "home":
        await call.message.edit_text("🛠 <b>Admin Panel</b>", reply_markup=admin_kb())

    elif action.startswith("removefile:"):
        try:
            file_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid file", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT title FROM reward_files WHERE id=?", (file_id,)).fetchone()
            if not row:
                await call.answer("File not found", show_alert=True); return
            con.execute("DELETE FROM reward_files WHERE id=?", (file_id,))
            con.commit()
        await call.message.edit_text(f"✅ Deleted referral file: <b>{row['title']}</b>", reply_markup=admin_kb())

    elif action.startswith("editref:"):
        try:
            file_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid file", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT id, title, required_referrals FROM reward_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            await call.answer("File not found", show_alert=True); return
        await state.update_data(edit_file_id=file_id)
        await state.set_state(AdminStates.edit_file)
        await call.message.edit_text(
            f"✏️ <b>{row['title']}</b>\nCurrent: <b>{row['required_referrals']} referrals</b>\n\n"
            "Send the new referral number, e.g. <code>5</code>"
        )

    elif action == "stats":
        with closing(db()) as con:
            u = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            f = con.execute("SELECT COUNT(*) c FROM reward_files WHERE enabled=1").fetchone()["c"]
            c = con.execute("SELECT COUNT(*) c FROM force_channels WHERE enabled=1").fetchone()["c"]
            r = con.execute("SELECT COUNT(*) c FROM referrals").fetchone()["c"]
        await call.message.edit_text(
            f"📈 <b>Stats</b>\nUsers: {u}\nReferrals: {r}\nActive files: {f}\nForce-join: {c}",
            reply_markup=admin_kb()
        )

    elif action == "users":
        with closing(db()) as con:
            rows = con.execute("SELECT id, first_name, username, referrals, blocked FROM users ORDER BY last_seen DESC LIMIT 20").fetchall()
        text = "👥 <b>Latest users</b>\n\n" + "\n".join(
            f"<code>{r['id']}</code> {r['first_name'] or ''} @{r['username'] or '-'} | ref:{r['referrals']} | {'blocked' if r['blocked'] else 'active'}"
            for r in rows
        )
        await call.message.edit_text(text or "No users.", reply_markup=admin_kb())

    elif action == "files":
        with closing(db()) as con:
            rows = con.execute("SELECT id,title,required_referrals,enabled FROM reward_files ORDER BY id DESC").fetchall()
        await call.message.edit_text(
            "📁 <b>Referral Files</b>\n\n" +
            ("\n".join(f"#{r['id']} {r['title']} — {r['required_referrals']} refs — {'active' if r['enabled'] else 'off'}" for r in rows) or "No files added yet."),
            reply_markup=file_delete_kb(rows) if rows else admin_kb()
        )

    # ===== FORCE JOIN / CHANNELS (SIMPLIFIED) =====
    elif action == "channels":
        with closing(db()) as con:
            rows = con.execute("SELECT * FROM force_channels ORDER BY id DESC").fetchall()
        text = "📢 <b>Force Join Channels</b>\n\n"
        if rows:
            text += "\n".join(f"• <b>{r['title']}</b>\n  🔗 {r['invite_link']}" for r in rows)
        else:
            text += "No channels added yet.\n\nClick below to add one."
        await call.message.edit_text(text, reply_markup=channels_manage_kb(rows))

    elif action == "addchannel":
        await state.set_state(AdminStates.add_channel)
        await call.message.edit_text(
            "📢 <b>Add Force Join Channel</b>\n\n"
            "Send in this format:\n"
            "<code>Title | Invite Link</code>\n\n"
            "Example:\n"
            "<code>My Channel | https://t.me/+abcdefg</code>"
        )

    elif action.startswith("delchannel:"):
        try:
            ch_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT title FROM force_channels WHERE id=?", (ch_id,)).fetchone()
            con.execute("DELETE FROM force_channels WHERE id=?", (ch_id,))
            con.commit()
        await call.answer(f"Deleted {row['title'] if row else ''}", show_alert=True)
        with closing(db()) as con:
            rows = con.execute("SELECT * FROM force_channels ORDER BY id DESC").fetchall()
        text = "📢 <b>Force Join Channels</b>\n\n"
        if rows:
            text += "\n".join(f"• <b>{r['title']}</b>\n  🔗 {r['invite_link']}" for r in rows)
        else:
            text += "No channels added yet."
        await call.message.edit_text(text, reply_markup=channels_manage_kb(rows))

    # ===== REDEEM CODES MANAGEMENT =====
    elif action == "codes":
        with closing(db()) as con:
            rows = con.execute("SELECT * FROM redeem_codes ORDER BY id DESC").fetchall()
        text = "🎟 <b>Redeem Codes</b>\n\n"
        if rows:
            text += "\n".join(
                f"• <code>{r['code']}</code> → +{r['referral_bonus']} refs | "
                f"uses: {r['used_count']}/{'∞' if r['max_uses']==0 else r['max_uses']} | "
                f"{'🟢' if r['enabled'] else '🔴'}"
                for r in rows
            )
        else:
            text += "No codes created yet.\n\nClick below to create one."
        await call.message.edit_text(text, reply_markup=codes_manage_kb(rows))

    elif action == "createcode":
        await state.set_state(AdminStates.create_code)
        await call.message.edit_text(
            "🎟 <b>Create Redeem Code</b>\n\n"
            "Send in this format:\n"
            "<code>CODE | BONUS_REFERRALS | MAX_USES</code>\n\n"
            "Example:\n"
            "<code>WELCOME100 | 100 | 0</code>\n\n"
            "• BONUS_REFERRALS = number of referrals added to user\n"
            "• MAX_USES = 0 means unlimited"
        )

    elif action.startswith("codeinfo:"):
        try:
            code_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT * FROM redeem_codes WHERE id=?", (code_id,)).fetchone()
        if not row:
            await call.answer("Code not found", show_alert=True); return
        uses = "∞" if row["max_uses"] == 0 else f"{row['used_count']}/{row['max_uses']}"
        await call.message.edit_text(
            f"🎟 <b>Code Details</b>\n\n"
            f"Code: <code>{row['code']}</code>\n"
            f"Bonus: <b>+{row['referral_bonus']} referrals</b>\n"
            f"Uses: <b>{uses}</b>\n"
            f"Status: {'🟢 Active' if row['enabled'] else '🔴 Disabled'}\n"
            f"Created: {row['created_at']}",
            reply_markup=code_detail_kb(row["id"], row["enabled"])
        )

    elif action.startswith("togglecode:"):
        try:
            code_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT enabled FROM redeem_codes WHERE id=?", (code_id,)).fetchone()
            if not row:
                await call.answer("Code not found", show_alert=True); return
            new_val = 0 if row["enabled"] else 1
            con.execute("UPDATE redeem_codes SET enabled=? WHERE id=?", (new_val, code_id))
            con.commit()
        await call.answer("✅ Updated")
        with closing(db()) as con:
            rows = con.execute("SELECT * FROM redeem_codes ORDER BY id DESC").fetchall()
        text = "🎟 <b>Redeem Codes</b>\n\n" + "\n".join(
            f"• <code>{r['code']}</code> → +{r['referral_bonus']} refs | "
            f"uses: {r['used_count']}/{'∞' if r['max_uses']==0 else r['max_uses']} | "
            f"{'🟢' if r['enabled'] else '🔴'}"
            for r in rows
        )
        await call.message.edit_text(text, reply_markup=codes_manage_kb(rows))

    elif action.startswith("deletecode:"):
        try:
            code_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT code FROM redeem_codes WHERE id=?", (code_id,)).fetchone()
            con.execute("DELETE FROM redeem_codes WHERE id=?", (code_id,))
            con.commit()
        await call.answer(f"Deleted {row['code'] if row else ''}", show_alert=True)
        with closing(db()) as con:
            rows = con.execute("SELECT * FROM redeem_codes ORDER BY id DESC").fetchall()
        text = "🎟 <b>Redeem Codes</b>\n\n"
        text += "\n".join(
            f"• <code>{r['code']}</code> → +{r['referral_bonus']} refs | "
            f"uses: {r['used_count']}/{'∞' if r['max_uses']==0 else r['max_uses']} | "
            f"{'🟢' if r['enabled'] else '🔴'}"
            for r in rows
        ) if rows else "No codes created yet."
        await call.message.edit_text(text, reply_markup=codes_manage_kb(rows))

    elif action == "addfile":
        await state.set_state(AdminStates.add_file_caption)
        await call.message.edit_text("1️⃣ Send the file caption/name first.\nExample: <code>TG BOT HOSTING SRC ✅</code>")

    elif action == "deletefile":
        with closing(db()) as con:
            rows = con.execute("SELECT id,title,required_referrals FROM reward_files WHERE enabled=1 ORDER BY id DESC").fetchall()
        if not rows:
            await call.message.edit_text("📭 No active referral files to delete.", reply_markup=admin_kb())
        else:
            await call.message.edit_text("🗑 <b>Select the file to delete:</b>", reply_markup=file_delete_kb(rows))

    elif action == "editfile":
        with closing(db()) as con:
            rows = con.execute("SELECT id,title,required_referrals FROM reward_files ORDER BY id DESC").fetchall()
        if not rows:
            await call.message.edit_text("📭 No referral files added yet.", reply_markup=admin_kb())
        else:
            await call.message.edit_text("✏️ <b>Select a file to change its referral requirement:</b>", reply_markup=file_edit_kb(rows))

    elif action == "grantaccess":
        await state.set_state(AdminStates.grant_user)
        await call.message.edit_text("🎁 Send the user's numeric Telegram ID. This user will receive premium access without referrals.")

    elif action.startswith("grantfile:"):
        try:
            file_id = int(action.split(":", 1)[1])
        except ValueError:
            await call.answer("Invalid file", show_alert=True); return
        data = await state.get_data()
        user_id = data.get("grant_user_id")
        if not user_id:
            await call.answer("User ID is missing", show_alert=True); return
        with closing(db()) as con:
            row = con.execute("SELECT title FROM reward_files WHERE id=? AND enabled=1", (file_id,)).fetchone()
            if not row:
                await call.answer("File not found or disabled", show_alert=True); return
            con.execute("INSERT OR REPLACE INTO premium_access(user_id,file_id,granted_by,granted_at) VALUES (?,?,?,?)",
                        (user_id, file_id, call.from_user.id, now()))
            con.commit()
        await state.clear()
        await call.message.edit_text(f"✅ Premium access granted.\nUser: <code>{user_id}</code>\nFile: <b>{row['title']}</b>", reply_markup=admin_kb())

    elif action == "setsupport":
        await state.set_state(AdminStates.set_support)
        await call.message.edit_text("💬 Send the Telegram username for Premium Support (e.g., <code>YourUsername</code>):")

    elif action == "addadmin":
        await state.set_state(AdminStates.add_admin)
        await call.message.edit_text("Send the Telegram numeric user ID to promote.")
    elif action == "removeadmin":
        await state.set_state(AdminStates.remove_admin)
        await call.message.edit_text("Send the numeric Telegram ID to remove. The last admin cannot be removed.")
    elif action in {"block", "unblock"}:
        await state.set_state(AdminStates.block_user if action == "block" else AdminStates.unblock_user)
        await call.message.edit_text("Send the user's numeric Telegram ID.")
    await call.answer()


# ===================== STATE HANDLERS =====================

@bot_router.message(AdminStates.set_support, F.text)
async def set_support_username(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    username = message.text.strip()
    if username.startswith("@"):
        username = username[1:]
    if not username:
        await message.answer("❌ Username cannot be empty. Send again.")
        return
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('support_username', ?)", (username,))
        con.commit()
    await state.clear()
    await message.answer(f"✅ Premium support username set to: <b>@{username}</b>", reply_markup=admin_kb())


@bot_router.message(AdminStates.add_file_caption, F.text)
async def add_file_caption(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ Caption/name cannot be empty. Send the file name again.")
        return
    await state.update_data(file_title=title)
    await state.set_state(AdminStates.add_file_document)
    await message.answer("2️⃣ Now send the document/file.")


@bot_router.message(AdminStates.add_file_document, F.document)
async def add_file_document(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    await state.update_data(file_id=message.document.file_id, file_name=message.document.file_name or "document")
    await state.set_state(AdminStates.add_file_referrals)
    await message.answer("3️⃣ Now send the required referral number.\nExample: <code>5</code>")


@bot_router.message(AdminStates.add_file_referrals, F.text)
async def add_file_referrals(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    try:
        required = int((message.text or "").strip())
        if required < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Send only a valid referral number, for example: <code>5</code>")
        return
    data = await state.get_data()
    with closing(db()) as con:
        con.execute("INSERT INTO reward_files(title,file_id,file_name,required_referrals,created_at) VALUES (?,?,?,?,?)",
                    (data["file_title"], data["file_id"], data["file_name"], required, now()))
        con.commit()
    await state.clear()
    await message.answer(
        f"✅ Referral file added: <b>{data['file_title']}</b>\n"
        f"📄 File name: <code>{data['file_name']}</code>\n"
        f"🔓 Required referrals: <b>{required}</b>",
        reply_markup=admin_kb()
    )


@bot_router.message(AdminStates.edit_file)
async def edit_file(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    try:
        required = int((message.text or "").strip())
        if required < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Send a valid referral number, for example: <code>5</code>")
        return
    data = await state.get_data()
    file_id = data.get("edit_file_id")
    with closing(db()) as con:
        row = con.execute("SELECT title FROM reward_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            await state.clear()
            await message.answer("❌ File not found.", reply_markup=admin_kb())
            return
        con.execute("UPDATE reward_files SET required_referrals=? WHERE id=?", (required, file_id))
        con.commit()
    await state.clear()
    await message.answer(
        f"✅ Updated <b>{row['title']}</b> → <b>{required} referrals</b>",
        reply_markup=admin_kb()
    )


@bot_router.message(AdminStates.grant_user, F.text)
async def grant_user(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    try:
        user_id = int((message.text or "").strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Send a valid numeric Telegram user ID.")
        return
    with closing(db()) as con:
        files = con.execute("SELECT id,title FROM reward_files WHERE enabled=1 ORDER BY id DESC").fetchall()
    if not files:
        await state.clear()
        await message.answer("📭 No active files available.", reply_markup=admin_kb())
        return
    await state.update_data(grant_user_id=user_id)
    await state.set_state(AdminStates.grant_file)
    await message.answer(f"Select the file to grant to user <code>{user_id}</code>:", reply_markup=admin_grant_files_kb(files))


# ===== SIMPLIFIED ADD CHANNEL (TITLE + LINK only) =====
@bot_router.message(AdminStates.add_channel)
async def add_channel(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    text = (message.text or "").strip()
    parts = [p.strip() for p in text.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer(
            "❌ Invalid format. Send like this:\n"
            "<code>My Channel | https://t.me/+abcdefg</code>"
        )
        return
    title, link = parts[0], parts[1]
    # Try to resolve chat_id automatically
    chat_id = resolve_chat_id_from_link(link)
    if not chat_id:
        # Try bot.get_chat() with the link (works sometimes for public channels)
        try:
            chat = await bot.get_chat(link)
            chat_id = str(chat.id)
        except Exception as exc:
            log.warning("Could not resolve chat_id from link %s: %s", link, exc)
            chat_id = None
    with closing(db()) as con:
        con.execute(
            "INSERT INTO force_channels(chat_id,title,invite_link,created_at) VALUES (?,?,?,?)",
            (chat_id, title, link, now())
        )
        con.commit()
    await state.clear()
    note = "✅ Channel added. Bot will auto-verify joins." if chat_id else \
           "✅ Channel added.\n⚠️ Could not auto-verify (bot is not admin or link is private). Users will still see the join button."
    await message.answer(note, reply_markup=admin_kb())


# ===== CREATE REDEEM CODE =====
@bot_router.message(AdminStates.create_code)
async def create_code(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    text = (message.text or "").strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        await message.answer(
            "❌ Invalid format. Send like this:\n"
            "<code>CODE | BONUS_REFERRALS | MAX_USES</code>\n"
            "Example: <code>WELCOME100 | 100 | 0</code>"
        )
        return
    code = parts[0].upper()
    try:
        bonus = int(parts[1])
        max_uses = int(parts[2])
        if bonus < 0 or max_uses < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ BONUS and MAX_USES must be non-negative integers.")
        return
    with closing(db()) as con:
        exists = con.execute("SELECT 1 FROM redeem_codes WHERE code=?", (code,)).fetchone()
        if exists:
            await message.answer("❌ This code already exists. Try a different code.")
            return
        con.execute(
            "INSERT INTO redeem_codes(code, referral_bonus, max_uses, created_at) VALUES (?,?,?,?)",
            (code, bonus, max_uses, now())
        )
        con.commit()
    await state.clear()
    uses_text = "unlimited" if max_uses == 0 else f"{max_uses} uses"
    await message.answer(
        f"✅ <b>Redeem Code Created</b>\n\n"
        f"🎟 Code: <code>{code}</code>\n"
        f"🎁 Bonus: <b>+{bonus} referrals</b>\n"
        f"👥 Max Uses: <b>{uses_text}</b>",
        reply_markup=admin_kb()
    )


@bot_router.message(AdminStates.add_admin)
async def add_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    try: user_id = int((message.text or "").strip())
    except ValueError: await message.answer("Send a numeric Telegram ID."); return
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO admins(user_id,added_at) VALUES (?,?)", (user_id, now()))
        con.commit()
    await state.clear()
    await message.answer(f"✅ {user_id} is now an admin.", reply_markup=admin_kb())


@bot_router.message(AdminStates.remove_admin)
async def remove_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    try: user_id = int((message.text or "").strip())
    except ValueError: await message.answer("Send a numeric Telegram ID."); return
    if user_id == message.from_user.id:
        await message.answer("⚠️ You cannot remove yourself."); return
    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"]
        if total <= 1:
            await message.answer("⚠️ At least one admin must remain."); return
        con.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        changed = con.execute("SELECT changes()").fetchone()[0]
        con.commit()
    await state.clear()
    await message.answer(f"{'✅ Admin removed' if changed else 'ℹ️ ID was not an admin'}: {user_id}", reply_markup=admin_kb())


async def set_block(message: Message, state: FSMContext, blocked: int) -> None:
    if not is_admin(message.from_user.id): return
    try: uid = int((message.text or "").strip())
    except ValueError: await message.answer("Send a numeric Telegram ID."); return
    with closing(db()) as con:
        con.execute("UPDATE users SET blocked=? WHERE id=?", (blocked, uid))
        con.commit()
    await state.clear()
    await message.answer(f"✅ User {uid} {'blocked' if blocked else 'unblocked'}.", reply_markup=admin_kb())


@bot_router.message(AdminStates.block_user)
async def block(message: Message, state: FSMContext):
    await set_block(message, state, 1)


@bot_router.message(AdminStates.unblock_user)
async def unblock(message: Message, state: FSMContext):
    await set_block(message, state, 0)


@bot_router.message(Command("delchannel"))
async def del_channel(message: Message) -> None:
    if not is_admin(message.from_user.id): return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) != 2 or not arg[1].isdigit():
        await message.answer("Usage: /delchannel ID"); return
    with closing(db()) as con:
        con.execute("UPDATE force_channels SET enabled=0 WHERE id=?", (int(arg[1]),))
        con.commit()
    await message.answer("✅ Channel disabled.")


async def main() -> None:
    init_db()
    global bot
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(bot_router)
    log.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
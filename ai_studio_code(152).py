import os
import re
import sys
import html
import io
import asyncio
import sqlite3
import logging
from datetime import datetime

# ============================================================
# PYSIDE6 GUI IMPORTS
# ============================================================
from PySide6.QtCore import Qt, QThread, Signal, QObject, QSize
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QLineEdit,
    QTextEdit,
    QTextBrowser,
    QPushButton,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QCheckBox,
    QFileDialog,
    QMessageBox,
    QFrame,
    QDialog,
    QInputDialog,
)

# ============================================================
# TELETHON MTPROTO IMPORTS
# ============================================================
import httpx
from telethon import TelegramClient, events, Button
from telethon.errors import UserIsBlockedError, PeerIdInvalidError, FloodWaitError

# ============================================================
# CONFIGURATION (MTPROTO API + BOT TOKEN)
# ============================================================

API_ID = 21349867
API_HASH = "7ced3ee4c80117bd5138410811b91f9f"
BOT_TOKEN = os.getenv("BOT_TOKEN", "8939632946:AAFYeBygeMAGEnwzhVUNnAFRydgCJ03fb3s")
ADMIN_SECRET_KEY = "admin123"
DB_FILE = "support_bot.db"

BUSINESS_NAME = "Apex Global Business Hub"
DEFAULT_POSTER_URL = "https://images.unsplash.com/photo-1557804506-669a67965ba0?auto=format&fit=crop&w=1200&q=80"

DEFAULT_START_MESSAGE = (
    f"👋 Welcome to <b>{BUSINESS_NAME}</b>!\n\n"
    "🌟 <i>Your premium partner for business solutions and 24/7 client care.</i>\n\n"
    "<b>📞 Official Phone Numbers:</b>\n"
    "• 📞 Smart Hotline: <code>+855 87 654 321</code>\n"
    "• 📞 Cellcard VIP: <code>+855 12 345 678</code>\n"
    "• 📞 Office Line: <code>+855 23 999 888</code>\n\n"
    "<b>👨‍💼 Official Telegram Staff:</b>\n"
    "• Support Lead: @ApexSupportDesk\n"
    "• Sales Dept: @ApexSalesTeam\n\n"
    "💬 <i>You can send inquiries, photos, documents, or voice notes directly here!</i>"
)

# Live session tracker: admin_id -> target_customer_id
ACTIVE_ADMIN_SESSIONS = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("SupportDesk")


# ============================================================
# DATABASE ENGINE WITH AUTO-MIGRATIONS
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=25)
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            status TEXT DEFAULT 'waiting',
            last_message TEXT DEFAULT '',
            last_seen TEXT DEFAULT ''
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    cols = {col[1] for col in cur.fetchall()}
    if "status" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'waiting'")
    if "last_message" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_message TEXT DEFAULT ''")
    if "last_seen" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_seen TEXT DEFAULT ''")

    cur.execute("UPDATE users SET status='waiting' WHERE status IS NULL OR status=''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            sender_type TEXT,
            message TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            admin_id INTEGER PRIMARY KEY,
            claimed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_message_map (
            admin_id INTEGER,
            admin_msg_id INTEGER,
            customer_id INTEGER,
            created_at TEXT,
            PRIMARY KEY (admin_id, admin_msg_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_reply_enabled', '1')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('start_image_path', ?)", (DEFAULT_POSTER_URL,))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('start_text', ?)", (DEFAULT_START_MESSAGE,))

    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else default


def set_setting(key: str, value: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def save_user_info(user_id, username, first_name, last_name, initial_status="waiting"):
    conn = db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, status, last_message, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=excluded.last_seen
    """, (
        user_id,
        username or "",
        first_name or "",
        last_name or "",
        initial_status,
        "",
        now_str,
    ))
    conn.commit()
    conn.close()


def set_user_status(user_id: int, status: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
    conn.commit()
    conn.close()


def update_last_message(user_id: int, message: str, status: str = None):
    conn = db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if status:
        cur.execute("UPDATE users SET last_message=?, last_seen=?, status=? WHERE user_id=?",
                    (message[:250], now_str, status, user_id))
    else:
        cur.execute("UPDATE users SET last_message=?, last_seen=? WHERE user_id=?",
                    (message[:250], now_str, user_id))
    conn.commit()
    conn.close()


def save_message(user_id: int, sender_type: str, message: str):
    conn = db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO messages (user_id, sender_type, message, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, sender_type, message, now_str))
    conn.commit()
    conn.close()


def map_admin_message(admin_id: int, admin_msg_id: int, customer_id: int):
    conn = db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT OR REPLACE INTO admin_message_map (admin_id, admin_msg_id, customer_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (admin_id, admin_msg_id, customer_id, now_str))
    conn.commit()
    conn.close()


def get_customer_id_by_admin_msg(admin_id: int, admin_msg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT customer_id FROM admin_message_map WHERE admin_id=? AND admin_msg_id=?",
                (admin_id, admin_msg_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_all_admin_ids() -> set:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT admin_id FROM admins")
    rows = cur.fetchall()
    conn.close()
    return {r[0] for r in rows if r[0] != 123456789}


def add_admin(admin_id: int):
    conn = db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT OR IGNORE INTO admins (admin_id, claimed_at) VALUES (?, ?)", (admin_id, now_str))
    conn.commit()
    conn.close()


def remove_admin(admin_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE admin_id=?", (admin_id,))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in get_all_admin_ids()


def get_users_list(query: str = "", filter_status: str = "all"):
    conn = db()
    cur = conn.cursor()
    sql = "SELECT user_id, username, first_name, last_name, status, last_message, last_seen FROM users WHERE 1=1"
    params = []

    if filter_status in ("waiting", "handled"):
        sql += " AND status=?"
        params.append(filter_status)

    if query.strip():
        sql += " AND (CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR first_name LIKE ? OR last_name LIKE ?)"
        pattern = f"%{query.strip()}%"
        params.extend([pattern, pattern, pattern, pattern])

    sql += " ORDER BY last_seen DESC"
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_chat_history(user_id: int, limit: int = 200):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT sender_type, message, created_at
        FROM messages
        WHERE user_id=?
        ORDER BY id ASC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_stats_counts():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE status='waiting'")
    waiting = cur.fetchone()[0]
    conn.close()
    return total, waiting


# ============================================================
# CROSS-THREAD BRIDGE
# ============================================================

class BotBridge(QObject):
    user_message_received = Signal(dict)
    admin_replied_signal = Signal(dict)
    user_status_changed = Signal(int, str)
    operation_status_signal = Signal(str, bool)


bridge = BotBridge()


# ============================================================
# ROBUST VISUAL POSTER & MEDIA DISPATCHERS
# ============================================================

async def load_photo_bytes(photo_source: str) -> bytes:
    if photo_source.startswith("http://") or photo_source.startswith("https://"):
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(photo_source)
            resp.raise_for_status()
            return resp.content
    else:
        if not os.path.exists(photo_source):
            raise FileNotFoundError(f"File not found: {photo_source}")
        with open(photo_source, "rb") as f:
            return f.read()


async def send_visual_poster(client: TelegramClient, chat_id: int, photo_source, caption: str = "", buttons=None):
    """
    Delivers photo directly as an inline visual poster.
    Guaranteed: force_document=False prevents it from showing as a file.
    """
    if isinstance(photo_source, bytes):
        raw_bytes = photo_source
    else:
        raw_bytes = await load_photo_bytes(photo_source)

    img_stream = io.BytesIO(raw_bytes)
    img_stream.name = "poster.jpg"

    short_caption = caption[:1000] if caption else ""
    remainder_text = caption[1000:] if caption and len(caption) > 1000 else None

    try:
        sent_msg = await client.send_file(
            chat_id,
            img_stream,
            caption=short_caption,
            force_document=False,
            parse_mode="html",
            buttons=buttons
        )
    except Exception:
        img_stream.seek(0)
        sent_msg = await client.send_file(
            chat_id,
            img_stream,
            caption=short_caption,
            force_document=False,
            buttons=buttons
        )

    if remainder_text:
        try:
            await client.send_message(chat_id, remainder_text, parse_mode="html")
        except Exception:
            await client.send_message(chat_id, remainder_text)

    return sent_msg


async def send_native_voice(client: TelegramClient, chat_id: int, voice_path: str):
    """Delivers native Telegram voice notes with audio waveforms."""
    if not os.path.exists(voice_path):
        raise FileNotFoundError(f"File not found: {voice_path}")

    with open(voice_path, "rb") as f:
        audio_data = f.read()

    voice_stream = io.BytesIO(audio_data)
    voice_stream.name = "voice.ogg"

    await client.send_file(
        chat_id,
        voice_stream,
        voice_note=True
    )


def get_customer_main_menu():
    return [
        [
            Button.inline("🛍 Pricing & Packages", b"menu_pricing"),
            Button.inline("💳 Payment Methods", b"menu_payment"),
        ],
        [
            Button.inline("👨‍💼 Speak to Live Agent (Voice/Text)", b"menu_agent")
        ]
    ]


# ============================================================
# BOT WORKER THREAD (TELETHON MTPROTO ENGINE)
# ============================================================

class BotWorkerThread(QThread):
    def __init__(self):
        super().__init__()
        self.loop = None
        self.client = None
        self._is_running = True

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self.client = TelegramClient("support_bot_mtproto", API_ID, API_HASH)
        self.setup_handlers()

        try:
            self.loop.run_until_complete(self.run_bot())
        except Exception as e:
            logger.error("Bot loop halted: %s", e)
        finally:
            self.loop.close()

    def setup_handlers(self):
        # 1. /start command
        @self.client.on(events.NewMessage(pattern=r"^/start(?:\s+(.*))?$"))
        async def on_start(event):
            sender = await event.get_sender()
            user_id = event.sender_id
            username = sender.username or ""
            fname = sender.first_name or ""
            lname = sender.last_name or ""

            save_user_info(user_id, username, fname, lname, initial_status="handled")

            if is_admin(user_id):
                await event.respond(
                    f"👨‍💼 <b>Admin Workstation Active ({BUSINESS_NAME})</b>\n\n"
                    "<b>Ways to respond to customers:</b>\n"
                    "• Tap the <b>[💬 Reply to User]</b> button on ticket alerts.\n"
                    "• <b>Swipe / Quote-Reply</b> directly to tickets with <b>Text or Voice</b>.\n"
                    "• <code>/reply USER_ID message</code> — Send text reply\n"
                    "• <code>/resolve USER_ID</code> — Mark ticket handled\n"
                    "• <code>/addadmin USER_ID</code> — Authorize another admin\n"
                    "• <code>/admins</code> — List authorized admins\n"
                    "• <code>/broadcast message</code> — Dispatch broadcast\n"
                    "• Or reply live from the <b>Desktop Support Desk GUI</b>.",
                    parse_mode="html"
                )
                return

            start_text = get_setting("start_text", DEFAULT_START_MESSAGE)
            start_img = get_setting("start_image_path", DEFAULT_POSTER_URL)

            sent = False
            if start_img.strip():
                try:
                    await send_visual_poster(
                        client=self.client,
                        chat_id=user_id,
                        photo_source=start_img,
                        caption=start_text,
                        buttons=get_customer_main_menu()
                    )
                    sent = True
                except Exception as e:
                    logger.warning("Custom poster send failed: %s", e)

            if not sent:
                await event.respond(start_text, parse_mode="html", buttons=get_customer_main_menu())

        # 2. Cancel active reply mode command
        @self.client.on(events.NewMessage(pattern=r"^/cancel$"))
        async def on_cancel(event):
            if event.sender_id in ACTIVE_ADMIN_SESSIONS:
                target_uid = ACTIVE_ADMIN_SESSIONS.pop(event.sender_id)
                await event.respond(f"❌ Closed reply session with user <code>{target_uid}</code>.", parse_mode="html")
            else:
                await event.respond("No active reply session to cancel.")

        # 3. Admin authorization command
        @self.client.on(events.NewMessage(pattern=r"^/admin(?:\s+(.*))?$"))
        async def on_admin(event):
            user_id = event.sender_id
            if is_admin(user_id):
                await event.respond("✅ You are already authorized as an Admin.", parse_mode="html")
                return

            args = event.pattern_match.group(1)
            if not args:
                await event.respond("⚠️ Usage: <code>/admin YOUR_PASSWORD</code>", parse_mode="html")
                return

            if args.strip() == ADMIN_SECRET_KEY:
                add_admin(user_id)
                await event.respond(f"🎉 <b>Success!</b> Admin privileges granted to <code>{user_id}</code>.", parse_mode="html")
            else:
                await event.respond("❌ Incorrect authorization password.")

        # 4. Add admin command
        @self.client.on(events.NewMessage(pattern=r"^/addadmin(?:\s+(.*))?$"))
        async def on_addadmin(event):
            if not is_admin(event.sender_id):
                await event.respond("❌ Admin only.")
                return

            args = event.pattern_match.group(1)
            if not args or not args.strip().isdigit():
                await event.respond("Usage: <code>/addadmin USER_ID</code>", parse_mode="html")
                return

            new_aid = int(args.strip())
            add_admin(new_aid)
            await event.respond(f"✅ Added user <code>{new_aid}</code> as Admin.", parse_mode="html")

        # 5. List admins command
        @self.client.on(events.NewMessage(pattern=r"^/admins$"))
        async def on_listadmins(event):
            if not is_admin(event.sender_id):
                await event.respond("❌ Admin only.")
                return

            admins = get_all_admin_ids()
            txt = "🛡 <b>Authorized Admins:</b>\n\n"
            for aid in admins:
                txt += f"• <code>{aid}</code>\n"
            await event.respond(txt, parse_mode="html")

        # 6. /resolve command
        @self.client.on(events.NewMessage(pattern=r"^/resolve(?:\s+(.*))?$"))
        async def on_resolve(event):
            if not is_admin(event.sender_id):
                await event.respond("❌ Admin only.")
                return

            args = event.pattern_match.group(1)
            if not args or not args.strip().isdigit():
                await event.respond("Usage: <code>/resolve USER_ID</code>", parse_mode="html")
                return

            uid = int(args.strip())
            set_user_status(uid, "handled")
            bridge.user_status_changed.emit(uid, "handled")
            await event.respond(f"✅ Ticket for user <code>{uid}</code> marked <b>Resolved</b>.", parse_mode="html")

        # 7. /reply command
        @self.client.on(events.NewMessage(pattern=r"^/reply(?:\s+(.*))?$"))
        async def on_reply(event):
            if not is_admin(event.sender_id):
                await event.respond("❌ Admin only.")
                return

            args = event.pattern_match.group(1)
            reply_to = await event.get_reply_message()
            user_id = None
            msg_text = ""

            if reply_to and args:
                user_id = get_customer_id_by_admin_msg(event.sender_id, reply_to.id)
                msg_text = args.strip()
            elif args:
                parts = args.strip().split(maxsplit=1)
                if len(parts) == 2 and parts[0].isdigit():
                    user_id = int(parts[0])
                    msg_text = parts[1]

            if not user_id or not msg_text:
                await event.respond("Usage: <code>/reply USER_ID message</code> or use the 💬 Reply button.", parse_mode="html")
                return

            try:
                await self.client.send_message(user_id, msg_text, parse_mode="html")
                save_message(user_id, "ADMIN", msg_text)
                update_last_message(user_id, msg_text, status="handled")
                set_user_status(user_id, "handled")

                bridge.admin_replied_signal.emit({"user_id": user_id, "message": msg_text})
                bridge.user_status_changed.emit(user_id, "handled")
                await event.respond(f"✅ Delivered to <code>{user_id}</code>", parse_mode="html")
            except UserIsBlockedError:
                await event.respond("❌ Delivery failed: Customer has blocked the bot.")
            except Exception as e:
                await event.respond(f"❌ Failed: {e}")

        # 8. /broadcast command
        @self.client.on(events.NewMessage(pattern=r"^/broadcast(?:\s+(.*))?$"))
        async def on_broadcast(event):
            if not is_admin(event.sender_id):
                await event.respond("❌ Admin only.")
                return

            args = event.pattern_match.group(1)
            if not args:
                await event.respond("Usage: <code>/broadcast Your message</code>", parse_mode="html")
                return

            users = get_users_list(filter_status="all")
            sent, failed = 0, 0
            progress = await event.respond("📢 Starting broadcast...")

            for row in users:
                uid = row[0]
                try:
                    await self.client.send_message(uid, args, parse_mode="html")
                    save_message(uid, "ADMIN", args)
                    sent += 1
                    await asyncio.sleep(0.04)
                except Exception:
                    failed += 1

            await progress.edit(f"📢 Broadcast Done:\n✅ Delivered: {sent}\n❌ Failed: {failed}")

        # 9. Interactive Callback Queries
        @self.client.on(events.CallbackQuery)
        async def on_callback(event):
            data = event.data.decode("utf-8")
            admin_id = event.sender_id
            await event.answer()

            # Admin triggers interactive Reply Session
            if data.startswith("replyto_"):
                if not is_admin(admin_id):
                    return
                target_uid = int(data.split("_")[1])
                ACTIVE_ADMIN_SESSIONS[admin_id] = target_uid

                session_box = [
                    [Button.inline("❌ Cancel Reply Mode", b"cancel_reply_session")]
                ]
                await event.respond(
                    f"✍️ <b>Live Reply Mode Active:</b>\n"
                    f"You are now replying to customer <code>{target_uid}</code>.\n\n"
                    "<b>Send your message now:</b>\n"
                    "• 📝 Type any text\n"
                    "• 🎙 Record a voice message\n"
                    "• 🖼 Send a photo/poster\n"
                    "• 📁 Send a document\n\n"
                    "<i>It will be delivered immediately to their chat! (Send /cancel to exit)</i>",
                    parse_mode="html",
                    buttons=session_box
                )
                return

            # Cancel reply mode button
            if data == "cancel_reply_session":
                if admin_id in ACTIVE_ADMIN_SESSIONS:
                    target_uid = ACTIVE_ADMIN_SESSIONS.pop(admin_id)
                    await event.respond(f"❌ Exited reply mode for customer <code>{target_uid}</code>.", parse_mode="html")
                return

            # Admin marks ticket resolved
            if data.startswith("resolve_"):
                if not is_admin(admin_id):
                    return
                target_uid = int(data.split("_")[1])
                set_user_status(target_uid, "handled")
                bridge.user_status_changed.emit(target_uid, "handled")
                await event.edit(f"✅ Ticket for user <code>{target_uid}</code> marked RESOLVED.", parse_mode="html")
                return

            # Customer menus
            if data == "menu_pricing":
                text = (
                    "🛍 <b>Packages & Pricing:</b>\n\n"
                    "• Starter: $29/mo\n"
                    "• Business Pro: $89/mo\n"
                    "• Enterprise: Custom quote\n\n"
                    "Please describe your project or send a voice note for a fast quotation!"
                )
                await event.respond(text, parse_mode="html", buttons=get_customer_main_menu())
                save_message(admin_id, "BOT", text)
            elif data == "menu_payment":
                text = (
                    "💳 <b>Payment Methods:</b>\n\n"
                    "• ABA Bank / KHQR\n"
                    "• ACLEDA Bank Plc\n"
                    "• USDT (TRC-20)\n\n"
                    "Please send payment confirmation slips here after payment."
                )
                await event.respond(text, parse_mode="html", buttons=get_customer_main_menu())
                save_message(admin_id, "BOT", text)
            elif data == "menu_agent":
                text = (
                    "👨‍💼 <b>Live Representative Queue:</b>\n\n"
                    "Your ticket is now queued with our agent team.\n"
                    "You can type your message, record a <b>voice note</b>, or send photos below!"
                )
                set_user_status(admin_id, "waiting")
                bridge.user_status_changed.emit(admin_id, "waiting")
                await event.respond(text, parse_mode="html")
                save_message(admin_id, "BOT", text)

        # 10. Message Dispatcher: Active Reply Session, Swipe-Replies, and Customer Incoming
        @self.client.on(events.NewMessage)
        async def on_general_message(event):
            if not event.is_private or event.text.startswith("/"):
                return

            sender_id = event.sender_id

            # Case A: Admin in Active Reply Mode (from [💬 Reply to User] button)
            if is_admin(sender_id) and sender_id in ACTIVE_ADMIN_SESSIONS:
                customer_id = ACTIVE_ADMIN_SESSIONS[sender_id]
                try:
                    if event.voice or event.audio:
                        await self.client.send_file(customer_id, event.media, voice_note=True)
                        content = "[🎙 Voice Note Delivered]"
                    elif event.photo:
                        await self.client.send_file(
                            customer_id,
                            event.media,
                            caption=event.text,
                            force_document=False,
                            parse_mode="html"
                        )
                        content = f"[🖼 Poster Sent] {event.text or ''}".strip()
                    elif event.file:
                        await self.client.send_file(customer_id, event.media, caption=event.text)
                        content = f"[📁 Document Sent] {event.text or ''}".strip()
                    else:
                        await self.client.send_message(customer_id, event.text, parse_mode="html")
                        content = event.text

                    save_message(customer_id, "ADMIN", content)
                    update_last_message(customer_id, content, status="handled")
                    set_user_status(customer_id, "handled")

                    bridge.admin_replied_signal.emit({"user_id": customer_id, "message": content})
                    bridge.user_status_changed.emit(customer_id, "handled")

                    await event.respond(
                        f"✅ <b>Reply Delivered to <code>{customer_id}</code>!</b>\n"
                        "<i>You are still in reply mode. Send more messages or tap below to exit:</i>",
                        parse_mode="html",
                        buttons=[[Button.inline("❌ Exit Reply Mode", b"cancel_reply_session")]]
                    )

                except UserIsBlockedError:
                    await event.respond(f"❌ Customer <code>{customer_id}</code> blocked the bot.", parse_mode="html")
                    ACTIVE_ADMIN_SESSIONS.pop(sender_id, None)
                except Exception as e:
                    await event.respond(f"❌ Delivery error: {e}")
                return

            # Case B: Admin Swipe-Reply / Quote-Reply
            if is_admin(sender_id):
                if not event.is_reply:
                    return

                reply_to = await event.get_reply_message()
                customer_id = get_customer_id_by_admin_msg(sender_id, reply_to.id)
                if not customer_id:
                    match = re.search(r"Customer ID:\s*(\d+)", reply_to.text or "", re.IGNORECASE)
                    if match:
                        customer_id = int(match.group(1))

                if not customer_id:
                    return

                try:
                    if event.voice or event.audio:
                        await self.client.send_file(customer_id, event.media, voice_note=True)
                        content = "[🎙 Voice Note Delivered]"
                    elif event.photo:
                        await self.client.send_file(
                            customer_id,
                            event.media,
                            caption=event.text,
                            force_document=False,
                            parse_mode="html"
                        )
                        content = f"[🖼 Poster Sent] {event.text or ''}".strip()
                    elif event.file:
                        await self.client.send_file(customer_id, event.media, caption=event.text)
                        content = f"[📁 Document Sent] {event.text or ''}".strip()
                    else:
                        await self.client.send_message(customer_id, event.text, parse_mode="html")
                        content = event.text

                    save_message(customer_id, "ADMIN", content)
                    update_last_message(customer_id, content, status="handled")
                    set_user_status(customer_id, "handled")

                    bridge.admin_replied_signal.emit({"user_id": customer_id, "message": content})
                    bridge.user_status_changed.emit(customer_id, "handled")
                    map_admin_message(sender_id, event.id, customer_id)
                    await event.respond(f"✅ Delivered to customer <code>{customer_id}</code>", parse_mode="html")

                except UserIsBlockedError:
                    await event.respond("❌ Cannot deliver: Customer has blocked the bot.")
                except Exception as e:
                    await event.respond(f"❌ Delivery error: {e}")
                return

            # Case C: Customer Incoming Message
            sender = await event.get_sender()
            user_id = event.sender_id
            username = sender.username or ""
            fname = sender.first_name or ""
            lname = sender.last_name or ""

            text = event.text or ""
            media_tag = None
            if event.voice:
                media_tag = "🎙 Voice Message"
            elif event.audio:
                media_tag = "🎵 Audio Note"
            elif event.photo:
                media_tag = "🖼 Photo / Poster"
            elif event.file:
                media_tag = "📁 Document"

            display_content = text if text else f"[{media_tag or 'Attachment'}]"

            save_user_info(user_id, username, fname, lname, initial_status="waiting")
            update_last_message(user_id, display_content, status="waiting")
            save_message(user_id, "USER", display_content)

            bridge.user_message_received.emit({
                "user_id": user_id,
                "username": username,
                "first_name": fname,
                "last_name": lname,
                "message": display_content,
                "status": "waiting",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            name_str = html.escape(f"{fname} {lname}".strip() or "Valued Customer")
            username_str = f"@{html.escape(username)}" if username else "No Username"

            admin_notification = (
                "📩 <b>NEW CUSTOMER TICKET</b> 🟡 [Waiting]\n\n"
                f"👤 <b>Name:</b> {name_str}\n"
                f"🆔 <b>Customer ID:</b> <code>{user_id}</code>\n"
                f"🔗 <b>Username:</b> {username_str}\n"
            )
            if text:
                admin_notification += f"💬 <b>Message:</b>\n{html.escape(text)}\n\n"
            if media_tag:
                admin_notification += f"📎 <b>Media:</b> {media_tag}\n\n"
            admin_notification += "👉 <i>Tap <b>[💬 Reply to User]</b> or quote-reply to respond!</i>"

            # Action Buttons on Notification
            admin_keyboard = [
                [
                    Button.inline("💬 Reply to User", f"replyto_{user_id}".encode()),
                    Button.inline("✅ Mark Handled", f"resolve_{user_id}".encode()),
                ]
            ]

            admins = get_all_admin_ids()
            for admin_id in admins:
                try:
                    n_msg = await self.client.send_message(
                        admin_id,
                        admin_notification,
                        parse_mode="html",
                        buttons=admin_keyboard
                    )
                    map_admin_message(admin_id, n_msg.id, user_id)

                    if media_tag:
                        c_msg = await self.client.send_file(
                            admin_id,
                            event.media,
                            force_document=False,
                            voice_note=bool(event.voice)
                        )
                        map_admin_message(admin_id, c_msg.id, user_id)
                except Exception:
                    pass

            if get_setting("auto_reply_enabled", "1") == "1":
                ack = "🤖 <i>Thank you! An agent has received your message and will answer shortly.</i>"
                if event.voice:
                    ack = "🎙 <i>Your voice message has been delivered to support staff. Please stand by.</i>"
                try:
                    await event.respond(ack, parse_mode="html")
                    save_message(user_id, "BOT", ack)
                except Exception:
                    pass

    async def run_bot(self):
        await self.client.start(bot_token=BOT_TOKEN)
        logger.info("Telethon MTProto Bot connected.")
        while self._is_running:
            await asyncio.sleep(0.5)

    def send_admin_reply_from_gui(self, user_id: int, message: str):
        if not self.loop or not self.client:
            return
        asyncio.run_coroutine_threadsafe(self._async_send(user_id, message), self.loop)

    async def _async_send(self, user_id: int, message: str):
        try:
            await self.client.send_message(user_id, message, parse_mode="html")
            save_message(user_id, "ADMIN", message)
            update_last_message(user_id, message, status="handled")
            set_user_status(user_id, "handled")

            bridge.admin_replied_signal.emit({"user_id": user_id, "message": message})
            bridge.user_status_changed.emit(user_id, "handled")
            bridge.operation_status_signal.emit(f"✅ Delivered to {user_id}", True)

        except UserIsBlockedError:
            bridge.operation_status_signal.emit(f"❌ User {user_id} blocked the bot.", False)
        except Exception as e:
            bridge.operation_status_signal.emit(f"❌ Error sending to {user_id}: {e}", False)

    def send_photo_from_gui(self, user_id: int, photo_path: str, caption: str):
        if not self.loop or not self.client:
            return
        asyncio.run_coroutine_threadsafe(self._async_send_photo(user_id, photo_path, caption), self.loop)

    async def _async_send_photo(self, user_id: int, photo_path: str, caption: str):
        try:
            await send_visual_poster(
                client=self.client,
                chat_id=user_id,
                photo_source=photo_path,
                caption=caption
            )
            msg_desc = f"[🖼 Poster Sent] {caption}".strip()
            save_message(user_id, "ADMIN", msg_desc)
            update_last_message(user_id, msg_desc, status="handled")
            set_user_status(user_id, "handled")

            bridge.admin_replied_signal.emit({"user_id": user_id, "message": msg_desc})
            bridge.user_status_changed.emit(user_id, "handled")
            bridge.operation_status_signal.emit(f"✅ Poster delivered to {user_id}", True)
        except UserIsBlockedError:
            bridge.operation_status_signal.emit(f"❌ Customer {user_id} blocked the bot.", False)
        except Exception as e:
            bridge.operation_status_signal.emit(f"❌ Poster sending failed: {e}", False)

    def send_voice_from_gui(self, user_id: int, voice_path: str):
        if not self.loop or not self.client:
            return
        asyncio.run_coroutine_threadsafe(self._async_send_voice(user_id, voice_path), self.loop)

    async def _async_send_voice(self, user_id: int, voice_path: str):
        try:
            await send_native_voice(self.client, user_id, voice_path)
            msg_desc = "[🎙 Voice Note Delivered]"
            save_message(user_id, "ADMIN", msg_desc)
            update_last_message(user_id, msg_desc, status="handled")
            set_user_status(user_id, "handled")

            bridge.admin_replied_signal.emit({"user_id": user_id, "message": msg_desc})
            bridge.user_status_changed.emit(user_id, "handled")
            bridge.operation_status_signal.emit(f"✅ Voice note delivered to {user_id}", True)
        except UserIsBlockedError:
            bridge.operation_status_signal.emit(f"❌ Customer {user_id} blocked the bot.", False)
        except Exception as e:
            bridge.operation_status_signal.emit(f"❌ Voice note failed: {e}", False)

    def dispatch_broadcast(self, users: list, photo_path: str, text: str):
        if not self.loop or not self.client:
            return
        asyncio.run_coroutine_threadsafe(self._async_broadcast(users, photo_path, text), self.loop)

    async def _async_broadcast(self, users: list, photo_path: str, text: str):
        photo_bytes = None
        if photo_path:
            try:
                photo_bytes = await load_photo_bytes(photo_path)
            except Exception as e:
                bridge.operation_status_signal.emit(f"❌ Could not load poster: {e}", False)
                return

        sent, failed = 0, 0
        for row in users:
            uid = row[0]
            try:
                if photo_bytes:
                    await send_visual_poster(
                        client=self.client,
                        chat_id=uid,
                        photo_source=photo_bytes,
                        caption=text
                    )
                else:
                    await self.client.send_message(uid, text, parse_mode="html")
                save_message(uid, "ADMIN", f"[Broadcast] {text[:60]}")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        bridge.operation_status_signal.emit(f"📢 Broadcast Finished: {sent} Sent, {failed} Failed", True)

    def stop(self):
        self._is_running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown_bot(), self.loop)
        self.quit()
        self.wait(2000)

    async def _shutdown_bot(self):
        try:
            if self.client and self.client.is_connected():
                await self.client.disconnect()
        except Exception:
            pass


# ============================================================
# GUI STYLESHEET & DIALOGS
# ============================================================

PALETTES = [
    ("#4F46E5", "#EEF2FF"),
    ("#059669", "#ECFDF5"),
    ("#D97706", "#FFFBEB"),
    ("#DB2777", "#FDF2F8"),
    ("#7C3AED", "#F5F3FF"),
    ("#0284C7", "#F0F9FF"),
]

def get_avatar_color(user_id: int):
    return PALETTES[user_id % len(PALETTES)][0]


MODERN_STYLESHEET = """
QMainWindow { background-color: #0B0E14; }
QWidget { color: #E2E8F0; font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif; font-size: 13px; }
QFrame#sidebar { background-color: #111622; border-right: 1px solid #1E293B; }
QFrame#chatPanel { background-color: #0B0E14; }
QFrame#topNav { background-color: #131926; border-bottom: 1px solid #1E293B; }
QLineEdit { background-color: #1A2234; border: 1px solid #28344E; border-radius: 9px; padding: 9px 14px; color: #F8FAFC; }
QLineEdit:focus { border: 1px solid #6366F1; background-color: #1E273D; }
QListWidget { background-color: transparent; border: none; outline: none; }
QListWidget::item { background-color: transparent; border: none; padding: 0px; margin-bottom: 4px; }
QComboBox { background-color: #1A2234; border: 1px solid #28344E; border-radius: 8px; padding: 6px 12px; color: #E2E8F0; }
QPushButton { background-color: #4F46E5; color: #FFFFFF; font-weight: 600; border-radius: 8px; padding: 8px 16px; border: none; }
QPushButton:hover { background-color: #4338CA; }
QPushButton:pressed { background-color: #3730A3; }
QPushButton#secondaryBtn { background-color: #1E293B; color: #CBD5E1; border: 1px solid #334155; }
QPushButton#secondaryBtn:hover { background-color: #334155; color: #FFFFFF; }
QPushButton#cannedBtn { background-color: #161F30; color: #94A3B8; border: 1px solid #25334D; border-radius: 14px; padding: 4px 12px; font-size: 11px; }
QPushButton#cannedBtn:hover { background-color: #1E2C44; color: #F8FAFC; border-color: #4F46E5; }
QCheckBox { spacing: 8px; font-weight: 500; color: #CBD5E1; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px; border: 1px solid #334155; background-color: #1A2234; }
QCheckBox::indicator:checked { background-color: #10B981; border-color: #10B981; }
QStatusBar { background-color: #0E131C; border-top: 1px solid #1E293B; color: #94A3B8; font-size: 12px; }
"""


class StartSetupModal(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure /start Auto-Reply Poster & Message")
        self.resize(600, 520)
        self.setStyleSheet(MODERN_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("⚙️ Customize Customer /start Greeting")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)

        desc = QLabel("The bot automatically displays this poster directly in chat (never as a file):")
        desc.setStyleSheet("color: #94A3B8;")
        layout.addWidget(desc)

        img_box = QHBoxLayout()
        self.img_input = QLineEdit()
        self.img_input.setPlaceholderText("Select image path or enter public image URL...")
        self.img_input.setText(get_setting("start_image_path", DEFAULT_POSTER_URL))
        img_box.addWidget(self.img_input)

        browse_btn = QPushButton("📁 Browse...")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.clicked.connect(self.browse_image)
        img_box.addWidget(browse_btn)
        layout.addLayout(img_box)

        layout.addWidget(QLabel("Welcome Message Text (HTML Supported):"))
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Enter welcome text with phone numbers and emojis...")
        self.text_edit.setText(get_setting("start_text", DEFAULT_START_MESSAGE))
        self.text_edit.setStyleSheet("background-color: #1A2234; border: 1px solid #28344E; border-radius: 8px; padding: 10px;")
        layout.addWidget(self.text_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("secondaryBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("💾 Save /start Settings")
        save_btn.clicked.connect(self.save_settings)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def browse_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Poster Image", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.img_input.setText(path)

    def save_settings(self):
        set_setting("start_image_path", self.img_input.text().strip())
        set_setting("start_text", self.text_edit.toPlainText().strip())
        self.accept()


class AdminManagerModal(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Bot Admins")
        self.resize(440, 360)
        self.setStyleSheet(MODERN_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("🛡 Bot Administrators")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)

        self.admin_list = QListWidget()
        self.refresh_list()
        layout.addWidget(self.admin_list)

        add_row = QHBoxLayout()
        self.new_admin_input = QLineEdit()
        self.new_admin_input.setPlaceholderText("Enter new Admin numeric User ID...")
        add_row.addWidget(self.new_admin_input)

        add_btn = QPushButton("➕ Add Admin")
        add_btn.clicked.connect(self.add_admin_action)
        add_row.addWidget(add_btn)
        layout.addLayout(add_row)

        del_btn = QPushButton("🗑 Remove Selected Admin")
        del_btn.setObjectName("secondaryBtn")
        del_btn.clicked.connect(self.remove_admin_action)
        layout.addWidget(del_btn)

    def refresh_list(self):
        self.admin_list.clear()
        for aid in get_all_admin_ids():
            self.admin_list.addItem(f"👤 Admin User ID: {aid}")

    def add_admin_action(self):
        txt = self.new_admin_input.text().strip()
        if not txt.isdigit():
            QMessageBox.warning(self, "Invalid ID", "Please enter a valid numeric Telegram ID.")
            return
        add_admin(int(txt))
        self.new_admin_input.clear()
        self.refresh_list()

    def remove_admin_action(self):
        item = self.admin_list.currentItem()
        if not item:
            return
        aid = int(re.search(r"\d+", item.text()).group())
        remove_admin(aid)
        self.refresh_list()


class CustomerCardWidget(QWidget):
    def __init__(self, uid: int, name: str, username: str, status: str, last_msg: str, last_seen: str):
        super().__init__()
        self.uid = uid
        self.status = status

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(12)

        initials = "".join([part[0] for part in name.split()[:2]]).upper() or "U"
        color = get_avatar_color(uid)

        avatar = QLabel(initials)
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFont(QFont("Segoe UI", 12, QFont.Bold))
        avatar.setStyleSheet(f"background-color: {color}; color: #FFFFFF; border-radius: 20px;")
        layout.addWidget(avatar)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)

        top_row = QHBoxLayout()
        name_label = QLabel(name)
        name_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        name_label.setStyleSheet("color: #F8FAFC;")
        top_row.addWidget(name_label)
        top_row.addStretch()

        badge_text = "WAITING" if status == "waiting" else "RESOLVED"
        badge_bg = "#78350F" if status == "waiting" else "#064E3B"
        badge_color = "#FCD34D" if status == "waiting" else "#6EE7B7"

        badge = QLabel(badge_text)
        badge.setFont(QFont("Segoe UI", 8, QFont.Bold))
        badge.setStyleSheet(f"background-color: {badge_bg}; color: {badge_color}; padding: 2px 7px; border-radius: 9px;")
        top_row.addWidget(badge)
        info_col.addLayout(top_row)

        bottom_row = QHBoxLayout()
        clean_snippet = (last_msg or "No messages").replace("\n", " ")
        if len(clean_snippet) > 26:
            clean_snippet = clean_snippet[:26] + "..."

        snippet_label = QLabel(clean_snippet)
        snippet_label.setStyleSheet("color: #94A3B8; font-size: 11px;")
        bottom_row.addWidget(snippet_label)
        bottom_row.addStretch()

        time_str = last_seen[-8:-3] if last_seen and len(last_seen) >= 8 else ""
        time_label = QLabel(time_str)
        time_label.setStyleSheet("color: #64748B; font-size: 10px;")
        bottom_row.addWidget(time_label)

        info_col.addLayout(bottom_row)
        layout.addLayout(info_col)

        self.setStyleSheet("""
            CustomerCardWidget { background-color: #171E2B; border-radius: 10px; border: 1px solid #1E283C; }
            CustomerCardWidget:hover { background-color: #1E273A; border: 1px solid #334155; }
        """)


class ChatInputEdit(QTextEdit):
    send_triggered = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            event.accept()
            self.send_triggered.emit()
        else:
            super().keyPressEvent(event)


class BroadcastModal(QDialog):
    def __init__(self, parent=None, total_users_count=0):
        super().__init__(parent)
        self.setWindowTitle("Broadcast Poster & Announcement")
        self.resize(540, 420)
        self.setStyleSheet(MODERN_STYLESHEET)
        self.selected_photo_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel(f"📢 Broadcast Announcement — {BUSINESS_NAME}")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)

        desc = QLabel(f"Delivers directly as an inline visual poster to <b>{total_users_count}</b> customer chats:")
        desc.setStyleSheet("color: #94A3B8;")
        layout.addWidget(desc)

        photo_row = QHBoxLayout()
        self.photo_label = QLabel("No Poster Selected (Optional)")
        self.photo_label.setStyleSheet("color: #64748B;")
        photo_row.addWidget(self.photo_label)

        choose_photo_btn = QPushButton("📁 Pick Image...")
        choose_photo_btn.setObjectName("secondaryBtn")
        choose_photo_btn.clicked.connect(self.pick_image)
        photo_row.addWidget(choose_photo_btn)
        layout.addLayout(photo_row)

        self.msg_edit = QTextEdit()
        self.msg_edit.setPlaceholderText("Write your news or notice here...")
        self.msg_edit.setStyleSheet("background-color: #1A2234; border: 1px solid #28344E; border-radius: 8px; padding: 10px;")
        layout.addWidget(self.msg_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("secondaryBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self.send_btn = QPushButton("🚀 Send Broadcast")
        self.send_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.send_btn)

        layout.addLayout(btn_row)

    def pick_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Poster Image", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.selected_photo_path = path
            self.photo_label.setText(os.path.basename(path))

    def get_payload(self):
        return self.selected_photo_path, self.msg_edit.toPlainText().strip()


# ============================================================
# MAIN WINDOW: MODERN SUPPORT DESK
# ============================================================

class ModernSupportDesk(QMainWindow):
    def __init__(self, bot_worker: BotWorkerThread):
        super().__init__()
        self.bot_worker = bot_worker
        self.selected_user_id = None
        self.selected_user_name = ""
        self.selected_user_username = ""

        self.setWindowTitle(f"Customer Helpdesk & Live Console — {BUSINESS_NAME}")
        self.resize(1280, 840)
        self.setStyleSheet(MODERN_STYLESHEET)

        self.setup_ui()
        self.setup_signals()

        self.refresh_stats_bar()
        self.refresh_users_list()

    def closeEvent(self, event):
        if self.bot_worker and self.bot_worker.isRunning():
            self.bot_worker.stop()
        event.accept()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top Bar
        top_nav = QFrame()
        top_nav.setObjectName("topNav")
        top_nav_layout = QHBoxLayout(top_nav)
        top_nav_layout.setContentsMargins(20, 12, 20, 12)

        brand_icon = QLabel("🏢")
        brand_icon.setFont(QFont("Segoe UI", 16))
        top_nav_layout.addWidget(brand_icon)

        brand_title = QLabel(BUSINESS_NAME.upper())
        brand_title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        brand_title.setStyleSheet("color: #FFFFFF; letter-spacing: 1px;")
        top_nav_layout.addWidget(brand_title)

        top_nav_layout.addSpacing(20)

        self.total_pill = QLabel("👥 Customers: 0")
        self.total_pill.setStyleSheet("background-color: #1E293B; color: #94A3B8; padding: 4px 12px; border-radius: 12px; font-weight: 600;")
        top_nav_layout.addWidget(self.total_pill)

        self.waiting_pill = QLabel("🟡 Waiting: 0")
        self.waiting_pill.setStyleSheet("background-color: #451A03; color: #FCD34D; padding: 4px 12px; border-radius: 12px; font-weight: 600;")
        top_nav_layout.addWidget(self.waiting_pill)

        top_nav_layout.addStretch()

        start_setup_btn = QPushButton("⚙️ /start Greeting Setup")
        start_setup_btn.setObjectName("secondaryBtn")
        start_setup_btn.clicked.connect(self.open_start_setup)
        top_nav_layout.addWidget(start_setup_btn)

        admin_manage_btn = QPushButton("🛡 Admins")
        admin_manage_btn.setObjectName("secondaryBtn")
        admin_manage_btn.clicked.connect(self.open_admin_manager)
        top_nav_layout.addWidget(admin_manage_btn)

        broadcast_btn = QPushButton("📢 Broadcast")
        broadcast_btn.setObjectName("secondaryBtn")
        broadcast_btn.clicked.connect(self.open_broadcast_modal)
        top_nav_layout.addWidget(broadcast_btn)

        main_layout.addWidget(top_nav)

        splitter = QSplitter(Qt.Horizontal)

        # Left Sidebar
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search contacts, @username, ID...")
        self.search_input.textChanged.connect(self.refresh_users_list)
        sidebar_layout.addWidget(self.search_input)

        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All Tickets", "🟡 Waiting Action", "🟢 Resolved Handled"])
        self.filter_combo.currentIndexChanged.connect(self.refresh_users_list)
        sidebar_layout.addWidget(self.filter_combo)

        self.user_list_widget = QListWidget()
        self.user_list_widget.itemClicked.connect(self.on_user_selected)
        sidebar_layout.addWidget(self.user_list_widget)

        splitter.addWidget(sidebar)

        # Right Chat Canvas
        chat_panel = QFrame()
        chat_panel.setObjectName("chatPanel")
        chat_layout = QVBoxLayout(chat_panel)
        chat_layout.setContentsMargins(20, 16, 20, 16)
        chat_layout.setSpacing(10)

        # Header Info Card
        self.chat_header_card = QFrame()
        self.chat_header_card.setStyleSheet("background-color: #131926; border-radius: 12px; border: 1px solid #1E293B; padding: 6px 12px;")
        header_card_layout = QHBoxLayout(self.chat_header_card)
        header_card_layout.setContentsMargins(10, 6, 10, 6)

        self.chat_header_avatar = QLabel("?")
        self.chat_header_avatar.setFixedSize(38, 38)
        self.chat_header_avatar.setAlignment(Qt.AlignCenter)
        self.chat_header_avatar.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.chat_header_avatar.setStyleSheet("background-color: #334155; color: #FFFFFF; border-radius: 19px;")
        header_card_layout.addWidget(self.chat_header_avatar)

        header_info = QVBoxLayout()
        header_info.setSpacing(1)
        self.chat_title_label = QLabel("No Customer Selected")
        self.chat_title_label.setFont(QFont("Segoe UI", 13, QFont.Bold))
        self.chat_title_label.setStyleSheet("color: #FFFFFF;")
        header_info.addWidget(self.chat_title_label)

        self.chat_sub_label = QLabel("Select any customer to review chat and reply live")
        self.chat_sub_label.setStyleSheet("color: #64748B; font-size: 11px;")
        header_info.addWidget(self.chat_sub_label)

        header_card_layout.addLayout(header_info)
        header_card_layout.addStretch()

        self.copy_id_btn = QPushButton("📋 Copy ID")
        self.copy_id_btn.setObjectName("secondaryBtn")
        self.copy_id_btn.setVisible(False)
        self.copy_id_btn.clicked.connect(self.copy_selected_user_id)
        header_card_layout.addWidget(self.copy_id_btn)

        self.status_action_btn = QPushButton("Mark Resolved 🟢")
        self.status_action_btn.setObjectName("secondaryBtn")
        self.status_action_btn.setVisible(False)
        self.status_action_btn.clicked.connect(self.toggle_selected_user_status)
        header_card_layout.addWidget(self.status_action_btn)

        chat_layout.addWidget(self.chat_header_card)

        self.chat_browser = QTextBrowser()
        self.chat_browser.setOpenExternalLinks(True)
        self.chat_browser.setStyleSheet("background-color: #0E121A; border: 1px solid #1E283C; border-radius: 12px; padding: 16px;")
        chat_layout.addWidget(self.chat_browser)

        # Multi-Media Actions + Chat Input
        input_container = QFrame()
        input_container.setStyleSheet("background-color: #131926; border-radius: 12px; border: 1px solid #1E283C; padding: 6px;")
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(6, 6, 6, 6)
        input_layout.setSpacing(8)

        self.attach_img_btn = QPushButton("🖼 Poster")
        self.attach_img_btn.setObjectName("secondaryBtn")
        self.attach_img_btn.setToolTip("Send an inline visual poster directly in chat (never as a file)")
        self.attach_img_btn.clicked.connect(self.gui_send_photo)
        input_layout.addWidget(self.attach_img_btn)

        self.attach_voice_btn = QPushButton("🎙 Voice")
        self.attach_voice_btn.setObjectName("secondaryBtn")
        self.attach_voice_btn.setToolTip("Send a native voice note")
        self.attach_voice_btn.clicked.connect(self.gui_send_voice)
        input_layout.addWidget(self.attach_voice_btn)

        self.reply_input = ChatInputEdit()
        self.reply_input.setFixedHeight(54)
        self.reply_input.setPlaceholderText("Write your reply... (Enter to send, Shift+Enter for newline)")
        self.reply_input.setStyleSheet("background-color: transparent; border: none; padding: 6px; color: #FFFFFF; font-size: 13px;")
        self.reply_input.send_triggered.connect(self.send_reply)
        input_layout.addWidget(self.reply_input)

        self.send_btn = QPushButton("Send ➤")
        self.send_btn.setFixedSize(90, 48)
        self.send_btn.clicked.connect(self.send_reply)
        input_layout.addWidget(self.send_btn)

        chat_layout.addWidget(input_container)

        splitter.addWidget(chat_panel)
        splitter.setSizes([360, 880])

        main_layout.addWidget(splitter)
        self.statusBar().showMessage("🟢 Support Desk Engine Online. Visual poster rendering active.")

    def setup_signals(self):
        bridge.user_message_received.connect(self.on_user_message_received)
        bridge.admin_replied_signal.connect(self.on_admin_replied)
        bridge.user_status_changed.connect(self.on_user_status_changed)
        bridge.operation_status_signal.connect(self.on_operation_status)

    def refresh_stats_bar(self):
        total, waiting = get_stats_counts()
        self.total_pill.setText(f"👥 Customers: {total}")
        self.waiting_pill.setText(f"🟡 Waiting: {waiting}")

    def copy_selected_user_id(self):
        if self.selected_user_id:
            QApplication.clipboard().setText(str(self.selected_user_id))
            self.statusBar().showMessage(f"Copied ID {self.selected_user_id} to clipboard!", 2500)

    def refresh_users_list(self):
        query = self.search_input.text()
        filter_idx = self.filter_combo.currentIndex()
        status = "all"
        if filter_idx == 1:
            status = "waiting"
        elif filter_idx == 2:
            status = "handled"

        users = get_users_list(query, status)
        self.user_list_widget.clear()

        for u in users:
            uid, username, fname, lname, u_status, last_msg, last_seen = u
            name = f"{fname} {lname}".strip() or "Valued Customer"

            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 68))
            item.setData(Qt.UserRole, uid)
            item.setData(Qt.UserRole + 1, u_status)
            item.setData(Qt.UserRole + 2, name)
            item.setData(Qt.UserRole + 3, username)

            widget = CustomerCardWidget(uid, name, username, u_status, last_msg, last_seen)
            self.user_list_widget.addItem(item)
            self.user_list_widget.setItemWidget(item, widget)

        self.refresh_stats_bar()

    def on_user_selected(self, item: QListWidgetItem):
        self.selected_user_id = item.data(Qt.UserRole)
        self.selected_user_name = item.data(Qt.UserRole + 2)
        self.selected_user_username = item.data(Qt.UserRole + 3)
        status = item.data(Qt.UserRole + 1)

        initials = "".join([part[0] for part in self.selected_user_name.split()[:2]]).upper() or "U"
        color = get_avatar_color(self.selected_user_id)

        self.chat_header_avatar.setText(initials)
        self.chat_header_avatar.setStyleSheet(f"background-color: {color}; color: #FFFFFF; border-radius: 19px;")

        self.chat_title_label.setText(self.selected_user_name)
        uname_display = f"@{self.selected_user_username}" if self.selected_user_username else "No Telegram handle"
        self.chat_sub_label.setText(f"{uname_display} • Numeric ID: {self.selected_user_id}")

        self.copy_id_btn.setVisible(True)
        self.status_action_btn.setVisible(True)
        self.update_status_button(status)

        self.load_chat_history(self.selected_user_id)

    def update_status_button(self, status: str):
        if status == "waiting":
            self.status_action_btn.setText("Mark Resolved 🟢")
            self.status_action_btn.setStyleSheet("background-color: #065F46; color: #6EE7B7; border: 1px solid #047857;")
        else:
            self.status_action_btn.setText("Mark Waiting 🟡")
            self.status_action_btn.setStyleSheet("background-color: #78350F; color: #FDE68A; border: 1px solid #B45309;")

    def toggle_selected_user_status(self):
        if not self.selected_user_id:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM users WHERE user_id=?", (self.selected_user_id,))
        row = cur.fetchone()
        conn.close()

        curr = row[0] if row else "waiting"
        new_status = "handled" if curr == "waiting" else "waiting"

        set_user_status(self.selected_user_id, new_status)
        self.update_status_button(new_status)
        self.refresh_users_list()

    def load_chat_history(self, user_id: int):
        self.chat_browser.clear()
        messages = get_chat_history(user_id)

        bubble_html = """
        <style>
            body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; }
            .msg-row-user { display: flex; margin-bottom: 14px; text-align: left; }
            .bubble-user {
                background-color: #1E283C; color: #F1F5F9; border: 1px solid #28354E;
                border-radius: 14px 14px 14px 2px; padding: 10px 14px; max-width: 75%; display: inline-block;
            }
            .msg-row-admin { text-align: right; margin-bottom: 14px; }
            .bubble-admin {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4F46E5, stop:1 #4338CA);
                color: #FFFFFF; border-radius: 14px 14px 2px 14px; padding: 10px 14px;
                max-width: 75%; display: inline-block; text-align: left;
            }
            .bubble-bot { text-align: center; margin: 12px 0; }
            .bot-pill {
                background-color: #171E2D; color: #94A3B8; border: 1px solid #242E44;
                padding: 4px 12px; border-radius: 12px; font-size: 11px; display: inline-block;
            }
            .meta { font-size: 10px; color: #94A3B8; margin-top: 4px; display: block; }
            .meta-admin { font-size: 10px; color: #C7D2FE; margin-top: 4px; text-align: right; display: block; }
            .sender-name { font-weight: bold; font-size: 11px; margin-bottom: 3px; display: block; }
        </style>
        """

        for sender, text, created in messages:
            escaped_text = html.escape(text).replace("\n", "<br>")
            time_part = created[-8:-3] if len(created) >= 8 else ""

            if sender == "USER":
                bubble_html += f"""
                <div class="msg-row-user">
                    <div class="bubble-user">
                        <span class="sender-name" style="color: #38BDF8;">👤 {html.escape(self.selected_user_name or 'Customer')}</span>
                        <div>{escaped_text}</div>
                        <span class="meta">{time_part}</span>
                    </div>
                </div>
                """
            elif sender == "ADMIN":
                bubble_html += f"""
                <div class="msg-row-admin">
                    <div class="bubble-admin">
                        <span class="sender-name" style="color: #A5B4FC;">👨‍💼 Support Staff</span>
                        <div>{escaped_text}</div>
                        <span class="meta-admin">{time_part}</span>
                    </div>
                </div>
                """
            else:
                bubble_html += f"""
                <div class="bubble-bot">
                    <span class="bot-pill">🤖 {BUSINESS_NAME} Assistant: {escaped_text} ({time_part})</span>
                </div>
                """

        self.chat_browser.setHtml(bubble_html)
        self.chat_browser.verticalScrollBar().setValue(self.chat_browser.verticalScrollBar().maximum())

    def send_reply(self):
        if not self.selected_user_id:
            QMessageBox.warning(self, "No Selection", "Please click on a customer from the left list first.")
            return

        text = self.reply_input.toPlainText().strip()
        if not text:
            return

        self.bot_worker.send_admin_reply_from_gui(self.selected_user_id, text)
        self.reply_input.clear()
        self.load_chat_history(self.selected_user_id)
        self.refresh_users_list()

    def gui_send_photo(self):
        if not self.selected_user_id:
            QMessageBox.warning(self, "No Selection", "Please choose a customer first.")
            return

        path, _ = QFileDialog.getOpenFileName(self, "Select Poster Image to Send", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return

        caption, ok = QInputDialog.getText(self, "Poster Caption", "Enter description / caption (Optional):")
        if not ok:
            return

        self.statusBar().showMessage("⏳ Uploading and delivering visual poster...", 3000)
        self.bot_worker.send_photo_from_gui(self.selected_user_id, path, caption.strip())
        self.load_chat_history(self.selected_user_id)
        self.refresh_users_list()

    def gui_send_voice(self):
        if not self.selected_user_id:
            QMessageBox.warning(self, "No Selection", "Please choose a customer first.")
            return

        path, _ = QFileDialog.getOpenFileName(self, "Select Voice / Audio File", "", "Audio (*.ogg *.mp3 *.wav *.m4a)")
        if not path:
            return

        self.statusBar().showMessage("⏳ Transmitting native voice note...", 3000)
        self.bot_worker.send_voice_from_gui(self.selected_user_id, path)
        self.load_chat_history(self.selected_user_id)
        self.refresh_users_list()

    def open_start_setup(self):
        modal = StartSetupModal(self)
        if modal.exec() == QDialog.Accepted:
            self.statusBar().showMessage("✅ Custom /start greeting settings updated successfully!", 4000)

    def open_admin_manager(self):
        modal = AdminManagerModal(self)
        modal.exec()

    def on_user_message_received(self, data: dict):
        self.refresh_users_list()
        if self.selected_user_id == data["user_id"]:
            self.load_chat_history(self.selected_user_id)
        self.statusBar().showMessage(f"🔔 Incoming message from {data.get('first_name', 'Customer')} ({data['user_id']})", 4500)

    def on_admin_replied(self, data: dict):
        self.refresh_users_list()
        if self.selected_user_id == data["user_id"]:
            self.load_chat_history(self.selected_user_id)

    def on_user_status_changed(self, user_id: int, status: str):
        if self.selected_user_id == user_id:
            self.update_status_button(status)
        self.refresh_users_list()

    def on_operation_status(self, msg: str, success: bool):
        self.statusBar().showMessage(msg, 5000)

    def open_broadcast_modal(self):
        total, _ = get_stats_counts()
        modal = BroadcastModal(self, total_users_count=total)
        if modal.exec() == QDialog.Accepted:
            photo_path, text = modal.get_payload()
            if not text and not photo_path:
                return

            users = get_users_list(filter_status="all")
            self.bot_worker.dispatch_broadcast(users, photo_path, text)
            QMessageBox.information(self, "Broadcast Queued", f"Delivering announcement to {len(users)} customer chats.")


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    init_db()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    bot_worker = BotWorkerThread()
    bot_worker.start()

    try:
        window = ModernSupportDesk(bot_worker)
        window.show()
        sys.exit(app.exec())
    finally:
        bot_worker.stop()


if __name__ == "__main__":
    main()
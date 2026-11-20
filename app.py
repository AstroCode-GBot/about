import os
import re
import json
import time
import random
import string
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import regex
import telebot
from telebot import types
from flask import Flask, jsonify

# ============================================================
# Venzoo Premium Emoji Bot
# Render-compatible: one polling worker + Flask health endpoint
# Storage: JSON files only (no SQL database)
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("venzoo-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "9017814782:AAGL6EILZ5ovhPRQ0bMx6GyRSS5flMct9wQ").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5684263622"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "VenzooPremiumEmojiBot").strip().lstrip("@")
PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing. Add your Telegram bot token in Render Environment.")

# --- Keep every custom emoji ID from the original source code ---
DEFAULT_TEXT_EMOJIS = [
    "5303040006347046717", "5303548912726982102", "6154631793787015439", "5870568528049868382",
    "6154603635981423575", "5267394977540889773", "5870619152829386951", "6154729229415093721",
    "6154590274338166128", "6154565990593073474", "6154314868150243661", "6154659543570714696",
    "6154635564768300782", "5267102967009402987", "5303178192624823421", "5337310516769467285",
]
DEFAULT_BUTTON_EMOJIS = [
    "5213285132709929474", "5303040006347046717", "6154631793787015439", "5870568528049868382",
    "6154729229415093721", "6154635564768300782", "6154718341672997377",
]
# Additional IDs that were used directly in the original menus/status messages.
EXTRA_EMOJIS = [
    "5870869476408299692", "4956599758044005301", "6154611916678369390",
    "5429651785352501917", "6154619152829386951",
]
ALL_EMOJI_IDS = list(dict.fromkeys(DEFAULT_TEXT_EMOJIS + DEFAULT_BUTTON_EMOJIS + EXTRA_EMOJIS))

USERS_FILE = DATA_DIR / "users.json"
CHANNELS_FILE = DATA_DIR / "channels.json"
POSTS_FILE = DATA_DIR / "posts.json"

file_lock = threading.RLock()
state_lock = threading.RLock()
user_states = {}


def load_json(path, fallback):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return fallback


def save_json(path, value):
    """Atomic JSON write, so a partial write does not corrupt the data file."""
    with file_lock:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)


# Backward-compatible loading: an old users.json may contain a simple list.
_raw_users = load_json(USERS_FILE, {})
if isinstance(_raw_users, list):
    users_db = {str(uid): {"id": int(uid), "first_name": "", "username": ""} for uid in _raw_users}
elif isinstance(_raw_users, dict):
    users_db = _raw_users
else:
    users_db = {}

_raw_channels = load_json(CHANNELS_FILE, {})
# Old code used channels.json for a global mandatory-join list. That list is
# deliberately not treated as user-owned channels.
channels_db = _raw_channels if isinstance(_raw_channels, dict) else {}
_raw_posts = load_json(POSTS_FILE, {})
posts_db = _raw_posts if isinstance(_raw_posts, dict) else {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)
app = Flask(__name__)

BOT_STARTED = False
BOT_THREAD = None
BOT_LAST_ERROR = None
BOT_START_TIME = datetime.now(timezone.utc).isoformat()


# ========================= SMALL HELPERS =========================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def uid_of(message_or_call):
    return int(message_or_call.from_user.id)


def set_state(user_id, value):
    with state_lock:
        user_states[int(user_id)] = value


def get_state(user_id):
    with state_lock:
        return user_states.get(int(user_id))


def clear_state(user_id):
    with state_lock:
        user_states.pop(int(user_id), None)


def register_user(user):
    key = str(user.id)
    old = users_db.get(key, {})
    users_db[key] = {
        "id": int(user.id),
        "first_name": getattr(user, "first_name", "") or old.get("first_name", ""),
        "username": getattr(user, "username", "") or old.get("username", ""),
        "last_name": getattr(user, "last_name", "") or old.get("last_name", ""),
        "joined_at": old.get("joined_at", utc_now()),
        "last_seen": utc_now(),
    }
    save_json(USERS_FILE, users_db)


def get_user_channels(user_id):
    result = channels_db.get(str(user_id), [])
    return result if isinstance(result, list) else []


def save_user_channels(user_id, items):
    channels_db[str(user_id)] = items
    save_json(CHANNELS_FILE, channels_db)


def private_only_message(message):
    return message.chat.type == "private"


def private_only_call(call):
    return bool(call.message and call.message.chat.type == "private")


def shorten(value, limit=34):
    value = str(value or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def is_emoji_cluster(value):
    # Handles emoji sequences (skin tones, flags, ZWJ sequences and keycaps).
    return bool(regex.search(r"\p{Extended_Pictographic}|\p{Emoji_Presentation}|\p{Regional_Indicator}|[#*0-9]\uFE0F?\u20E3", value))


def utf16_len(value):
    return len(value.encode("utf-16-le")) // 2


def choose_emoji(pool=None, previous=None):
    pool = pool or DEFAULT_TEXT_EMOJIS
    if not pool:
        return ALL_EMOJI_IDS[0]
    choices = [item for item in pool if item != previous] or list(pool)
    return random.choice(choices)


def entity_to_dict(entity):
    data = {
        "type": getattr(entity, "type", None),
        "offset": int(getattr(entity, "offset", 0)),
        "length": int(getattr(entity, "length", 0)),
    }
    for key in ("url", "language", "custom_emoji_id"):
        value = getattr(entity, key, None)
        if value is not None:
            data[key] = value
    user = getattr(entity, "user", None)
    if user is not None:
        try:
            data["user"] = user.to_dict()
        except Exception:
            pass
    return data


def entity_from_dict(data):
    allowed = {"type", "offset", "length", "url", "language", "custom_emoji_id", "user"}
    kwargs = {key: value for key, value in data.items() if key in allowed and value is not None}
    if isinstance(kwargs.get("user"), dict):
        try:
            kwargs["user"] = types.User.de_json(kwargs["user"])
        except Exception:
            kwargs.pop("user", None)
    return types.MessageEntity(**kwargs)


def process_text_and_entities(message):
    """Convert ordinary emoji graphemes to Telegram custom emoji entities."""
    original_text = message.text or message.caption or ""
    source_entities = message.entities or message.caption_entities or []
    source_custom = [
        (int(ent.offset), int(ent.offset + ent.length))
        for ent in source_entities if getattr(ent, "type", "") == "custom_emoji"
    ]

    output_parts = []
    generated_entities = []
    offset_map = {}
    old_offset = 0
    new_offset = 0
    last_id = None

    for match in regex.finditer(r"\X", original_text):
        cluster = match.group(0)
        old_start = old_offset
        old_end = old_start + utf16_len(cluster)
        new_start = new_offset
        offset_map[old_start] = new_start

        already_custom = any(old_start < custom_end and old_end > custom_start for custom_start, custom_end in source_custom)
        if is_emoji_cluster(cluster) and not already_custom:
            emoji_id = choose_emoji(DEFAULT_TEXT_EMOJIS, last_id)
            last_id = emoji_id
            placeholder = "✨"
            output_parts.append(placeholder)
            new_offset += utf16_len(placeholder)
            generated_entities.append({
                "type": "custom_emoji",
                "offset": new_start,
                "length": utf16_len(placeholder),
                "custom_emoji_id": emoji_id,
            })
        else:
            output_parts.append(cluster)
            new_offset += utf16_len(cluster)

        old_offset = old_end
        offset_map[old_offset] = new_offset

    output_text = "".join(output_parts)

    for entity in source_entities:
        start = offset_map.get(int(entity.offset))
        end = offset_map.get(int(entity.offset + entity.length))
        if start is None or end is None or end <= start:
            continue
        item = entity_to_dict(entity)
        item["offset"] = start
        item["length"] = end - start
        generated_entities.append(item)

    # Append a branded footer with one of the original custom emoji IDs.
    footer_prefix = f"\n\nPost by @{BOT_USERNAME} "
    footer_start = utf16_len(output_text + footer_prefix)
    footer_emoji_id = choose_emoji(DEFAULT_BUTTON_EMOJIS)
    output_text += footer_prefix + "✨"
    generated_entities.append({
        "type": "custom_emoji",
        "offset": footer_start,
        "length": utf16_len("✨"),
        "custom_emoji_id": footer_emoji_id,
    })
    generated_entities.sort(key=lambda item: (item.get("offset", 0), item.get("length", 0)))
    return output_text, generated_entities


def entities_for_api(items):
    return [entity_from_dict(item) for item in (items or [])]


def main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📝 Create & Publish", callback_data="menu:create"),
        types.InlineKeyboardButton("🔗 Connect Channel", callback_data="menu:connect"),
    )
    markup.add(
        types.InlineKeyboardButton("📂 My Channels", callback_data="menu:channels"),
        types.InlineKeyboardButton("🗂 My Posts / Edit", callback_data="menu:posts"),
    )
    markup.add(types.InlineKeyboardButton("❔ Help & Emoji IDs", callback_data="menu:help"))
    if int(user_id) == ADMIN_ID:
        markup.add(types.InlineKeyboardButton("👑 Admin Panel", callback_data="menu:admin"))
    return markup


def admin_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Bot Status", callback_data="admin:stats"),
        types.InlineKeyboardButton("👥 Registered Users", callback_data="admin:users"),
    )
    markup.add(
        types.InlineKeyboardButton("📡 All User Channels", callback_data="admin:channels"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast"),
    )
    markup.add(types.InlineKeyboardButton("📨 Post to Any User Channel", callback_data="admin:post"))
    markup.add(types.InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:home"))
    return markup


def back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:home"))
    return markup


def safe_send_text(chat_id, text, **kwargs):
    # Telegram's legacy Markdown uses one asterisk for bold, not two.
    text = str(text).replace("**", "*")
    kwargs.setdefault("parse_mode", "Markdown")
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:
        # Keep messages usable if Markdown is rejected by dynamic text.
        fallback = dict(kwargs)
        fallback.pop("parse_mode", None)
        fallback.pop("entities", None)
        log.warning("Formatted message failed; retrying as plain text: %s", exc)
        return bot.send_message(chat_id, text.replace("*", "").replace("`", ""), **fallback)


def send_post_to_channel(chat_id, post):
    entities = entities_for_api(post.get("entities", []))
    try:
        if post.get("photo_id"):
            return bot.send_photo(
                chat_id,
                post["photo_id"],
                caption=post["text"],
                caption_entities=entities,
            )
        return bot.send_message(chat_id, post["text"], entities=entities, disable_web_page_preview=False)
    except Exception:
        # If Telegram rejects an optional entity (for example custom emoji
        # permissions), still publish the post as readable plain text.
        if post.get("photo_id"):
            return bot.send_photo(chat_id, post["photo_id"], caption=post["text"])
        return bot.send_message(chat_id, post["text"], disable_web_page_preview=False)


def build_channel_picker(user_id, post_id, selected=None):
    selected = set(str(item) for item in (selected or []))
    channels = get_user_channels(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in channels:
        channel_id = str(channel.get("id"))
        checked = "✅" if channel_id in selected else "▫️"
        title = shorten(channel.get("title") or channel_id, 43)
        markup.add(types.InlineKeyboardButton(
            f"{checked} {title}", callback_data=f"channel:toggle:{post_id}:{channel_id}"
        ))
    markup.add(types.InlineKeyboardButton("🚀 Publish Selected", callback_data=f"channel:publish:{post_id}"))
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="menu:home"))
    return markup


def show_channel_picker(user_id, post_id):
    channels = get_user_channels(user_id)
    if not channels:
        safe_send_text(
            user_id,
            "No channel connected yet. Tap **Connect Channel** first, add the bot as a channel administrator, then try again.",
            reply_markup=main_keyboard(user_id),
        )
        return
    set_state(user_id, {"step": "publish_pick", "post_id": post_id, "selected": []})
    bot.send_message(
        user_id,
        "Select one or more of your connected channels. Tap a channel to select/deselect it, then press Publish Selected.",
        reply_markup=build_channel_picker(user_id, post_id),
    )


def store_new_post(user_id, state):
    post_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    while post_id in posts_db:
        post_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

    post = {
        "id": post_id,
        "owner_id": int(user_id),
        "created_by": int(user_id),
        "text": state["text"],
        "entities": state.get("entities", []),
        "photo_id": state.get("photo_id"),
        "created_at": utc_now(),
        "published": [],
    }
    posts_db[post_id] = post
    save_json(POSTS_FILE, posts_db)
    return post_id


def complete_regular_post(user_id, state):
    post_id = store_new_post(user_id, state)
    clear_state(user_id)
    safe_send_text(user_id, f"✅ Post created. **Post ID:** `{post_id}`\n\nNow choose where to publish it.", reply_markup=None)
    show_channel_picker(user_id, post_id)


def complete_admin_post(admin_id, state):
    post_id = store_new_post(admin_id, state)
    post = posts_db[post_id]
    target_chat = state["target_chat"]
    target_title = state.get("target_title", str(target_chat))
    clear_state(admin_id)
    try:
        sent = send_post_to_channel(target_chat, post)
        post["published"].append({
            "chat_id": int(target_chat),
            "message_id": int(sent.message_id),
            "title": target_title,
            "channel_owner": int(state.get("target_owner", 0)),
        })
        save_json(POSTS_FILE, posts_db)
        safe_send_text(admin_id, f"✅ Admin post published to **{target_title}**.\nPost ID: `{post_id}`", reply_markup=admin_keyboard())
    except Exception as exc:
        log.exception("Admin publish failed")
        safe_send_text(admin_id, f"❌ Could not publish to {target_title}. Telegram says: `{str(exc)[:700]}`", reply_markup=admin_keyboard())


def start_post_flow(user_id, admin_target=None):
    state = {"step": "post_text"}
    if admin_target:
        state.update(admin_target)
        state["step"] = "admin_post_text"
    set_state(user_id, state)
    safe_send_text(
        user_id,
        "Send your post text now. Normal emojis will be converted to custom emojis, and the post footer will be added automatically.\n\nUse /cancel to stop.",
    )


def ask_media_choice(user_id, state):
    if state["step"] == "admin_media_choice":
        yes_data, no_data = "adminmedia:yes", "adminmedia:no"
    else:
        yes_data, no_data = "media:yes", "media:no"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🖼 Add Photo", callback_data=yes_data),
        types.InlineKeyboardButton("📝 Text Only", callback_data=no_data),
    )
    bot.send_message(user_id, "Would you like to attach a photo to this post?", reply_markup=markup)


def publish_one_for_user(user_id, post_id, channel_id):
    post = posts_db.get(post_id)
    if not post:
        return False, "Post not found."
    own_channels = {str(ch.get("id")): ch for ch in get_user_channels(user_id)}
    channel = own_channels.get(str(channel_id))
    if not channel:
        return False, "That channel does not belong to your account."
    try:
        sent = send_post_to_channel(channel_id, post)
        post.setdefault("published", []).append({
            "chat_id": int(channel_id),
            "message_id": int(sent.message_id),
            "title": channel.get("title", str(channel_id)),
            "channel_owner": int(user_id),
        })
        save_json(POSTS_FILE, posts_db)
        return True, channel.get("title", str(channel_id))
    except Exception as exc:
        log.exception("Channel publish failed for %s", channel_id)
        return False, str(exc)[:250]


# ========================= CHANNEL CONNECTION =========================

def forwarded_channel(message):
    source = getattr(message, "forward_from_chat", None)
    if source:
        return source
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "type", None) == "channel":
        return getattr(origin, "chat", None)
    return None


def connect_channel_from_message(message):
    user_id = int(message.from_user.id)
    channel_ref = None
    forwarded = forwarded_channel(message)
    if forwarded:
        channel_ref = forwarded.id
    elif message.text:
        raw = message.text.strip()
        match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,})", raw)
        if match:
            raw = "@" + match.group(1)
        channel_ref = raw

    if not channel_ref:
        safe_send_text(user_id, "Send a public channel username like `@mychannel`, a channel ID, or forward a message from your channel. The bot must already be an administrator there.")
        return

    try:
        chat = bot.get_chat(channel_ref)
        if chat.type not in ("channel", "supergroup"):
            safe_send_text(user_id, "This is not a channel/supergroup. Please send a channel username or forward a channel post.")
            return
        member = bot.get_chat_member(chat.id, bot.get_me().id)
        if member.status not in ("administrator", "creator"):
            safe_send_text(user_id, "❌ The bot is not an administrator in that channel. Add it as an admin with post/edit/delete permissions, then try again.")
            return
        missing = []
        if getattr(member, "can_post_messages", True) is False:
            missing.append("Post Messages")
        if getattr(member, "can_edit_messages", True) is False:
            missing.append("Edit Messages")
        if getattr(member, "can_delete_messages", True) is False:
            missing.append("Delete Messages")
        if missing:
            safe_send_text(user_id, "❌ The bot is missing these administrator permissions: " + ", ".join(missing) + ". Enable them in channel admins and connect again.")
            return

        items = get_user_channels(user_id)
        if any(str(item.get("id")) == str(chat.id) for item in items):
            safe_send_text(user_id, f"ℹ️ **{chat.title}** is already connected.", reply_markup=main_keyboard(user_id))
            return

        items.append({
            "id": int(chat.id),
            "title": chat.title or chat.username or str(chat.id),
            "username": chat.username,
            "type": chat.type,
            "added_at": utc_now(),
        })
        save_user_channels(user_id, items)
        safe_send_text(
            user_id,
            f"✅ **Channel connected:** {chat.title}\n\nOnly your account can see and use this channel. Other users cannot access your channel list.",
            reply_markup=main_keyboard(user_id),
        )
    except Exception as exc:
        log.exception("Connect channel failed")
        safe_send_text(user_id, f"❌ Could not connect this channel. Check the username/ID and make sure the bot is an administrator.\n\n`{str(exc)[:500]}`")


# ========================= POST MANAGEMENT =========================

def show_my_channels(user_id):
    items = get_user_channels(user_id)
    if not items:
        safe_send_text(user_id, "You have not connected any channel yet.", reply_markup=main_keyboard(user_id))
        return
    lines = [f"{index}. **{ch.get('title', 'Channel')}** — `{ch.get('id')}`" for index, ch in enumerate(items, 1)]
    safe_send_text(user_id, "📡 **Your connected channels**\n\n" + "\n".join(lines), reply_markup=main_keyboard(user_id))


def show_my_posts(user_id):
    items = [p for p in posts_db.values() if int(p.get("owner_id", -1)) == int(user_id)]
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    if not items:
        safe_send_text(user_id, "You have not created any posts yet.", reply_markup=main_keyboard(user_id))
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for post in items[:25]:
        label = shorten(post.get("text", "(empty)").splitlines()[0], 42)
        markup.add(types.InlineKeyboardButton(f"{post['id']} · {label}", callback_data=f"post:open:{post['id']}"))
    markup.add(types.InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:home"))
    bot.send_message(user_id, f"🗂 *Your posts* ({len(items)} total)\nChoose a post to edit or delete.", reply_markup=markup, parse_mode="Markdown")


def show_post_actions(user_id, post_id):
    post = posts_db.get(post_id)
    if not post or int(post.get("owner_id", -1)) != int(user_id):
        safe_send_text(user_id, "Post not found or you do not own this post.", reply_markup=main_keyboard(user_id))
        return
    destinations = len(post.get("published", []))
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✏️ Edit Post", callback_data=f"post:edit:{post_id}"),
        types.InlineKeyboardButton("🗑 Delete Post", callback_data=f"post:delete:{post_id}"),
    )
    markup.add(types.InlineKeyboardButton("⬅️ My Posts", callback_data="menu:posts"))
    safe_send_text(user_id, f"**Post ID:** `{post_id}`\n**Published copies:** {destinations}\n\nChoose Edit or Delete.", reply_markup=markup)


def edit_published_copies(post):
    successes, failures = 0, 0
    entities = entities_for_api(post.get("entities", []))
    for item in post.get("published", []):
        try:
            if post.get("photo_id"):
                bot.edit_message_caption(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    caption=post["text"],
                    caption_entities=entities,
                )
            else:
                bot.edit_message_text(
                    post["text"],
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    entities=entities,
                    disable_web_page_preview=False,
                )
            successes += 1
        except Exception as exc:
            try:
                if post.get("photo_id"):
                    bot.edit_message_caption(
                        chat_id=item["chat_id"], message_id=item["message_id"], caption=post["text"]
                    )
                else:
                    bot.edit_message_text(
                        post["text"], chat_id=item["chat_id"], message_id=item["message_id"],
                        disable_web_page_preview=False,
                    )
                successes += 1
            except Exception as fallback_exc:
                failures += 1
                log.warning("Could not edit channel copy %s/%s: %s / %s", item.get("chat_id"), item.get("message_id"), exc, fallback_exc)
    return successes, failures


def delete_published_copies(post):
    successes, failures = 0, 0
    for item in post.get("published", []):
        try:
            bot.delete_message(item["chat_id"], item["message_id"])
            successes += 1
        except Exception as exc:
            failures += 1
            log.warning("Could not delete channel copy %s/%s: %s", item.get("chat_id"), item.get("message_id"), exc)
    return successes, failures


def help_text_and_entities():
    lines = [
        "Venzoo Premium Emoji Bot — Help",
        "",
        "1. Connect Channel: add the bot as administrator, then send @username/channel ID or forward a channel post.",
        "2. Create & Publish: write a post, optionally attach a photo, select your connected channels and publish.",
        "3. My Posts: edit a post; the bot will update its published channel copies. Delete removes its published copies.",
        "4. Channel privacy: each user sees only the channels they connected. Admin can view all users/channels and publish to any connected channel.",
        "5. Every published post automatically ends with the bot signature.",
        "",
        f"Custom emoji collection: {len(ALL_EMOJI_IDS)} unique IDs",
        "Preview → custom emoji ID:",
    ]
    text = "\n".join(lines) + "\n"
    entities = []
    for index, emoji_id in enumerate(ALL_EMOJI_IDS, 1):
        line = f"{index}. ✨ → {emoji_id}\n"
        emoji_offset = utf16_len(text) + utf16_len(f"{index}. ")
        entities.append({
            "type": "custom_emoji",
            "offset": emoji_offset,
            "length": utf16_len("✨"),
            "custom_emoji_id": emoji_id,
        })
        text += line
    return text, entities


def show_help(chat_id):
    text, entities = help_text_and_entities()
    try:
        bot.send_message(chat_id, text, entities=entities, reply_markup=main_keyboard(chat_id))
    except Exception as exc:
        log.warning("Custom emoji help rendering failed; sending ID list without custom entities: %s", exc)
        bot.send_message(chat_id, text, reply_markup=main_keyboard(chat_id))


# ========================= ADMIN FUNCTIONS =========================

def is_admin(user_id):
    return int(user_id) == ADMIN_ID


def admin_show_users(chat_id):
    if not users_db:
        safe_send_text(chat_id, "No users registered yet.", reply_markup=admin_keyboard())
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    all_users = sorted(users_db.values(), key=lambda x: x.get("last_seen", ""), reverse=True)
    for user in all_users[:50]:
        uid = int(user.get("id"))
        name = user.get("first_name") or "User"
        username = f"@{user['username']}" if user.get("username") else "No username"
        markup.add(types.InlineKeyboardButton(f"{name} · {username} · {uid}", callback_data=f"admin:userinfo:{uid}"))
    markup.add(types.InlineKeyboardButton("⬅️ Admin Panel", callback_data="menu:admin"))
    bot.send_message(chat_id, f"👥 *Registered users: {len(users_db)}*\nShowing up to 50 recent users.", reply_markup=markup, parse_mode="Markdown")


def admin_show_all_channels(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    total = 0
    for owner_id, items in channels_db.items():
        for channel in items if isinstance(items, list) else []:
            total += 1
            title = shorten(channel.get("title", "Channel"), 27)
            markup.add(types.InlineKeyboardButton(
                f"{title} · owner {owner_id}",
                callback_data=f"admin:userinfo:{owner_id}",
            ))
    markup.add(types.InlineKeyboardButton("⬅️ Admin Panel", callback_data="menu:admin"))
    bot.send_message(chat_id, f"📡 *Connected channels: {total}*\nEach entry opens its owner’s details.", reply_markup=markup, parse_mode="Markdown")


def admin_user_info(chat_id, owner_id):
    user = users_db.get(str(owner_id), {})
    channels = get_user_channels(owner_id)
    name = user.get("first_name", "Unknown")
    username = "@" + user["username"] if user.get("username") else "No username"
    lines = [f"**User:** {name}", f"**Username:** {username}", f"**Telegram ID:** `{owner_id}`", f"**Connected channels:** {len(channels)}", ""]
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in channels:
        title = shorten(channel.get("title", "Channel"), 38)
        markup.add(types.InlineKeyboardButton(
            f"📨 Post to {title}", callback_data=f"admin:pickchannel:{owner_id}:{channel.get('id')}"
        ))
    markup.add(types.InlineKeyboardButton("⬅️ Users", callback_data="admin:users"))
    safe_send_text(chat_id, "\n".join(lines), reply_markup=markup)


def admin_start_post_for_channel(admin_id, owner_id, channel_id):
    channel = next((ch for ch in get_user_channels(owner_id) if str(ch.get("id")) == str(channel_id)), None)
    if not channel:
        safe_send_text(admin_id, "Channel not found for that user.", reply_markup=admin_keyboard())
        return
    start_post_flow(admin_id, {
        "target_owner": int(owner_id),
        "target_chat": int(channel_id),
        "target_title": channel.get("title", str(channel_id)),
    })


def admin_broadcast(admin_id, message):
    success, failed = 0, 0
    for user_key in list(users_db.keys()):
        try:
            bot.copy_message(int(user_key), message.chat.id, message.message_id)
            success += 1
        except Exception as exc:
            failed += 1
            log.info("Broadcast failed for %s: %s", user_key, exc)
    clear_state(admin_id)
    safe_send_text(admin_id, f"📢 **Broadcast finished**\n✅ Sent: `{success}`\n❌ Failed: `{failed}`", reply_markup=admin_keyboard())


# ========================= MESSAGE HANDLERS =========================

@bot.message_handler(commands=["start"])
def start_command(message):
    if not private_only_message(message):
        return
    register_user(message.from_user)
    clear_state(message.from_user.id)
    greeting = (
        "👑 **Welcome to Venzoo Premium Emoji Bot!**\n\n"
        "Create emoji-enhanced posts and publish them to your connected Telegram channels. "
        "You can edit or delete your published posts from this bot.\n\n"
        "Connect a channel first, and make sure the bot is an administrator with posting permission."
    )
    safe_send_text(message.chat.id, greeting, reply_markup=main_keyboard(message.from_user.id))


@bot.message_handler(commands=["help"])
def help_command(message):
    if private_only_message(message):
        register_user(message.from_user)
        show_help(message.chat.id)


@bot.message_handler(commands=["cancel"])
def cancel_command(message):
    if private_only_message(message):
        clear_state(message.from_user.id)
        safe_send_text(message.chat.id, "Cancelled. Main menu:", reply_markup=main_keyboard(message.from_user.id))


@bot.message_handler(func=lambda message: True, content_types=[
    "text", "photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker"
])
def private_message_router(message):
    if not private_only_message(message):
        return
    register_user(message.from_user)
    user_id = int(message.from_user.id)
    state = get_state(user_id)
    if not state:
        if message.text:
            safe_send_text(user_id, "Choose an option from the menu or type /help.", reply_markup=main_keyboard(user_id))
        return

    step = state.get("step")

    if step == "connect_channel":
        connect_channel_from_message(message)
        clear_state(user_id)
        return

    if step in ("post_text", "admin_post_text"):
        if not message.text or message.text.startswith("/"):
            safe_send_text(user_id, "Please send the post text as a normal text message, or use /cancel.")
            return
        text_value, entities = process_text_and_entities(message)
        if utf16_len(text_value) > 4096:
            safe_send_text(user_id, "The post is too long. Telegram text posts support up to 4096 characters. Please send a shorter post.")
            return
        state["text"] = text_value
        state["entities"] = entities
        state["step"] = "admin_media_choice" if step == "admin_post_text" else "media_choice"
        set_state(user_id, state)
        ask_media_choice(user_id, state)
        return

    if step == "post_photo":
        if message.content_type != "photo":
            safe_send_text(user_id, "Please upload a photo, or use /cancel.")
            return
        if utf16_len(state.get("text", "")) > 1024:
            state["step"] = "admin_post_text" if state.get("target_chat") else "post_text"
            set_state(user_id, state)
            safe_send_text(user_id, "This text is too long for a Telegram photo caption (1024 characters). Please send a shorter version, then choose Text Only or Photo again.")
            return
        state["photo_id"] = message.photo[-1].file_id
        if state.get("target_chat"):
            complete_admin_post(user_id, state)
        else:
            complete_regular_post(user_id, state)
        return

    if step == "edit_text":
        if not message.text or message.text.startswith("/"):
            safe_send_text(user_id, "Please send the replacement post text, or use /cancel.")
            return
        post_id = state.get("post_id")
        post = posts_db.get(post_id)
        if not post or (int(post.get("owner_id", -1)) != user_id and not is_admin(user_id)):
            clear_state(user_id)
            safe_send_text(user_id, "Post not found or access denied.", reply_markup=main_keyboard(user_id))
            return
        new_text, new_entities = process_text_and_entities(message)
        limit = 1024 if post.get("photo_id") else 4096
        if utf16_len(new_text) > limit:
            safe_send_text(user_id, f"This post exceeds Telegram's {limit}-character {'caption' if limit == 1024 else 'message'} limit. Please send a shorter version.")
            return
        post["text"] = new_text
        post["entities"] = new_entities
        save_json(POSTS_FILE, posts_db)
        ok, failed = edit_published_copies(post)
        clear_state(user_id)
        safe_send_text(user_id, f"✅ Post saved.\nChannel copies updated: {ok}\nCould not update: {failed}", reply_markup=main_keyboard(user_id))
        return

    if step == "broadcast":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        admin_broadcast(user_id, message)
        return

    safe_send_text(user_id, "I could not match that action. Use /cancel and start again.", reply_markup=main_keyboard(user_id))


# ========================= CALLBACK HANDLER =========================

@bot.callback_query_handler(func=lambda call: True)
def callback_router(call):
    try:
        if not private_only_call(call):
            bot.answer_callback_query(call.id, "Please use the bot in a private chat.", show_alert=True)
            return
        user_id = int(call.from_user.id)
        register_user(call.from_user)
        data = call.data or ""
        bot.answer_callback_query(call.id)

        if data == "menu:home":
            clear_state(user_id)
            safe_send_text(user_id, "Main menu:", reply_markup=main_keyboard(user_id))
            return
        if data == "menu:help":
            clear_state(user_id)
            show_help(user_id)
            return
        if data == "menu:create":
            if not get_user_channels(user_id):
                safe_send_text(user_id, "Connect at least one channel before creating a post.", reply_markup=main_keyboard(user_id))
                return
            start_post_flow(user_id)
            return
        if data == "menu:connect":
            set_state(user_id, {"step": "connect_channel"})
            safe_send_text(user_id, "Send a channel username like `@mychannel`, a channel ID, or forward a post from your channel. The bot must be an administrator there.")
            return
        if data == "menu:channels":
            clear_state(user_id)
            show_my_channels(user_id)
            return
        if data == "menu:posts":
            clear_state(user_id)
            show_my_posts(user_id)
            return
        if data == "menu:admin":
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "Unauthorized.", show_alert=True)
                return
            clear_state(user_id)
            safe_send_text(user_id, "👑 **Venzoo Admin Panel**", reply_markup=admin_keyboard())
            return

        if data in ("media:yes", "media:no", "adminmedia:yes", "adminmedia:no"):
            state = get_state(user_id)
            if not state or state.get("step") not in ("media_choice", "admin_media_choice"):
                safe_send_text(user_id, "This post session expired. Start again from the menu.", reply_markup=main_keyboard(user_id))
                return
            if data.endswith(":yes"):
                state["step"] = "post_photo"
                set_state(user_id, state)
                safe_send_text(user_id, "Now upload the photo for this post. Use /cancel to stop.")
            else:
                if state.get("target_chat"):
                    complete_admin_post(user_id, state)
                else:
                    complete_regular_post(user_id, state)
            return

        if data.startswith("channel:toggle:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                return
            post_id, channel_id = parts[2], parts[3]
            state = get_state(user_id)
            if not state or state.get("step") != "publish_pick" or state.get("post_id") != post_id:
                bot.answer_callback_query(call.id, "This selection expired. Start again.", show_alert=True)
                return
            allowed = {str(ch.get("id")) for ch in get_user_channels(user_id)}
            if channel_id not in allowed:
                bot.answer_callback_query(call.id, "That channel does not belong to your account.", show_alert=True)
                return
            selected = set(str(item) for item in state.get("selected", []))
            if channel_id in selected:
                selected.remove(channel_id)
            else:
                selected.add(channel_id)
            state["selected"] = list(selected)
            set_state(user_id, state)
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_channel_picker(user_id, post_id, selected),
            )
            return

        if data.startswith("channel:publish:"):
            post_id = data.split(":", 2)[2]
            state = get_state(user_id)
            if not state or state.get("step") != "publish_pick" or state.get("post_id") != post_id:
                safe_send_text(user_id, "This publish session expired.", reply_markup=main_keyboard(user_id))
                return
            selected = state.get("selected", [])
            if not selected:
                bot.answer_callback_query(call.id, "Select at least one channel first.", show_alert=True)
                return
            results = []
            for channel_id in selected:
                success, detail = publish_one_for_user(user_id, post_id, channel_id)
                results.append((success, detail))
            clear_state(user_id)
            sent = sum(1 for success, _ in results if success)
            failed = len(results) - sent
            report = "\n".join(("✅ " if success else "❌ ") + str(detail) for success, detail in results)
            safe_send_text(user_id, f"**Publishing finished**\nSuccessful: {sent}\nFailed: {failed}\n\n{report}", reply_markup=main_keyboard(user_id))
            return

        if data.startswith("post:open:"):
            show_post_actions(user_id, data.split(":", 2)[2])
            return
        if data.startswith("post:edit:"):
            post_id = data.split(":", 2)[2]
            post = posts_db.get(post_id)
            if not post or int(post.get("owner_id", -1)) != user_id:
                safe_send_text(user_id, "Post not found or access denied.")
                return
            set_state(user_id, {"step": "edit_text", "post_id": post_id})
            safe_send_text(user_id, "Send the complete replacement text. The bot signature will be added automatically. Existing photo (if any) will stay attached.\n\nUse /cancel to stop.")
            return
        if data.startswith("post:delete:"):
            post_id = data.split(":", 2)[2]
            post = posts_db.get(post_id)
            if not post or int(post.get("owner_id", -1)) != user_id:
                safe_send_text(user_id, "Post not found or access denied.")
                return
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("✅ Confirm Delete", callback_data=f"post:confirmdelete:{post_id}"),
                types.InlineKeyboardButton("Cancel", callback_data=f"post:open:{post_id}"),
            )
            safe_send_text(user_id, "Delete this post and its published channel copies? This cannot be undone.", reply_markup=markup)
            return
        if data.startswith("post:confirmdelete:"):
            post_id = data.split(":", 2)[2]
            post = posts_db.get(post_id)
            if not post or int(post.get("owner_id", -1)) != user_id:
                safe_send_text(user_id, "Post not found or access denied.")
                return
            ok, failed = delete_published_copies(post)
            posts_db.pop(post_id, None)
            save_json(POSTS_FILE, posts_db)
            safe_send_text(user_id, f"🗑 Post removed from storage.\nChannel messages deleted: {ok}\nCould not delete: {failed}", reply_markup=main_keyboard(user_id))
            return

        # Admin-only callbacks
        if data.startswith("admin:"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "Unauthorized access.", show_alert=True)
                return
            if data == "admin:stats":
                total_channels = sum(len(items) for items in channels_db.values() if isinstance(items, list))
                total_posts = len(posts_db)
                safe_send_text(user_id, f"📊 **Bot Status**\nUsers: `{len(users_db)}`\nConnected channels: `{total_channels}`\nPosts stored: `{total_posts}`\nCustom emoji IDs: `{len(ALL_EMOJI_IDS)}`", reply_markup=admin_keyboard())
                return
            if data == "admin:users":
                admin_show_users(user_id)
                return
            if data == "admin:channels":
                admin_show_all_channels(user_id)
                return
            if data == "admin:broadcast":
                set_state(user_id, {"step": "broadcast"})
                safe_send_text(user_id, "Send the message/media you want to broadcast to all registered bot users. Use /cancel to stop.")
                return
            if data == "admin:post":
                markup = types.InlineKeyboardMarkup(row_width=1)
                owners = [owner for owner, items in channels_db.items() if isinstance(items, list) and items]
                owners.sort(key=lambda item: item)
                for owner in owners[:50]:
                    user = users_db.get(str(owner), {})
                    name = user.get("first_name") or "User"
                    username = "@" + user["username"] if user.get("username") else str(owner)
                    markup.add(types.InlineKeyboardButton(f"{name} · {username}", callback_data=f"admin:userinfo:{owner}"))
                markup.add(types.InlineKeyboardButton("⬅️ Admin Panel", callback_data="menu:admin"))
                bot.send_message(user_id, "Choose a user to view their connected channels, then select a channel to publish.", reply_markup=markup)
                return
            if data.startswith("admin:userinfo:"):
                owner_id = data.split(":", 2)[2]
                admin_user_info(user_id, owner_id)
                return
            if data.startswith("admin:pickchannel:"):
                parts = data.split(":", 3)
                if len(parts) != 4:
                    return
                admin_start_post_for_channel(user_id, int(parts[2]), parts[3])
                return

        bot.answer_callback_query(call.id, "Unknown action.", show_alert=False)
    except Exception as exc:
        log.exception("Callback handler error")
        try:
            bot.answer_callback_query(call.id, "Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


# ========================= FLASK / RENDER HEALTH ENDPOINTS =========================

@app.get("/")
def home():
    return (
        f"<html><head><title>@{BOT_USERNAME} status</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'></head>"
        "<body style='margin:0;background:#0b1020;color:#eef2ff;font:16px Arial,sans-serif;"
        "display:grid;place-items:center;min-height:100vh;text-align:center'>"
        "<main style='padding:32px;border:1px solid #29334f;border-radius:18px;background:#111a2e'>"
        f"<div style='font-size:34px;margin-bottom:12px'>🟢</div><h1 style='margin:0 0 10px'>@{BOT_USERNAME} is running</h1>"
        "<p style='color:#aab6d3;margin:0'>Telegram bot service is online.</p>"
        "<p style='margin:20px 0 0'><a style='color:#9dc1ff' href='/health'>Health check</a></p>"
        "</main></body></html>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/health")
def health():
    payload = {
        "ok": bool(BOT_STARTED),
        "service": f"@{BOT_USERNAME}",
        "bot_polling_started": BOT_STARTED,
        "started_at": BOT_START_TIME,
        "last_error": BOT_LAST_ERROR,
    }
    return jsonify(payload), (200 if BOT_STARTED else 503)


def bot_polling_loop():
    global BOT_STARTED, BOT_LAST_ERROR
    while True:
        try:
            log.info("Checking Telegram bot connection for @%s", BOT_USERNAME)
            bot.get_me()  # Validate the token/API connection before reporting healthy.
            BOT_STARTED = True
            BOT_LAST_ERROR = None
            log.info("Telegram connection established for @%s", BOT_USERNAME)
            bot.infinity_polling(
                timeout=25,
                long_polling_timeout=25,
                skip_pending=True,
                allowed_updates=["message", "callback_query"],
            )
            BOT_STARTED = False
        except Exception as exc:
            BOT_STARTED = False
            BOT_LAST_ERROR = str(exc)[:500]
            log.exception("Telegram polling stopped; retrying in 5 seconds")
            time.sleep(5)


def start_polling_thread():
    global BOT_THREAD
    if BOT_THREAD and BOT_THREAD.is_alive():
        return
    BOT_THREAD = threading.Thread(target=bot_polling_loop, name="telegram-polling", daemon=True)
    BOT_THREAD.start()


# Gunicorn imports app:app. Start exactly one polling thread per web worker.
if os.getenv("DISABLE_BOT_THREAD", "0") != "1":
    start_polling_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

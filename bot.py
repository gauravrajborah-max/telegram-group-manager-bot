import os
import logging
from telegram import Update, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta, timezone
# Import firestore package directly for SERVER_TIMESTAMP
from firebase_admin import initialize_app, firestore, credentials 

# --- Configuration and Initialization ---

# 1. Environment Variables (Required for security and hosting)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0)) # Your personal Telegram User ID
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8080))
# Firestore credentials will be loaded from a JSON string in an environment variable

# 2. Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 3. Firestore Database Setup
try:
    # Load Firestore Service Account Key from environment variable
    firebase_config_json = os.environ.get("FIREBASE_CREDENTIALS")
    if firebase_config_json:
        import json
        service_account_info = json.loads(firebase_config_json)
        cred = credentials.Certificate(service_account_info)
        initialize_app(cred)
        db = firestore.client()
        logger.info("Firestore connected successfully.")
    else:
        logger.error("FIREBASE_CREDENTIALS environment variable not set. Persistence will fail.")
        db = None
except Exception as e:
    logger.error(f"Error initializing Firestore: {e}")
    db = None

# --- Helper Functions (Checks and Database Interactions) ---

def is_owner(user_id: int) -> bool:
    """Checks if the user is the bot owner."""
    return user_id == OWNER_ID

async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user is an admin or the bot owner."""
    if update.effective_chat.type not in ["group", "supergroup", "channel"]:
        # Allow owner commands outside of groups for testing/private chat control
        if is_owner(update.effective_user.id):
            return True
        await update.message.reply_text("This command only works in groups/channels.")
        return False

    user_id = update.effective_user.id
    if is_owner(user_id):
        return True

    member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
    if member.status in ["creator", "administrator"]:
        return True
    
    await update.message.reply_text("You must be an administrator or the bot owner to use this command.")
    return False

def get_user_ref(group_id: int, user_id: int):
    """Returns the Firestore document reference for a user in a group."""
    if not db:
        return None
    return db.collection("groups").document(str(group_id)).collection("users").document(str(user_id))

def get_filter_ref(group_id: int, keyword: str):
    """Returns the Firestore document reference for a filter keyword in a group."""
    if not db:
        return None
    # Use lowercase and stripped keyword for document ID for consistency
    safe_keyword = keyword.strip().lower().replace(" ", "_")
    return db.collection("groups").document(str(group_id)).collection("filters").document(safe_keyword)

def get_banned_word_ref(group_id: int, word: str):
    """Returns the Firestore document reference for a banned word."""
    if not db:
        return None
    safe_word = word.strip().lower().replace(" ", "_")
    # Using a subcollection named 'banned_words' inside the group document
    return db.collection("groups").document(str(group_id)).collection("banned_words").document(safe_word)

async def get_warn_count(group_id: int, user_id: int) -> int:
    """Fetches the current warning count for a user."""
    ref = get_user_ref(group_id, user_id)
    if not ref: return 0
    try:
        doc = ref.get()
        return doc.to_dict().get("warnings", 0) if doc.exists else 0
    except Exception as e:
        logger.error(f"Error reading warn count: {e}")
        return 0

async def update_warn_count(group_id: int, user_id: int, change: int):
    """Adds or removes warnings for a user."""
    ref = get_user_ref(group_id, user_id)
    if not ref: return
    try:
        current_warnings = await get_warn_count(group_id, user_id)
        new_warnings = max(0, current_warnings + change)
        
        ref.set({"warnings": new_warnings}, merge=True)
        return new_warnings
    except Exception as e:
        logger.error(f"Error updating warn count: {e}")
        return current_warnings

# --- Utility Commands ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message with bot information and tracks the chat for broadcasting."""
    await update.message.reply_text(
        "👋 This is a group moderation bot made with ♥ by @Tota_ton (Gaurav). "
        "You can contact the owner through this bot. Just type your message—"
        "\n\nThank you🦚"
    )

    # --- Track chat for broadcast functionality in the global 'broadcast_chats' collection ---
    if db and update.effective_chat.type in ["group", "supergroup", "private"]:
        chat_id = str(update.effective_chat.id)
        chat_ref = db.collection("broadcast_chats").document(chat_id)
        
        chat_data = {
            "chat_id": chat_id,
            "chat_type": update.effective_chat.type,
            "title": update.effective_chat.title or update.effective_user.full_name, 
            "last_active": firestore.SERVER_TIMESTAMP,
        }
        
        try:
            chat_ref.set(chat_data, merge=True)
            logger.info(f"Chat {chat_id} added/updated for broadcast list.")
        except Exception as e:
            logger.error(f"Failed to add chat to broadcast list: {e}")


async def get_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the user ID of the sender or a replied user."""
    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
    else:
        user = update.effective_user
    
    await update.message.reply_text(
        f"The Telegram User ID for **{user.first_name}** is:\n`{user.id}`\n\nChat ID:\n`{update.effective_chat.id}`",
        parse_mode="Markdown"
    )

# --- Countdown Commands ---

async def set_countdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets a group-wide countdown to a specific date."""
    if not await check_admin(update, context): return
    if not db: 
        await update.message.reply_text("Database not available.")
        return

    # Expected format: /set_countdown DD/MM/YYYY Name of the event
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/set_countdown DD/MM/YYYY <Name of the event>` (e.g., `/set_countdown 31/12/2025 New Year's Eve`)", parse_mode="Markdown")
        return

    date_str = context.args[0]
    countdown_name = " ".join(context.args[1:])
    group_id = update.effective_chat.id
    
    try:
        # Note: Timezone is set to UTC for safe comparison
        target_date = datetime.strptime(date_str, "%d/%m/%Y").replace(tzinfo=timezone.utc)
        if target_date < datetime.now(timezone.utc):
            await update.message.reply_text("The target date must be in the future.")
            return

        # Store countdown in group_settings document
        ref = db.collection("group_settings").document(str(group_id))
        ref.set({
            "countdown_name": countdown_name,
            "target_date_iso": target_date.isoformat(),
            "target_date_human": date_str
        }, merge=True)

        await update.message.reply_text(
            f"🚀 Countdown for **{countdown_name}** set successfully!\nTarget date: `{date_str}`. Use `/check_countdown` to see the remaining time.",
            parse_mode="Markdown"
        )

    except ValueError:
        await update.message.reply_text("Invalid date format. Please use DD/MM/YYYY.")
    except Exception as e:
        logger.error(f"Error setting countdown: {e}")
        await update.message.reply_text(f"An unexpected error occurred: {e}")


async def check_countdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks and displays the remaining time for the group-wide countdown."""
    if not db: 
        await update.message.reply_text("Database not available.")
        return

    group_id = update.effective_chat.id
    # Countdown is stored in the group_settings document
    ref = db.collection("group_settings").document(str(group_id))

    try:
        doc = ref.get().to_dict()
        if not doc or "target_date_iso" not in doc:
            await update.message.reply_text("No active countdown set for this chat. Use `/set_countdown` to start one.")
            return

        target_date_iso = doc["target_date_iso"]
        countdown_name = doc["countdown_name"]
        target_date_human = doc["target_date_human"]
        
        target_date = datetime.fromisoformat(target_date_iso).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        
        remaining_time = target_date - now

        if remaining_time.total_seconds() <= 0:
            # Clear old countdown
            ref.update({
                "countdown_name": firestore.DELETE_FIELD,
                "target_date_iso": firestore.DELETE_FIELD,
                "target_date_human": firestore.DELETE_FIELD
            })
            await update.message.reply_text(f"🎉 **{countdown_name}** is here! The countdown has finished.")
            return
        
        # Format remaining time
        days = remaining_time.days
        # calculate remaining hours, minutes, seconds from the remaining_time.seconds attribute
        hours = remaining_time.seconds // 3600
        minutes = (remaining_time.seconds % 3600) // 60
        seconds = remaining_time.seconds % 60
        
        await update.message.reply_text(
            f"⏳ **{countdown_name}**\n"
            f"Target: `{target_date_human}`\n\n"
            f"**Time Remaining:**\n"
            f"• `{days}` days\n"
            f"• `{hours}` hours\n"
            f"• `{minutes}` minutes\n"
            f"• `{seconds}` seconds",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error checking countdown: {e}")
        await update.message.reply_text(f"An error occurred while checking the countdown: {e}")

# --- Lock/Unlock Commands ---

async def handle_lock_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE, lock: bool) -> None:
    """Handles /lock and /unlock commands for various features."""
    if not await check_admin(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/lock <feature>` or `/unlock <feature>`. Features: `all`, `text`, `stickers`, `media`, `images`, `audio`.", parse_mode="Markdown")
        return

    feature_arg = context.args[0].lower()
    group_id = update.effective_chat.id
    
    # --- New Logic: Use a dictionary to hold the new permissions based on lock status ---
    
    # Start with all permissions enabled (as a fallback/default)
    new_perms = {
        "can_send_messages": True,
        "can_send_audios": True,
        "can_send_documents": True,
        "can_send_photos": True,
        "can_send_videos": True,
        "can_send_video_notes": True,
        "can_send_voice_notes": True,
        "can_send_polls": True,
        "can_send_other_messages": True,
        "can_add_web_page_previews": True
    }
    
    # Fetch current permissions from the chat to ensure we only change the requested feature
    try:
        current_chat = await context.bot.get_chat(group_id)
        # Use the current chat permissions as the baseline if available
        if current_chat.permissions:
             # NOTE: If any of these properties are None (e.g., if Telegram hasn't set them explicitly), 
             # we default to True, assuming default group member permissions are permissive.
             new_perms = {
                "can_send_messages": current_chat.permissions.can_send_messages if current_chat.permissions.can_send_messages is not None else True,
                "can_send_audios": current_chat.permissions.can_send_audios if current_chat.permissions.can_send_audios is not None else True,
                "can_send_documents": current_chat.permissions.can_send_documents if current_chat.permissions.can_send_documents is not None else True,
                "can_send_photos": current_chat.permissions.can_send_photos if current_chat.permissions.can_send_photos is not None else True,
                "can_send_videos": current_chat.permissions.can_send_videos if current_chat.permissions.can_send_videos is not None else True,
                "can_send_video_notes": current_chat.permissions.can_send_video_notes if current_chat.permissions.can_send_video_notes is not None else True,
                "can_send_voice_notes": current_chat.permissions.can_send_voice_notes if current_chat.permissions.can_send_voice_notes is not None else True,
                "can_send_polls": current_chat.permissions.can_send_polls if current_chat.permissions.can_send_polls is not None else True,
                "can_send_other_messages": current_chat.permissions.can_send_other_messages if current_chat.permissions.can_send_other_messages is not None else True,
                "can_add_web_page_previews": current_chat.permissions.can_add_web_page_previews if current_chat.permissions.can_add_web_page_previews is not None else True,
            }
    except Exception as e:
        logger.warning(f"Could not fetch current chat permissions, defaulting to all True: {e}")
        # If fetching fails, we keep the permissive default `new_perms` defined above.


    # Determine the target value (False for lock, True for unlock)
    target_value = not lock 

    if feature_arg == "all":
        # Lock all means setting can_send_messages to False, which implicitly disables almost everything else.
        new_perms["can_send_messages"] = target_value
        # Also explicitly restrict other messages (stickers/animations)
        new_perms["can_send_other_messages"] = target_value
        # Restrict everything else for a full lock/unlock
        new_perms["can_send_audios"] = target_value
        new_perms["can_send_documents"] = target_value
        new_perms["can_send_photos"] = target_value
        new_perms["can_send_videos"] = target_value
        new_perms["can_send_video_notes"] = target_value
        new_perms["can_send_voice_notes"] = target_value
        new_perms["can_send_polls"] = target_value
        new_perms["can_add_web_page_previews"] = target_value
        
    elif feature_arg == "text":
        new_perms["can_send_messages"] = target_value
    elif feature_arg == "stickers":
        # Stickers, animations (GIFs), and games are usually controlled by this flag
        new_perms["can_send_other_messages"] = target_value
    elif feature_arg == "media":
        # All media types
        new_perms["can_send_photos"] = target_value
        new_perms["can_send_videos"] = target_value
        new_perms["can_send_documents"] = target_value
        new_perms["can_send_audios"] = target_value
        new_perms["can_send_voice_notes"] = target_value
        new_perms["can_send_video_notes"] = target_value
        new_perms["can_send_other_messages"] = target_value # Covers animations/games
    elif feature_arg == "images":
        new_perms["can_send_photos"] = target_value
    elif feature_arg == "audio":
        new_perms["can_send_audios"] = target_value
        new_perms["can_send_voice_notes"] = target_value
    else:
        await update.message.reply_text("Invalid feature. Choose from: `all`, `text`, `stickers`, `media`, `images`, `audio`.", parse_mode="Markdown")
        return
    
    # Recreate the ChatPermissions object with the new dictionary
    final_permissions = ChatPermissions(**new_perms)

    try:
        # Use set_chat_permissions to change default permissions for the group
        await context.bot.set_chat_permissions(chat_id=group_id, permissions=final_permissions)
        action = "LOCKED" if lock else "UNLOCKED"
        await update.message.reply_text(
            f"🔒 Feature **'{feature_arg.upper()}'** successfully **{action}** for general members.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not update chat permissions. Make sure the bot is an admin with 'manage group' permissions. Error: {e}")

async def lock_feature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Locks a specific feature."""
    await handle_lock_unlock(update, context, True)

async def unlock_feature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unlocks a specific feature."""
    await handle_lock_unlock(update, context, False)

# --- Banned Word Commands ---

async def ban_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bans a word from the group and stores it in Firestore."""
    if not await check_admin(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/ban_word <word>` (e.g., `/ban_word spam`)")
        return
    if not db:
        await update.message.reply_text("Database not available.")
        return

    word = context.args[0].lower().strip()
    group_id = update.effective_chat.id
    ref = get_banned_word_ref(group_id, word)

    try:
        ref.set({"word": word, "timestamp": firestore.SERVER_TIMESTAMP})
        await update.message.reply_text(
            f"🚫 Word **'{word}'** has been successfully banned. Messages containing this word will be deleted.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error banning word: {e}")
        await update.message.reply_text(f"Failed to ban word due to a database error: {e}")

async def unban_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbans a word and removes it from Firestore."""
    if not await check_admin(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/unban_word <word>`")
        return
    if not db:
        await update.message.reply_text("Database not available.")
        return

    word = context.args[0].lower().strip()
    group_id = update.effective_chat.id
    ref = get_banned_word_ref(group_id, word)

    try:
        if ref.get().exists:
            ref.delete()
            await update.message.reply_text(
                f"✅ Word **'{word}'** has been successfully unbanned.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"Word **'{word}'** was not found in the banned list.")
    except Exception as e:
        logger.error(f"Error unbanning word: {e}")
        await update.message.reply_text(f"Failed to unban word due to a database error: {e}")

async def handle_banned_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Intercepts messages, checks for banned words, and deletes the message if found."""
    if not db or not update.message.text:
        return
    
    # Do not check/delete messages from admins or owner
    try:
        # Check admin status without reply, as this is a message handler
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        is_user_admin = False
        if is_owner(user_id):
            is_user_admin = True
        elif update.effective_chat.type in ["group", "supergroup", "channel"]:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ["creator", "administrator"]:
                is_user_admin = True

        if is_user_admin:
            return
            
    except Exception as e:
        logger.warning(f"Failed to check admin status in handle_banned_words: {e}")
        # Continue execution, better safe than sorry

    message = update.message
    message_text = message.text.lower()
    group_id = update.effective_chat.id

    # The banned words are stored in a subcollection under the group document
    banned_words_ref = db.collection("groups").document(str(group_id)).collection("banned_words")
    
    try:
        banned_words_snapshot = banned_words_ref.stream()
        
        for doc in banned_words_snapshot:
            word = doc.to_dict().get("word", "")
            
            # Check for banned word existence in the message
            if word and word in message_text:
                # Delete the message
                try:
                    await message.delete()
                    
                    # Notify the user (using the group chat_id)
                    await context.bot.send_message(
                        chat_id=group_id,
                        text=f"⚠️ {message.from_user.mention_html()}, your message was deleted for using a banned word: **{word}**.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete banned word message or send warning: {e}")
                    # If deletion fails (bot lacks permission), just log and move on.
                return # Stop checking after the first match
                
    except Exception as e:
        logger.error(f"Error handling banned words: {e}")


# --- Group Management Commands (Admin/Owner Required) ---

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Warns a user and tracks the count. Kicks/bans on reaching 3 warnings."""
    if not await check_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to warn them.")
        return
    
    target_user = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id

    if is_owner(target_user.id):
        await update.message.reply_text("I cannot warn the owner.")
        return

    new_warnings = await update_warn_count(group_id, target_user.id, 1)
    
    reason = " ".join(context.args) if context.args else "No reason provided."

    if new_warnings >= 3:
        try:
            # Ban the user
            await context.bot.ban_chat_member(group_id, target_user.id)
            await update_warn_count(group_id, target_user.id, -new_warnings) # Reset warnings
            await update.message.reply_text(
                f"🚨 User {target_user.mention_html()} reached 3 warnings and has been **BANNED**.\nReason: {reason}",
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"Could not ban user. Make sure the bot is an admin with 'ban users' permission. Error: {e}")
    else:
        await update.message.reply_text(
            f"⚠️ User {target_user.mention_html()} has been **WARNED** (Warning {new_warnings}/3).\nReason: {reason}",
            parse_mode="HTML"
        )

async def remove_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes one warning from a user."""
    if not await check_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to remove a warning.")
        return

    target_user = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    
    current_warnings = await get_warn_count(group_id, target_user.id)
    if current_warnings > 0:
        new_warnings = await update_warn_count(group_id, target_user.id, -1)
        await update.message.reply_text(
            f"✅ Warning removed from {target_user.mention_html()}. Current warnings: {new_warnings}/3.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"User {target_user.mention_html()} has no active warnings to remove.", parse_mode="HTML")

async def warn_counts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the warning count of a user."""
    # This command is often visible to all, but we will make it admin only for consistency
    if not await check_admin(update, context): return
    
    target_user = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    
    warns = await get_warn_count(update.effective_chat.id, target_user.id)
    await update.message.reply_text(
        f"User {target_user.mention_html()} has **{warns}** active warnings.",
        parse_mode="HTML"
    )

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bans a user from the group."""
    if not await check_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to ban them.")
        return

    target_user = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    reason = " ".join(context.args) if context.args else "No reason provided."

    try:
        await context.bot.ban_chat_member(group_id, target_user.id)
        # Also remove warnings
        await update_warn_count(group_id, target_user.id, -await get_warn_count(group_id, target_user.id))

        await update.message.reply_text(
            f"🔨 User {target_user.mention_html()} has been **BANNED**.\nReason: {reason}",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not ban user. Make sure the bot is an admin with 'ban users' permission. Error: {e}")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbans a user from the group. User ID must be provided."""
    if not await check_admin(update, context): return
    
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Please provide the User ID of the user to unban. Usage: `/unban 123456789`")
        return

    target_id = int(context.args[0])

    try:
        # The unban function requires a user ID and will only work if the user is currently banned.
        # Setting until_date to 0 unbans the user.
        await context.bot.unban_chat_member(update.effective_chat.id, target_id)
        await update.message.reply_text(f"🔓 User with ID `{target_id}` has been **UNBANNED**.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Could not unban user. Error: {e}")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mutes a user for a specified duration (default 1 hour)."""
    if not await check_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to mute them.")
        return

    target_user = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    
    # Default mute duration: 1 hour
    mute_seconds = 3600
    duration_str = "1 hour"

    if context.args and context.args[0].isdigit():
        # User provides minutes
        mute_minutes = int(context.args[0])
        mute_seconds = mute_minutes * 60
        duration_str = f"{mute_minutes} minutes"

    mute_until = datetime.now(timezone.utc) + timedelta(seconds=mute_seconds)

    try:
        # Mute means can_send_messages=False, and all other permissions are also set to False by default
        await context.bot.restrict_chat_member(
            group_id,
            target_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=mute_until
        )
        await update.message.reply_text(
            f"🔇 User {target_user.mention_html()} has been **MUTED** for {duration_str}.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not mute user. Make sure the bot is an admin with 'restrict users' permission. Error: {e}")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unmutes a user by resetting permissions."""
    if not await check_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to unmute them.")
        return

    target_user = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id

    try:
        # Give back all default permissions (unmute)
        # Permissions are all set to True to ensure they can send all types of messages again
        await context.bot.restrict_chat_member(
            group_id,
            target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True, # Covers stickers, animations, games
                can_add_web_page_previews=True,
            )
        )
        await update.message.reply_text(
            f"🔊 User {target_user.mention_html()} has been **UNMUTED**.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not unmute user. Error: {e}")

async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Promotes a replied user to administrator."""
    if not await check_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to promote them.")
        return

    target_user = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id

    try:
        # Promote the user with no optional permissions granted by default (except basic ones)
        await context.bot.promote_chat_member(
            chat_id=group_id,
            user_id=target_user.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False, # Do not allow them to promote others initially
            can_change_info=False
        )
        await update.message.reply_text(
            f"👑 User {target_user.mention_html()} has been **PROMOTED** to a standard administrator.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not promote user. Make sure the bot is the group creator or has the 'Add New Admins' permission. Error: {e}")

# --- Filter Management Commands ---

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Assigns a filter keyword to a replied message (text, sticker, or photo)."""
    if not await check_admin(update, context): return
    
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Please reply to the message (text, sticker, or image) you want to filter and provide a keyword. Usage: `/filter <keyword>`")
        return
    
    if not context.args:
        await update.message.reply_text("You must provide a keyword for the filter. Usage: `/filter <keyword>`")
        return

    keyword = context.args[0]
    group_id = update.effective_chat.id
    ref = db.collection("groups").document(str(group_id)).collection("filters").document(keyword.lower().strip())
    
    filter_data = {"keyword": keyword}

    if reply.text:
        filter_data.update({
            "type": "text",
            "content": reply.text
        })
    elif reply.sticker:
        filter_data.update({
            "type": "sticker",
            "file_id": reply.sticker.file_id
        })
    elif reply.photo:
        # Get the highest resolution photo file_id
        filter_data.update({
            "type": "photo",
            "file_id": reply.photo[-1].file_id 
        })
    else:
        await update.message.reply_text("Unsupported message type. Only text, stickers, and photos can be set as filters.")
        return

    try:
        ref.set(filter_data)
        await update.message.reply_text(
            f"✅ Filter **'{keyword.lower()}'** set! When this word is used, I will reply with the saved {filter_data['type']}.", 
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error setting filter: {e}")
        await update.message.reply_text(f"Failed to set filter due to a database error. Error: {e}")

async def stop_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops (deletes) a filter by keyword."""
    if not await check_admin(update, context): return
    
    if not context.args:
        await update.message.reply_text("You must provide the keyword of the filter to stop. Usage: `/stop <keyword>`")
        return

    keyword = context.args[0]
    group_id = update.effective_chat.id
    ref = db.collection("groups").document(str(group_id)).collection("filters").document(keyword.lower().strip())

    try:
        # Check if the filter exists before deleting
        if ref.get().exists:
            ref.delete()
            await update.message.reply_text(
                f"🛑 Filter **'{keyword.lower()}'** has been stopped and deleted.", 
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"Filter **'{keyword.lower()}'** not found.", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error deleting filter: {e}")
        await update.message.reply_text(f"Failed to stop filter due to a database error. Error: {e}")

async def handle_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks incoming messages against active filters and sends the corresponding content."""
    if not db or not update.message.text:
        return
        
    group_id = update.effective_chat.id
    message_text = update.message.text.lower()

    # Get the reference to the filters collection for this group
    filters_collection_ref = db.collection("groups").document(str(group_id)).collection("filters")

    try:
        # Fetch all filter documents
        filters_snapshot = filters_collection_ref.stream()
        
        for doc in filters_snapshot:
            filter_data = doc.to_dict()
            keyword = filter_data.get("keyword", "").lower()

            # Check if the keyword is present in the message text
            if keyword and keyword in message_text:
                filter_type = filter_data.get("type")
                file_id = filter_data.get("file_id")
                content = filter_data.get("content")
                
                # Reply to the user's message with the filtered content
                if filter_type == "text" and content:
                    await update.message.reply_text(content)
                elif filter_type == "sticker" and file_id:
                    await update.message.reply_sticker(file_id)
                elif filter_type == "photo" and file_id:
                    await update.message.reply_photo(file_id)
                
                # Stop processing after the first matching filter is found
                return

    except Exception as e:
        logger.error(f"Error checking/handling filters: {e}")

# --- Owner-Only Commands ---

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only command to broadcast a message to all tracked chats."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("This command is restricted to the bot owner.")
        return
    
    # We expect the full message content after the command, e.g., /broadcast Hello world!
    if len(context.args) < 1:
        await update.message.reply_text("Usage: `/broadcast <message>`", parse_mode="Markdown")
        return

    if not db:
        await update.message.reply_text("Database not available. Cannot broadcast.")
        return

    # Extract the message content (skipping the /broadcast command and the space)
    # update.message.text is like "/broadcast Hello world!", args are ["Hello", "world!"]
    # We join the args back into a single string.
    message_to_send = update.message.text.split(" ", 1)[1]
    
    # 1. Fetch all chat IDs from the 'broadcast_chats' collection
    chats_ref = db.collection("broadcast_chats")
    try:
        chats_snapshot = chats_ref.stream()
        
        sent_count = 0
        failed_count = 0
        
        # Determine the source chat ID to skip sending the broadcast back to the owner's chat
        source_chat_id = str(update.effective_chat.id)

        for doc in chats_snapshot:
            chat_data = doc.to_dict()
            chat_id = chat_data.get("chat_id")
            
            if not chat_id or chat_id == source_chat_id:
                continue

            try:
                # 2. Send the message to each chat
                # We use HTML parsing for formatting (bold, links, etc.)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_to_send,
                    parse_mode="HTML"
                )
                sent_count += 1
            except Exception as e:
                # Catch exceptions like 'Bot was blocked by the user', 'Chat not found', etc.
                logger.warning(f"Failed to send broadcast to chat {chat_id}: {e}")
                failed_count += 1
                
        # 3. Report the result back to the owner
        await update.message.reply_text(
            f"✅ Broadcast complete!\n"
            f"Sent to **{sent_count}** chats.\n"
            f"Failed to send to **{failed_count}** chats (likely blocked or left the group).",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error during broadcast: {e}")
        await update.message.reply_text(f"An unexpected database error occurred during broadcast: {e}")


# --- Main Application Setup ---

def main() -> None:
    """Starts the bot using Webhook (required for free hosting)."""
    if not BOT_TOKEN or not WEBHOOK_URL:
        logger.error("BOT_TOKEN or WEBHOOK_URL environment variables are not set. Exiting.")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Register Handlers ---
    
    # Public/Utility Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("id", get_user_id))

    # Countdown Commands
    application.add_handler(CommandHandler("set_countdown", set_countdown))
    application.add_handler(CommandHandler("check_countdown", check_countdown))

    # Admin/Management Commands
    application.add_handler(CommandHandler("warn", warn_user))
    application.add_handler(CommandHandler("removewarn", remove_warn))
    application.add_handler(CommandHandler("warns", warn_counts))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("unban", unban_user))
    application.add_handler(CommandHandler("mute", mute_user))
    application.add_handler(CommandHandler("unmute", unmute_user))
    application.add_handler(CommandHandler("promote", promote_user))
    application.add_handler(CommandHandler("filter", set_filter))
    application.add_handler(CommandHandler("stop", stop_filter))

    # Lock/Unlock Commands
    application.add_handler(CommandHandler("lock", lock_feature))
    application.add_handler(CommandHandler("unlock", unlock_feature))

    # Banned Word Commands
    application.add_handler(CommandHandler("ban_word", ban_word))
    application.add_handler(CommandHandler("unban_word", unban_word))

    # Owner-Only Commands (FIXED: broadcast_message is now defined)
    application.add_handler(CommandHandler("broadcast", broadcast_message))

    # Message Handlers (must run on TEXT messages that are NOT commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_filters))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_banned_words))


    # --- Start Webhook (for Render/Cloud Hosting) ---
    logger.info(f"Starting webhook on port {PORT}...")
    
    # Run the bot as a webhook server
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )


if __name__ == "__main__":
    main()

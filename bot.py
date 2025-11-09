import os
import logging
from telegram import Update, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta, timezone
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
    """Sends a welcome message with bot information."""
    await update.message.reply_text(
        "👋 This is a group moderation bot made with ♥ by @Tota_ton (Gaurav). "
        "Just add the bot to your group and give the admin rights and you're good to go👌"
        "\n\nThank you🦚"
    )

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
        # Give back all permissions (unmute)
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
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
                can_manage_topics=False
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
    ref = get_filter_ref(group_id, keyword)
    if not ref: return
    
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
    ref = get_filter_ref(group_id, keyword)
    if not ref: return

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
    # Ensure checking against the raw text, not just the command-stripped text
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
    """Owner command to broadcast a message to a hardcoded list of chats (if needed) or just reply to the message."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 This command is reserved for the bot owner only.")
        return

    if not context.args:
        await update.message.reply_text("Please provide the message content to broadcast.")
        return

    message_text = " ".join(context.args)
    
    # --- Owner-Only Broadcast Logic ---
    # NOTE: To implement a real broadcast to multiple groups, you would need to store 
    # all group IDs in a separate Firestore collection and iterate over them.
    # For a simple starting point, we will just confirm the owner command.
    
    await update.message.reply_text(
        f"Owner Broadcast initiated. (To make this work across multiple chats, you need to set up a system to track all chat IDs in Firestore.)\n\nMessage: *{message_text}*",
        parse_mode="Markdown"
    )


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


    # Owner-Only Commands
    application.add_handler(CommandHandler("broadcast", broadcast_message))

    # Message Handler for Filters (must run on TEXT messages that are NOT commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_filters))


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
